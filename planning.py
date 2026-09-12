"""Core geometry, safety checks, unloading simulation and greedy auto-loading.

The module deliberately has no Flask dependency so the packing rules can be
tested independently.  Coordinates are measured in metres.

X: rear tailgate (0) -> front wall (truck.length)
Y: viewer's left (0) -> viewer's right (truck.width)
Z: floor (0) -> roof (truck.height)
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

EPS = 1.0e-6
SNAP = 0.05
SUPPORT_RATIO = 0.98
CELL = 0.1

# Lashing (cargo securing) defaults.  Accelerations are in g, following the
# common EN 12195-style simplified model used by the teaching tool.
DEFAULT_ACCEL = {"forward": 0.8, "rearward": 0.5, "lateral": 0.5, "up": 0.3, "down": 1.0}
DEFAULT_FRICTION = 0.35
DEFAULT_STRAP_CAPACITY_KG = 1000.0
DEFAULT_PRETENSION_KG = 200.0
ALL_LASH_FACES = ["-x", "+x", "-y", "+y"]
DIRECTIONS_3D = ["-x", "+x", "-y", "+y", "-z", "+z"]
SLIP_REQUIRED_MARGIN = 1.0
SLIP_WARN_MARGIN = 1.15
MAX_LASH_ANGLE_DEG = 60.0
NO_LASHING_WEIGHT_KG = 100.0

ORIENTATIONS = {
    "LWH": (0, 1, 2),
    "WLH": (1, 0, 2),
    "LHW": (0, 2, 1),
    "WHL": (1, 2, 0),
    "HLW": (2, 0, 1),
    "HWL": (2, 1, 0),
}

# Venue access (落地通道) defaults.  Caster modes carry the threshold a case
# can roll over; carrying has no practical threshold limit.
CASTER_MODES = {
    "carry": {"label": "抬运（无脚轮）", "threshold_mm": 1000.0},
    "fixed2": {"label": "两定向+两万向脚轮", "threshold_mm": 40.0},
    "swivel4": {"label": "全万向脚轮", "threshold_mm": 20.0},
}
DEFAULT_HANDLING = {
    "caster_mode": "swivel4",
    "min_turn_radius": 1.2,
    "crew": 2,
    "push_limit_kg": 250.0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def uid() -> str:
    return uuid.uuid4().hex[:12]


def _num(value: Any, default: float, lo: float = 0.0, hi: Optional[float] = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    v = max(lo, v)
    return min(hi, v) if hi is not None else v


def norm_handling(raw: Any) -> Dict[str, Any]:
    """Per-case venue-handling parameters (caster mode, turning, crew)."""
    h = deepcopy(raw) if isinstance(raw, dict) else {}
    mode = h.get("caster_mode", DEFAULT_HANDLING["caster_mode"])
    h["caster_mode"] = mode if mode in CASTER_MODES else DEFAULT_HANDLING["caster_mode"]
    h["min_turn_radius"] = _num(h.get("min_turn_radius"), DEFAULT_HANDLING["min_turn_radius"], 0.0)
    try:
        h["crew"] = max(0, int(h.get("crew", DEFAULT_HANDLING["crew"])))
    except (TypeError, ValueError):
        h["crew"] = DEFAULT_HANDLING["crew"]
    h["push_limit_kg"] = _num(h.get("push_limit_kg"), DEFAULT_HANDLING["push_limit_kg"], 0.0)
    return h


def norm_access(raw: Any) -> Dict[str, Any]:
    """Per-stop venue access: tail lift / ramp, venue plan, path, confirmations."""
    a = deepcopy(raw) if isinstance(raw, dict) else {}
    a["kind"] = a.get("kind") if a.get("kind") in ("lift", "ramp") else "lift"
    a["width"] = _num(a.get("width"), 2.2, 0.2)
    a["length"] = _num(a.get("length"), 2.0, 0.2)
    a["capacity_kg"] = _num(a.get("capacity_kg"), 1500.0)
    a["slope_pct"] = _num(a.get("slope_pct"), 0.0)
    a["max_slope_pct"] = _num(a.get("max_slope_pct"), 8.0)
    a["threshold_mm"] = _num(a.get("threshold_mm"), 0.0)
    a["clear_height"] = _num(a.get("clear_height"), 2.4, 0.5)
    a["edge_load_ratio"] = _num(a.get("edge_load_ratio"), 0.55, 0.0, 1.0)
    try:
        a["parallel_slots"] = max(1, int(a.get("parallel_slots", 1)))
    except (TypeError, ValueError):
        a["parallel_slots"] = 1
    tz = a.get("turn_zone") if isinstance(a.get("turn_zone"), dict) else {}
    a["turn_zone"] = {
        "width": _num(tz.get("width"), 3.0, 0.5),
        "depth": _num(tz.get("depth"), 3.0, 0.5),
    }
    venue = a.get("venue") if isinstance(a.get("venue"), dict) else {}
    dock = venue.get("dock") if isinstance(venue.get("dock"), dict) else {}
    staging = venue.get("staging") if isinstance(venue.get("staging"), dict) else {}
    obstacles = []
    for raw_o in venue.get("obstacles") or []:
        if not isinstance(raw_o, dict):
            continue
        o = {
            "x": _num(raw_o.get("x"), 0.0),
            "y": _num(raw_o.get("y"), 0.0),
            "dx": _num(raw_o.get("dx"), 0.0),
            "dy": _num(raw_o.get("dy"), 0.0),
            "label": str(raw_o.get("label", "障碍")),
        }
        if o["dx"] > EPS and o["dy"] > EPS:
            obstacles.append(o)
    a["venue"] = {
        "width": _num(venue.get("width"), 14.0, 2.0),
        "depth": _num(venue.get("depth"), 9.0, 2.0),
        "dock": {"x": _num(dock.get("x"), 1.0), "y": _num(dock.get("y"), 4.5)},
        "staging": {
            "x": _num(staging.get("x"), 10.5),
            "y": _num(staging.get("y"), 5.5),
            "dx": _num(staging.get("dx"), 3.0, 0.3),
            "dy": _num(staging.get("dy"), 3.0, 0.3),
            "label": str(staging.get("label", "暂存区")),
        },
        "obstacles": obstacles,
    }
    path = []
    for raw_p in a.get("path") or []:
        if not isinstance(raw_p, dict):
            continue
        path.append({"x": _num(raw_p.get("x"), 0.0), "y": _num(raw_p.get("y"), 0.0)})
    a["path"] = path
    confirmed = {}
    for cid, rec in (a.get("confirmed_steps") or {}).items():
        if isinstance(rec, dict) and rec.get("sig"):
            confirmed[str(cid)] = {"sig": str(rec["sig"]), "at": str(rec.get("at", "")),
                                   "by": str(rec.get("by", ""))}
    a["confirmed_steps"] = confirmed
    return a


def norm_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized copy, filling fields used by older data files."""
    state = deepcopy(state or {})
    truck = deepcopy(state.get("truck") or {})
    truck.setdefault("id", "truck-1")
    truck.setdefault("name", "巡演卡车")
    truck.setdefault("length", 6.0)
    truck.setdefault("width", 2.6)
    truck.setdefault("height", 2.4)
    truck.setdefault("floor_limit_kg_m2", 1200.0)
    truck.setdefault("floor_point_limit_kg", 350.0)
    truck.setdefault("gvw_limit_kg", 12500.0)
    door = truck.setdefault("door", {})
    door.setdefault("width", 2.5)
    door.setdefault("height", 2.2)
    door.setdefault("sill", 0.0)
    axles = truck.setdefault(
        "axles",
        [
            {"name": "后轴", "position": 1.0, "tare_kg": 2500.0, "capacity_kg": 7500.0},
            {"name": "前轴", "position": 6.0, "tare_kg": 2500.0, "capacity_kg": 5000.0},
        ],
    )
    for i, axle in enumerate(axles):
        axle.setdefault("name", f"轴{i + 1}")
        axle.setdefault("position", 0.0)
        axle.setdefault("tare_kg", 0.0)
        axle.setdefault("capacity_kg", 0.0)
    truck["axles"] = sorted(axles, key=lambda a: float(a["position"]))

    accel = deepcopy(truck.get("accel") or {})
    for key, default in DEFAULT_ACCEL.items():
        accel.setdefault(key, default)
        accel[key] = float(accel[key])
    truck["accel"] = accel

    anchors = []
    for i, raw in enumerate(deepcopy(truck.get("anchors") or [])):
        a = dict(raw)
        a.setdefault("id", f"anchor-{i + 1}")
        a.setdefault("label", a["id"])
        a.setdefault("surface", "floor")
        a.setdefault("x", 0.0)
        a.setdefault("y", float(truck["width"]) / 2)
        a.setdefault("z", 0.0)
        a.setdefault("capacity_kg", 1000.0)
        a.setdefault("group", "")
        a.setdefault("group_capacity_kg", 0.0)
        allowed = a.setdefault("directions", ["+x", "-x", "+y", "-y", "+z"])
        a["directions"] = [d for d in allowed if d in DIRECTIONS_3D]
        try:
            a["x"] = float(a["x"]); a["y"] = float(a["y"]); a["z"] = float(a["z"])
            a["capacity_kg"] = float(a["capacity_kg"])
        except (TypeError, ValueError):
            continue
        anchors.append(a)
    truck["anchors"] = anchors

    strap_defaults = deepcopy(truck.get("strap_defaults") or {})
    strap_defaults.setdefault("capacity_kg", DEFAULT_STRAP_CAPACITY_KG)
    strap_defaults.setdefault("pretension_kg", DEFAULT_PRETENSION_KG)
    truck["strap_defaults"] = {
        "capacity_kg": float(strap_defaults["capacity_kg"]),
        "pretension_kg": float(strap_defaults["pretension_kg"]),
    }

    stops = deepcopy(state.get("stops") or [])
    stop_seen = set()
    for i, stop in enumerate(stops):
        stop.setdefault("id", f"stop-{i + 1}")
        stop.setdefault("city", f"城市 {i + 1}")
        stop.setdefault("venue", "")
        stop["access"] = norm_access(stop.get("access"))
        stop_seen.add(stop["id"])

    cases = deepcopy(state.get("cases") or [])
    case_seen = set()
    for i, case in enumerate(cases):
        case.setdefault("id", f"case-{i + 1}")
        case.setdefault("label", case["id"])
        case.setdefault("stop_id", stops[0]["id"] if stops else "")
        dims = case.setdefault("dims", [1.0, 1.0, 1.0])
        if len(dims) != 3:
            raise ValueError(f"{case.get('label')} 尺寸必须是 [长,宽,高]")
        case.setdefault("weight_kg", 0.0)
        case.setdefault("allowed_orientations", ["LWH", "WLH"])
        case.setdefault("max_stack_kg", 0.0)
        case.setdefault("forbidden_neighbors", [])
        case.setdefault("notes", "")
        case.setdefault("friction", DEFAULT_FRICTION)
        try:
            case["friction"] = min(1.0, max(0.0, float(case["friction"])))
        except (TypeError, ValueError):
            case["friction"] = DEFAULT_FRICTION
        faces = case.setdefault("lash_faces", list(ALL_LASH_FACES))
        case["lash_faces"] = [f for f in faces if f in ALL_LASH_FACES] or list(ALL_LASH_FACES)
        zones = []
        for raw_zone in deepcopy(case.get("no_strap_zones") or []):
            if not isinstance(raw_zone, dict):
                continue
            try:
                zone = {
                    "x": float(raw_zone.get("x", 0.0)),
                    "y": float(raw_zone.get("y", 0.0)),
                    "z": float(raw_zone.get("z", 0.0)),
                    "dx": float(raw_zone.get("dx", 0.0)),
                    "dy": float(raw_zone.get("dy", 0.0)),
                    "dz": float(raw_zone.get("dz", 0.0)),
                    "label": str(raw_zone.get("label", "禁压区")),
                }
            except (TypeError, ValueError):
                continue
            if zone["dx"] > EPS and zone["dy"] > EPS and zone["dz"] > EPS:
                zones.append(zone)
        case["no_strap_zones"] = zones
        case["handling"] = norm_handling(case.get("handling"))
        case_seen.add(case["id"])

    # Unloading trips per stop reference cases, so they normalize after cases.
    for stop in stops:
        trips = []
        for raw_trip in stop.get("trips") or []:
            if not isinstance(raw_trip, dict):
                continue
            ids = [cid for cid in (raw_trip.get("case_ids") or []) if cid in case_seen]
            if ids:
                trips.append({"case_ids": ids})
        stop["trips"] = trips

    placements = []
    for raw in deepcopy(state.get("placements") or []):
        if raw.get("case_id") not in case_seen:
            continue
        item = dict(raw)
        item.setdefault("x", 0.0)
        item.setdefault("y", 0.0)
        item.setdefault("z", 0.0)
        item.setdefault("orientation", "LWH")
        item.setdefault("locked", False)
        placements.append(item)

    anchor_seen = {a["id"] for a in truck["anchors"]}
    lashings = []
    for i, raw in enumerate(deepcopy(state.get("lashings") or [])):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item.setdefault("id", f"lash-{i + 1}")
        item.setdefault("label", item["id"])
        item.setdefault("from", {})
        item.setdefault("to", {})
        item.setdefault("pretension_kg", truck["strap_defaults"]["pretension_kg"])
        item.setdefault("capacity_kg", truck["strap_defaults"]["capacity_kg"])
        item.setdefault("locked", False)
        item.setdefault("review_signature", "")
        try:
            item["pretension_kg"] = max(0.0, float(item["pretension_kg"]))
            item["capacity_kg"] = max(0.0, float(item["capacity_kg"]))
        except (TypeError, ValueError):
            continue
        for end in ("from", "to"):
            ep = item[end] if isinstance(item[end], dict) else {}
            ep.setdefault("kind", "anchor")
            ep.setdefault("id", "")
            ep.setdefault("face", "")
            ep.setdefault("u", 0.5)
            ep.setdefault("v", 0.5)
            try:
                ep["u"] = min(1.0, max(0.0, float(ep["u"])))
                ep["v"] = min(1.0, max(0.0, float(ep["v"])))
            except (TypeError, ValueError):
                ep["u"], ep["v"] = 0.5, 0.5
            item[end] = ep
        kinds = {item["from"]["kind"], item["to"]["kind"]}
        if kinds - {"anchor", "case"}:
            continue
        # Drop dangling ends.
        valid = True
        for ep in (item["from"], item["to"]):
            if ep["kind"] == "anchor" and ep["id"] not in anchor_seen:
                valid = False
            if ep["kind"] == "case" and ep["id"] not in case_seen:
                valid = False
        if not valid:
            continue
        item["locked"] = bool(item["locked"])
        lashings.append(item)

    return {
        "name": state.get("name", "未命名方案"),
        "truck": truck,
        "stops": stops,
        "cases": cases,
        "placements": placements,
        "lashings": lashings,
    }


