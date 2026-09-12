"""分站落地通道复核（venue access recheck）。

纯几何/力学模块，不依赖 Flask，规则可独立测试，约定与 ``planning`` 一致。

航空箱在卡车里能沿直线拉到尾门，到了场馆装卸口仍可能卡住：本模块按各站
卸货步骤逐箱模拟 平移 / 转向 / 坡道通行 / 尾板趟次，检查：

* 扫掠包络碰撞（路径上箱体四角扫过障碍物或场馆边界）；
* 净宽 / 净高不足（尾板或坡道与装卸口）；
* 坡度越限（坡道坡度超过登记上限）；
* 门槛干涉（门槛高过脚轮通过能力）；
* 平台偏载（尾板平台重心偏移超限）；
* 并行占用（一趟多箱超过通道通行位，需拆成多趟）；
* 人员能力不足（等效推行重量超过登记人数 × 单人上限）。

场馆平面坐标（每站独立）：x 向右、y 向下，单位米；车尾（尾门中心）登记在
``access.venue.dock``，路径为由车尾到暂存区的折线。

确认锁定：每个通过步骤的结果签名记录在 ``access.confirmed_steps``；箱位、
站序或通道参数变化只让结果真正变化的步骤回到“待复核”，其余确认保持锁定。
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lashing import fnv1a32
from planning import (
    CASTER_MODES,
    EPS,
    boxes_from,
    norm_state,
    now_iso,
    unload_simulation,
)

SWEEP_DS = 0.08              # 扫掠采样步长 m
SWEEP_DISPLAY_STEP = 4       # 报告中每 N 个采样保留 1 个（SVG 用）
SLOPE_EFFORT_REF_PCT = 8.0   # 8% 坡推行等效重量翻倍
TURN_ZONE_OVERHANG = 0.6     # 转向区允许向车尾方向伸出的余量 m

KIND_CN = {"lift": "尾板", "ramp": "坡道"}

ACCESS_ISSUE_CN = {
    "ACCESS_CAPACITY": "承载不足",
    "ACCESS_CLEAR_WIDTH": "净宽不足",
    "ACCESS_CLEAR_HEIGHT": "净高不足",
    "ACCESS_SLOPE": "坡度越限",
    "ACCESS_THRESHOLD": "门槛干涉",
    "ACCESS_CREW": "人员能力不足",
    "ACCESS_PARALLEL": "并行占用",
    "ACCESS_ECCENTRIC": "平台偏载",
    "ACCESS_SWEPT": "扫掠包络碰撞",
    "ACCESS_TURN_RADIUS": "转弯净空不足",
    "ACCESS_TURN_ZONE": "转向区不足",
    "ACCESS_PATH_MISSING": "未登记路径",
    "ACCESS_STAGING": "未达暂存区",
}


# ----------------------------- basic geometry -----------------------------

def suggested_path(access: Dict[str, Any]) -> List[Dict[str, float]]:
    """Default L-shaped route dock -> staging centre (used until user draws)."""
    v = access["venue"]
    dock, staging = v["dock"], v["staging"]
    cx, cy = staging["x"] + staging["dx"] / 2, staging["y"] + staging["dy"] / 2
    return [{"x": dock["x"], "y": dock["y"]},
            {"x": cx, "y": dock["y"]},
            {"x": cx, "y": cy}]


def _rect_corners(cx: float, cy: float, theta: float, hl: float, hw: float
                  ) -> List[Tuple[float, float]]:
    ca, sa = math.cos(theta), math.sin(theta)
    return [(cx + ca * hl * sx - sa * hw * sy, cy + sa * hl * sx + ca * hw * sy)
            for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1))]


def swept_poses(path: Sequence[Dict[str, float]], ds: float = SWEEP_DS
                ) -> List[Tuple[float, float, float, float]]:
    """(x, y, theta, dist) samples along the polyline, rotating at vertices."""
    poses: List[Tuple[float, float, float, float]] = []
    dist = 0.0
    n = len(path)
    for i in range(n - 1):
        x0, y0 = float(path[i]["x"]), float(path[i]["y"])
        x1, y1 = float(path[i + 1]["x"]), float(path[i + 1]["y"])
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= EPS:
            continue
        theta = math.atan2(y1 - y0, x1 - x0)
        steps = max(1, int(math.ceil(seg / ds)))
        for k in range(steps):
            t = k / steps
            poses.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, theta, dist + seg * t))
        dist += seg
        if i + 2 < n:
            x2, y2 = float(path[i + 2]["x"]), float(path[i + 2]["y"])
            theta2 = math.atan2(y2 - y1, x2 - x1)
            turn = (theta2 - theta + math.pi) % (2 * math.pi) - math.pi
            rsteps = max(2, int(math.ceil(abs(turn) / (math.pi / 18))))
            for k in range(1, rsteps + 1):
                poses.append((x1, y1, theta + turn * k / rsteps, dist))
        else:
            poses.append((x1, y1, theta, dist))
    return poses


def _point_in_rect(px: float, py: float, r: Dict[str, float]) -> bool:
    return (r["x"] - EPS <= px <= r["x"] + r["dx"] + EPS and
            r["y"] - EPS <= py <= r["y"] + r["dy"] + EPS)


def _dist_point_rect(px: float, py: float, r: Dict[str, float]) -> float:
    dx = max(r["x"] - px, 0.0, px - (r["x"] + r["dx"]))
    dy = max(r["y"] - py, 0.0, py - (r["y"] + r["dy"]))
    return math.hypot(dx, dy)


def _sweep_polygon(poses: List[Tuple[float, float, float, float]], hw: float
                   ) -> List[List[float]]:
    """Corridor outline for SVG: left edge forward, right edge reversed."""
    if not poses:
        return []
    step = max(1, SWEEP_DISPLAY_STEP)
    sampled = poses[::step]
    if poses[-1] not in sampled:
        sampled.append(poses[-1])
    left, right = [], []
    for x, y, theta, _d in sampled:
        nx, ny = -math.sin(theta), math.cos(theta)
        left.append((x + nx * hw, y + ny * hw))
        right.append((x - nx * hw, y - ny * hw))
    poly = left + list(reversed(right))
    return [[round(px, 2), round(py, 2)] for px, py in poly]


# ----------------------------- ordering / trips -----------------------------

def stop_orders(state: Dict[str, Any]) -> Dict[str, List[str]]:
    """Unload order of placed cases per stop (from the truck-side simulation)."""
    sim = unload_simulation(state, boxes_from(state), [], {})
    orders: Dict[str, List[str]] = {}
    for step in sim["steps"]:
        orders.setdefault(step["stop_id"], []).append(step["case_id"])
    return orders


def trips_for(stop: Dict[str, Any], order: List[str]) -> Tuple[List[List[str]], bool]:
    """Trips actually used: registered trips plus one auto trip per missing case."""
    trips: List[List[str]] = []
    seen = set()
    for raw in stop.get("trips") or []:
        ids = [cid for cid in raw.get("case_ids", []) if cid in order and cid not in seen]
        if ids:
            trips.append(ids)
            seen.update(ids)
    missing = [cid for cid in order if cid not in seen]
    auto = bool(missing) or not trips
    for cid in missing:
        trips.append([cid])
    rank = {cid: i for i, cid in enumerate(order)}
    trips.sort(key=lambda ids: min(rank[c] for c in ids))
    return trips, auto


# ----------------------------- checks -----------------------------

def _check(code: str, ok: bool, severity: str, message: str,
           point: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    c = {"code": code, "ok": bool(ok), "severity": severity, "message": message,
         "label": ACCESS_ISSUE_CN.get(code, code)}
    if point is not None:
        c["point"] = {"x": round(point["x"], 2), "y": round(point["y"], 2)}
    return c


def _trip_checks(acc: Dict[str, Any], t_idx: int,
                 trip_cases: List[Tuple[str, Dict[str, Any], Dict[str, Any]]]
                 ) -> List[Dict[str, Any]]:
    """Trip-level checks shared by every case of the trip (lift cycles etc.)."""
    kind_cn = KIND_CN[acc["kind"]]
    dock = acc["venue"]["dock"]
    checks: List[Dict[str, Any]] = []
    n = len(trip_cases)
    slots = int(acc["parallel_slots"])
    checks.append(_check(
        "ACCESS_PARALLEL", n <= slots, "error",
        f"第 {t_idx + 1} 趟 {n} 箱并行，通道通行位 {slots} 个"
        + ("，需拆成多趟" if n > slots else ""),
        point=dock))
    total = sum(float(c["weight_kg"]) for _id, c, _b in trip_cases)
    cap = float(acc["capacity_kg"])
    checks.append(_check(
        "ACCESS_CAPACITY", total <= cap + EPS, "error",
        f"第 {t_idx + 1} 趟总重 {total:.0f} kg / {kind_cn}承载 {cap:.0f} kg",
        point=dock))
    if acc["kind"] == "lift":
        used = sum(b["dx"] for _id, _c, b in trip_cases)
        checks.append(_check(
            "ACCESS_CAPACITY", used <= float(acc["length"]) + EPS, "error",
            f"第 {t_idx + 1} 趟箱体总长 {used:.2f} m / 尾板平台长 {float(acc['length']):.2f} m",
            point=dock))
        # Eccentric load: cases load sequentially along the platform length,
        # centred across the width; a single case is always centred.
        if n >= 2 and total > EPS:
            cursor = 0.0
            moment = 0.0
            for _id, c, b in trip_cases:
                w = float(c["weight_kg"])
                moment += w * (cursor + b["dx"] / 2)
                cursor += b["dx"]
            cg = moment / total
            offset = abs(cg - float(acc["length"]) / 2)
            limit = float(acc["edge_load_ratio"]) * float(acc["length"]) / 2
            checks.append(_check(
                "ACCESS_ECCENTRIC", offset <= limit + EPS, "error",
                f"第 {t_idx + 1} 趟平台重心偏移 {offset:.2f} m / 偏载限 {limit:.2f} m",
                point=dock))
    return checks


def _case_checks(acc: Dict[str, Any], case: Dict[str, Any], box: Dict[str, Any],
                 path: List[Dict[str, float]], path_suggested: bool
                 ) -> Tuple[List[Dict[str, Any]], List[Tuple[float, float, float, float]]]:
    """Per-case checks; returns (checks, swept poses used for display)."""
    venue = acc["venue"]
    h = case["handling"]
    mode = CASTER_MODES[h["caster_mode"]]
    kind_cn = KIND_CN[acc["kind"]]
    label = case.get("label", case["id"])
    dx, dy, dz = box["dx"], box["dy"], box["dz"]
    weight = float(case["weight_kg"])
    checks: List[Dict[str, Any]] = []
    dock = venue["dock"]

    checks.append(_check(
        "ACCESS_CLEAR_WIDTH", dy <= float(acc["width"]) + EPS, "error",
        f"{label} 横向 {dy:.2f} m / {kind_cn}净宽 {float(acc['width']):.2f} m",
        point=dock))
    checks.append(_check(
        "ACCESS_CLEAR_HEIGHT", dz <= float(acc["clear_height"]) + EPS, "error",
        f"{label} 高 {dz:.2f} m / 装卸口净高 {float(acc['clear_height']):.2f} m",
        point=dock))
    thr_cap = float(mode["threshold_mm"])
    checks.append(_check(
        "ACCESS_THRESHOLD", float(acc["threshold_mm"]) <= thr_cap + EPS, "error",
        f"门槛 {float(acc['threshold_mm']):.0f} mm / {mode['label']}可通过 {thr_cap:.0f} mm",
        point=dock))
    if acc["kind"] == "ramp":
        checks.append(_check(
            "ACCESS_SLOPE", float(acc["slope_pct"]) <= float(acc["max_slope_pct"]) + EPS,
            "error",
            f"坡道 {float(acc['slope_pct']):.1f}% / 允许 {float(acc['max_slope_pct']):.1f}%",
            point=dock))
    slope_factor = 1.0 + (float(acc["slope_pct"]) / SLOPE_EFFORT_REF_PCT
                          if acc["kind"] == "ramp" else 0.0)
    eff = weight * slope_factor
    push = max(0.0, float(h["push_limit_kg"]))
    required = math.ceil(eff / push - EPS) if push > EPS else 10 ** 9
    crew = int(h["crew"])
    checks.append(_check(
        "ACCESS_CREW", crew >= required, "error",
        f"{label} 等效 {eff:.0f} kg 需 {required} 人，登记 {crew} 人"
        f"（单人上限 {push:.0f} kg）",
        point=dock))

    if path_suggested:
        checks.append(_check("ACCESS_PATH_MISSING", False, "warning",
                             "尚未拖画落地路径，按建议通道预演", point=dock))
    staging = venue["staging"]
    last = path[-1]
    checks.append(_check(
        "ACCESS_STAGING", _point_in_rect(last["x"], last["y"], staging), "warning",
        f"路径终点 ({last['x']:.1f},{last['y']:.1f}) "
        + ("已进入暂存区" if _point_in_rect(last["x"], last["y"], staging) else "未进入暂存区"),
        point={"x": last["x"], "y": last["y"]}))

    # Swept envelope vs obstacles / venue bounds, and the turn zone near dock.
    poses = swept_poses(path)
    hl, hw = dx / 2, dy / 2
    half_diag = math.hypot(hl, hw)
    W, D = float(venue["width"]), float(venue["depth"])
    obstacles = venue["obstacles"]
    tz = acc["turn_zone"]
    tz_y0, tz_y1 = dock["y"] - tz["width"] / 2, dock["y"] + tz["width"] / 2
    swept_hit: Optional[Tuple[float, float, str]] = None
    tz_hit: Optional[Tuple[float, float]] = None
    for x, y, theta, dist in poses:
        for cx, cy in _rect_corners(x, y, theta, hl, hw):
            if swept_hit is None:
                # Bounds are only enforced beyond the registered turn zone: at
                # the dock the case still overhangs the tail lift / doorway.
                beyond_zone = dist > float(tz["depth"]) + EPS
                if beyond_zone and (cx < -EPS or cy < -EPS or cx > W + EPS or cy > D + EPS):
                    swept_hit = (cx, cy, "场馆边界")
                else:
                    for ob in obstacles:
                        if _point_in_rect(cx, cy, ob):
                            swept_hit = (cx, cy, ob.get("label") or "障碍物")
                            break
            if tz_hit is None and dist <= float(tz["depth"]) + EPS:
                if cy < tz_y0 - EPS or cy > tz_y1 + EPS:
                    tz_hit = (cx, cy)
        if swept_hit and tz_hit:
            break
    if swept_hit:
        checks.append(_check(
            "ACCESS_SWEPT", False, "error",
            f"{label} 扫掠包络撞上{swept_hit[2]}",
            point={"x": swept_hit[0], "y": swept_hit[1]}))
    else:
        checks.append(_check("ACCESS_SWEPT", True, "error",
                             f"{label} 扫掠包络无障碍/越界干涉"))
    checks.append(_check(
        "ACCESS_TURN_ZONE", tz_hit is None, "error",
        f"{label} 在转向区内横向摆出（转向区宽 {float(tz['width']):.2f} m）"
        if tz_hit else f"{label} 转向区（{float(tz['width']):.1f}×{float(tz['depth']):.1f} m）内通过",
        point=({"x": tz_hit[0], "y": tz_hit[1]} if tz_hit else dock)))

    # Turning clearance at each interior vertex.
    required = half_diag if h["caster_mode"] == "carry" else float(h["min_turn_radius"])
    turn_hit: Optional[Tuple[float, float, float]] = None
    for i in range(1, len(path) - 1):
        vx, vy = float(path[i]["x"]), float(path[i]["y"])
        clearance = min(vx, W - vx, vy, D - vy)
        for ob in obstacles:
            clearance = min(clearance, _dist_point_rect(vx, vy, ob))
        if clearance + EPS < required:
            turn_hit = (vx, vy, clearance)
            break
    checks.append(_check(
        "ACCESS_TURN_RADIUS", turn_hit is None, "error",
        (f"{label} 转弯净空 {turn_hit[2]:.2f} m < 所需 {required:.2f} m"
         if turn_hit else f"{label} 转弯净空满足 {required:.2f} m"),
        point=({"x": turn_hit[0], "y": turn_hit[1]} if turn_hit else dock)))
    return checks, poses


# ----------------------------- signatures -----------------------------

def _access_input_dict(acc: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the recheck depends on (confirmations excluded)."""
    return {k: acc[k] for k in
            ("kind", "width", "length", "capacity_kg", "slope_pct", "max_slope_pct",
             "threshold_mm", "clear_height", "edge_load_ratio", "parallel_slots",
             "turn_zone", "venue")}


