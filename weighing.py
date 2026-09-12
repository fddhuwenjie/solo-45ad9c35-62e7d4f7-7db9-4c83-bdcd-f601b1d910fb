"""分站称重核对（地磅 reconciliation）。

纯几何/力学模块，不依赖 Flask，规则可独立测试，约定与 ``planning`` 一致：

X: 尾门 (0) -> 前墙；车轴按 ``truck.axles`` 的 position 排列。

称重阶段（stage）按巡演时序固定排列：

* ``departure``        —— 满载发车前；
* ``after-<stop_id>``  —— 在该站卸货后、驶往下一程之前。

每次过磅录入：总重、各轴读数、称重时刻、秤的误差范围与量程，以及油量、
随车人员、其他非器材载荷等非器材重量。系统按“当站应留在车上的箱体”
重算理论总重与各轴理论轴荷，核对：

1. 总重守恒（实测总重 vs 理论总重）；
2. 各轴轴荷差（含非器材载荷的轴间分配）；
3. 前后两次称重的卸载差（本程实测变化 vs 计划卸下的箱体重量）。

时刻倒序、读数缺测/超量程、轴荷和与总重互不相容等情况只列“证据缺口”，
不做病因搜索。无证据缺口且存在超差时，再按“最少异常项”搜索
漏装 / 错卸 / 重量偏差 / 纵向错位候选。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lashing import fnv1a32
from planning import EPS, boxes_from, norm_state, now_iso

DEFAULT_FUEL_DENSITY_KG_L = 0.84
DEFAULT_FUEL_TANK_X_RATIO = 0.15          # 油箱通常靠近尾门一侧
DEFAULT_CREW_KG_PERSON = 80.0
DEFAULT_CREW_X_RATIO = 0.62               # 驾驶室附近，偏车头
DEFAULT_MISC_X_RATIO = 0.5
DEFAULT_TOLERANCE_KG = 20.0
DEFAULT_MASS_TOLERANCE_KG = 5.0           # 清单重量与实物的最小判差分辨率

# 搜索纵向错位时扫描的重心位移（m，+ 向车头，- 向车尾）。
SHIFT_GRID = (-0.8, -0.4, -0.2, 0.2, 0.4, 0.8)
MAX_CANDIDATE_EVENTS = 3
MAX_CANDIDATES_RETURNED = 8


# ----------------------------- stage helpers -----------------------------

def stage_sequence(state: Dict[str, Any]) -> List[str]:
    """Fixed chronological stage keys: departure then one stage per stop."""
    state = norm_state(state)
    return ["departure"] + [f"after-{s['id']}" for s in state["stops"]]


def stage_rank(state: Dict[str, Any], stage: str) -> int:
    seq = stage_sequence(state)
    if stage not in seq:
        raise ValueError(f"未知称重阶段：{stage}")
    return seq.index(stage)


def stage_stop(state: Dict[str, Any], stage: str) -> Optional[Dict[str, Any]]:
    if not stage.startswith("after-"):
        return None
    sid = stage[len("after-"):]
    return next((s for s in state["stops"] if s["id"] == sid), None)


def onboard_cases(state: Dict[str, Any], stage: str) -> List[Dict[str, Any]]:
    """Cases that the plan says should still be on board at ``stage``."""
    state = norm_state(state)
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    rank = stage_rank(state, stage)
    boxes = boxes_from(state)
    out = []
    for b in boxes:
        sr = ranks.get(b["case"].get("stop_id"))
        if sr is None:
            continue
        # departure (rank 0): every case on board.
        # after-stop k: cases unloaded at stops k+1, k+2, ... remain.
        if sr >= rank:
            out.append(b)
    return out


def unloaded_at_stop(state: Dict[str, Any], stop_id: str) -> List[Dict[str, Any]]:
    return [b for b in boxes_from(state) if b["case"].get("stop_id") == stop_id]


def weigh_config(truck: Dict[str, Any]) -> Dict[str, Any]:
    """Fill weighing-related truck fields (fuel tank, crew seat defaults)."""
    cfg = deepcopy(truck.get("weigh") or {})
    L = float(truck.get("length", 6.0))
    cfg.setdefault("fuel_density_kg_l", DEFAULT_FUEL_DENSITY_KG_L)
    cfg.setdefault("fuel_tank_x_m", round(L * DEFAULT_FUEL_TANK_X_RATIO, 3))
    cfg.setdefault("crew_kg_per_person", DEFAULT_CREW_KG_PERSON)
    cfg.setdefault("crew_x_m", round(L * DEFAULT_CREW_X_RATIO, 3))
    cfg.setdefault("misc_x_m", round(L * DEFAULT_MISC_X_RATIO, 3))
    for k in ("fuel_density_kg_l", "fuel_tank_x_m", "crew_kg_per_person",
              "crew_x_m", "misc_x_m"):
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = 0.0
    return cfg


def axle_fractions(truck: Dict[str, Any], x: float) -> List[float]:
    """Lever-rule share of a point mass at x carried by each axle.

    Mirrors ``planning.axle_loads``: outside the first/last axle the end axle
    carries everything; between two axles the load splits linearly.
    """
    axles = truck["axles"]
    if not axles:
        return []
    frac = [0.0 for _ in axles]
    positions = [float(a["position"]) for a in axles]
    if x <= positions[0] + EPS:
        frac[0] = 1.0
    elif x >= positions[-1] - EPS:
        frac[-1] = 1.0
    else:
        for i in range(len(axles) - 1):
            x0, x1 = positions[i], positions[i + 1]
            if x0 - EPS <= x <= x1 + EPS:
                span = max(x1 - x0, EPS)
                frac[i] = (x1 - x) / span
                frac[i + 1] = (x - x0) / span
                break
    return frac


# ----------------------------- predicted readings -----------------------------

def case_axle_contributions(boxes: Sequence[Dict[str, Any]], truck: Dict[str, Any]
                            ) -> Dict[str, List[float]]:
    """Per-case payload share landing on each axle (kg)."""
    out: Dict[str, List[float]] = {}
    n = len(truck["axles"])
    for b in boxes:
        f = axle_fractions(truck, b["x"] + b["dx"] / 2.0)
        out[b["id"]] = [b["weight"] * v for v in f] if f else [0.0] * n
    return out


def non_equipment_axles(truck: Dict[str, Any], readings: Dict[str, Any]
                        ) -> Tuple[float, List[float], List[Dict[str, Any]]]:
    """Fuel / crew / misc mass and its axle distribution.

    Returns (mass_kg, [kg per axle], breakdown rows for the UI).
    """
    cfg = weigh_config(truck)
    rows: List[Dict[str, Any]] = []
    n = len(truck["axles"])
    total_axles = [0.0] * n

    def add(label: str, mass: float, x: float) -> None:
        mass = max(0.0, float(mass or 0.0))
        if mass <= EPS:
            return
        f = axle_fractions(truck, x) or [0.0] * n
        per = [mass * v for v in f]
        for i, v in enumerate(per):
            total_axles[i] += v
        rows.append({"label": label, "mass_kg": mass, "x_m": x, "axle_kg": per})

    fuel_l = float(readings.get("fuel_l") or 0.0)
    add("燃油", fuel_l * cfg["fuel_density_kg_l"], cfg["fuel_tank_x_m"])
    crew_n = int(float(readings.get("crew_count") or 0.0))
    add(f"随车人员 ×{crew_n}", crew_n * cfg["crew_kg_per_person"], cfg["crew_x_m"])
    add("其他随车载荷", float(readings.get("misc_kg") or 0.0), cfg["misc_x_m"])
    return sum(r["mass_kg"] for r in rows), total_axles, rows


def predicted_readings(state: Dict[str, Any], stage: str,
                       readings: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """Theoretical total and per-axle readings for the cases that remain."""
    state = norm_state(state)
    readings = readings or {}
    truck = state["truck"]
    boxes = onboard_cases(state, stage)
    contrib = case_axle_contributions(boxes, truck)
    n = len(truck["axles"])
    cargo_axles = [0.0] * n
    for cid, per in contrib.items():
        for i, v in enumerate(per):
            cargo_axles[i] += v
    tare = [float(a.get("tare_kg", 0.0)) for a in truck["axles"]]
    extra_mass, extra_axles, extra_rows = non_equipment_axles(truck, readings)
    axle = []
    for i, a in enumerate(truck["axles"]):
        axle.append({
            "name": a.get("name", f"轴{i + 1}"),
            "position": float(a["position"]),
            "tare_kg": tare[i],
            "cargo_kg": cargo_axles[i],
            "extra_kg": extra_axles[i] if i < len(extra_axles) else 0.0,
            "total_kg": tare[i] + cargo_axles[i] + (extra_axles[i] if i < len(extra_axles) else 0.0),
            "capacity_kg": float(a.get("capacity_kg", 0.0)),
        })
    cargo_mass = sum(b["weight"] for b in boxes)
    return {
        "stage": stage,
        "onboard_case_ids": [b["id"] for b in boxes],
        "unloaded_case_ids": [b["id"] for b in
                              (unloaded_at_stop(state, stage_stop(state, stage)["id"])
                               if stage_stop(state, stage) else [])],
        "cargo_mass_kg": cargo_mass,
        "tare_kg": sum(tare),
        "extra_mass_kg": extra_mass,
        "extra_rows": extra_rows,
        "gross_kg": sum(tare) + cargo_mass + extra_mass,
        "axles": axle,
        "case_contributions": contrib,
    }


# ----------------------------- readings normalization -----------------------------

def norm_readings(raw: Optional[Dict[str, Any]], n_axles: int) -> Dict[str, Any]:
    raw = raw or {}

    def num(key: str) -> Optional[float]:
        if key not in raw or raw[key] in (None, ""):
            return None
        try:
            return float(raw[key])
        except (TypeError, ValueError):
            return None

    axles_raw = raw.get("axle_kg")
    axle_values: List[Optional[float]]
    if isinstance(axles_raw, list) and axles_raw:
        axle_values = []
        for v in axles_raw[:n_axles]:
            try:
                axle_values.append(float(v))
            except (TypeError, ValueError):
                axle_values.append(None)
        while len(axle_values) < n_axles:
            axle_values.append(None)
    else:
        axle_values = [num(f"axle_{i}_kg") for i in range(n_axles)]

    return {
        "gross_kg": num("gross_kg"),
        "axle_kg": axle_values,
        "tolerance_kg": num("tolerance_kg"),
        "scale_max_kg": num("scale_max_kg"),
        "fuel_l": num("fuel_l") or 0.0,
        "crew_count": num("crew_count") or 0.0,
        "misc_kg": num("misc_kg") or 0.0,
        "crew_names": str(raw.get("crew_names", "") or ""),
        "weighed_at": str(raw.get("weighed_at", "") or ""),
        "notes": str(raw.get("notes", "") or ""),
    }


def parse_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("/", "-")
    fmts = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ----------------------------- evidence gaps -----------------------------

def _gap(code: str, message: str, axle_index: Optional[int] = None,
         stage: str = "") -> Dict[str, Any]:
    g = {"code": code, "message": message}
    if axle_index is not None:
        g["axle_index"] = axle_index
    if stage:
        g["stage"] = stage
    return g


def evidence_gaps(state: Dict[str, Any], stage: str, readings: Dict[str, Any],
                  tol: float, chronology: Optional[Sequence[Dict[str, Any]]] = None
                  ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Validate the ticket itself.  Returns (gaps, normalized readings)."""
    state = norm_state(state)
    truck = state["truck"]
    gaps: List[Dict[str, Any]] = []
    norm = norm_readings(readings, len(truck["axles"]))

    stop = stage_stop(state, stage)
    if not stop and stage != "departure":
        gaps.append(_gap("UNKNOWN_STAGE", f"称重阶段 {stage} 在当前站序中不存在。", stage=stage))

    if norm["tolerance_kg"] is None:
        gaps.append(_gap("MISSING_TOLERANCE", "缺少秤的误差范围（±kg），无法判定公差带。"))
        tol_eff = tol
    elif norm["tolerance_kg"] < -EPS:
        gaps.append(_gap("BAD_TOLERANCE", "秤的误差范围不能为负。"))
        tol_eff = tol
    else:
        tol_eff = max(tol, norm["tolerance_kg"])

    scale_max = norm["scale_max_kg"]
    if scale_max is not None and scale_max <= EPS:
        gaps.append(_gap("BAD_SCALE_RANGE", "秤的量程必须为正数。"))
        scale_max = None

    def range_ok(v: Optional[float]) -> bool:
        return v is not None and v >= -EPS and (scale_max is None or v <= scale_max + EPS)

    if norm["gross_kg"] is None:
        gaps.append(_gap("MISSING_GROSS", "缺少实测总重读数。"))
    elif not range_ok(norm["gross_kg"]):
        gaps.append(_gap("GROSS_OUT_OF_RANGE",
                         f"实测总重 {norm['gross_kg']:.0f} kg 缺测、为负或超过秤的量程"
                         + (f" {scale_max:.0f} kg。" if scale_max else "。")))

    axle_names = [a.get("name", f"轴{i + 1}") for i, a in enumerate(truck["axles"])]
    for i, v in enumerate(norm["axle_kg"]):
        if v is None:
            gaps.append(_gap("MISSING_AXLE", f"{axle_names[i]}轴缺少实测读数。", i))
        elif not range_ok(v):
            gaps.append(_gap("AXLE_OUT_OF_RANGE",
                             f"{axle_names[i]}轴读数 {v:.0f} kg 为负或超过秤的量程"
                             + (f" {scale_max:.0f} kg。" if scale_max else "。"), i))

    # Axle sum must agree with the gross ticket within n*tol (each axle carries
    # its own scale error band).
    measured_axles = [v for v in norm["axle_kg"] if v is not None]
    if norm["gross_kg"] is not None and len(measured_axles) == len(norm["axle_kg"]):
        diff = norm["gross_kg"] - sum(measured_axles)
        band = tol_eff * max(1, len(measured_axles))
        if abs(diff) > band + EPS:
            gaps.append(_gap("AXLE_SUM_MISMATCH",
                             f"轴荷合计 {sum(measured_axles):.0f} kg 与总重 {norm['gross_kg']:.0f} kg "
                             f"相差 {diff:+.0f} kg，超出累计公差 ±{band:.0f} kg；"
                             "读数互不相容，需要重新过磅或核对录入。"))

    # Chronology: this ticket must not predate an earlier chronologically
    # recorded ticket.  ``chronology`` items expose stage + weighed_at.
    cur_t = parse_time(norm["weighed_at"])
    if cur_t is not None and chronology:
        try:
            rank = stage_rank(state, stage)
        except ValueError:
            rank = -1
        for prev in chronology:
            prev_stage = prev.get("stage", "")
            try:
                prev_rank = stage_rank(state, prev_stage)
            except ValueError:
                continue
            if rank < 0 or prev_rank >= rank:
                continue
            prev_t = parse_time(str(prev.get("weighed_at", "")))
            if prev_t is not None and cur_t < prev_t:
                gaps.append(_gap(
                    "TIME_ORDER",
                    f"称重时刻 {norm['weighed_at']} 早于上一程 {prev.get('label') or prev_stage} "
                    f"的 {prev.get('weighed_at')}；时刻倒序，先校正称重单时间。",
                    stage=prev_stage,
                ))

    return gaps, norm


