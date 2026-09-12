"""分站称重核对（weighing.py）回归测试。

直接运行：``python3 test_weighing.py``（仅依赖标准库，无需 Flask）。

覆盖：
- 按当站应留箱体重算理论总重/轴荷（含燃油、人员等非器材载荷分配）；
- 核对通过、证据缺口（时刻倒序、轴荷和≠总重、读数超量程）；
- 超差时按最少异常项搜索漏装 / 错卸 / 重量偏差 / 纵向错位；
- 前后两程卸载差；
- 现场复核后派生实际装载版本（apply_resolution），原计划不被覆盖；
- 站序 / 箱位 / 重量修改后，仅关联站点的签名失效（待复核）。
"""
from __future__ import annotations

from copy import deepcopy

from planning import analyze, boxes_from, norm_state
from sample_data import BAD_STATE
from weighing import (
    apply_resolution,
    evaluate_sheet,
    onboard_cases,
    predicted_readings,
    sheet_signature,
    stage_sequence,
)

EXTRA = {"fuel_l": 100, "crew_count": 2}


def readings_from(pred: dict, when: str = "2026-09-01T08:00", fuel: float = 100.0,
                  scale_max: float = 20000.0) -> dict:
    return {
        "gross_kg": round(pred["gross_kg"]),
        "axle_kg": [round(a["total_kg"]) for a in pred["axles"]],
        "tolerance_kg": 20, "scale_max_kg": scale_max,
        "fuel_l": fuel, "crew_count": 2, "weighed_at": when,
    }


def actual_stage_pred(state: dict, stage: str, remove: set[str] | None = None,
                      keep: set[str] | None = None, fuel: float = 80.0,
                      weight_delta: dict[str, float] | None = None,
                      shift: dict[str, float] | None = None):
    """Predicted readings of the ACTUAL vehicle: stage-filtered placements ± changes."""
    rank = {s: i for i, s in enumerate(stage_sequence(state))}[stage]
    st = deepcopy(state)
    for cid, d in (weight_delta or {}).items():
        next(c for c in st["cases"] if c["id"] == cid)["weight_kg"] += d
    for cid, dx in (shift or {}).items():
        next(p for p in st["placements"] if p["case_id"] == cid)["x"] += dx
    ranks = {s["id"]: i for i, s in enumerate(st["stops"])}
    kept = []
    for p in st["placements"]:
        cid = p["case_id"]
        if cid in (remove or set()):
            continue
        c = next(c for c in st["cases"] if c["id"] == cid)
        if ranks[c["stop_id"]] >= rank or cid in (keep or set()):
            kept.append(p)
    st["placements"] = kept
    return predicted_readings(st, stage, {"fuel_l": fuel, "crew_count": 2})


def test_stage_onboard_sets() -> None:
    state = norm_state(BAD_STATE)
    dep = {b["id"] for b in onboard_cases(state, "departure")}
    assert len(dep) == 15, dep
    ams = {b["id"] for b in onboard_cases(state, "after-ams")}
    assert dep - ams == {"BASS-A", "BASS-B", "FOH-L", "FOH-R", "CON-MON", "CON-CAT"}
    par = {b["id"] for b in onboard_cases(state, "after-par")}
    assert par == set()
    print("OK stage onboard sets follow the stop sequence")


def test_departure_within_tolerance() -> None:
    state = norm_state(BAD_STATE)
    pred = predicted_readings(state, "departure", EXTRA)
    r = evaluate_sheet(state, "departure", readings_from(pred))
    assert r["verdict"] == "within_tolerance", r["gaps"]
    assert not r["candidates"]
    # Non-equipment mass is included: tare 5000 + cargo 3860 + fuel/crew ≈ 9354.
    assert pred["extra_mass_kg"] > 200
    assert abs(sum(a["total_kg"] for a in pred["axles"]) - pred["gross_kg"]) < 1e-6
    print("OK departure reconciles inside scale tolerance")


def test_plan_consistent_after_unload() -> None:
    state = norm_state(BAD_STATE)
    dep_pred = predicted_readings(state, "departure", EXTRA)
    prev = {"stage": "departure", "status": "frozen",
            "readings": readings_from(dep_pred), "weighed_at": "2026-09-01T08:00"}
    ams_pred = actual_stage_pred(state, "after-ams", fuel=80)
    r = evaluate_sheet(state, "after-ams",
                       readings_from(ams_pred, "2026-09-02T10:00", fuel=80),
                       chronology=[prev], prev_sheet=prev)
    assert r["verdict"] == "within_tolerance", (r["verdict"], r["gaps"])
    assert all(d["within_tolerance"] for d in r["deltas"])
    print("OK plan-consistent unload matches both tickets and deltas")