def step_outcome_sig(stop: Dict[str, Any], order: List[str], trips: List[List[str]],
                     case: Dict[str, Any], box: Dict[str, Any],
                     path: List[Dict[str, float]], checks: List[Dict[str, Any]]) -> str:
    """Content hash of one step's inputs AND results.

    A confirmation survives any edit that leaves this step's outcome untouched
    (e.g. another stop's channel); it flips to 待复核 as soon as the case, the
    trip, the path, the channel parameters or the check results change.
    """
    acc = stop["access"]
    parts = [
        "stop=" + stop["id"],
        "order=" + ",".join(order),
        "trips=" + ";".join("+".join(t) for t in trips),
        "case=" + "|".join([
            case["id"],
            ",".join(str(round(float(v), 3)) for v in case["dims"]),
            str(round(float(case["weight_kg"]), 2)),
            json.dumps(case["handling"], sort_keys=True, ensure_ascii=False),
            box.get("orientation", "LWH"),
            ",".join(str(round(float(v), 3)) for v in (box["dx"], box["dy"], box["dz"])),
        ]),
        "acc=" + json.dumps(_access_input_dict(acc), sort_keys=True, ensure_ascii=False),
        "path=" + ";".join(f"{p['x']:.3f},{p['y']:.3f}" for p in path),
        "res=" + ";".join(f"{c['code']}:{1 if c['ok'] else 0}" for c in checks),
    ]
    return fnv1a32("\n".join(parts))