# ----------------------------- residual / verdict -----------------------------

def residual_components(pred: Dict[str, Any], norm: Dict[str, Any]
                        ) -> Tuple[List[Dict[str, Any]], float]:
    """Measured-minus-predicted components for the CURRENT stage.

    Returns (components, tolerance used).
    """
    tol = max(DEFAULT_TOLERANCE_KG, float(norm.get("tolerance_kg") or 0.0))
    comps: List[Dict[str, Any]] = []
    gross = norm.get("gross_kg")
    if gross is not None:
        comps.append({"key": "gross", "label": "总重",
                      "measured": gross, "predicted": pred["gross_kg"],
                      "residual": gross - pred["gross_kg"], "tolerance": tol})
    for i, (a, m) in enumerate(zip(pred["axles"], norm.get("axle_kg", []))):
        if m is None:
            continue
        comps.append({"key": f"axle_{i}", "label": f"{a['name']}轴",
                      "axle_index": i,
                      "measured": m, "predicted": a["total_kg"],
                      "residual": m - a["total_kg"], "tolerance": tol})
    return comps, tol


def delta_components(state: Dict[str, Any], stage: str,
                     cur_norm: Dict[str, Any], prev_sheet: Optional[Dict[str, Any]],
                     cur_pred: Dict[str, Any], prev_pred: Optional[Dict[str, Any]],
                     tol: float) -> List[Dict[str, Any]]:
    """Front-to-back unload delta: measured mass shed vs planned shed mass."""
    if prev_sheet is None or prev_pred is None:
        return []
    prev_read = prev_sheet.get("readings") or {}
    prev_norm = norm_readings(prev_read, len(state["truck"]["axles"]))
    out: List[Dict[str, Any]] = []
    if cur_norm.get("gross_kg") is not None and prev_norm.get("gross_kg") is not None:
        measured_shed = prev_norm["gross_kg"] - cur_norm["gross_kg"]
        predicted_shed = prev_pred["gross_kg"] - cur_pred["gross_kg"]
        # Non-equipment load may change between tickets; the declared change is
        # known and removed so the delta compares equipment mass only.
        declared_extra_change = cur_pred["extra_mass_kg"] - prev_pred["extra_mass_kg"]
        measured_shed -= declared_extra_change
        out.append({"key": "delta_gross", "label": "总重卸载量",
                    "measured": measured_shed, "predicted": predicted_shed,
                    "residual": measured_shed - predicted_shed,
                    "tolerance": tol * 2})
    for i, axle in enumerate(cur_pred["axles"]):
        m_cur = (cur_norm.get("axle_kg") or [None] * len(cur_pred["axles"]))[i]
        m_prev = (prev_norm.get("axle_kg") or [None] * len(cur_pred["axles"]))[i]
        if m_cur is None or m_prev is None:
            continue
        shed = m_prev - m_cur
        predicted_shed = prev_pred["axles"][i]["total_kg"] - axle["total_kg"]
        shed -= cur_pred["axles"][i]["extra_kg"] - prev_pred["axles"][i]["extra_kg"]
        out.append({"key": f"delta_axle_{i}", "label": f"{axle['name']}轴卸载量",
                    "axle_index": i,
                    "measured": shed, "predicted": predicted_shed,
                    "residual": shed - predicted_shed, "tolerance": tol * 2})
    return out