def test_missing_case_is_single_event_candidate() -> None:
    state = norm_state(BAD_STATE)
    pred = actual_stage_pred(state, "departure", remove={"BASS-A"})
    r = evaluate_sheet(state, "departure", readings_from(pred))
    assert r["verdict"] == "out_of_tolerance"
    top = r["candidates"][0]
    assert top["event_count"] == 1
    assert top["events"][0]["kind"] == "missing"
    assert top["events"][0]["case_id"] == "BASS-A"
    assert top["excess_kg"] <= 1.0
    print("OK one never-loaded box is the minimum-anomaly answer")


def test_wrong_unload_detected_via_delta() -> None:
    state = norm_state(BAD_STATE)
    dep_pred = predicted_readings(state, "departure", EXTRA)
    prev = {"stage": "departure", "status": "frozen",
            "readings": readings_from(dep_pred), "weighed_at": "2026-09-01T08:00"}
    # AMP is a Paris case that is still supposed to be on board after Amsterdam.
    ams_pred = actual_stage_pred(state, "after-ams", remove={"AMP"}, fuel=80)
    r = evaluate_sheet(state, "after-ams",
                       readings_from(ams_pred, "2026-09-02T10:00", fuel=80),
                       chronology=[prev], prev_sheet=prev)
    assert r["verdict"] == "out_of_tolerance"
    top = r["candidates"][0]
    assert top["event_count"] == 1
    ev = top["events"][0]
    assert (ev["kind"], ev["case_id"], ev["present_prev"]) == ("missing", "AMP", True)
    print("OK a later-leg box wrongly unloaded at this stop is found")


def test_missed_unload_extra_case() -> None:
    state = norm_state(BAD_STATE)
    # Berlin lighting rack should be gone after Berlin; readings keep its weight.
    pred = predicted_readings(state, "after-ber", {"fuel_l": 60, "crew_count": 2})
    from weighing import axle_fractions
    b = next(b for b in boxes_from(state) if b["id"] == "BER-LIGHT")
    f = axle_fractions(state["truck"], b["x"] + b["dx"] / 2)
    read = readings_from(pred, "2026-09-03T10:00", fuel=60)
    read["gross_kg"] = round(read["gross_kg"] + b["weight"])
    read["axle_kg"] = [round(read["axle_kg"][i] + b["weight"] * f[i]) for i in range(2)]
    ams_pred = actual_stage_pred(state, "after-ams", fuel=80)
    prev = {"stage": "after-ams", "status": "frozen",
            "readings": readings_from(ams_pred, "2026-09-02T10:00", fuel=80),
            "weighed_at": "2026-09-02T10:00"}
    r = evaluate_sheet(state, "after-ber", read, chronology=[prev], prev_sheet=prev)
    assert r["verdict"] == "out_of_tolerance"
    top = r["candidates"][0]
    assert (top["events"][0]["kind"], top["events"][0]["case_id"]) == ("extra", "BER-LIGHT")
    print("OK a box that should have been unloaded is offered as 错卸留车")


def test_weight_discrepancy() -> None:
    state = norm_state(BAD_STATE)
    pred = actual_stage_pred(state, "departure", weight_delta={"BASS-A": 100}, fuel=100)
    r = evaluate_sheet(state, "departure", readings_from(pred))
    assert r["verdict"] == "out_of_tolerance"
    # With a 2-axle truck the measured gross + two axle sums leave the weight
    # delta under-determined among cases with similar axle fractions: BASS-A is
    # one valid single-event explanation; the crew verifies the physical case.
    bass = [c for c in r["candidates"]
            if c["event_count"] == 1 and c["events"][0]["kind"] == "weight"
            and c["events"][0]["case_id"] == "BASS-A"]
    assert bass, [c["events"][0]["case_id"] for c in r["candidates"]]
    assert all(c["excess_kg"] <= 1.0 for c in bass)
    # BASS-A's own perfect fit (d≈100) is among the candidates.
    assert any(abs(c["events"][0]["weight_delta_kg"] - 100) <= 2 for c in bass)
    print("OK weight discrepancy offers the actual box as a single-event fit")


