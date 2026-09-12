"""Regression tests for station-leg lashing (cargo securing) checks.

Run directly with ``python3 test_lashing_departure.py`` (no Flask needed;
the securing engine is pure Python).

Covers the fix where a full-load *departure* leg with a locked but
directionally insufficient strap reported ``stages[0].failures == []`` and
``first_failure is None`` ("各航段系固校核通过" false positive).
"""
from __future__ import annotations

from copy import deepcopy

from planning import analyze, norm_state
from sample_data import BAD_STATE, TRUCK


def anchor_end(aid: str) -> dict:
    return {"kind": "anchor", "id": aid, "face": "", "u": 0.5, "v": 0.5}


def case_end(cid: str, face: str, u: float = 0.5, v: float = 0.5) -> dict:
    return {"kind": "case", "id": cid, "face": face, "u": u, "v": v}


def one_strap_state(lock: bool = True) -> dict:
    """Single stop / single heavy box / one side-only strap (slip margin < 1)."""
    stops = [{"id": "s1", "city": "唯一站", "venue": ""}]
    cases = [{
        "id": "H", "label": "重箱", "dims": [1.2, 1.0, 1.0], "weight_kg": 400,
        "stop_id": "s1", "allowed_orientations": ["LWH"], "max_stack_kg": 0,
        "forbidden_neighbors": [], "friction": 0.3,
        "lash_faces": ["-x", "+x", "-y", "+y"],
    }]
    lashings = [{
        "id": "only", "label": "单侧绑带",
        "from": anchor_end("F3R"), "to": case_end("H", "+y"),
        "pretension_kg": 200, "capacity_kg": 1000,
        "locked": lock, "review_signature": "",
    }]
    return norm_state({
        "name": "one-strap", "truck": deepcopy(TRUCK), "stops": stops,
        "cases": cases,
        "placements": [{"case_id": "H", "x": 3.6, "y": 0.8, "z": 0,
                        "orientation": "LWH", "locked": False}],
        "lashings": lashings,
    })


def test_departure_reports_locked_insufficient_strap() -> None:
    """The reported defect: departure must surface the slip/tipping failure."""
    state = one_strap_state(lock=True)
    report = analyze(state)
    lashing = report["lashing"]
    departure = lashing["stages"][0]

    assert report["error_count"] > 0, "global report must already flag errors"
    failure_codes = {f["code"] for f in departure["failures"]}
    assert "SLIP_MARGIN" in failure_codes, failure_codes
    ff = lashing["first_failure"]
    assert ff is not None, "first_failure must be located at departure"
    assert ff["stage_key"] == "departure"
    assert ff["code"] in {"SLIP_MARGIN", "TIP_MARGIN", "ANCHOR_OVERLOAD",
                          "LASH_OVERLOAD", "LASH_THROUGH_BOX"}
    assert ff["suggestion"], "a securing suggestion must be provided"

    case_row = next(c for c in lashing["cases"] if c["case_id"] == "H")
    worst = min(v for v in case_row["slip"].values() if v is not None)
    assert worst < 1.0, worst
    print("OK departure reports locked insufficient strap "
          f"(worst slip margin {worst:.3f}, first failure {ff['code']})")


def test_departure_ignores_unlocked_drafts() -> None:
    """Unlocked drafts are editing warnings, not leg securing failures."""
    state = one_strap_state(lock=False)
    lashing = analyze(state)["lashing"]
    departure = lashing["stages"][0]
    assert departure["failures"] == [], departure["failures"]
    assert lashing["first_failure"] is None
    print("OK unlocked draft does not fail a leg")


def test_builtin_sample_behavior() -> None:
    """Unlocked built-in sample: departure clean; unload leg flags unsecured."""
    state = norm_state(BAD_STATE)
    lashing = analyze(state)["lashing"]
    assert lashing["stages"][0]["failures"] == []
    ams = next(s for s in lashing["stages"] if s["key"] == "after-ams")
    assert any(f["code"] == "LASH_MISSING" for f in ams["failures"])
    assert lashing["first_failure"]["stage_key"] != "departure"

    # Once every sample strap is locked, the genuinely bad geometry must fail
    # at departure as well (no more false "all legs pass").
    for lash in state["lashings"]:
        lash["locked"] = True
    lashing2 = analyze(state)["lashing"]
    assert lashing2["stages"][0]["failures"], "locked bad straps must fail departure"
    assert lashing2["first_failure"]["stage_key"] == "departure"
    print("OK built-in sample departure/leg behavior")


def test_well_secured_plan_passes_all_legs() -> None:
    """A properly strapped multi-stop plan keeps every stage clean."""
    stops = [{"id": "s1", "city": "A城"}, {"id": "s2", "city": "B城"}]
    cases = [
        {"id": "A", "label": "A箱", "dims": [1.2, 1.0, 1.0], "weight_kg": 300,
         "stop_id": "s1", "allowed_orientations": ["LWH"], "max_stack_kg": 0,
         "forbidden_neighbors": [], "friction": 0.4,
         "lash_faces": ["-x", "+x", "-y", "+y"]},
        {"id": "B", "label": "B箱", "dims": [1.2, 1.0, 1.0], "weight_kg": 300,
         "stop_id": "s2", "allowed_orientations": ["LWH"], "max_stack_kg": 0,
         "forbidden_neighbors": [], "friction": 0.4,
         "lash_faces": ["-x", "+x", "-y", "+y"]},
    ]
    layout = [
        ("a1", "F1R", "A", "+y"), ("a2", "F1L", "A", "-y"),
        ("a3", "F1L", "A", "-x"), ("a4", "F2R", "A", "+x"),
        ("b1", "F3R", "B", "+y"), ("b2", "F3L", "B", "-y"),
        ("b3", "F2L", "B", "-x"), ("b4", "F4R", "B", "+x"),
    ]
    lashings = [{
        "id": lid, "from": anchor_end(a), "to": case_end(c, f, v=0.85),
        "pretension_kg": 200, "capacity_kg": 1000, "locked": True,
        "review_signature": "",
    } for lid, a, c, f in layout]
    state = norm_state({
        "name": "good", "truck": deepcopy(TRUCK), "stops": stops, "cases": cases,
        "placements": [
            {"case_id": "A", "x": 0.3, "y": 0.8, "z": 0, "orientation": "LWH"},
            {"case_id": "B", "x": 3.6, "y": 0.8, "z": 0, "orientation": "LWH"},
        ],
        "lashings": lashings,
    })
    lashing = analyze(state)["lashing"]
    for stage in lashing["stages"]:
        assert stage["failures"] == [], (stage["title"], stage["failures"])
    assert lashing["first_failure"] is None
    print("OK well-secured plan passes every leg")


def test_leg_simulation_still_runs_after_fix() -> None:
    """Existing per-stop recomputation still produces one stage per stop + 1."""
    state = one_strap_state(lock=True)
    lashing = analyze(state)["lashing"]
    assert len(lashing["stages"]) == len(state["stops"]) + 1
    assert lashing["release_steps"], "release steps must still be generated"
    assert {s["id"] for s in lashing["straps"]} == {"only"}
    print("OK per-stop leg simulation unchanged")


if __name__ == "__main__":
    test_departure_reports_locked_insufficient_strap()
    test_departure_ignores_unlocked_drafts()
    test_builtin_sample_behavior()
    test_well_secured_plan_passes_all_legs()
    test_leg_simulation_still_runs_after_fix()
    print("\nALL LASHING DEPARTURE REGRESSION TESTS PASS")