def status_for(status: str, stale: bool) -> str:
    if status == "frozen" and stale:
        return "stale"
    return status


# ----------------------------- anomaly search -----------------------------

@dataclass
class Event:
    """One hypothesized on-site anomaly.

    kind:
      missing       应在车上的箱体漏装（从未装 / 已错卸），当前少了它
      extra         不应在车上的箱体（本站该卸未卸 / 错卸到车上），当前多了它
      weight        器材实际重量与清单不符，乘子 d (kg) 为实际-清单
      shift         纵向错位，重心相对计划位置平移 dx m
    """

    kind: str
    case_id: str
    label: str
    present_prev: bool = True
    d: float = 0.0
    dx: float = 0.0

    def key(self) -> Tuple[Any, ...]:
        if self.kind == "weight":
            return ("weight", self.case_id)
        if self.kind == "shift":
            return ("shift", self.case_id)
        return (self.kind, self.case_id, self.present_prev)


def _candidate_events(state: Dict[str, Any], stage: str,
                      has_prev: bool) -> List[Event]:
    """Build the discrete/continuous anomaly event pool."""
    state = norm_state(state)
    rank = stage_rank(state, stage)
    onboard = onboard_cases(state, stage)
    onboard_ids = {b["id"] for b in onboard}
    placed = {b["id"]: b for b in boxes_from(state)}
    stop = stage_stop(state, stage)
    events: List[Event] = []

    def add(ev: Event) -> None:
        events.append(ev)

    for b in onboard:
        cid, label = b["id"], b["label"]
        # 漏装：从未装上车（前一程也不在）。
        add(Event("missing", cid, f"{label} 漏装（车上找不到）", present_prev=False))
        # 错卸：前一程在车上、本站被错卸或提前卸下。
        if has_prev and rank > 0:
            add(Event("missing", cid, f"{label} 在本站被错卸", present_prev=True))
        # 重量偏差：实物重量 ≠ 清单重量（连续乘子在求解时拟合）。
        add(Event("weight", cid, f"{label} 实际重量与清单不符", present_prev=True))

    if stop is not None:
        # 漏卸（应卸未卸）：本站应卸的箱子还在车上；前一程它本就该在车上。
        for b in unloaded_at_stop(state, stop["id"]):
            add(Event("extra", b["id"], f"{b['label']} 本站应卸未卸（错卸留车）",
                      present_prev=True))
            # 重量偏差只可能在卸货这一程暴露：当前已不在车，只影响前一程读数。
            add(Event("weight", b["id"], f"{b['label']} 实际重量与清单不符（本程卸下）",
                      present_prev=False))
        # 前站就该卸下却一直留在车上的箱子：两张称重单都偏重。
        ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
        for cid, b in placed.items():
            if cid in onboard_ids:
                continue
            sr = ranks.get(b["case"].get("stop_id"))
            if sr is not None and sr < rank - 1:
                add(Event("extra", cid, f"{b['label']} 前站应卸未卸，仍留在车上",
                          present_prev=False))

    return events