def stop_input_sig(state: Dict[str, Any], stop_id: str) -> Optional[str]:
    """Stop-scoped signature for the revision diff (相关站待复核)."""
    state = norm_state(state)
    stops = state["stops"]
    stop = next((s for s in stops if s["id"] == stop_id), None)
    if stop is None:
        return None
    rank = stops.index(stop)
    acc = stop["access"]
    placements = {p["case_id"]: p for p in state["placements"]}
    cases = []
    for c in state["cases"]:
        if c.get("stop_id") != stop_id:
            continue
        p = placements.get(c["id"]) or {}
        cases.append([
            c["id"],
            [round(float(v), 3) for v in c["dims"]],
            round(float(c["weight_kg"]), 2),
            c["handling"],
            [round(float(p.get(k, 0.0)), 3) for k in ("x", "y", "z")],
            p.get("orientation", "LWH"),
        ])
    parts = [
        f"stop={stop_id}@{rank}",
        "acc=" + json.dumps(_access_input_dict(acc), sort_keys=True, ensure_ascii=False),
        "path=" + ";".join(f"{p['x']:.3f},{p['y']:.3f}" for p in acc["path"]),
        "trips=" + json.dumps(stop.get("trips") or [], sort_keys=True, ensure_ascii=False),
        "cases=" + json.dumps(cases, sort_keys=True, ensure_ascii=False),
    ]
    return fnv1a32("\n".join(parts))