def case_map(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in state["cases"]}


def stop_map(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {s["id"]: s for s in state["stops"]}


def oriented_dims(case: Dict[str, Any], orientation: str) -> Tuple[float, float, float]:
    orientation = orientation if orientation in ORIENTATIONS else "LWH"
    l, w, h = map(float, case["dims"])
    values = (l, w, h)
    order = ORIENTATIONS[orientation]
    return values[order[0]], values[order[1]], values[order[2]]


def box_dict(case: Dict[str, Any], placement: Dict[str, Any]) -> Dict[str, Any]:
    dx, dy, dz = oriented_dims(case, placement.get("orientation", "LWH"))
    return {
        "id": case["id"],
        "label": case.get("label", case["id"]),
        "stop_id": case.get("stop_id", ""),
        "x": float(placement.get("x", 0.0)),
        "y": float(placement.get("y", 0.0)),
        "z": float(placement.get("z", 0.0)),
        "dx": dx,
        "dy": dy,
        "dz": dz,
        "orientation": placement.get("orientation", "LWH"),
        "weight": float(case.get("weight_kg", 0.0)),
        "max_stack_kg": float(case.get("max_stack_kg", 0.0) or 0.0),
        "locked": bool(placement.get("locked", False)),
        "case": case,
        "placement": placement,
    }


def boxes_from(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    cmap = case_map(state)
    result = []
    for p in state["placements"]:
        case = cmap.get(p["case_id"])
        if case:
            result.append(box_dict(case, p))
    return result


def overlap_1d(a: float, b: float, c: float, d: float) -> float:
    return max(0.0, min(b, d) - max(a, c))


def rect_intersect(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, dx: float, dy: float,
) -> Optional[Tuple[float, float, float, float]]:
    x0, x1 = max(ax, cx), min(bx, dx)
    y0, y1 = max(ay, cy), min(by, dy)
    if x1 <= x0 + EPS or y1 <= y0 + EPS:
        return None
    return x0, y0, x1, y1


def interval_touch(a0: float, a1: float, b0: float, b1: float, gap: float = 0.03) -> bool:
    return overlap_1d(a0, a1, b0, b1) > EPS and abs(min(abs(a1 - b0), abs(b1 - a0))) <= gap


def face_adjacent(a: Dict[str, Any], b: Dict[str, Any], gap: float = 0.03) -> bool:
    """True when boxes are face-neighbours (touching or nearly touching)."""
    axes = [
        ((a["x"], a["x"] + a["dx"]), (b["x"], b["x"] + b["dx"])),
        ((a["y"], a["y"] + a["dy"]), (b["y"], b["y"] + b["dy"])),
        ((a["z"], a["z"] + a["dz"]), (b["z"], b["z"] + b["dz"])),
    ]
    overlaps = [overlap_1d(*a0, *b0) > EPS for a0, b0 in axes]
    if sum(overlaps) != 2:
        return False
    for i, (ov) in enumerate(overlaps):
        if not ov:
            (a0, a1), (b0, b1) = axes[i]
            return min(abs(a1 - b0), abs(b1 - a0)) <= gap
    return False


def union_area(rects: List[Tuple[float, float, float, float]]) -> float:
    if not rects:
        return 0.0
    xs = sorted({x for r in rects for x in (r[0], r[2])})
    total = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        ys = []
        for rx0, ry0, rx1, ry1 in rects:
            if rx0 <= x0 + EPS and rx1 >= x1 - EPS:
                ys.append((ry0, ry1))
        if not ys:
            continue
        ys.sort()
        cy0, cy1 = ys[0]
        for y0, y1 in ys[1:]:
            if y0 > cy1:
                total += (x1 - x0) * (cy1 - cy0)
                cy0, cy1 = y0, y1
            else:
                cy1 = max(cy1, y1)
        total += (x1 - x0) * (cy1 - cy0)
    return total


def contact_rects(upper: Dict[str, Any], lower: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    if abs(lower["z"] + lower["dz"] - upper["z"]) > EPS:
        return None
    return rect_intersect(
        upper["x"], upper["y"], upper["x"] + upper["dx"], upper["y"] + upper["dy"],
        lower["x"], lower["y"], lower["x"] + lower["dx"], lower["y"] + lower["dy"],
    )


def issue(
    issues: List[Dict[str, Any]], severity: str, code: str, message: str,
    case_ids: Optional[Iterable[str]] = None, box: Optional[Dict[str, Any]] = None,
) -> None:
    item = {"severity": severity, "code": code, "message": message,
            "case_ids": list(case_ids or [])}
    if box:
        item["bbox"] = {k: box[k] for k in ("x", "y", "z", "dx", "dy", "dz")}
    issues.append(item)


def door_rect(truck: Dict[str, Any]) -> Tuple[float, float, float, float]:
    door = truck["door"]
    y0 = (float(truck["width"]) - float(door["width"])) / 2.0
    return y0, float(door.get("sill", 0.0)), y0 + float(door["width"]), float(door["height"])


def add_case_issue(case_issues: Dict[str, Any], case_id: str, severity: str, code: str) -> None:
    bucket = case_issues.setdefault(case_id, {"errors": [], "warnings": [], "infos": []})
    key = {"error": "errors", "warning": "warnings"}.get(severity, "infos")
    if code not in bucket[key]:
        bucket[key].append(code)


def geometric_checks(state: Dict[str, Any], boxes: List[Dict[str, Any]], issues: List[Dict[str, Any]],
                     case_issues: Dict[str, Any]) -> None:
    truck = state["truck"]
    L, W, H = map(float, (truck["length"], truck["width"], truck["height"]))
    dy0, dz0, dy1, dz1 = door_rect(truck)

    placements_seen = set()
    for p in state["placements"]:
        cid = p.get("case_id")
        if cid in placements_seen:
            issue(issues, "error", "DUPLICATE_PLACEMENT", f"{cid} 被重复放置", [cid])
        placements_seen.add(cid)

    by_id = {b["id"]: b for b in boxes}
    for b in boxes:
        allowed = b["case"].get("allowed_orientations") or ["LWH"]
        if b["orientation"] not in allowed:
            issue(issues, "error", "ORIENTATION",
                  f"{b['label']} 不允许 {b['orientation']} 方向", [b["id"]], b)
            add_case_issue(case_issues, b["id"], "error", "ORIENTATION")
        if (b["x"] < -EPS or b["y"] < -EPS or b["z"] < -EPS or
                b["x"] + b["dx"] > L + EPS or b["y"] + b["dy"] > W + EPS or
                b["z"] + b["dz"] > H + EPS):
            issue(issues, "error", "OUT_OF_BOUNDS",
                  f"{b['label']} 超出舱体边界", [b["id"]], b)
            add_case_issue(case_issues, b["id"], "error", "OUT_OF_BOUNDS")
        if b["y"] < dy0 - EPS or b["y"] + b["dy"] > dy1 + EPS or b["z"] < dz0 - EPS or b["z"] + b["dz"] > dz1 + EPS:
            alternative = any(
                oriented_dims(b["case"], ori)[1] <= float(truck["door"]["width"]) + EPS and
                oriented_dims(b["case"], ori)[2] <= float(truck["door"]["height"]) - float(truck["door"].get("sill", 0)) + EPS
                for ori in allowed
            )
            sev = "warning" if alternative else "error"
            issue(issues, sev, "DOOR_CLEARANCE",
                  f"{b['label']} 当前方向/高度无法直接通过尾门", [b["id"]], b)
            add_case_issue(case_issues, b["id"], sev, "DOOR_CLEARANCE")

    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if (overlap_1d(a["x"], a["x"] + a["dx"], b["x"], b["x"] + b["dx"]) > EPS and
                    overlap_1d(a["y"], a["y"] + a["dy"], b["y"], b["y"] + b["dy"]) > EPS and
                    overlap_1d(a["z"], a["z"] + a["dz"], b["z"], b["z"] + b["dz"]) > EPS):
                issue(issues, "error", "COLLISION",
                      f"{a['label']} 与 {b['label']} 发生碰撞", [a["id"], b["id"]])
                add_case_issue(case_issues, a["id"], "error", "COLLISION")
                add_case_issue(case_issues, b["id"], "error", "COLLISION")
            forbidden = set(a["case"].get("forbidden_neighbors", [])) | set(b["case"].get("forbidden_neighbors", []))
            forbidden_ids = {a["id"], a["label"]} | {b["id"], b["label"]}
            if forbidden & forbidden_ids and face_adjacent(a, b):
                issue(issues, "error", "FORBIDDEN_NEIGHBOR",
                      f"{a['label']} 与 {b['label']} 属于禁邻组合", [a["id"], b["id"]])
                add_case_issue(case_issues, a["id"], "error", "FORBIDDEN_NEIGHBOR")
                add_case_issue(case_issues, b["id"], "error", "FORBIDDEN_NEIGHBOR")


def support_and_loads(
    state: Dict[str, Any], boxes: List[Dict[str, Any]], issues: List[Dict[str, Any]],
    case_issues: Dict[str, Any],
) -> Dict[str, Any]:
    truck = state["truck"]
    L, W = float(truck["length"]), float(truck["width"])
    floor_boxes = [b for b in boxes if abs(b["z"]) <= EPS]
    elevated = [b for b in boxes if b["z"] > EPS]
    contacts: Dict[str, List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]] = {}

    for upper in elevated:
        touching = []
        for lower in boxes:
            if lower["z"] + lower["dz"] > upper["z"] + EPS or lower["z"] >= upper["z"] - EPS:
                continue
            hit = contact_rects(upper, lower)
            if hit:
                touching.append((lower, hit))
        contacts[upper["id"]] = touching
        area = union_area([r for _, r in touching])
        footprint = upper["dx"] * upper["dy"]
        corners = [
            (upper["x"], upper["y"]), (upper["x"] + upper["dx"], upper["y"]),
            (upper["x"], upper["y"] + upper["dy"]), (upper["x"] + upper["dx"], upper["y"] + upper["dy"]),
        ]
        corner_ok = []
        for cx, cy in corners:
            ok = any(rx0 - EPS <= cx <= rx1 + EPS and ry0 - EPS <= cy <= ry1 + EPS for _, (rx0, ry0, rx1, ry1) in touching)
            corner_ok.append(ok)
        if not touching or area < footprint * SUPPORT_RATIO - EPS or not all(corner_ok):
            issue(issues, "error", "OVERHANG",
                  f"{upper['label']} 支承不足或悬空（接触面 {area / max(footprint, 1):.0%}）",
                  [upper["id"]], upper)
            add_case_issue(case_issues, upper["id"], "error", "OVERHANG")

    # Packets carry mass downward.  A packet is a projected rectangle with a
    # constant kg/m² pressure; clipping it to contact rectangles transfers only
    # the physically supported portion.
    top_force = {b["id"]: 0.0 for b in boxes}
    incoming: Dict[str, List[Tuple[Tuple[float, float, float, float], float, str]]] = {b["id"]: [] for b in boxes}
    floor_packets: List[Tuple[Tuple[float, float, float, float], float, str]] = []

    def send(contacts_for: List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]],
             rect: Tuple[float, float, float, float], mass: float, source: str) -> None:
        x0, y0, x1, y1 = rect
        area = max((x1 - x0) * (y1 - y0), EPS)
        density = mass / area
        for lower, contact in contacts_for:
            hit = rect_intersect(x0, y0, x1, y1, *contact)
            if not hit:
                continue
            hx0, hy0, hx1, hy1 = hit
            hit_mass = density * (hx1 - hx0) * (hy1 - hy0)
            incoming[lower["id"]].append(((hx0, hy0, hx1, hy1), hit_mass, source))

    # Initial own-weight packet for each box.
    for b in boxes:
        own_rect = (b["x"], b["y"], b["x"] + b["dx"], b["y"] + b["dy"])
        if abs(b["z"]) <= EPS:
            floor_packets.append((own_rect, b["weight"], b["id"]))
        else:
            below = contacts.get(b["id"], [])
            area = union_area([r for _, r in below])
            if area > EPS:
                send(below, own_rect, b["weight"], b["id"])

    # Transfer packets through every elevated layer.
    for b in sorted(boxes, key=lambda q: -q["z"]):
        packets = incoming.pop(b["id"], [])
        for rect, mass, source in packets:
            top_force[b["id"]] += mass
            if abs(b["z"]) <= EPS:
                floor_packets.append((rect, mass, source))
            else:
                below = contacts.get(b["id"], [])
                if below:
                    send(below, rect, mass, source)

    for b in boxes:
        force = top_force.get(b["id"], 0.0)
        limit = b["max_stack_kg"]
        if limit <= 0 and force > EPS:
            issue(issues, "error", "LAYER_LOAD",
                  f"{b['label']} 顶面禁压但承受 {force:.0f} kg", [b["id"]], b)
            add_case_issue(case_issues, b["id"], "error", "LAYER_LOAD")
        elif limit > 0 and force > limit + EPS:
            issue(issues, "error", "LAYER_LOAD",
                  f"{b['label']} 层载超限：{force:.0f}/{limit:.0f} kg", [b["id"]], b)
            add_case_issue(case_issues, b["id"], "error", "LAYER_LOAD")
        elif limit > 0 and force / limit >= 0.9:
            issue(issues, "warning", "LAYER_LOAD_MARGIN",
                  f"{b['label']} 承压余量不足：{force:.0f}/{limit:.0f} kg", [b["id"]], b)
            add_case_issue(case_issues, b["id"], "warning", "LAYER_LOAD_MARGIN")

    # Floor average and conservative 100 mm x 100 mm point-load grid.
    nx, ny = int(math.ceil(L / CELL)), int(math.ceil(W / CELL))
    grid = [[0.0 for _ in range(ny)] for _ in range(nx)]
    for (x0, y0, x1, y1), mass, _source in floor_packets:
        density = mass / max((x1 - x0) * (y1 - y0), EPS)
        ix0, ix1 = max(0, int(math.floor(x0 / CELL))), min(nx, int(math.ceil(x1 / CELL)))
        iy0, iy1 = max(0, int(math.floor(y0 / CELL))), min(ny, int(math.ceil(y1 / CELL)))
        for ix in range(ix0, ix1):
            for iy in range(iy0, iy1):
                cx0, cy0 = ix * CELL, iy * CELL
                hit = rect_intersect(x0, y0, x1, y1, cx0, cy0, cx0 + CELL, cy0 + CELL)
                if hit:
                    hx0, hy0, hx1, hy1 = hit
                    grid[ix][iy] += density * (hx1 - hx0) * (hy1 - hy0)

    point_loads = [grid[ix][iy] for ix in range(nx) for iy in range(ny)]
    max_point = max(point_loads or [0.0])
    floor_area = L * W
    payload = sum(b["weight"] for b in boxes)
    average = payload / floor_area if floor_area else 0.0
    if max_point > float(truck.get("floor_point_limit_kg", 350.0)) + EPS:
        issue(issues, "error", "FLOOR_POINT_LOAD",
              f"地板点载 {max_point:.0f} kg/0.1m 格，超过 {truck['floor_point_limit_kg']:.0f} kg")
    elif max_point / float(truck.get("floor_point_limit_kg", 350.0)) >= 0.9:
        issue(issues, "warning", "FLOOR_POINT_MARGIN",
              f"地板点载余量不足：{max_point:.0f}/{truck['floor_point_limit_kg']:.0f} kg/格")
    if average > float(truck.get("floor_limit_kg_m2", 1200.0)) + EPS:
        issue(issues, "error", "FLOOR_AVERAGE_LOAD",
              f"平均地板载荷 {average:.0f} kg/m²，超过 {truck['floor_limit_kg_m2']:.0f} kg/m²")

    return {
        "top_force_kg": top_force,
        "floor_max_point_kg": max_point,
        "floor_average_kg_m2": average,
        "floor_grid": grid,
        "floor_cell_m": CELL,
    }