def event_vector(state: Dict[str, Any], stage: str, ev: Event,
                 pred: Dict[str, Any], prev_pred: Optional[Dict[str, Any]]
                 ) -> Tuple[List[float], List[float]]:
    """Effect vectors [gross, axle_i...] for (current stage, previous stage).

    Sign follows the residual measured-minus-predicted.  The previous-stage
    vector is what distinguishes a box that was never loaded (already missing on
    the prior ticket) from one wrongly unloaded during this leg (prior ticket
    still matched the plan).
    """
    truck = state["truck"]
    n = len(truck["axles"])
    cur_vec = [0.0] * (n + 1)
    prev_vec = [0.0] * (n + 1)
    all_boxes = {b["id"]: b for b in boxes_from(state)}

    def add_payload(vec: List[float], cid: str, sign: float,
                    x_override: Optional[float] = None,
                    weight_override: Optional[float] = None) -> None:
        b = all_boxes.get(cid)
        if b is None:
            return
        w = b["weight"] if weight_override is None else weight_override
        x = (b["x"] + b["dx"] / 2.0) if x_override is None else x_override
        vec[0] += sign * w
        for i, f in enumerate(axle_fractions(truck, x)):
            vec[i + 1] += sign * w * f

    onboard_ids = set(pred["onboard_case_ids"])
    prev_onboard_ids = set(prev_pred["onboard_case_ids"]) if prev_pred else set()

    if ev.kind == "missing":
        # Absent now though the plan keeps it on board.
        if ev.case_id in onboard_ids:
            add_payload(cur_vec, ev.case_id, -1.0)
        # present_prev=False: the box was never loaded, so the prior ticket was
        # already lighter by its full weight.
        if not ev.present_prev and ev.case_id in prev_onboard_ids:
            add_payload(prev_vec, ev.case_id, -1.0)
        # present_prev=True: box was correctly on board at the prior weighing,
        # so that ticket matched the plan (zero previous effect).
    elif ev.kind == "extra":
        # Still on board though this stop should have removed it.  The prior
        # ticket legitimately counted the box, so the previous effect is zero.
        add_payload(cur_vec, ev.case_id, +1.0)
    elif ev.kind == "weight":
        b = all_boxes.get(ev.case_id)
        if b is None:
            return cur_vec, prev_vec
        if ev.case_id in onboard_ids:
            # Unit vector for a +1 kg weight discrepancy.
            add_payload(cur_vec, ev.case_id, +1.0, weight_override=1.0)
        if ev.case_id in prev_onboard_ids:
            add_payload(prev_vec, ev.case_id, +1.0, weight_override=1.0)
    elif ev.kind == "shift":
        b = all_boxes.get(ev.case_id)
        if b is None:
            return cur_vec, prev_vec
        x_plan = b["x"] + b["dx"] / 2.0
        f_now = axle_fractions(truck, x_plan + ev.dx)
        f_plan = axle_fractions(truck, x_plan)
        if ev.case_id in onboard_ids:
            for i in range(n):
                cur_vec[i + 1] += b["weight"] * (f_now[i] - f_plan[i])
        if ev.present_prev and ev.case_id in prev_onboard_ids:
            for i in range(n):
                prev_vec[i + 1] += b["weight"] * (f_now[i] - f_plan[i])
    return cur_vec, prev_vec