def test_longitudinal_shift() -> None:
    state = norm_state(BAD_STATE)
    # Move the 400 kg amp rack 0.8 m toward the cab on the real truck.
    pred = actual_stage_pred(state, "departure", shift={"AMP": 0.8})
    r = evaluate_sheet(state, "departure", readings_from(pred))
    assert r["verdict"] == "out_of_tolerance"
    exact = [c for c in r["candidates"]
             if c["events"] and c["events"][0]["kind"] == "shift"
             and c["events"][0]["case_id"] == "AMP"
             and abs(c["events"][0]["dx_m"] - 0.8) < 1e-6 and c["excess_kg"] <= 1]
    assert exact, [(c["events"], c["excess_kg"]) for c in r["candidates"][:5]]
    # Side-view contribution rows expose per-axle effects for highlighting.
    contribs = exact[0]["axle_contributions"]
    row = next(x for x in contribs if x["case_id"] == "AMP")
    assert abs(sum(row["axle_delta_kg"])) < 1e-6, "shift conserves gross"
    assert any(abs(x) > 5 for x in row["axle_delta_kg"])
    print("OK longitudinal misplacement is found and axle contributions shown")


def test_evidence_gaps_only() -> None:
    state = norm_state(BAD_STATE)
    pred = predicted_readings(state, "departure", EXTRA)

    bad_sum = readings_from(pred)
    bad_sum["gross_kg"] += 500  # axle sum no longer matches gross
    r = evaluate_sheet(state, "departure", bad_sum)
    assert r["verdict"] == "evidence_gap"
    assert "AXLE_SUM_MISMATCH" in [g["code"] for g in r["gaps"]]
    assert r["candidates"] == []

    over_range = readings_from(pred)
    over_range["scale_max_kg"] = 5000
    r2 = evaluate_sheet(state, "departure", over_range)
    assert any(g["code"] in ("GROSS_OUT_OF_RANGE", "AXLE_OUT_OF_RANGE") for g in r2["gaps"])
    assert r2["verdict"] == "evidence_gap"

    missing = readings_from(pred)
    missing["axle_kg"][1] = None
    r3 = evaluate_sheet(state, "departure", missing)
    assert "MISSING_AXLE" in [g["code"] for g in r3["gaps"]]
    print("OK incompatible / out-of-range / missing readings list only evidence gaps")


def test_time_order_gap() -> None:
    state = norm_state(BAD_STATE)
    dep_pred = predicted_readings(state, "departure", EXTRA)
    prev = {"stage": "departure", "status": "frozen",
            "readings": readings_from(dep_pred, "2026-09-01T08:00"),
            "weighed_at": "2026-09-01T08:00", "label": "发车前"}
    ams_pred = actual_stage_pred(state, "after-ams", fuel=80)
    read = readings_from(ams_pred, "2026-08-30T09:00", fuel=80)
    r = evaluate_sheet(state, "after-ams", read, chronology=[prev], prev_sheet=prev)
    assert "TIME_ORDER" in [g["code"] for g in r["gaps"]]
    print("OK a ticket predating the previous leg ticket is a TIME_ORDER gap")


def test_contradictory_evidence_gives_no_false_positive() -> None:
    """Weight discrepancy visible now but not at the prior ticket cannot be one event."""
    state = norm_state(BAD_STATE)
    dep_pred = predicted_readings(state, "departure", EXTRA)
    prev = {"stage": "departure", "status": "frozen",
            "readings": readings_from(dep_pred), "weighed_at": "2026-09-01T08:00"}
    # BER-WARD (rear, ber stop) is 80 kg heavier on the actual truck; it is still
    # on board after Amsterdam, so current ticket is heavy but departure matched.
    ams_pred = actual_stage_pred(state, "after-ams", fuel=80, weight_delta={"BER-WARD": 80})
    r = evaluate_sheet(state, "after-ams",
                       readings_from(ams_pred, "2026-09-02T10:00", fuel=80),
                       chronology=[prev], prev_sheet=prev)
    # No single weight event may claim this (would contradict departure ticket).
    for c in r["candidates"]:
        if c["event_count"] == 1 and c["events"][0]["kind"] == "weight":
            assert c["events"][0]["case_id"] != "BER-WARD", c
    print("OK contradictory tickets are not explained by a fabricated single event")