def affected_stops(old_state: Dict[str, Any], new_state: Dict[str, Any]) -> List[str]:
    """Stops whose access-relevant inputs changed (箱位/站序/通道参数)."""
    old = norm_state(old_state)
    new = norm_state(new_state)
    old_ids = [s["id"] for s in old["stops"]]
    new_ids = [s["id"] for s in new["stops"]]
    old_sigs = {sid: stop_input_sig(old, sid) for sid in old_ids}
    new_sigs = {sid: stop_input_sig(new, sid) for sid in new_ids}
    out = [sid for sid in new_ids if old_sigs.get(sid) != new_sigs[sid]]
    out += [sid for sid in old_ids if sid not in new_sigs]
    return out


# ----------------------------- full report -----------------------------

def access_report(state: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate every stop's unloading across the venue access channel."""
    state = norm_state(state)
    issues: List[Dict[str, Any]] = []
    case_issues: Dict[str, Any] = {}
    boxes = {b["id"]: b for b in boxes_from(state)}
    cmap = {c["id"]: c for c in state["cases"]}
    orders = stop_orders(state)
    stops_out: List[Dict[str, Any]] = []
    first_failure: Optional[Dict[str, Any]] = None

    def flag(case_id: str, severity: str, code: str) -> None:
        bucket = case_issues.setdefault(case_id, {"errors": [], "warnings": [], "infos": []})
        key = {"error": "errors", "warning": "warnings"}.get(severity, "infos")
        if code not in bucket[key]:
            bucket[key].append(code)

    for stop in state["stops"]:
        acc = stop["access"]
        venue = acc["venue"]
        order = orders.get(stop["id"], [])
        trips, trips_auto = trips_for(stop, order)
        path_suggested = len(acc["path"]) < 2
        path = suggested_path(acc) if path_suggested else acc["path"]
        confirmed = acc.get("confirmed_steps") or {}
        steps: List[Dict[str, Any]] = []
        counts = {"confirmed": 0, "pending": 0, "draft": 0, "failed": 0, "ok": 0}

        for t_idx, t_ids in enumerate(trips):
            trip_cases = [(cid, cmap[cid], boxes[cid]) for cid in t_ids
                          if cid in cmap and cid in boxes]
            if not trip_cases:
                continue
            trip_checks = _trip_checks(acc, t_idx, trip_cases)
            for cid, case, box in trip_cases:
                checks = [dict(c) for c in trip_checks]
                case_checks, poses = _case_checks(acc, case, box, path, path_suggested)
                checks.extend(case_checks)
                ok = all(c["ok"] for c in checks)
                sig = step_outcome_sig(stop, order, trips, case, box, path, checks)
                rec = confirmed.get(cid)
                if rec and rec.get("sig") == sig:
                    status = "confirmed"
                elif rec:
                    status = "pending"
                else:
                    status = "draft"
                counts[status] += 1
                counts["ok" if ok else "failed"] += 1
                fail_points = [c["point"] for c in checks
                               if not c["ok"] and c.get("point")]
                half_diag = math.hypot(box["dx"], box["dy"]) / 2
                steps.append({
                    "stop_id": stop["id"],
                    "case_id": cid,
                    "label": case.get("label", cid),
                    "step": order.index(cid) + 1,
                    "trip": t_idx + 1,
                    "trip_size": len(t_ids),
                    "ok": ok,
                    "status": status,
                    "outcome_sig": sig,
                    "checks": checks,
                    "sweep": _sweep_polygon(poses, half_diag),
                    "fail_points": fail_points,
                })
                for c in checks:
                    if c["ok"]:
                        continue
                    item = {"severity": c["severity"], "code": c["code"],
                            "message": f"{stop['city']}：{c['message']}",
                            "case_ids": [cid], "stop_id": stop["id"]}
                    if c.get("point"):
                        item["point"] = c["point"]
                    issues.append(item)
                    flag(cid, c["severity"], c["code"])
                    if first_failure is None:
                        first_failure = {
                            "stop_id": stop["id"], "city": stop["city"],
                            "case_id": cid, "label": case.get("label", cid),
                            "step": order.index(cid) + 1, "trip": t_idx + 1,
                            "code": c["code"], "severity": c["severity"],
                            "message": c["message"],
                            "point": c.get("point") or venue["dock"],
                        }

        needs_review = counts["pending"] > 0 or counts["failed"] > 0
        stops_out.append({
            "stop_id": stop["id"],
            "city": stop["city"],
            "kind": acc["kind"],
            "kind_cn": KIND_CN[acc["kind"]],
            "access": _access_input_dict(acc),
            "venue": venue,
            "path_used": path,
            "path_suggested": path_suggested,
            "trips": [{"index": i + 1, "case_ids": ids,
                       "labels": [cmap[c].get("label", c) for c in ids if c in cmap]}
                      for i, ids in enumerate(trips)],
            "trips_auto": trips_auto,
            "steps": steps,
            "counts": counts,
            "needs_review": needs_review,
        })

    summary = {
        "stops": len(stops_out),
        "stops_review": sum(1 for s in stops_out if s["needs_review"]),
        "total_steps": sum(len(s["steps"]) for s in stops_out),
        "confirmed_steps": sum(s["counts"]["confirmed"] for s in stops_out),
        "pending_steps": sum(s["counts"]["pending"] for s in stops_out),
        "failed_steps": sum(s["counts"]["failed"] for s in stops_out),
    }
    return {
        "stops": stops_out,
        "first_failure": first_failure,
        "issues": issues,
        "case_issues": case_issues,
        "summary": summary,
        "generated_at": now_iso(),
    }


# ----------------------------- confirmation -----------------------------

def confirm_stop(state: Dict[str, Any], stop_id: str, reviewer: str = ""
                 ) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Lock every PASSING step of one stop with its outcome signature.

    Failing steps are never confirmed; already confirmed steps whose outcome
    did not change simply keep their previous stamp (前序动作保持锁定).
    """
    state = norm_state(state)
    stop = next((s for s in state["stops"] if s["id"] == stop_id), None)
    if stop is None:
        raise ValueError("站点不存在")
    rep = access_report(state)
    stop_rep = next(s for s in rep["stops"] if s["stop_id"] == stop_id)
    confirmed_ids: List[str] = []
    failed_ids: List[str] = []
    conf = stop["access"].setdefault("confirmed_steps", {})
    now = now_iso()
    for step in stop_rep["steps"]:
        if step["ok"]:
            if step["status"] != "confirmed":
                conf[step["case_id"]] = {"sig": step["outcome_sig"], "at": now,
                                         "by": reviewer}
            confirmed_ids.append(step["case_id"])
        else:
            failed_ids.append(step["case_id"])
    return state, confirmed_ids, failed_ids


def unconfirm_step(state: Dict[str, Any], stop_id: str,
                   case_id: Optional[str] = None) -> Dict[str, Any]:
    """Unlock one step (or all steps of the stop when case_id is None)."""
    state = norm_state(state)
    stop = next((s for s in state["stops"] if s["id"] == stop_id), None)
    if stop is None:
        raise ValueError("站点不存在")
    conf = stop["access"].setdefault("confirmed_steps", {})
    if case_id is None:
        conf.clear()
    else:
        conf.pop(case_id, None)
    return state