def _solve_weight_multiplier(events: Sequence[Event], cur_v: List[List[float]],
                             prev_v: List[List[float]], r_cur: List[float],
                             r_prev: List[float], bands: List[float],
                             prev_bands: List[float], use_prev: List[bool]) -> float:
    """Best-fit d for exactly one weight event (weighted least squares)."""
    wi = next((i for i, e in enumerate(events) if e.kind == "weight"), None)
    if wi is None:
        return 0.0
    num = den = 0.0
    for j in range(len(r_cur)):
        base = sum(cur_v[i][j] for i in range(len(events)) if i != wi)
        a = cur_v[wi][j]
        if abs(a) <= EPS:
            continue
        b = bands[j]
        num += a * (r_cur[j] - base) / (b * b)
        den += (a / b) ** 2
    for j in range(len(r_prev)):
        if not use_prev[j]:
            continue
        base = sum(prev_v[i][j] for i in range(len(events)) if i != wi)
        a = prev_v[wi][j]
        if abs(a) <= EPS:
            continue
        b = prev_bands[j]
        num += a * (r_prev[j] - base) / (b * b)
        den += (a / b) ** 2
    return num / den if den > EPS else 0.0


def _excess(vec: List[float], target: List[float], bands: List[float]) -> float:
    return sum(max(0.0, abs(v - t) - bands[j] - EPS)
               for j, v in enumerate(vec) for t in [target[j]])