def test_apply_resolution_preserves_plan_and_unlocks_moved_straps() -> None:
    state = norm_state(BAD_STATE)
    original = deepcopy(state)
    # Freeze one strap touching AMP so we can verify re-review invalidation.
    for lash in state["lashings"]:
        lash["locked"] = lash["id"] == "L007"
        lash["review_signature"] = "stub" if lash["id"] == "L007" else ""
    events = [
        {"kind": "missing", "case_id": "BASS-A", "present_prev": False},
        {"kind": "shift", "case_id": "AMP", "dx_m": 0.8},
        {"kind": "weight", "case_id": "SPARE", "weight_delta_kg": -10},
    ]
    derived = apply_resolution(state, "departure", events)
    # Original state is untouched (planned snapshot preserved).
    assert len(original["placements"]) == len(state["placements"])
    assert next(p for p in original["placements"] if p["case_id"] == "BASS-A")
    # Missing box removed from the actual version.
    assert not any(p["case_id"] == "BASS-A" for p in derived["placements"])
    # Shift applied with weight adjusted.
    p_old = next(p for p in original["placements"] if p["case_id"] == "AMP")
    p_new = next(p for p in derived["placements"] if p["case_id"] == "AMP")
    assert abs(p_new["x"] - (p_old["x"] + 0.8)) < 1e-6
    spare = next(c for c in derived["cases"] if c["id"] == "SPARE")
    assert spare["weight_kg"] == 60
    # Strap on the shifted case is unlocked for re-review; other locked straps stay.
    l7 = next(l for l in derived["lashings"] if l["id"] == "L007")
    assert l7["locked"] is False and l7["review_signature"] == ""
    # The derived version still analyzes (axle + lashing recompute end to end).
    report = analyze(derived)
    assert "axle_loads" in report and "lashing" in report
    print("OK resolution derives an actual version, plan kept, moved straps unlocked")


def test_apply_extra_event_moves_box_to_next_stop() -> None:
    state = norm_state(BAD_STATE)
    derived = apply_resolution(state, "after-ber",
                               [{"kind": "extra", "case_id": "BER-LIGHT"}])
    c = next(c for c in derived["cases"] if c["id"] == "BER-LIGHT")
    assert c["stop_id"] == "par", c["stop_id"]
    print("OK 应卸未卸 box is deferred to the next stop in the actual version")


def test_signature_scoping_only_associated_stations() -> None:
    state = norm_state(BAD_STATE)
    sigs = {st: sheet_signature(state, st) for st in stage_sequence(state)}

    # Move a Berlin case: involved while it is on board (departure, after-ams)
    # and in the after-ber unload delta; the empty after-par ticket is untouched.
    moved = deepcopy(state)
    p = next(p for p in moved["placements"] if p["case_id"] == "BER-WARD")
    p["x"] += 0.3
    assert sheet_signature(moved, "departure") != sigs["departure"]
    assert sheet_signature(moved, "after-ams") != sigs["after-ams"]
    assert sheet_signature(moved, "after-ber") != sigs["after-ber"]
    assert sheet_signature(moved, "after-par") == sigs["after-par"]

    # A Paris case also participates in the after-par unload delta, so that
    # ticket is associated too.
    moved_par = deepcopy(state)
    next(pl for pl in moved_par["placements"] if pl["case_id"] == "AMP")["x"] += 0.3
    assert sheet_signature(moved_par, "after-par") != sigs["after-par"]

    # Change only an Amsterdam box's weight: departure and the after-ams unload
    # ticket are associated, later stages do not include it.
    weighted = deepcopy(state)
    next(c for c in weighted["cases"] if c["id"] == "BASS-A")["weight_kg"] += 20
    assert sheet_signature(weighted, "departure") != sigs["departure"]
    assert sheet_signature(weighted, "after-ams") != sigs["after-ams"]
    assert sheet_signature(weighted, "after-ber") == sigs["after-ber"]
    assert sheet_signature(weighted, "after-par") == sigs["after-par"]

    # Stop-order swap marks associated tickets via the changed stop sequence.
    reordered = deepcopy(state)
    reordered["stops"][0], reordered["stops"][1] = reordered["stops"][1], reordered["stops"][0]
    changed = [st for st in stage_sequence(state)
               if sheet_signature(reordered, st) != sigs[st]]
    assert "departure" in changed and len(changed) >= 2
    print("OK signatures invalidate only associated station tickets")