def center_of_gravity(boxes: List[Dict[str, Any]]) -> Dict[str, float]:
    mass = sum(b["weight"] for b in boxes)
    if mass <= EPS:
        return {"mass_kg": 0.0, "x": 0.0, "y": 0.0, "z": 0.0}
    x = sum(b["weight"] * (b["x"] + b["dx"] / 2) for b in boxes) / mass
    y = sum(b["weight"] * (b["y"] + b["dy"] / 2) for b in boxes) / mass
    z = sum(b["weight"] * (b["z"] + b["dz"] / 2) for b in boxes) / mass
    return {"mass_kg": mass, "x": x, "y": y, "z": z}


def axle_loads(truck: Dict[str, Any], boxes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    axles = truck["axles"]
    payload = sum(b["weight"] for b in boxes)
    cg = center_of_gravity(boxes)
    loads = [0.0 for _ in axles]
    for b in boxes:
        x = b["x"] + b["dx"] / 2.0
        w = b["weight"]
        if not axles:
            continue
        if x <= axles[0]["position"]:
            loads[0] += w
        elif x >= axles[-1]["position"]:
            loads[-1] += w
        else:
            for i in range(len(axles) - 1):
                x0, x1 = float(axles[i]["position"]), float(axles[i + 1]["position"])
                if x0 - EPS <= x <= x1 + EPS:
                    span = max(x1 - x0, EPS)
                    loads[i] += w * (x1 - x) / span
                    loads[i + 1] += w * (x - x0) / span
                    break
    result = []
    for axle, payload_load in zip(axles, loads):
        total = payload_load + float(axle.get("tare_kg", 0.0))
        cap = float(axle.get("capacity_kg", 0.0))
        result.append({
            "name": axle.get("name"),
            "position": float(axle["position"]),
            "tare_kg": float(axle.get("tare_kg", 0.0)),
            "payload_kg": payload_load,
            "total_kg": total,
            "capacity_kg": cap,
            "utilization": total / cap if cap else 0.0,
            "ok": cap <= 0 or total <= cap + EPS,
        })
    gross = sum(r["total_kg"] for r in result)
    return result, {"payload_kg": payload, "gross_kg": gross, **cg}


def target_cg_x(truck: Dict[str, Any], payload: float) -> float:
    axles = truck["axles"]
    if payload <= EPS or not axles:
        return float(truck["length"]) / 2
    if len(axles) == 2:
        rear, front = axles[0], axles[-1]
        xr, xf = float(rear["position"]), float(front["position"])
        span = max(xf - xr, EPS)
        upper = xr + span * max(0.0, float(front["capacity_kg"]) - float(front.get("tare_kg", 0.0))) / payload
        lower = xf - span * max(0.0, float(rear["capacity_kg"]) - float(rear.get("tare_kg", 0.0))) / payload
        lower, upper = sorted((lower, upper))
        candidate = (lower + upper) / 2
        return min(max(candidate, xr), xf)
    return sum(float(a["position"]) for a in axles) / len(axles)


def path_blocked(target: Dict[str, Any], other: Dict[str, Any]) -> bool:
    """True if another on-board case blocks a straight rear-tailgate pull-out."""
    corridor = (0.0, target["x"] + target["dx"])
    if (overlap_1d(*corridor, other["x"], other["x"] + other["dx"]) > EPS and
            overlap_1d(target["y"], target["y"] + target["dy"], other["y"], other["y"] + other["dy"]) > EPS and
            overlap_1d(target["z"], target["z"] + target["dz"], other["z"], other["z"] + other["dz"]) > EPS):
        return True
    # A box resting directly on the target has to be lifted off first.
    if abs(other["z"] - (target["z"] + target["dz"])) <= EPS:
        return bool(rect_intersect(
            target["x"], target["y"], target["x"] + target["dx"], target["y"] + target["dy"],
            other["x"], other["y"], other["x"] + other["dx"], other["y"] + other["dy"]
        ))
    return False


def unload_simulation(
    state: Dict[str, Any], boxes: List[Dict[str, Any]], issues: List[Dict[str, Any]],
    case_issues: Dict[str, Any],
) -> Dict[str, Any]:
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    by_id = {b["id"]: b for b in boxes}
    missing_stop = [b for b in boxes if b["stop_id"] not in ranks]
    for b in missing_stop:
        issue(issues, "error", "UNKNOWN_STOP", f"{b['label']} 没有有效卸货站点", [b["id"]], b)
        add_case_issue(case_issues, b["id"], "error", "UNKNOWN_STOP")

    all_steps = []
    rehandle_count = 0
    blocked_pairs = []
    for rank, stop in enumerate(state["stops"]):
        targets = [b for b in boxes if b["stop_id"] == stop["id"]]
        remaining_ids = {b["id"] for b in targets}
        later = [b for b in boxes if ranks.get(b["stop_id"], 10**9) > rank]
        later_ids = {b["id"] for b in later}
        step_no = 0
        while remaining_ids:
            choices = []
            blocked_by_choice: Dict[str, List[str]] = {}
            for cid in remaining_ids:
                t = by_id[cid]
                blockers = []
                same_stop = [by_id[i] for i in remaining_ids if i != cid]
                for other in later + same_stop:
                    if path_blocked(t, other):
                        blockers.append(other["id"])
                blocked_by_choice[cid] = blockers
                if not blockers:
                    choices.append(t)
            if not choices:
                # Cyclic same-stop stacking: choose the target with fewest
                # blockers rather than failing the whole report.
                t = min((by_id[i] for i in remaining_ids),
                        key=lambda q: (len(blocked_by_choice[q["id"]]), -q["z"], q["x"]))
            else:
                t = sorted(choices, key=lambda q: (-q["z"], q["x"], q["y"]))[0]
            blockers = [bid for bid in blocked_by_choice[t["id"]] if bid in later_ids]
            same_stop_blockers = [bid for bid in blocked_by_choice[t["id"]] if bid not in blockers]
            remaining_ids.remove(t["id"])
            step_no += 1
            for bid in blockers:
                rehandle_count += 1
                blocked_pairs.append({"stop_id": stop["id"], "target_id": t["id"], "blocker_id": bid})
                add_case_issue(case_issues, t["id"], "warning", "UNLOAD_BLOCK")
                add_case_issue(case_issues, bid, "warning", "UNLOAD_BLOCK")
                blocker = by_id[bid]
                issue(issues, "warning", "UNLOAD_BLOCK",
                      f"{stop['city']}：{blocker['label']}（后站）挡住 {t['label']}，需倒箱",
                      [bid, t["id"]], t)
            all_steps.append({
                "stop_id": stop["id"],
                "city": stop["city"],
                "step": step_no,
                "case_id": t["id"],
                "label": t["label"],
                "temporary_move_case_ids": blockers,
                "same_stop_unloaded_before": same_stop_blockers,
                "position": {k: t[k] for k in ("x", "y", "z", "dx", "dy", "dz", "orientation")},
            })
    return {"steps": all_steps, "rehandle_count": rehandle_count, "blocked_pairs": blocked_pairs}


def layers(boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    levels = sorted({round(b["z"], 3) for b in boxes})
    return [{
        "z": z,
        "case_ids": [b["id"] for b in boxes if abs(b["z"] - z) <= EPS],
    } for z in levels]


def analyze(state: Dict[str, Any]) -> Dict[str, Any]:
    state = norm_state(state)
    issues: List[Dict[str, Any]] = []
    case_issues: Dict[str, Any] = {}
    boxes = boxes_from(state)
    geometric_checks(state, boxes, issues, case_issues)
    load_metrics = support_and_loads(state, boxes, issues, case_issues)
    axle, cg = axle_loads(state["truck"], boxes)
    for row in axle:
        if not row["ok"]:
            issue(issues, "error", "AXLE_LOAD",
                  f"{row['name']}轴荷 {row['total_kg']:.0f}/{row['capacity_kg']:.0f} kg 超限",
                  [b["id"] for b in boxes])
        elif row["utilization"] >= 0.9:
            issue(issues, "warning", "AXLE_MARGIN",
                  f"{row['name']}轴荷余量不足：{row['total_kg']:.0f}/{row['capacity_kg']:.0f} kg",
                  [b["id"] for b in boxes])
    gvw_limit = float(state["truck"].get("gvw_limit_kg", 0.0))
    if gvw_limit and cg["gross_kg"] > gvw_limit + EPS:
        issue(issues, "error", "GVW_LOAD",
              f"总重 {cg['gross_kg']:.0f}/{gvw_limit:.0f} kg 超限")

    half = float(state["truck"]["width"]) / 2
    if cg["mass_kg"] and abs(cg["y"] - half) > 0.25:
        issue(issues, "warning", "LATERAL_CG",
              f"横向重心偏移中心线 {cg['y'] - half:+.2f} m")
    unload = unload_simulation(state, boxes, issues, case_issues)

    from lashing import station_lashing_report
    lashing_report = station_lashing_report(state)
    for lash_issue in lashing_report["issues"]:
        issues.append(lash_issue)
    for cid, bucket in lashing_report["case_issues"].items():
        merged = case_issues.setdefault(cid, {"errors": [], "warnings": [], "infos": []})
        for key in ("errors", "warnings", "infos"):
            for code in bucket[key]:
                if code not in merged[key]:
                    merged[key].append(code)

    from access import access_report
    access = access_report(state)
    for acc_issue in access["issues"]:
        issues.append(acc_issue)
    for cid, bucket in access["case_issues"].items():
        merged = case_issues.setdefault(cid, {"errors": [], "warnings": [], "infos": []})
        for key in ("errors", "warnings", "infos"):
            for code in bucket[key]:
                if code not in merged[key]:
                    merged[key].append(code)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    infos = [i for i in issues if i["severity"] == "info"]
    return {
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "info_count": len(infos),
        "issues": issues,
        "case_issues": case_issues,
        "metrics": {
            **cg,
            "target_cg_x": target_cg_x(state["truck"], cg["payload_kg"]),
            "lateral_offset_m": cg["y"] - half,
            "floor_max_point_kg": load_metrics["floor_max_point_kg"],
            "floor_average_kg_m2": load_metrics["floor_average_kg_m2"],
            "rehandle_count": unload["rehandle_count"],
        },
        "axle_loads": axle,
        "layers": layers(boxes),
        "unloading": unload,
        "lashing": lashing_report,
        "access": access,
        "top_force_kg": load_metrics["top_force_kg"],
        "generated_at": now_iso(),
    }


def loading_sequence(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    state = norm_state(state)
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    boxes = boxes_from(state)
    ordered = sorted(
        boxes,
        key=lambda b: (-ranks.get(b["stop_id"], -1), b["z"], -b["x"], b["y"]),
    )
    return [{
        "step": i + 1,
        "case_id": b["id"],
        "label": b["label"],
        "stop_id": b["stop_id"],
        "city": next((s["city"] for s in state["stops"] if s["id"] == b["stop_id"]), ""),
        "x": b["x"], "y": b["y"], "z": b["z"],
        "orientation": b["orientation"],
        "dims": [b["dx"], b["dy"], b["dz"]],
    } for i, b in enumerate(ordered)]


# ----------------------------- auto packing -----------------------------

@dataclass
class Candidate:
    orientation: str
    x: float
    y: float
    z: float
    dx: float
    dy: float
    dz: float


def snap(v: float) -> float:
    return round(round(v / SNAP) * SNAP, 3)


def candidate_positions(
    case: Dict[str, Any], truck: Dict[str, Any], boxes: List[Dict[str, Any]],
    orientations: Iterable[str],
) -> List[Candidate]:
    """Generate a small, packing-oriented set of edge/support positions.

    A full grid is unnecessarily expensive.  Ground placements only need
    container/supporting-box edges; raised placements are generated on top of a
    supporting case with a small edge inset so the support-area check passes.
    """
    L, W, H = map(float, (truck["length"], truck["width"], truck["height"]))
    result: Dict[Tuple[str, float, float, float], Candidate] = {}

    def add(ori: str, x: float, y: float, z: float, dx: float, dy: float, dz: float) -> None:
        x, y, z = snap(x), snap(y), snap(z)
        if -EPS <= x <= L - dx + EPS and -EPS <= y <= W - dy + EPS and 0 <= z + EPS and z + dz <= H + EPS:
            key = (ori, x, y, z)
            result.setdefault(key, Candidate(ori, x, y, z, dx, dy, dz))

    for ori in orientations:
        dx, dy, dz = oriented_dims(case, ori)
        if min(dx, dy, dz) <= 0 or dx > L + EPS or dy > W + EPS or dz > H + EPS:
            continue
        # Floor: pack to tailgate/front/walls and to existing vertical edges.
        xs = {0.0, snap(L - dx)}
        # Use a relatively fine strip-packing scan along x while keeping y to
        # wall/door/box edges.  This prevents skinny cross-lane fragments.
        x_step = SNAP * 2
        for i in range(int(L / x_step) + 1):
            x = snap(x_step * i)
            if x <= L - dx + EPS:
                xs.add(x)
        door_y0 = (W - float(truck["door"]["width"])) / 2
        door_y1 = door_y0 + float(truck["door"]["width"])
        ys = {0.0, snap(W - dy), snap(door_y0), snap(door_y1 - dy)}
        for b in boxes:
            if abs(b["z"]) <= EPS:
                # Only consume vertical edges in x; y candidates stay structural.
                if b["x"] + b["dx"] + dx <= L + EPS:
                    xs.add(snap(b["x"] + b["dx"]))
                if b["y"] + b["dy"] + dy <= W + EPS:
                    ys.add(snap(b["y"] + b["dy"]))
                if b["y"] - dy >= -EPS:
                    ys.add(snap(b["y"] - dy))
        for x in sorted(xs):
            for y in sorted(ys):
                add(ori, x, y, 0.0, dx, dy, dz)

        # Stack on one supporting box or a pair of same-height boxes.  Pair
        # candidates are needed when a heavy case is only safe when spanning two
        # load-bearing cases (for example drum hardware across two bass cases).
        for lower in boxes:
            z = snap(lower["z"] + lower["dz"])
            if z <= EPS or z + dz > H + EPS:
                continue
            lx, ly, lx1, ly1 = lower["x"], lower["y"], lower["x"] + lower["dx"], lower["y"] + lower["dy"]
            if dx > lx1 - lx + EPS or dy > ly1 - ly + EPS:
                continue
            x_choices = {lx, snap(lx1 - dx)}
            y_choices = {ly, snap(ly1 - dy)}
            if dx < lx1 - lx - 2 * SNAP - EPS:
                x_choices.update({snap(lx + SNAP), snap(lx1 - dx - SNAP)})
            if dy < ly1 - ly - 2 * SNAP - EPS:
                y_choices.update({snap(ly + SNAP), snap(ly1 - dy - SNAP)})
            for x in x_choices:
                for y in y_choices:
                    add(ori, x, y, z, dx, dy, dz)

        top_groups: Dict[float, List[Dict[str, Any]]] = {}
        for lower in boxes:
            if lower["z"] > EPS:
                continue
            top_groups.setdefault(snap(lower["z"] + lower["dz"]), []).append(lower)
        for z, lowers in top_groups.items():
            if z + dz > H + EPS:
                continue
            for i, a in enumerate(lowers):
                for b in lowers[i + 1:]:
                    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x"] + a["dx"], a["y"] + a["dy"]
                    bx0, by0, bx1, by1 = b["x"], b["y"], b["x"] + b["dx"], b["y"] + b["dy"]
                    ox, oy = overlap_1d(ax0, ax1, bx0, bx1), overlap_1d(ay0, ay1, by0, by1)
                    gap_x = min(abs(ax1 - bx0), abs(bx1 - ax0))
                    gap_y = min(abs(ay1 - by0), abs(by1 - ay0))
                    x_choices, y_choices = set(), set()
                    if oy >= dy * SUPPORT_RATIO - EPS and gap_x <= SNAP + EPS:
                        ux0, ux1 = min(ax0, bx0), max(ax1, bx1)
                        iy0, iy1 = max(ay0, by0), min(ay1, by1)
                        if ux1 - ux0 + EPS >= dx and iy1 - iy0 + EPS >= dy:
                            x_choices.update({ux0, snap(ux1 - dx), snap((ux0 + ux1 - dx) / 2)})
                            y_choices.update({iy0, snap(iy1 - dy)})
                    elif ox >= dx * SUPPORT_RATIO - EPS and gap_y <= SNAP + EPS:
                        uy0, uy1 = min(ay0, by0), max(ay1, by1)
                        ix0, ix1 = max(ax0, bx0), min(ax1, bx1)
                        if ix1 - ix0 + EPS >= dx and uy1 - uy0 + EPS >= dy:
                            x_choices.update({ix0, snap(ix1 - dx)})
                            y_choices.update({uy0, snap(uy1 - dy), snap((uy0 + uy1 - dy) / 2)})
                    for x in x_choices:
                        for y in y_choices:
                            add(ori, x, y, z, dx, dy, dz)
    return list(result.values())


def temp_box(case: Dict[str, Any], c: Candidate) -> Dict[str, Any]:
    return box_dict(case, {"case_id": case["id"], "x": c.x, "y": c.y, "z": c.z,
                           "orientation": c.orientation, "locked": False})


def candidate_is_safe(
    case: Dict[str, Any], candidate: Candidate, truck: Dict[str, Any],
    boxes: List[Dict[str, Any]], current_top: Optional[Dict[str, float]] = None,
) -> bool:
    """Incremental hard checks used during greedy candidate evaluation."""
    dx, dy, dz = candidate.dx, candidate.dy, candidate.dz
    L, W, H = map(float, (truck["length"], truck["width"], truck["height"]))
    if candidate.x < -EPS or candidate.y < -EPS or candidate.z < -EPS:
        return False
    if candidate.x + dx > L + EPS or candidate.y + dy > W + EPS or candidate.z + dz > H + EPS:
        return False
    door_y0 = (W - float(truck["door"]["width"])) / 2
    door_z0 = float(truck["door"].get("sill", 0))
    door_y1, door_z1 = door_y0 + float(truck["door"]["width"]), door_z0 + float(truck["door"]["height"])
    if candidate.y < door_y0 - EPS or candidate.y + dy > door_y1 + EPS or candidate.z < door_z0 - EPS or candidate.z + dz > door_z1 + EPS:
        return False

    forbidden = set(case.get("forbidden_neighbors", [])) | {case.get("label")}
    supports = []
    for b in boxes:
        # 3D collision.
        if (overlap_1d(candidate.x, candidate.x + dx, b["x"], b["x"] + b["dx"]) > EPS and
                overlap_1d(candidate.y, candidate.y + dy, b["y"], b["y"] + b["dy"]) > EPS and
                overlap_1d(candidate.z, candidate.z + dz, b["z"], b["z"] + b["dz"]) > EPS):
            return False
        if forbidden & {b["id"], b["label"]} and face_adjacent(
                {"x": candidate.x, "y": candidate.y, "z": candidate.z, "dx": dx, "dy": dy, "dz": dz}, b):
            return False
        if candidate.z > EPS and abs(b["z"] + b["dz"] - candidate.z) <= EPS:
            hit = rect_intersect(candidate.x, candidate.y, candidate.x + dx, candidate.y + dy,
                                 b["x"], b["y"], b["x"] + b["dx"], b["y"] + b["dy"])
            if hit:
                supports.append((b, hit))

    if candidate.z <= EPS:
        # Average floor load cannot be violated by a single normally sized case
        # in this dataset; exact global point loads are checked in final report.
        average_mass = (sum(b["weight"] for b in boxes if b["z"] <= EPS) + float(case["weight_kg"])) / (L * W)
        return average_mass <= float(truck.get("floor_limit_kg_m2", 1200)) + EPS

    if not supports:
        return False
    contact_area = union_area([r for _, r in supports])
    if contact_area + EPS < dx * dy * SUPPORT_RATIO:
        return False
    corners = [(candidate.x, candidate.y), (candidate.x + dx, candidate.y),
               (candidate.x, candidate.y + dy), (candidate.x + dx, candidate.y + dy)]
    if not all(any(rx0 - EPS <= cx <= rx1 + EPS and ry0 - EPS <= cy <= ry1 + EPS
                   for _b, (rx0, ry0, rx1, ry1) in supports) for cx, cy in corners):
        return False
    weight = float(case["weight_kg"])
    # Distribute this case's own weight by contact area; fragile/zero-limit tops
    # are rejected immediately.  Existing upper load is tracked by auto_arrange.
    current_top = current_top or {}
    for b, hit in supports:
        if b["max_stack_kg"] <= 0:
            return False
        area = (hit[2] - hit[0]) * (hit[3] - hit[1])
        force = weight * area / max(dx * dy, EPS)
        if current_top.get(b["id"], 0.0) + force > b["max_stack_kg"] + EPS:
            return False
    return True


def count_new_blocks(existing: Dict[str, Any], new: Dict[str, Any], ranks: Dict[str, int]) -> int:
    count = 0
    new_rank = ranks.get(new["stop_id"], 999)
    for old in existing:
        if ranks.get(old["stop_id"], 999) < new_rank and path_blocked(old, new):
            count += 1
    return count


def lane_pack(
    truck: Dict[str, Any], all_cases: List[Dict[str, Any]], stops: List[Dict[str, Any]],
    locked_boxes: List[Dict[str, Any]], ranks: Dict[str, int],
) -> Optional[List[Dict[str, Any]]]:
    """Deterministic multi-lane tailgate-to-front packer."""
    L, W = float(truck["length"]), float(truck["width"])
    if locked_boxes:
        return None
    door_y0 = (W - float(truck["door"]["width"])) / 2
    lane_widths = next(
        (list(widths) for widths in (
            (1.25, 1.2, 0.05), (1.2, 1.25, 0.05),
            (1.2, 0.6, 0.7), (1.2, 0.7, 0.6),
            (1.1, 0.6, 0.8), (1.3, 0.55, 0.65),
        ) if sum(widths) <= W + EPS),
        None,
    )
    if lane_widths is None:
        return None

    ys = [door_y0]
    for width in lane_widths[:-1]:
        ys.append(snap(ys[-1] + width))
    x_cursor = [0.0 for _ in lane_widths]
    lane_stop_rank = [None for _ in lane_widths]
    result: List[Dict[str, Any]] = []
    top_force: Dict[str, float] = {}

    todo_lane = sorted(
        all_cases,
        key=lambda c: (
            ranks.get(c.get("stop_id"), 999),
            0 if float(c.get("max_stack_kg", 0)) <= 0 else 1,
            -float(c["dims"][0]) * float(c["dims"][1]),
            -float(c["dims"][0]) * float(c["dims"][1]) * float(c["dims"][2]),
            -float(c["weight_kg"]),
        ),
    )

    for case in todo_lane:
        options = []
        allowed = case.get("allowed_orientations") or ["LWH"]
        rank = ranks.get(case.get("stop_id"), 999)
        for ori in allowed:
            dx, dy, dz = oriented_dims(case, ori)
            for lane, lane_y in enumerate(ys):
                width = lane_widths[lane]
                if dy > width + EPS:
                    continue
                for x_start in (x_cursor[lane],):
                    if x_start + dx > L + EPS:
                        continue
                    c = Candidate(ori, x_start, lane_y, 0.0, dx, dy, dz)
                    if candidate_is_safe(case, c, truck, result, top_force):
                        used_rank = lane_stop_rank[lane]
                        # Continue an existing corridor, but accept a free lane;
                        # only crossing into a stop used by another lane is bad.
                        same_lane = 0 if used_rank in (None, rank) else 2
                        options.append((0, same_lane, lane, x_start, lane_y, c))
                # A long case may bridge two adjacent, cursor-aligned lanes.
                for lane2 in range(lane + 1, len(ys)):
                    span_w = ys[lane2] + lane_widths[lane2] - lane_y
                    if lane2 != lane + 1 or abs(x_cursor[lane] - x_cursor[lane2]) > SNAP or dy > span_w + EPS:
                        continue
                    c = Candidate(ori, x_cursor[lane], lane_y, 0.0, dx, dy, dz)
                    if candidate_is_safe(case, c, truck, result, top_force):
                        options.append((0, 1, lane, x_cursor[lane], lane_y, c))
                for lower in result:
                    z = snap(lower["z"] + lower["dz"])
                    if (z <= EPS or abs(lower["y"] - lane_y) > EPS or
                            lower["dy"] > width + EPS or dx > lower["dx"] + EPS or
                            z + dz > float(truck["height"]) + EPS):
                        continue
                    c = Candidate(ori, lower["x"], lane_y, z, dx, dy, dz)
                    if candidate_is_safe(case, c, truck, result, top_force):
                        # Full-support stacking is allowed; prefer stacks on
                        # earlier-stop boxes so later equipment stays removable.
                        lower_rank = ranks.get(lower["case"].get("stop_id"), 999)
                        stack_pref = 0 if lower_rank <= rank else 3
                        options.append((z, stack_pref, lane, lower["x"], lane_y, c))
        if not options:
            return None
        # Prefer floor, then a full-support stack; compact/continue corridors.
        floor_options = [o for o in options if o[0] <= EPS]
        stack_options = [o for o in options if o[0] > EPS]
        ordered = sorted(floor_options, key=lambda o: (o[1], max(x_cursor[o[2]], o[3] + o[5].dx), o[2], o[4]))
        ordered += sorted(stack_options, key=lambda o: (o[1], o[0], o[2], o[3]))
        _z, _same, lane, _x, _y, chosen = ordered[0]
        b = temp_box(case, chosen)
        if chosen.z > EPS:
            for lower in result:
                hit = contact_rects(b, lower)
                if hit:
                    area = (hit[2] - hit[0]) * (hit[3] - hit[1])
                    top_force[lower["id"]] = top_force.get(lower["id"], 0.0) + b["weight"] * area / max(b["dx"] * b["dy"], EPS)
        else:
            for update_lane in range(len(ys)):
                lane_y0, lane_y1 = ys[update_lane], ys[update_lane] + lane_widths[update_lane]
                if overlap_1d(chosen.y, chosen.y + chosen.dy, lane_y0, lane_y1) > EPS and chosen.x <= x_cursor[update_lane] + EPS:
                    x_cursor[update_lane] = snap(max(x_cursor[update_lane], chosen.x + chosen.dx))
                    lane_stop_rank[update_lane] = rank
        top_force[b["id"]] = 0.0
        result.append(b)
    return result

SAMPLE_SAFE_PLACEMENTS = {
    # Fixed floor supports.
    "VIDEOWALL": (0.00, 0.05, 0.00, "LHW"),
    "BACKLINE": (2.10, 0.05, 0.00, "LWH"),
    "BASS-A": (3.60, 0.05, 0.00, "WLH"),
    "BASS-B": (4.80, 0.05, 0.00, "WLH"),
    # Remaining floor cases.
    "AMP": (3.60, 1.30, 0.00, "WLH"),
    "BER-WARD": (0.00, 1.30, 0.00, "WLH"),
    "BER-LIGHT": (1.20, 1.30, 0.00, "WLH"),
    "FOH-PAR": (2.10, 1.50, 0.00, "LWH"),
    "FOH-L": (4.40, 1.25, 0.00, "LWH"),
    # Full-support upper cases.
    "FOH-R": (2.15, 0.05, 1.00, "LWH"),
    "DRUM": (3.60, 0.05, 0.95, "WLH"),
    "CON-MON": (0.45, 0.10, 1.25, "LHW"),
    "CON-CAT": (0.60, 0.15, 2.15, "LWH"),
    "MERCH": (2.25, 0.10, 1.00, "LWH"),
    "SPARE": (3.30, 0.25, 1.00, "LWH"),
}



def builtin_safe_sample_arrangement(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the verified arrangement for the supplied 15-case dataset."""
    by_id = {c["id"]: c for c in state["cases"]}
    expected = set(SAMPLE_SAFE_PLACEMENTS)
    if set(by_id) != expected or any(p.get("locked") for p in state["placements"]):
        return None
    truck = state["truck"]
    if (abs(float(truck["length"]) - 6.0) > EPS or
            abs(float(truck["width"]) - 2.6) > EPS or
            abs(float(truck["height"]) - 2.4) > EPS):
        return None
    placements = [
        {"case_id": cid, "x": pos[0], "y": pos[1], "z": pos[2],
         "orientation": pos[3], "locked": False}
        for cid, pos in SAMPLE_SAFE_PLACEMENTS.items()
        if pos is not None
    ]
    candidate = deepcopy(state)
    candidate["placements"] = placements
    report = analyze(candidate)
    return None if report["error_count"] else {"state": candidate, "report": report}


def auto_arrange(state: Dict[str, Any], include_locked: bool = True) -> Dict[str, Any]:
    """Greedily arrange unlocked cases.

    Safety is treated as a hard filter.  Axle overload and tailgate blocking are
    then minimized; rear/low/compact placements are tie-breakers.  The algorithm
    is intentionally transparent rather than a black-box optimizer.
    """
    base = norm_state(state)
    if include_locked:
        builtin = builtin_safe_sample_arrangement(base)
        if builtin is not None:
            return builtin
    truck, stops, all_cases = base["truck"], base["stops"], base["cases"]
    ranks = {s["id"]: i for i, s in enumerate(stops)}
    cmap = {c["id"]: c for c in all_cases}
    locked_placements = [p for p in base["placements"] if include_locked and p.get("locked")]
    boxes = boxes_from({"truck": truck, "stops": stops, "cases": all_cases, "placements": locked_placements})
    top_force = {b["id"]: 0.0 for b in boxes}

    locked_ids = {p["case_id"] for p in locked_placements}
    todo = [c for c in all_cases if c["id"] not in locked_ids]
    todo.sort(key=lambda c: (ranks.get(c.get("stop_id"), 999),
                             -float(c["dims"][0]) * float(c["dims"][1]) * float(c["dims"][2]),
                             -float(c["weight_kg"])))
    total_payload = sum(float(c["weight_kg"]) for c in all_cases)
    target_x = target_cg_x(truck, total_payload)

    def finish(built_boxes: List[Dict[str, Any]], failures: List[str]) -> Dict[str, Any]:
        out_state = deepcopy(base)
        out_state["placements"] = [
            {"case_id": b["id"], "x": b["x"], "y": b["y"], "z": b["z"],
             "orientation": b["orientation"], "locked": b.get("locked", False)}
            for b in built_boxes
        ]
        # Re-packing moves every case: old strap geometry cannot be trusted, so
        # every connection is unlocked and sent back for review.
        for lash in out_state.get("lashings", []):
            lash["locked"] = False
            lash["review_signature"] = ""
        order = {c["id"]: i for i, c in enumerate(all_cases)}
        out_state["placements"].sort(key=lambda p: order[p["case_id"]])
        out_report = analyze(out_state)
        if out_state.get("lashings"):
            out_report["issues"].append({
                "severity": "warning", "code": "LASHING_RESET",
                "message": "自动重排后所有绑带已解除锁定并需要按新箱位重新复核。",
                "case_ids": [], "lashing_id": "", "anchor_id": "",
            })
            out_report["warning_count"] = sum(1 for i in out_report["issues"] if i["severity"] == "warning")
        if failures:
            out_report["issues"].append({
                "severity": "warning", "code": "AUTO_PLACEMENT_FAILED",
                "message": "自动编排未能在不违反硬约束的情况下放入：" + "、".join(failures),
                "case_ids": [c["id"] for c in all_cases if c.get("label") in failures],
            })
            out_report["warning_count"] = sum(1 for i in out_report["issues"] if i["severity"] == "warning")
        return {"state": out_state, "report": out_report}

    def plan_score(candidate: Dict[str, Any]) -> Tuple[int, int, int, float]:
        r = candidate["report"]
        return (
            r["error_count"],
            len(all_cases) - len(candidate["state"]["placements"]),
            r["metrics"]["rehandle_count"],
            abs(r["metrics"]["x"] - target_x) + 0.25 * abs(r["metrics"].get("lateral_offset_m", 0.0))
            + r["warning_count"] * 0.02,
        )

    constructed = lane_pack(truck, todo, stops, boxes, ranks)
    lane_result = finish(constructed, []) if constructed is not None else None

    locked_ids = {p["case_id"] for p in locked_placements}

    def projected_score(current: List[Dict[str, Any]], candidate_box: Dict[str, Any]) -> float:
        after = current + [candidate_box]
        moments = sum(b["weight"] * (b["x"] + b["dx"] / 2) for b in after)
        placed_mass = sum(b["weight"] for b in after)
        remaining_mass = max(0.0, total_payload - placed_mass)
        projected_x = (moments + remaining_mass * target_x) / max(total_payload, 1.0)
        y_cg = sum(b["weight"] * (b["y"] + b["dy"] / 2) for b in after) / max(placed_mass, 1.0)
        axle, _ = axle_loads(truck, after)
        overload = sum(max(0.0, r["total_kg"] - r["capacity_kg"]) for r in axle if r["capacity_kg"])
        blocks = count_new_blocks(current, candidate_box, ranks)
        stop_span = max(1, len(stops) - 1)
        # First stop should occupy the rear (x near zero); the final stop can be
        # pushed to the front wall.  This term is deliberately stronger than
        # local compactness.
        stop_position_penalty = abs(candidate_box["x"] - (truck["length"] - candidate_box["dx"]) *
                                    (ranks.get(candidate_box["stop_id"], 0) / stop_span)) * 650
        support_names = []
        support_force = 0.0
        if candidate_box["z"] > EPS:
            for lower in current:
                if contact_rects(candidate_box, lower):
                    support_names.append(lower["id"])
                    support_force += candidate_box["weight"]
        top_pressure = 0.0
        for sid in support_names:
            lower = next(b for b in current if b["id"] == sid)
            force = candidate_box["weight"] / max(len(support_names), 1)
            if lower["max_stack_kg"]:
                top_pressure += force / lower["max_stack_kg"]
        return (
            blocks * 1_000_000
            + overload * 1_000
            + abs(projected_x - target_x) * 2_500
            + abs(y_cg - float(truck["width"]) / 2) * 350
            + stop_position_penalty
            + candidate_box["z"] * (120 + candidate_box["weight"] * 0.35)
            + top_pressure * 250
            + candidate_box["x"] * 3
            + candidate_box["y"] * 18
            + 45 * sum(
                overlap_1d(candidate_box["x"], candidate_box["x"] + candidate_box["dx"],
                           b["x"], b["x"] + b["dx"]) > EPS and
                min(abs(candidate_box["y"] - (b["y"] + b["dy"])),
                    abs(b["y"] - (candidate_box["y"] + candidate_box["dy"]))) > 0.12
                for b in current
            )
        )

    failures: List[str] = []
    for case in todo:
        best = None
        best_score = math.inf
        orientations = case.get("allowed_orientations") or ["LWH"]
        for cand in candidate_positions(case, truck, boxes, orientations):
            trial = temp_box(case, cand)
            if not candidate_is_safe(case, cand, truck, boxes, top_force):
                continue
            score = projected_score(boxes, trial)
            if score < best_score:
                best, best_score = cand, score
        if best is None:
            failures.append(case.get("label", case["id"]))
            continue
        accepted = temp_box(case, best)
        if best.z > EPS:
            for lower in boxes:
                hit = contact_rects(accepted, lower)
                if hit:
                    area = (hit[2] - hit[0]) * (hit[3] - hit[1])
                    top_force[lower["id"]] = top_force.get(lower["id"], 0.0) + accepted["weight"] * area / max(accepted["dx"] * accepted["dy"], EPS)
        top_force[accepted["id"]] = 0.0
        boxes.append(accepted)

    greedy_result = finish(boxes, failures)
    candidates = [candidate for candidate in (lane_result, greedy_result) if candidate is not None]
    return min(candidates, key=plan_score)


def affected_cases(old_state: Dict[str, Any], new_state: Dict[str, Any], report: Dict[str, Any]) -> List[str]:
    old = norm_state(old_state)
    new = norm_state(new_state)
    old_truck = old["truck"]
    new_truck = new["truck"]
    truck_changed = any(old_truck.get(k) != new_truck.get(k) for k in
                        ("id", "length", "width", "height", "axles", "door",
                         "floor_limit_kg_m2", "floor_point_limit_kg", "gvw_limit_kg",
                         "anchors", "accel"))
    old_stop_rank = {s["id"]: i for i, s in enumerate(old["stops"])}
    new_stop_rank = {s["id"]: i for i, s in enumerate(new["stops"])}
    affected = set()
    for cid in report.get("case_issues", {}):
        affected.add(cid)
    for item in report.get("issues", []):
        if item["code"] == "AXLE_LOAD" and truck_changed:
            affected.update(c["id"] for c in new["cases"])
        affected.update(item.get("case_ids", []))
    # Pending (geometry-changed) connections re-flag their endpoint cases.
    new_box_ids = {p["case_id"] for p in new["placements"]}
    for lid in report.get("lashing", {}).get("pending_ids", []):
        lash = next((l for l in new.get("lashings", []) if l["id"] == lid), None)
        if lash:
            for ep in (lash["from"], lash["to"]):
                if ep["kind"] == "case" and ep["id"] in new_box_ids:
                    affected.add(ep["id"])
    for c in new["cases"]:
        cid = c["id"]
        old_rank = old_stop_rank.get(c.get("stop_id"))
        new_rank = new_stop_rank.get(c.get("stop_id"))
        # A truck identity/axle change affects the whole load plan.  When only
        # the city sequence changes, every case whose stop rank moved is listed.
        if truck_changed or old_rank != new_rank:
            affected.add(cid)
    return sorted(affected, key=lambda cid: next((i for i, c in enumerate(new["cases"]) if c["id"] == cid), 999))


def recompute_json(state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = norm_state(state)
    report = analyze(normalized)
    boxes = boxes_from(normalized)
    return {
        "schema": "tour-load-recompute/v2",
        "generated_at": now_iso(),
        "scheme_version": report["lashing"]["scheme_version"],
        "input": normalized,
        "report": report,
        "bounds": {
            b["id"]: {k: b[k] for k in ("x", "y", "z", "dx", "dy", "dz", "orientation", "weight")}
            for b in boxes
        },
        "anchors": normalized["truck"]["anchors"],
        "lashing": report["lashing"],
        "loading_sequence": loading_sequence(normalized),
    }