def search_candidates(state: Dict[str, Any], stage: str,
                      cur_pred: Dict[str, Any], cur_norm: Dict[str, Any],
                      prev_pred: Optional[Dict[str, Any]],
                      prev_sheet: Optional[Dict[str, Any]],
                      tol: float) -> List[Dict[str, Any]]:
    """Minimum-event fit of anomalies explaining the residuals.

    Enumerates discrete combinations (missing / extra), at most one continuous
    weight discrepancy (least-squares kg), and optional longitudinal shifts on
    the involved cases.  Returns the cheapest fits with their axle contributions.
    """
    state = norm_state(state)
    truck = state["truck"]
    n = len(truck["axles"])
    has_prev = prev_sheet is not None and prev_pred is not None

    cur_comps, _ = residual_components(cur_pred, cur_norm)
    keys = [c["key"] for c in cur_comps]
    r_cur = [c["residual"] for c in cur_comps]
    bands = [max(tol, c["tolerance"]) for c in cur_comps]

    prev_norm = norm_readings(prev_sheet.get("readings") if prev_sheet else None, n)
    prev_comps, _ = residual_components(prev_pred, prev_norm) if prev_pred else ([], 0.0)
    prev_map = {c["key"]: c for c in prev_comps}
    r_prev_full = [prev_map[k]["residual"] if k in prev_map else 0.0 for k in keys]
    prev_bands = [prev_map[k]["tolerance"] if k in prev_map else tol for k in keys]

    # If there is no previous ticket, the "previous leg" comparison carries no
    # information: only use the previous vector for events that must explain a
    # delta even without a prior ticket? There is none — zero it.
    if not has_prev:
        r_prev = [0.0] * len(keys)
        use_prev = [False] * len(keys)
    else:
        r_prev = r_prev_full
        use_prev = [k in prev_map for k in keys]

    base_events = _candidate_events(state, stage, has_prev)
    # De-duplicate by event key.
    seen: set = set()
    events: List[Event] = []
    for ev in base_events:
        if ev.key() in seen:
            continue
        seen.add(ev.key())
        events.append(ev)

    # Vector cache for each event.
    vectors = {}
    for ev in events:
        cv_full, pv_full = event_vector(state, stage, ev, cur_pred, prev_pred)
        cv = _project_vector(cv_full, keys)
        pv = _project_vector(pv_full, keys)
        vectors[ev.key()] = (cv, pv)

    onboard = {b["id"]: b for b in onboard_cases(state, stage)}

    def evaluate(combo: List[Event], shifts: Dict[str, float]) -> Optional[Dict[str, Any]]:
        # Reject incompatible duplicates / two weight events.
        kinds = [e.kind for e in combo]
        if kinds.count("weight") > 1:
            return None
        keyset = [e.key() for e in combo]
        if len(set(keyset)) != len(keyset):
            return None
        # A longitudinal shift only makes sense for a case physically on board.
        for cid in shifts:
            if cid not in onboard:
                return None
        applied: List[Event] = list(combo)
        for cid, dx in shifts.items():
            if abs(dx) <= EPS:
                continue
            b = onboard[cid]
            applied.append(Event("shift", cid,
                                 f"{b['label']} 纵向错位 {dx:+.1f} m", dx=dx))
        cur_v = []
        prev_v = []
        for ev in applied:
            if ev.kind == "shift":
                cv, pv = _shift_vectors(state, stage, ev.case_id, ev.dx,
                                        cur_pred, prev_pred, keys)
            else:
                cv, pv = vectors[ev.key()]
            cur_v.append(cv)
            prev_v.append(pv)
        if any(e.kind == "weight" for e in applied):
            d = _solve_weight_multiplier(applied, cur_v, prev_v, r_cur,
                                         r_prev, bands, prev_bands, use_prev)
            wi = next(i for i, e in enumerate(applied) if e.kind == "weight")
            # Actual weight must stay non-negative: d >= -planned weight.
            planned = next((b["weight"] for b in boxes_from(state)
                            if b["id"] == applied[wi].case_id), 0.0)
            d = max(d, -planned + DEFAULT_MASS_TOLERANCE_KG)
            cur_v[wi] = [x * d for x in cur_v[wi]]
            prev_v[wi] = [x * d for x in prev_v[wi]]
            applied[wi].d = d
            # Ignore implausible tiny weight deltas as an explanation.
            if abs(d) < DEFAULT_MASS_TOLERANCE_KG:
                return None
        else:
            d = 0.0

        explained_cur = [sum(v[j] for v in cur_v) for j in range(len(keys))]
        explained_prev = [sum(v[j] for v in prev_v) for j in range(len(keys))]
        excess_cur = _excess(explained_cur, r_cur, bands)
        excess_prev = sum(max(0.0, abs(explained_prev[j] - r_prev[j]) - prev_bands[j] - EPS)
                          for j in range(len(keys)) if use_prev[j])
        excess = excess_cur + excess_prev
        if excess > max(tol, 1.0):
            return None
        # Residual after explanation, per component, for the UI.
        residual_after = [r_cur[j] - explained_cur[j] for j in range(len(keys))]
        n_discrete = sum(1 for e in applied if e.kind in ("missing", "extra"))
        n_weight = 1 if d else 0
        n_shift = sum(1 for e in applied if e.kind == "shift")
        return {
            "events": [_event_json(e) for e in applied],
            "event_count": n_discrete + n_weight + n_shift,
            "weight_delta_kg": round(d, 1),
            "excess_kg": round(excess, 1),
            "residual_after": [
                {"key": keys[j], "label": cur_comps[j]["label"],
                 "residual_kg": round(residual_after[j], 1),
                 "within_tolerance": abs(residual_after[j]) <= bands[j] + EPS}
                for j in range(len(keys))
            ],
            "axle_contributions": _axle_contribution_rows(
                state, stage, applied, cur_pred, d),
        }

    results: List[Dict[str, Any]] = []

    def consider(combo: List[Event]) -> None:
        # No shift.
        r = evaluate(combo, {})
        if r is not None:
            results.append(r)
        # Shift only the cases the combo involves; an empty combo also tries a
        # lone single-case shift (pure longitudinal misplacement).
        shift_targets = sorted({e.case_id for e in combo
                                if e.case_id in onboard and
                                e.kind in ("missing", "extra", "weight")})
        if not combo:
            shift_targets = list(onboard)
        if len(combo) >= MAX_CANDIDATE_EVENTS:
            return
        for cid in shift_targets[:14]:
            for dx in SHIFT_GRID:
                r = evaluate(combo, {cid: dx})
                if r is not None:
                    results.append(r)

    discrete = [e for e in events if e.kind in ("missing", "extra")]
    weights = [e for e in events if e.kind == "weight"]

    def any_complete() -> bool:
        return bool(results)

    # Tier 0: no discrete events — covers a lone longitudinal misplacement.
    consider([])
    if not any_complete():
        # Tier 1: a single anomaly.
        for e in discrete:
            consider([e])
        for w in weights:
            consider([w])
    if not any_complete() and len(events) <= 90:
        # Tier 2: two anomalies (only when a single anomaly cannot explain it).
        for i, e in enumerate(discrete):
            for e2 in discrete[i + 1:]:
                consider([e, e2])
            for w in weights:
                if w.case_id != e.case_id:
                    consider([e, w])
        for i, w in enumerate(weights):
            for w2 in weights[i + 1:]:
                consider([w, w2])
    if not any_complete() and len(discrete) <= 28:
        # Tier 3: three discrete anomalies.
        for i, e in enumerate(discrete):
            for j in range(i + 1, len(discrete)):
                for k in range(j + 1, len(discrete)):
                    consider([e, discrete[j], discrete[k]])

    # Deduplicate identical event sets and rank by fewest events then fit.
    ranked: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for r in results:
        sig = tuple(sorted(
            f"{e['kind']}:{e['case_id']}:{round(e.get('dx_m', 0.0), 2)}:{round(e.get('weight_delta_kg', 0.0), 1)}"
            for e in r["events"]))
        old = ranked.get(sig)
        # Fewer anomalies first; prefer an exact mass-conservation explanation:
        # the gross (total) residual is the direct conservation measurement, so
        # closing it ranks above absorbing the error into axle tolerances;
        # then least total leftover, smaller weight delta and smaller move.
        gross_leftover = 0.0
        for x in r["residual_after"]:
            if x["key"] == "gross":
                gross_leftover = abs(x["residual_kg"])
        weight_delta = sum(abs(e.get("weight_delta_kg", 0.0)) for e in r["events"])
        shift_move = sum(abs(e.get("dx_m", 0.0)) for e in r["events"])
        leftover = round(sum(abs(x["residual_kg"]) for x in r["residual_after"]), 1)
        score = (r["event_count"], round(r["excess_kg"], 1), round(gross_leftover, 1),
                 leftover, round(weight_delta, 1), round(shift_move, 3))
        r["_score"] = score
        if old is None or score < old["_score"]:
            ranked[sig] = r
    ordered = sorted(ranked.values(), key=lambda r: r["_score"])
    for r in ordered:
        r.pop("_score", None)
    return ordered[:MAX_CANDIDATES_RETURNED]