def test_fuel_change_is_handled_symmetrically_in_unload_delta() -> None:
    """Defect 1: fuel 300 L -> 100 L, each ticket hits its own theory.

    The front-to-back unload delta strips the declared non-equipment load on
    BOTH the measured and predicted sides; the old code subtracted the change
    once (with the wrong sign on gross), reporting a phantom ±168 kg residual
    and fabricating anomaly candidates.
    """
    state = norm_state(BAD_STATE)
    p0 = predicted_readings(state, "departure", {"fuel_l": 300, "crew_count": 2})
    dep_read = {
        "gross_kg": round(p0["gross_kg"]),
        "axle_kg": [round(a["total_kg"]) for a in p0["axles"]],
        "tolerance_kg": 20, "fuel_l": 300, "crew_count": 2,
        "weighed_at": "2026-09-01T08:00",
    }
    prev = {"stage": "departure", "status": "frozen", "readings": dep_read,
            "weighed_at": "2026-09-01T08:00"}
    rank = 1
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    actual = deepcopy(state)
    actual["placements"] = [
        p for p in actual["placements"]
        if ranks[next(c for c in actual["cases"] if c["id"] == p["case_id"])["stop_id"]] >= rank
    ]
    p1 = predicted_readings(actual, "after-ams", {"fuel_l": 100, "crew_count": 2})
    ams_read = {
        "gross_kg": round(p1["gross_kg"]),
        "axle_kg": [round(a["total_kg"]) for a in p1["axles"]],
        "tolerance_kg": 20, "fuel_l": 100, "crew_count": 2,
        "weighed_at": "2026-09-02T10:00",
    }
    ev = evaluate_sheet(state, "after-ams", ams_read, chronology=[prev], prev_sheet=prev)
    assert ev["verdict"] == "within_tolerance", ev["verdict"]
    for d in ev["deltas"]:
        assert d["within_tolerance"], d
        assert abs(d["residual_kg"]) < 1.0, d
    assert ev["candidates"] == []
    # Equipment-only shed equals the planned Amsterdam cargo mass.
    from planning import boxes_from
    gross_delta = next(d for d in ev["deltas"] if d["key"] == "delta_gross")
    planned = sum(b["weight"] for b in boxes_from(state) if b["case"]["stop_id"] == "ams")
    assert abs(gross_delta["measured_kg"] - planned) < 1.0
    print("OK fuel change 300->100 L yields zero unload-delta residual")


def test_real_unload_anomaly_still_detected_after_delta_fix() -> None:
    """A wrongly unloaded Paris box must still fail the delta, not be hidden."""
    state = norm_state(BAD_STATE)
    dep_pred = predicted_readings(state, "departure", {"fuel_l": 300, "crew_count": 2})
    prev = {"stage": "departure", "status": "frozen",
            "readings": readings_from(dep_pred, fuel=300), "weighed_at": "2026-09-01T08:00"}
    ams_pred = actual_stage_pred(state, "after-ams", remove={"AMP"}, fuel=100)
    r = evaluate_sheet(state, "after-ams",
                       readings_from(ams_pred, "2026-09-02T10:00", fuel=100),
                       chronology=[prev], prev_sheet=prev)
    assert r["verdict"] == "out_of_tolerance"
    assert (r["candidates"][0]["events"][0]["kind"],
            r["candidates"][0]["events"][0]["case_id"]) == ("missing", "AMP")
    delta_bad = [d for d in r["deltas"] if not d["within_tolerance"]]
    assert delta_bad, "the unload delta must expose the wrongly unloaded box"
    print("OK genuine unload anomaly is still flagged after symmetric delta fix")


if __name__ == "__main__":
    test_stage_onboard_sets()
    test_departure_within_tolerance()
    test_plan_consistent_after_unload()
    test_missing_case_is_single_event_candidate()
    test_wrong_unload_detected_via_delta()
    test_missed_unload_extra_case()
    test_weight_discrepancy()
    test_longitudinal_shift()
    test_evidence_gaps_only()
    test_time_order_gap()
    test_contradictory_evidence_gives_no_false_positive()
    test_fuel_change_is_handled_symmetrically_in_unload_delta()
    test_real_unload_anomaly_still_detected_after_delta_fix()
    test_apply_resolution_preserves_plan_and_unlocks_moved_straps()
    test_apply_extra_event_moves_box_to_next_stop()
    test_signature_scoping_only_associated_stations()
    print("\nALL WEIGHING REGRESSION TESTS PASS")