def _project_vector(full: List[float], keys: Sequence[str]) -> List[float]:
    """Project a [gross, axle_0, axle_1, ...] vector onto measured key order."""
    out = []
    for k in keys:
        if k == "gross":
            out.append(full[0])
        elif k.startswith("axle_"):
            i = int(k.split("_")[1])
            out.append(full[i + 1] if i + 1 < len(full) else 0.0)
        else:
            out.append(0.0)
    return out


def _shift_vectors(state: Dict[str, Any], stage: str, case_id: str, dx: float,
                   cur_pred: Dict[str, Any], prev_pred: Optional[Dict[str, Any]],
                   keys: Sequence[str]) -> Tuple[List[float], List[float]]:
    ev = Event("shift", case_id, "", dx=dx)
    cv, pv = event_vector(state, stage, ev, cur_pred, prev_pred)
    return _project_vector(cv, keys), _project_vector(pv, keys)


def _axle_contribution_rows(state: Dict[str, Any], stage: str,
                            events: Sequence[Event], pred: Dict[str, Any],
                            weight_d: float) -> List[Dict[str, Any]]:
    """Per-candidate contribution landing on each axle, for side-view overlay."""
    truck = state["truck"]
    onboard = {b["id"]: b for b in onboard_cases(state, stage)}
    all_boxes = {b["id"]: b for b in boxes_from(state)}
    rows = []
    for ev in events:
        b = onboard.get(ev.case_id) or all_boxes.get(ev.case_id)
        if b is None:
            continue
        if ev.kind == "shift":
            x_plan = b["x"] + b["dx"] / 2.0
            f_now = axle_fractions(truck, x_plan + ev.dx)
            f_plan = axle_fractions(truck, x_plan)
            per = [b["weight"] * (f_now[i] - f_plan[i]) for i in range(len(truck["axles"]))]
            kind_cn = "纵向错位"
        elif ev.kind == "weight":
            per = [ev.d * f for f in axle_fractions(truck, b["x"] + b["dx"] / 2.0)]
            kind_cn = "重量偏差"
        else:
            sign = -1.0 if ev.kind == "missing" else +1.0
            per = [sign * b["weight"] * f for f in
                   axle_fractions(truck, b["x"] + b["dx"] / 2.0)]
            kind_cn = "漏装" if ev.kind == "missing" else "错卸"
        rows.append({
            "kind": ev.kind, "kind_cn": kind_cn,
            "case_id": ev.case_id, "label": ev.label,
            "present_prev": ev.present_prev,
            "dx_m": ev.dx, "weight_delta_kg": ev.d if ev.kind == "weight" else 0.0,
            "x_m": b["x"], "dx_m_box": b["dx"],
            "center_x_m": b["x"] + b["dx"] / 2.0,
            "axle_delta_kg": [round(v, 1) for v in per],
        })
    return rows


def _event_json(ev: Event) -> Dict[str, Any]:
    return {
        "kind": ev.kind, "case_id": ev.case_id, "label": ev.label,
        "present_prev": ev.present_prev,
        "dx_m": ev.dx, "weight_delta_kg": round(ev.d, 1),
    }


# ----------------------------- apply resolution -----------------------------

def apply_resolution(state: Dict[str, Any], stage: str,
                     events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a new state reflecting the confirmed on-site findings.

    The original state is not mutated; callers persist the returned state as a
    derived version so the planned snapshot is preserved.  Cases that move
    longitudinally keep their orientation/y/z; straps touching moved cases are
    unlocked for re-review (same rule as ``auto_arrange``).
    """
    out = norm_state(state)
    moved: set = set()
    stop = stage_stop(out, stage)
    next_stop_id = ""
    rank = stage_rank(out, stage)
    # Stage rank 0 is departure; after-stop k has rank k+1, so the next stop is
    # stops[rank].  A box not unloaded at the last stop simply stays assigned.
    if 0 < rank < len(out["stops"]):
        next_stop_id = out["stops"][rank]["id"]

    for ev in events:
        kind, cid = ev.get("kind"), ev.get("case_id")
        case = next((c for c in out["cases"] if c["id"] == cid), None)
        p = next((q for q in out["placements"] if q["case_id"] == cid), None)
        if kind in ("missing",) and not ev.get("present_prev", True):
            # 漏装（从未上车）：从装载中移除。
            out["placements"] = [q for q in out["placements"] if q["case_id"] != cid]
            moved.add(cid)
        elif kind == "missing" and ev.get("present_prev", True):
            # 本站被错卸 / 提前错卸：箱子已在地面，同样从车上移除。
            out["placements"] = [q for q in out["placements"] if q["case_id"] != cid]
            moved.add(cid)
        elif kind == "extra":
            # 应卸未卸：顺延到下一站（无下一站则留在当前站）。
            if case is not None and next_stop_id:
                case["stop_id"] = next_stop_id
        elif kind == "weight":
            if case is not None:
                case["weight_kg"] = round(max(0.0, float(case["weight_kg"])
                                              + float(ev.get("weight_delta_kg", 0.0))), 1)
        elif kind == "shift":
            if p is not None:
                L = float(out["truck"]["length"])
                box = next((b for b in boxes_from(out) if b["id"] == cid), None)
                dx = float(ev.get("dx_m", 0.0))
                p["x"] = round(min(max(0.0, p["x"] + dx),
                                   L - (box["dx"] if box else 0.0)), 3)
                moved.add(cid)

    # Straps touching removed or shifted cases become invalid / must be rechecked.
    placed_ids = {q["case_id"] for q in out["placements"]}
    kept_lashings = []
    for lash in out.get("lashings", []):
        case_ends = [ep for ep in (lash["from"], lash["to"]) if ep["kind"] == "case"]
        if any(ep["id"] not in placed_ids for ep in case_ends):
            continue
        if any(ep["id"] in moved for ep in case_ends):
            lash = deepcopy(lash)
            lash["locked"] = False
            lash["review_signature"] = ""
        kept_lashings.append(lash)
    out["lashings"] = kept_lashings
    return out


# ----------------------------- signature / staleness -----------------------------

def sheet_signature(state: Dict[str, Any], stage: str) -> str:
    """Content hash of everything the ticket is checked against.

    Scoped to the stage: axle geometry/weights, the stop order, and only the
    cases that are on board or unloaded at this stage participate, so editing a
    later leg merely marks the associated stations' tickets for re-review.
    """
    state = norm_state(state)
    truck = state["truck"]
    parts = ["stage=" + stage]
    parts.append("stops=" + ",".join(f"{s['id']}@{s['city']}" for s in state["stops"]))
    for i, a in enumerate(truck["axles"]):
        parts.append(f"ax{i}:{a.get('name','')},{float(a['position'])},{float(a.get('tare_kg', 0))},"
                     f"{float(a.get('capacity_kg', 0))}")
    cfg = weigh_config(truck)
    parts.append("weighcfg=" + ",".join(f"{k}={cfg[k]}" for k in sorted(cfg)))
    rank = stage_rank(state, stage)
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    stop = stage_stop(state, stage)
    involved = set()
    for b in boxes_from(state):
        sr = ranks.get(b["case"].get("stop_id"))
        if sr is None:
            continue
        # On board at this stage, or unloaded at exactly this stop.
        if sr >= rank or (stop is not None and b["case"].get("stop_id") == stop["id"]):
            involved.add(b["id"])
    for b in sorted(boxes_from(state), key=lambda q: q["id"]):
        if b["id"] not in involved:
            continue
        parts.append("|".join([
            b["id"], b["case"].get("stop_id", ""),
            ",".join(str(round(v, 3)) for v in (b["x"], b["y"], b["z"], b["dx"], b["dy"], b["dz"])),
            f"w{round(b['weight'], 2)}", b.get("orientation", "LWH"),
        ]))
    return fnv1a32("\n".join(parts))


# ----------------------------- full evaluation -----------------------------

def evaluate_sheet(state: Dict[str, Any], stage: str,
                   readings: Optional[Dict[str, Any]] = None,
                   chronology: Optional[Sequence[Dict[str, Any]]] = None,
                   prev_sheet: Optional[Dict[str, Any]] = None,
                   ) -> Dict[str, Any]:
    """Validate one ticket, compute residuals, and search anomaly candidates."""
    state = norm_state(state)
    truck = state["truck"]
    stop = stage_stop(state, stage)
    norm = norm_readings(readings, len(truck["axles"]))
    tol = max(DEFAULT_TOLERANCE_KG, float(norm.get("tolerance_kg") or 0.0))

    gaps, norm = evidence_gaps(state, stage, readings, tol, chronology)
    unknown_stage = any(g["code"] == "UNKNOWN_STAGE" for g in gaps)

    pred = None
    if not unknown_stage:
        pred = predicted_readings(state, stage, norm)

    prev_pred = None
    prev_stage = ""
    if prev_sheet is not None and not unknown_stage:
        prev_stage = str(prev_sheet.get("stage", ""))
        if prev_stage and prev_stage != stage:
            try:
                prev_pred = predicted_readings(
                    state, prev_stage, prev_sheet.get("readings") or {})
            except ValueError:
                prev_pred = None

    result: Dict[str, Any] = {
        "stage": stage,
        "stage_title": ("发车前（满载）" if stage == "departure"
                        else f"{stop['city'] if stop else stage} 卸货后"),
        "stop_id": stop["id"] if stop else None,
        "predicted": ({k: v for k, v in pred.items() if k != "case_contributions"}
                      if pred is not None else None),
        "gaps": gaps,
        "components": [],
        "deltas": [],
        "verdict": "incomplete",
        "candidates": [],
        "generated_at": now_iso(),
        "signature": (sheet_signature(state, stage) if not unknown_stage else ""),
    }

    if gaps:
        result["verdict"] = "evidence_gap"
        return result

    comps, tol_used = residual_components(pred, norm)
    for c in comps:
        c["within_tolerance"] = abs(c["residual"]) <= c["tolerance"] + EPS
    result["components"] = [{
        "key": c["key"], "label": c["label"],
        "measured_kg": round(c["measured"], 1), "predicted_kg": round(c["predicted"], 1),
        "residual_kg": round(c["residual"], 1), "tolerance_kg": round(c["tolerance"], 1),
        "within_tolerance": c["within_tolerance"],
    } for c in comps]

    deltas = delta_components(state, stage, norm, prev_sheet, pred, prev_pred, tol_used)
    result["deltas"] = [{
        "key": d["key"], "label": d["label"],
        "measured_kg": round(d["measured"], 1), "predicted_kg": round(d["predicted"], 1),
        "residual_kg": round(d["residual"], 1), "tolerance_kg": round(d["tolerance"], 1),
        "within_tolerance": abs(d["residual"]) <= d["tolerance"] + EPS,
    } for d in deltas]

    over = [c for c in result["components"] if not c["within_tolerance"]]
    delta_over = [d for d in result["deltas"] if not d["within_tolerance"]]
    if not over and not delta_over:
        result["verdict"] = "within_tolerance"
        return result

    result["verdict"] = "out_of_tolerance"
    result["candidates"] = search_candidates(
        state, stage, pred, norm, prev_pred, prev_sheet, tol_used)
    return result
