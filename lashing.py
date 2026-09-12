"""Cargo securing (lashing) model: anchors, straps, margins and station legs.

Pure geometry/mechanics, no Flask dependency.  Conventions match ``planning``:

X: tailgate (0) -> front wall; Y: left -> right; Z: floor -> roof.

Forces are expressed in kg-force (1 kg weight = g newtons) so they can be read
directly next to the strap/anchor rating plates.  The simplified mechanics
follow the EN 12195 style teaching model used throughout the app:

* friction :c_n:`mu * (W + downward pretension)` resists horizontal sliding;
* a direct lashing carries the residual demand shared equally (equal strain)
  among the straps that oppose the movement direction;
* tipping is checked as a moment balance about the leading bottom edge;
* a vertical bump uncovers straps whose downward component cannot hold the
  uplift ``W * a_up``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Tuple

from planning import (
    ALL_LASH_FACES,
    EPS,
    MAX_LASH_ANGLE_DEG,
    NO_LASHING_WEIGHT_KG,
    SLIP_REQUIRED_MARGIN,
    SLIP_WARN_MARGIN,
    boxes_from,
    norm_state,
    now_iso,
)

import math

FACE_NORMAL = {
    "-x": (-1.0, 0.0, 0.0),
    "+x": (1.0, 0.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
}
AXIS_VECTOR = {
    "-x": (-1.0, 0.0, 0.0), "+x": (1.0, 0.0, 0.0),
    "-y": (0.0, -1.0, 0.0), "+y": (0.0, 1.0, 0.0),
    "-z": (0.0, 0.0, -1.0), "+z": (0.0, 0.0, 1.0),
}
# Horizontal tendency directions checked for sliding / tipping.
TENDENCIES = [("forward", "+x"), ("rearward", "-x"), ("side_right", "+y"), ("side_left", "-y")]


# --------------------------- basic geometry ---------------------------

def case_endpoint_point(box: Dict[str, Any], face: str, u: float, v: float) -> Tuple[float, float, float]:
    """World point of an attachment on a vertical face of an oriented box."""
    x, y, z = box["x"], box["y"], box["z"]
    dx, dy, dz = box["dx"], box["dy"], box["dz"]
    if face == "-x":
        return (x, y + u * dy, z + v * dz)
    if face == "+x":
        return (x + dx, y + u * dy, z + v * dz)
    if face == "-y":
        return (x + u * dx, y, z + v * dz)
    if face == "+y":
        return (x + u * dx, y + dy, z + v * dz)
    return (x + dx / 2, y + dy / 2, z + dz / 2)


def zone_world_box(box: Dict[str, Any], zone: Dict[str, Any]) -> Tuple[float, float, float, float, float, float]:
    """Transform a case-local no-strap zone AABB into world coordinates."""
    order = {"LWH": (0, 1, 2), "WLH": (1, 0, 2), "LHW": (0, 2, 1),
             "WHL": (1, 2, 0), "HLW": (2, 0, 1), "HWL": (2, 1, 0)}[box.get("orientation", "LWH")]
    zo = (zone["x"], zone["y"], zone["z"])
    zd = (zone["dx"], zone["dy"], zone["dz"])
    return (
        box["x"] + zo[order[0]], box["y"] + zo[order[1]], box["z"] + zo[order[2]],
        zd[order[0]], zd[order[1]], zd[order[2]],
    )


def segment_box_overlap(p0: Sequence[float], p1: Sequence[float],
                        box: Tuple[float, float, float, float, float, float],
                        eps: float = 1.0e-4) -> bool:
    """Slab method: True if segment p0->p1 crosses the interior of an AABB."""
    x, y, z, dx, dy, dz = box
    t0, t1 = 0.0, 1.0
    for i, (lo, hi) in enumerate(((x, x + dx), (y, y + dy), (z, z + dz))):
        d = p1[i] - p0[i]
        if abs(d) < EPS:
            if p0[i] < lo - eps or p0[i] > hi + eps:
                return False
            continue
        ta, tb = (lo - p0[i]) / d, (hi - p0[i]) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1 + eps:
            return False
    return t1 - t0 > eps and t1 > eps and t0 < 1.0 - eps


def point_in_box(p: Sequence[float], box: Tuple[float, float, float, float, float, float],
                 eps: float = 1.0e-4) -> bool:
    x, y, z, dx, dy, dz = box
    return (x - eps <= p[0] <= x + dx + eps and y - eps <= p[1] <= y + dy + eps
            and z - eps <= p[2] <= z + dz + eps)


def sub(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm3(v: Sequence[float]) -> float:
    return math.sqrt(dot(v, v))


# --------------------------- scheme signatures ---------------------------

def _r(v: float, ndigits: int = 3) -> str:
    try:
        return str(round(float(v), ndigits))
    except (TypeError, ValueError):
        return "0"


def fnv1a32(text: str) -> str:
    h = 2166136261
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return f"{h:08x}"


def _box_canon(box: Dict[str, Any]) -> str:
    return "|".join(_r(box[k]) for k in ("x", "y", "z", "dx", "dy", "dz"))


def lashing_signature(state: Dict[str, Any], lash: Dict[str, Any]) -> str:
    """Geometry/force signature of one connection (client and server share it)."""
    boxes = {b["id"]: b for b in boxes_from(state)}
    anchors = {a["id"]: a for a in state["truck"]["anchors"]}
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    parts = [lash["id"]]
    for end in ("from", "to"):
        ep = lash[end]
        token = ep["kind"] + ":" + ep["id"] + ":" + str(ep.get("face", ""))
        token += f":{_r(ep['u'], 3)}:{_r(ep['v'], 3)}"
        if ep["kind"] == "anchor" and ep["id"] in anchors:
            a = anchors[ep["id"]]
            token += ":A" + ",".join(_r(a[k]) for k in ("x", "y", "z", "capacity_kg"))
            token += "," + "".join(ep_dir[0] + ep_dir[1:] for ep_dir in a.get("directions", []))
            token += ",g" + str(a.get("group", ""))
        elif ep["kind"] == "case" and ep["id"] in boxes:
            b = boxes[ep["id"]]
            token += ":B" + _box_canon(b) + f"@r{ranks.get(b['case'].get('stop_id'), -1)}"
            c = b["case"]
            token += ",mu" + _r(c.get("friction", 0.35), 2)
        else:
            token += ":MISSING"
        parts.append(token)
    parts.append(f"P{_r(lash['pretension_kg'], 1)}/C{_r(lash['capacity_kg'], 1)}")
    return fnv1a32(";".join(parts))


def scheme_version(state: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """Return (short scheme hash, {lash_id: geometry signature})."""
    boxes = {b["id"]: b for b in boxes_from(state)}
    anchors = state["truck"]["anchors"]
    accel = state["truck"]["accel"]
    canon = ["ACCEL", ",".join(f"{k}={_r(accel[k], 2)}" for k in sorted(accel))]
    for a in anchors:
        canon.append("A:" + a["id"] + "," + ",".join(
            _r(a[k]) for k in ("x", "y", "z", "capacity_kg"))
            + "," + "".join(sorted(a.get("directions", []))) + ",g" + str(a.get("group", "")))
    for cid, b in sorted(boxes.items()):
        c = b["case"]
        canon.append("C:" + cid + "," + _box_canon(b) + ",mu" + _r(c.get("friction", 0.35), 2)
                     + ",f" + "".join(sorted(c.get("lash_faces", ALL_LASH_FACES))))
        for z in c.get("no_strap_zones", []):
            canon.append("Z:" + cid + "," + ",".join(_r(z[k]) for k in ("x", "y", "z", "dx", "dy", "dz")))
    sigs: Dict[str, str] = {}
    for lash in state.get("lashings", []):
        sig = lashing_signature(state, lash)
        sigs[lash["id"]] = sig
        canon.append("L:" + sig)
    return fnv1a32("\n".join(canon)), sigs


def pending_lashings(state: Dict[str, Any], sigs: Optional[Dict[str, str]] = None) -> List[str]:
    """Locked connections whose stored signature no longer matches the scheme."""
    sigs = sigs or scheme_version(state)[1]
    out = []
    for lash in state.get("lashings", []):
        if lash.get("locked") and lash.get("review_signature") and lash["review_signature"] != sigs.get(lash["id"]):
            out.append(lash["id"])
    return out


def affected_lashings(old_state: Dict[str, Any], new_state: Dict[str, Any]) -> List[str]:
    """Connections that must be re-reviewed after an edit.

    Triggered by moved/rotated endpoint boxes, friction changes, moved or
    re-rated anchors, changed pretension/capacity, or a changed stop rank of
    any touched case.  Pure additions/deletions need no review marker.
    """
    old, new = norm_state(old_state), norm_state(new_state)
    old_boxes = {b["id"]: b for b in boxes_from(old)}
    new_boxes = {b["id"]: b for b in boxes_from(new)}
    old_anchors = {a["id"]: a for a in old["truck"]["anchors"]}
    new_anchors = {a["id"]: a for a in new["truck"]["anchors"]}
    old_rank = {s["id"]: i for i, s in enumerate(old["stops"])}
    new_rank = {s["id"]: i for i, s in enumerate(new["stops"])}
    changed_cases, changed_anchors = set(), set()
    for cid, nb in new_boxes.items():
        ob = old_boxes.get(cid)
        nc = nb["case"]
        if ob is None or any(abs(nb[k] - ob[k]) > EPS for k in ("x", "y", "z", "dx", "dy", "dz")):
            changed_cases.add(cid)
        elif abs(float(nc.get("friction", 0.35)) - float(ob["case"].get("friction", 0.35))) > EPS:
            changed_cases.add(cid)
        elif old_rank.get(nc.get("stop_id")) != new_rank.get(nc.get("stop_id")):
            changed_cases.add(cid)
    for aid, na in new_anchors.items():
        oa = old_anchors.get(aid)
        if oa is None or any(abs(float(na[k]) - float(oa[k])) > EPS for k in ("x", "y", "z", "capacity_kg")) \
                or sorted(na.get("directions", [])) != sorted(oa.get("directions", [])) \
                or na.get("group", "") != oa.get("group", ""):
            changed_anchors.add(aid)
    old_sigs, new_sigs = scheme_version(old)[1], scheme_version(new)[1]
    result = set()
    for lash in new.get("lashings", []):
        lid = lash["id"]
        if lid in old_sigs and old_sigs[lid] != new_sigs.get(lid):
            result.add(lid)
            continue
        for ep in (lash["from"], lash["to"]):
            if ep["kind"] == "case" and ep["id"] in changed_cases:
                result.add(lid)
            if ep["kind"] == "anchor" and ep["id"] in changed_anchors:
                result.add(lid)
    return sorted(result)


# --------------------------- issue helper ---------------------------

def _add(issues: Optional[List[Dict[str, Any]]], severity: str, code: str, message: str,
         lashing_id: str = "", case_ids: Optional[List[str]] = None,
         anchor_id: str = "") -> None:
    if issues is None:
        return
    issues.append({
        "severity": severity, "code": code, "message": message,
        "case_ids": list(case_ids or []), "lashing_id": lashing_id, "anchor_id": anchor_id,
    })


def _sev(locked: bool, draft: str = "warning") -> str:
    """Locked connections violate hard rules; unlocked drafts only warn."""
    return "error" if locked else draft


# --------------------------- evaluation ---------------------------

def _resolve_endpoints(state: Dict[str, Any], boxes: Dict[str, Dict[str, Any]]):
    anchors = {a["id"]: a for a in state["truck"]["anchors"]}
    resolved = []
    for lash in state.get("lashings", []):
        ends = []
        ok = True
        for ep in (lash["from"], lash["to"]):
            if ep["kind"] == "anchor":
                a = anchors.get(ep["id"])
                if a is None:
                    ok = False
                    ends.append(None)
                else:
                    ends.append({"kind": "anchor", "id": a["id"], "label": a.get("label", a["id"]),
                                 "point": (float(a["x"]), float(a["y"]), float(a["z"]))})
            else:
                b = boxes.get(ep["id"])
                if b is None or ep.get("face") not in FACE_NORMAL:
                    ok = False
                    ends.append(None)
                else:
                    pt = case_endpoint_point(b, ep["face"], ep["u"], ep["v"])
                    ends.append({"kind": "case", "id": b["id"], "label": b["label"],
                                 "face": ep["face"], "point": pt, "box": b})
        resolved.append((lash, ends, ok))
    return resolved, anchors


def _attachment(ends: List[Optional[Dict[str, Any]]], case_id: str):
    """Return (own end, other end) for the strap endpoint belonging to case_id."""
    own = next((e for e in ends if e and e["kind"] == "case" and e["id"] == case_id), None)
    other = next((e for e in ends if e is not own), None)
    return own, other


def evaluate_lashing(
    state: Dict[str, Any],
    issues: Optional[List[Dict[str, Any]]] = None,
    case_issues: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check every strap and compute per-case securing margins.

    ``issues=None`` selects silent mode (used on simulated legs, where only the
    numeric margins and failure codes are needed).
    """
    state = norm_state(state)
    truck = state["truck"]
    accel = truck["accel"]
    boxes = boxes_from(state)
    box_map = {b["id"]: b for b in boxes}
    resolved, anchor_map = _resolve_endpoints(state, box_map)
    sigs = scheme_version(state)[1]
    pending = set(pending_lashings(state, sigs))

    def case_flag(cid: str, severity: str, code: str) -> None:
        if case_issues is not None:
            bucket = case_issues.setdefault(cid, {"errors": [], "warnings": [], "infos": []})
            key = {"error": "errors", "warning": "warnings"}.get(severity, "infos")
            if code not in bucket[key]:
                bucket[key].append(code)

    # ---- per-strap geometry / rating checks ----
    strap_rows: Dict[str, Dict[str, Any]] = {}
    locked_ids = set()
    for lash, ends, ok in resolved:
        lid, label = lash["id"], lash.get("label", lash["id"])
        locked = bool(lash["locked"])
        row = {
            "id": lid, "label": label, "locked": locked, "pending": lid in pending,
            "codes": [], "length_m": 0.0, "angle_deg": 0.0,
            "tension_kg": 0.0, "capacity_kg": float(lash["capacity_kg"]),
            "utilization": 0.0, "ok": True, "bears_load": False,
            "from": _end_json(ends[0]), "to": _end_json(ends[1]),
        }
        strap_rows[lid] = row
        if not ok:
            row["codes"].append("LASH_DANGLING"); row["ok"] = False
            _add(issues, "error", f"{label} 的端点缺失（锚点/箱体已删除或未放置）",
                 lid, [e["id"] for e in ends if e and e["kind"] == "case"])
            continue
        p0, p1 = ends[0]["point"], ends[1]["point"]
        vec = sub(p1, p0)
        length = norm3(vec)
        row["length_m"] = length
        if length < EPS:
            row["codes"].append("LASH_LENGTH"); row["ok"] = False
            _add(issues, "error", f"{label} 两端重合，长度为 0", lid)
            continue
        evec = tuple(v / length for v in vec)
        angle = math.degrees(math.asin(min(1.0, abs(evec[2]))))
        row["angle_deg"] = angle
        if angle >= 80.0:
            row["codes"].append("LASH_ANGLE"); row["ok"] = False
            _add(issues, _sev(locked, "info"), "LASH_ANGLE",
                 f"{label} 与地板夹角 {angle:.0f}°（≥80°），几乎不提供水平约束",
                 lid, [e["id"] for e in ends if e["kind"] == "case"])
        elif angle >= MAX_LASH_ANGLE_DEG:
            row["codes"].append("LASH_ANGLE")
            _add(issues, "warning", "LASH_ANGLE",
                 f"{label} 与地板夹角 {angle:.0f}° 偏大（>{MAX_LASH_ANGLE_DEG:.0f}°），水平分力低",
                 lid, [e["id"] for e in ends if e["kind"] == "case"])

        # Allowed lashing faces of each case end.
        for end in ends:
            if end["kind"] != "case":
                continue
            c = end["box"]["case"]
            if end["face"] not in c.get("lash_faces", ALL_LASH_FACES):
                row["codes"].append("LASH_FACE"); row["ok"] = False
                _add(issues, _sev(locked), "LASH_FACE",
                     f"{label} 接在 {end['label']} 的 {end['face']} 面，该面不允许系固",
                     lid, [end["id"]])
                case_flag(end["id"], _sev(locked, "warning"), "LASH_FACE")

        # Strap must not enter another case, nor touch a no-strap zone.
        own_case_ids = {e["id"] for e in ends if e["kind"] == "case"}
        for b in boxes:
            if b["id"] in own_case_ids:
                continue
            aabb = (b["x"], b["y"], b["z"], b["dx"], b["dy"], b["dz"])
            if segment_box_overlap(p0, p1, aabb):
                row["codes"].append("LASH_THROUGH_BOX"); row["ok"] = False
                _add(issues, _sev(locked), "LASH_THROUGH_BOX",
                     f"{label} 路径穿过 {b['label']} 箱体",
                     lid, [b["id"]] + list(own_case_ids))
                case_flag(b["id"], _sev(locked, "warning"), "LASH_THROUGH_BOX")
        for end in ends:
            if end["kind"] != "case":
                continue
            b = end["box"]
            for zone in b["case"].get("no_strap_zones", []):
                zaabb = zone_world_box(b, zone)
                if point_in_box(end["point"], zaabb) or segment_box_overlap(p0, p1, zaabb):
                    row["codes"].append("LASH_ZONE"); row["ok"] = False
                    _add(issues, _sev(locked, "info"), "LASH_ZONE",
                         f"{label} 压住 {b['label']} 的{zone.get('label', '禁压区')}",
                         lid, [b["id"]])
                    case_flag(b["id"], _sev(locked, "info"), "LASH_ZONE")

        # Anchor pull-direction window.
        anchor_end = next((e for e in ends if e["kind"] == "anchor"), None)
        if anchor_end is not None:
            a = anchor_map[anchor_end["id"]]
            dirs = a.get("directions", [])
            # Direction the strap pulls the cargo = from case toward anchor.
            case_end = next(e for e in ends if e["kind"] == "case")
            pull = tuple(v / length for v in sub(anchor_end["point"], case_end["point"]))
            cos45 = math.cos(math.radians(45.0))
            cos60 = math.cos(math.radians(60.0))
            if abs(pull[2]) > cos45 and "+z" in dirs:
                best = 1.0  # near-vertical down-strap on a floor D-ring
            else:
                horiz = [d for d in dirs if d.endswith("x") or d.endswith("y")]
                best = max((dot(pull, AXIS_VECTOR[d]) for d in horiz), default=0.0)
            if dirs and best < cos60:
                row["codes"].append("ANCHOR_DIRECTION"); row["ok"] = False
                _add(issues, _sev(locked, "info"), "ANCHOR_DIRECTION",
                     f"{label} 的拉力方向超出锚点 {a.get('label', a['id'])} 可用方向",
                     lid, [case_end["id"]], a["id"])

        if not locked:
            _add(issues, "info", "LASH_UNLOCKED", f"{label} 尚未锁定，不参与承载力校核", lid)
        else:
            locked_ids.add(lid)
        if row["pending"]:
            _add(issues, "warning", "LASH_PENDING",
                 f"{label} 自上次锁定后几何/参数已变化，待复核", lid)
        if float(lash["pretension_kg"]) > float(lash["capacity_kg"]) + EPS:
            row["codes"].append("LASH_OVERLOAD"); row["ok"] = False
            _add(issues, _sev(locked, "warning"), "LASH_OVERLOAD",
                 f"{label} 预紧力 {lash['pretension_kg']:.0f} kg 已超过额定 "
                 f"{lash['capacity_kg']:.0f} kg", lid)
        # De-duplicate codes (e.g. one diagonal may cross several boxes).
        seen_codes: set[str] = set()
        deduped = []
        for c in row["codes"]:
            if c not in seen_codes:
                seen_codes.add(c)
                deduped.append(c)
        row["codes"] = deduped
        # A strap with a hard geometry fault cannot be trusted to carry load.
        hard_nonbearing = {"LASH_DANGLING", "LASH_LENGTH", "LASH_FACE",
                           "LASH_THROUGH_BOX", "LASH_ZONE", "ANCHOR_DIRECTION"}
        if not (hard_nonbearing & set(deduped)) and not (angle >= 80.0):
            row["bears_load"] = True

    # ---- per-case force model (locked straps only) ----
    case_rows: Dict[str, Dict[str, Any]] = {}

    for b in boxes:
        cid = b["id"]
        W = float(b["weight"])
        mu = float(b["case"].get("friction", 0.35))
        attachments = []  # (strap row, own point, other point, unit e, lash, other)
        for lash, ends, ok in resolved:
            if not ok or not lash["locked"]:
                continue
            srow = strap_rows[lash["id"]]
            if not srow["bears_load"]:
                continue
            own, other = _attachment(ends, cid)
            if own is None or other is None:
                continue
            v = sub(other["point"], own["point"])
            L = norm3(v)
            if L < EPS:
                continue
            attachments.append((strap_rows[lash["id"]], own["point"], other["point"],
                                tuple(q / L for q in v), lash, other))

        # Friction normal gain from downward pretension.
        down_pre = sum(float(l["pretension_kg"]) * max(0.0, -e[2]) for _, _, _, e, l, _ in attachments)
        Ff = mu * (W + down_pre)
        slip_margins: Dict[str, Optional[float]] = {}
        required_t: Dict[str, float] = {row["id"]: 0.0 for row, *_ in attachments}
        tendency_demand: Dict[str, float] = {}
        for tname, axis in TENDENCIES:
            u = AXIS_VECTOR[axis]
            demand = W * float(accel[{"+x": "forward", "-x": "rearward",
                                      "+y": "lateral", "-y": "lateral"}[axis]])
            tendency_demand[tname] = demand
            if demand <= EPS:
                slip_margins[tname] = None
                continue
            resistors = [(row, e, l) for row, _, _, e, l, _ in attachments if -dot(e, u) > EPS]
            sum_cap = sum(float(l["capacity_kg"]) * -dot(e, u) for _, e, l in resistors)
            margin = (Ff + sum_cap) / demand
            slip_margins[tname] = margin
            deficit = max(0.0, demand - Ff)
            if deficit > EPS and resistors:
                sum_w = sum(-dot(e, u) for _, e, _ in resistors)
                req = deficit / max(sum_w, EPS)
                for row, e, l in resistors:
                    required_t[row["id"]] = max(required_t[row["id"]], req)
        # Tipping moment balance per horizontal tendency.
        tip_margins: Dict[str, Optional[float]] = {}
        z_cg = b["z"] + b["dz"] * 0.5
        for tname, axis in TENDENCIES:
            u = AXIS_VECTOR[axis]
            a_g = float(accel[{"+x": "forward", "-x": "rearward",
                               "+y": "lateral", "-y": "lateral"}[axis]])
            demand = W * a_g
            if demand <= EPS:
                tip_margins[tname] = None
                continue
            # Leading bottom edge in the tendency direction = pivot.
            pivot = [b["x"] + b["dx"] / 2, b["y"] + b["dy"] / 2, b["z"]]
            pivot[0] += u[0] * b["dx"] / 2
            pivot[1] += u[1] * b["dy"] / 2
            s = cross(u, (0.0, 0.0, 1.0))  # pivot axis; positive scalar = restoring
            cg_r = (b["x"] + b["dx"] / 2 - pivot[0], b["y"] + b["dy"] / 2 - pivot[1], z_cg - pivot[2])
            # Gravity F=(0,0,-W) with inward r gives positive (restoring);
            # the braking/turning pseudo-force +W*a_g*u at height gives, after
            # the sign flip, a positive tipping load.
            gravity_m = dot(s, cross(cg_r, (0.0, 0.0, -W)))
            inertia_m = -dot(s, cross(cg_r, (W * a_g * u[0], W * a_g * u[1], 0.0)))
            tip_load = max(0.0, inertia_m) + max(0.0, -gravity_m)
            restore = max(0.0, gravity_m) + max(0.0, -inertia_m)
            for row, own_pt, _, e, l, _other in attachments:
                T = float(l["pretension_kg"]) + required_t.get(row["id"], 0.0)
                r = (own_pt[0] - pivot[0], own_pt[1] - pivot[1], own_pt[2] - pivot[2])
                m_s = dot(s, cross(r, (T * e[0], T * e[1], T * e[2])))
                restore += max(0.0, m_s)
                tip_load += max(0.0, -m_s)
            tip_margins[tname] = restore / tip_load if tip_load > EPS else None

        # Vertical uplift (bump): only checked for stacked boxes or boxes that
        # already have straps with a vertical component; ground boxes rely on
        # their own weight plus friction and need no top-over lashing.
        up_demand = W * float(accel["up"])
        vertical_lash = any(-e[2] > 0.1 for _, _, _, e, _l, _o in attachments)
        if up_demand > EPS and attachments and vertical_lash:
            hold = sum((float(l["pretension_kg"]) + required_t.get(row["id"], 0.0))
                       * max(0.0, -e[2]) for row, _, _, e, l, _ in attachments)
            lift_margin: Optional[float] = hold / up_demand
        else:
            lift_margin = None

        margins = [m for m in list(slip_margins.values()) + list(tip_margins.values()) + [lift_margin] if m is not None]
        case_rows[cid] = {
            "case_id": cid, "label": b["label"], "weight_kg": W, "friction": mu,
            "slip": slip_margins, "tip": tip_margins, "lift": lift_margin,
            "min_margin": min(margins) if margins else None,
            "lashing_ids": [l["id"] for *_, l, _ in attachments],
        }

        if W >= NO_LASHING_WEIGHT_KG and not attachments:
            _add(issues, "warning", "LASH_MISSING",
                 f"{b['label']}（{W:.0f} kg）没有任何已锁定绑带", lashing_id="", case_ids=[cid])
            case_flag(cid, "warning", "LASH_MISSING")

        def margin_issue(kind: str, m: Optional[float], name: str, tname: str = "") -> None:
            if m is None:
                return
            code = kind.upper() + "_MARGIN"
            # Without a single locked strap the plan is simply unfinished ->
            # warn.  Once straps are locked and still short, it is a hard error.
            severity = "error" if attachments else "warning"
            if m < SLIP_REQUIRED_MARGIN - EPS:
                _add(issues, severity, code,
                     f"{b['label']} {name}余量 {m:.2f}（要求 ≥{SLIP_REQUIRED_MARGIN:.2f}）{('·'+tname) if tname else ''}",
                     case_ids=[cid])
                case_flag(cid, severity, code)
            elif m < SLIP_WARN_MARGIN - EPS:
                _add(issues, "warning", code,
                     f"{b['label']} {name}余量偏低 {m:.2f}", case_ids=[cid])
                case_flag(cid, "warning", code)

        for tname, m in slip_margins.items():
            margin_issue("SLIP", m, "防滑", tname)
        for tname, m in tip_margins.items():
            margin_issue("TIP", m, "防倾覆", tname)
        margin_issue("LIFT", lift_margin, "防跳起")

        # Peak tension per strap + anchor load bookkeeping.
        for row, _own, _other, e, l, other in attachments:
            peak = float(l["pretension_kg"]) + required_t.get(row["id"], 0.0)
            if peak > row["tension_kg"]:
                row["tension_kg"] = peak
            cap = float(l["capacity_kg"])
            row["utilization"] = max(row["utilization"], peak / cap if cap else 0.0)
            if cap and peak > cap + EPS:
                row["codes"].append("LASH_OVERLOAD"); row["ok"] = False
                _add(issues, "error", "LASH_OVERLOAD",
                     f"{row['label']} 工作张力 {peak:.0f} kg 超过额定 {cap:.0f} kg",
                     row["id"], [cid])
            elif cap and peak / cap >= 0.9:
                row["codes"].append("LASH_TENSION_MARGIN")
                _add(issues, "warning", "LASH_TENSION_MARGIN",
                     f"{row['label']} 张力余量不足：{peak:.0f}/{cap:.0f} kg", row["id"], [cid])

    # Anchor totals: sum strap peaks per anchor, then per shared group.
    anchor_rows: Dict[str, Dict[str, Any]] = {}
    group_members: Dict[str, List[str]] = {}
    for a in truck["anchors"]:
        total = 0.0
        for lash, ends, ok in resolved:
            if not ok or not lash["locked"] or not strap_rows[lash["id"]]["bears_load"]:
                continue
            anchor_end = next((e for e in ends if e and e["kind"] == "anchor"), None)
            if anchor_end and anchor_end["id"] == a["id"]:
                total += strap_rows[lash["id"]]["tension_kg"] or float(lash["pretension_kg"])
        cap = float(a["capacity_kg"])
        anchor_rows[a["id"]] = {
            "id": a["id"], "label": a.get("label", a["id"]),
            "load_kg": total, "capacity_kg": cap,
            "utilization": total / cap if cap else 0.0,
            "group": a.get("group", ""), "surface": a.get("surface", "floor"),
            "ok": cap <= 0 or total <= cap + EPS,
        }
        if a.get("group"):
            group_members.setdefault(a["group"], []).append(a["id"])
        if cap and total > cap + EPS:
            _add(issues, "error", "ANCHOR_OVERLOAD",
                 f"锚点 {a.get('label', a['id'])} 合计受力 {total:.0f} kg 超过额定 {cap:.0f} kg",
                 anchor_id=a["id"])
        elif cap and total / cap >= 0.9:
            _add(issues, "warning", "ANCHOR_MARGIN",
                 f"锚点 {a.get('label', a['id'])} 受力余量不足：{total:.0f}/{cap:.0f} kg",
                 anchor_id=a["id"])
    for group, members in group_members.items():
        total = sum(anchor_rows[m]["load_kg"] for m in members)
        group_caps = {float(anchor_map[m].get("group_capacity_kg", 0.0) or 0.0) for m in members}
        group_cap = next(iter(group_caps)) if len(group_caps) == 1 else 0.0
        for m in members:
            anchor_rows[m]["group_load_kg"] = total
            anchor_rows[m]["group_capacity_kg"] = group_cap
        if group_cap and total > group_cap + EPS:
            _add(issues, "error", "ANCHOR_GROUP",
                 f"共用锚点组 {group} 合计 {total:.0f} kg 超过共用容量 {group_cap:.0f} kg")
        elif group_cap and total / group_cap >= 0.9:
            _add(issues, "warning", "ANCHOR_GROUP",
                 f"共用锚点组 {group} 余量不足：{total:.0f}/{group_cap:.0f} kg")

    all_codes = [c for row in strap_rows.values() for c in row["codes"]]
    min_slip = _safe_min(m for r in case_rows.values() for m in r["slip"].values())
    min_tip = _safe_min(m for r in case_rows.values() for m in r["tip"].values())
    min_lift = _safe_min(r["lift"] for r in case_rows.values())
    return {
        "ok": not any(i["severity"] == "error" for i in (issues or [])),
        "generated_at": now_iso(),
        "scheme_version": sigs and scheme_version(state)[0] or fnv1a32(""),
        "signatures": sigs,
        "pending_ids": sorted(pending),
        "straps": [strap_rows[l["id"]] for l in state.get("lashings", []) if l["id"] in strap_rows],
        "cases": list(case_rows.values()),
        "anchors": list(anchor_rows.values()),
        "min_slip_margin": min_slip,
        "min_tip_margin": min_tip,
        "min_lift_margin": min_lift,
        "locked_count": len(locked_ids),
        "total_count": len(strap_rows),
        "codes": sorted(set(all_codes)),
    }


def _safe_min(values):
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _end_json(end: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if end is None:
        return None
    return {"kind": end["kind"], "id": end["id"], "label": end.get("label", end["id"]),
            "face": end.get("face", ""), "point": list(end["point"])}


# --------------------------- station-leg simulation ---------------------------

def _leg_state(state: Dict[str, Any], keep_stop_ranks: set[int]) -> Dict[str, Any]:
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}
    keep_cases = {c["id"] for c in state["cases"] if ranks.get(c.get("stop_id"), -1) in keep_stop_ranks}
    sub = deepcopy(state)
    sub["placements"] = [p for p in sub["placements"] if p["case_id"] in keep_cases]
    kept_lashings = []
    for lash in sub["lashings"]:
        case_ends = [ep for ep in (lash["from"], lash["to"]) if ep["kind"] == "case"]
        if all(ep["id"] in keep_cases for ep in case_ends):
            kept_lashings.append(lash)
    sub["lashings"] = kept_lashings
    return sub


def _leg_failures(eval_result: Dict[str, Any], strict: bool = True) -> List[Dict[str, Any]]:
    """Error codes produced on a simulated leg (silent evaluation).

    ``strict=False`` is used for the full-load departure stage, where unlocked
    /missing lashing is an unfinished-plan warning rather than a leg failure.
    """
    failures = []
    for row in eval_result["straps"]:
        hard = [c for c in row["codes"] if c in {
            "LASH_DANGLING", "LASH_LENGTH", "LASH_FACE", "LASH_THROUGH_BOX",
            "LASH_ZONE", "ANCHOR_DIRECTION", "LASH_OVERLOAD"}]
        # On legs hard capacity/geometry failures count; the >=80 deg angle
        # leaves the strap with no horizontal hold, so it fails too.
        if "LASH_ANGLE" in row["codes"] and row["angle_deg"] >= 80.0:
            hard.append("LASH_ANGLE")
        for c in hard:
            failures.append({"kind": "lashing", "id": row["id"], "label": row["label"], "code": c})
    for a in eval_result["anchors"]:
        if a["capacity_kg"] and a["load_kg"] > a["capacity_kg"] + EPS:
            failures.append({"kind": "anchor", "id": a["id"], "label": a["label"], "code": "ANCHOR_OVERLOAD"})
        if a.get("group_capacity_kg") and a.get("group_load_kg", 0) > a["group_capacity_kg"] + EPS:
            failures.append({"kind": "anchor_group", "id": a["group"], "label": a["group"], "code": "ANCHOR_GROUP"})
    if strict:
        for r in eval_result["cases"]:
            secured = bool(r["lashing_ids"])
            if not secured and r["weight_kg"] >= NO_LASHING_WEIGHT_KG:
                failures.append({"kind": "case", "id": r["case_id"], "label": r["label"], "code": "LASH_MISSING"})
            if secured:
                for m in r["slip"].values():
                    if m is not None and m < SLIP_REQUIRED_MARGIN - EPS:
                        failures.append({"kind": "case", "id": r["case_id"], "label": r["label"], "code": "SLIP_MARGIN"})
                for m in r["tip"].values():
                    if m is not None and m < SLIP_REQUIRED_MARGIN - EPS:
                        failures.append({"kind": "case", "id": r["case_id"], "label": r["label"], "code": "TIP_MARGIN"})
                if r["lift"] is not None and r["lift"] < SLIP_REQUIRED_MARGIN - EPS:
                    failures.append({"kind": "case", "id": r["case_id"], "label": r["label"], "code": "LIFT_MARGIN"})
    # De-duplicate.
    seen, out = set(), []
    for f in failures:
        key = (f["kind"], f["id"], f["code"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


FAILURE_PRIORITY = ["ANCHOR_OVERLOAD", "ANCHOR_GROUP", "LASH_OVERLOAD", "LASH_THROUGH_BOX",
                    "ANCHOR_DIRECTION", "LASH_FACE", "LASH_ZONE", "LASH_ANGLE",
                    "SLIP_MARGIN", "TIP_MARGIN", "LIFT_MARGIN", "LASH_MISSING",
                    "LASH_LENGTH", "LASH_DANGLING"]


def _suggest(state: Dict[str, Any], leg: Dict[str, Any], failure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Construct an add-strap suggestion for the first failing connection."""
    boxes = {b["id"]: b for b in boxes_from(leg)}
    anchors = {a["id"]: a for a in state["truck"]["anchors"]}
    code = failure["code"]

    if failure["kind"] in ("anchor", "anchor_group"):
        return {"type": "relieve_anchor", "anchor_id": failure["id"],
                "message": f"锚点 {failure['label']} 超载：将部分绑带改接到邻近锚点，或新增同容量锚点分担受力。"}

    target_case = None
    if failure["kind"] == "case":
        target_case = boxes.get(failure["id"])
    elif failure["kind"] == "lashing":
        # Find the case end of the failing strap in the leg.
        for lash in leg["lashings"]:
            if lash["id"] != failure["id"]:
                continue
            ep = next((e for e in (lash["from"], lash["to"]) if e["kind"] == "case"), None)
            if ep:
                target_case = boxes.get(ep["id"])
    if target_case is None:
        return None
    b = target_case
    # Choose tendency to fix: slip/tip → weakest direction; lift → downward.
    rows = {r["case_id"]: r for r in evaluate_lashing(leg)["cases"]}
    r = rows.get(b["id"])
    axis, tname = "+x", "forward"
    if code == "LIFT_MARGIN":
        axis = ""  # vertical suggestion
    elif r is not None:
        worst = min(((m, t) for t, m in r["slip"].items() if m is not None),
                    default=(None, None), key=lambda x: x[0])
        if worst[1]:
            axis = dict(TENDENCIES)[worst[1]]
            tname = worst[1]

    if not axis:
        # Need a floor anchor beside the box, attachment high on that side.
        candidates = _nearby_anchors(anchors.values(), b, lambda a, bb: True)
        if not candidates:
            return {"type": "add_anchor", "case_id": b["id"],
                    "message": f"{b['label']} 防跳起余量不足：在箱体侧下方增设地板锚点并加垂直绑带。"}
        a = candidates[0]
        face = "+y" if a["y"] >= b["y"] + b["dy"] / 2 else "-y"
        return {"type": "add_lashing", "case_id": b["id"], "anchor_id": a["id"],
                "face": face, "u": 0.5, "v": 0.9, "pretension_kg": 250.0,
                "message": f"为 {b['label']} 从锚点 {a.get('label', a['id'])} 向箱体上部加一条下拉绑带（预紧约 250 kg）。"}

    u = AXIS_VECTOR[axis]
    # Restraining anchor lies on the OPPOSITE side of the movement tendency.
    def side_ok(a, bb):
        if axis == "+x":
            return a["x"] <= bb["x"] + 0.15 and "-x" in a.get("directions", [])
        if axis == "-x":
            return a["x"] >= bb["x"] + bb["dx"] - 0.15 and "+x" in a.get("directions", [])
        if axis == "+y":
            return a["y"] <= bb["y"] + 0.15 and "-y" in a.get("directions", [])
        return a["y"] >= bb["y"] + bb["dy"] - 0.15 and "+y" in a.get("directions", [])

    pool = [a for a in anchors.values() if side_ok(a, b)]
    pool.sort(key=lambda a: (abs(a["z"]),
                             (a["x"] - (b["x"] + b["dx"] / 2)) ** 2 + (a["y"] - (b["y"] + b["dy"] / 2)) ** 2))
    face = {"+x": "-x", "-x": "+x", "+y": "-y", "-y": "+y"}[axis]
    v = 0.85 if code == "TIP_MARGIN" else 0.5
    if not pool:
        return {"type": "add_anchor", "case_id": b["id"],
                "message": f"{b['label']} 沿 {tname} 方向{('抗倾覆' if code == 'TIP_MARGIN' else '防滑')}余量不足："
                           f"先在箱体{_face_cn(face)}一侧增加允许 {face} 向受拉的锚点，再加绑带。"}
    a = pool[0]
    deficit = 0.0
    if r is not None and r["slip"].get(tname) is not None and r["slip"][tname] < SLIP_REQUIRED_MARGIN:
        demand = float(b["weight"]) * float(state["truck"]["accel"][
            {"forward": "forward", "rearward": "rearward"}.get(tname, "lateral")])
        deficit = max(0.0, SLIP_REQUIRED_MARGIN * demand - float(b["weight"]) * float(b["case"].get("friction", 0.35)))
    pre = min(float(a["capacity_kg"]), max(150.0, deficit * 1.25 if deficit > EPS else 250.0))
    return {"type": "add_lashing", "case_id": b["id"], "anchor_id": a["id"],
            "face": face, "u": 0.5, "v": round(v, 2), "pretension_kg": round(pre, 0),
            "message": f"为 {b['label']} 增加一条到锚点 {a.get('label', a['id'])} 的{_face_cn(face)}绑带"
                       f"（预紧约 {pre:.0f} kg），补足{('抗倾覆' if code == 'TIP_MARGIN' else '防滑')}余量。"}


def _face_cn(face: str) -> str:
    return {"-x": "车尾", "+x": "车头", "-y": "左侧", "+y": "右侧"}.get(face, face)


def _nearby_anchors(anchors, b, ok):
    pool = [a for a in anchors if ok(a, b)]
    pool.sort(key=lambda a: (a["x"] - (b["x"] + b["dx"] / 2)) ** 2 + (a["y"] - (b["y"] + b["dy"] / 2)) ** 2)
    return pool


def station_lashing_report(state: Dict[str, Any]) -> Dict[str, Any]:
    """Full departure evaluation plus one leg per simulated unload stop."""
    state = norm_state(state)
    departure_issues: List[Dict[str, Any]] = []
    case_issues: Dict[str, Any] = {}
    departure = evaluate_lashing(state, departure_issues, case_issues)
    ranks = {s["id"]: i for i, s in enumerate(state["stops"])}

    departure_failures = _leg_failures(departure, strict=False)
    stages: List[Dict[str, Any]] = [{
        "key": "departure", "title": "发车前（满载）", "stop_id": None,
        "remaining_case_ids": [b["id"] for b in boxes_from(state)],
        "remaining_mass_kg": sum(b["weight"] for b in boxes_from(state)),
        "min_slip_margin": departure["min_slip_margin"],
        "min_tip_margin": departure["min_tip_margin"],
        "min_lift_margin": departure["min_lift_margin"],
        "failures": departure_failures,
        "evaluation": departure,
    }]
    first_failure = None
    for rank, stop in enumerate(state["stops"]):
        keep = {r for r in range(rank + 1, len(state["stops"]))}
        leg = _leg_state(state, keep)
        leg_boxes = boxes_from(leg)
        if not leg_boxes:
            stages.append({
                "key": f"after-{stop['id']}", "title": f"{stop['city']} 卸货后（空车）",
                "stop_id": stop["id"], "remaining_case_ids": [], "remaining_mass_kg": 0.0,
                "min_slip_margin": None, "min_tip_margin": None, "min_lift_margin": None,
                "failures": [], "empty": True,
            })
            continue
        ev = evaluate_lashing(leg)
        failures = _leg_failures(ev)
        stage = {
            "key": f"after-{stop['id']}", "title": f"{stop['city']} 卸货后",
            "stop_id": stop["id"],
            "remaining_case_ids": [b["id"] for b in leg_boxes],
            "remaining_mass_kg": sum(b["weight"] for b in leg_boxes),
            "min_slip_margin": ev["min_slip_margin"],
            "min_tip_margin": ev["min_tip_margin"],
            "min_lift_margin": ev["min_lift_margin"],
            "failures": failures,
            "evaluation": ev,
        }
        stages.append(stage)
        if first_failure is None and failures:
            failures.sort(key=lambda f: FAILURE_PRIORITY.index(f["code"]) if f["code"] in FAILURE_PRIORITY else 99)
            f0 = failures[0]
            suggestion = _suggest(state, leg, f0)
            first_failure = {
                "stage_key": stage["key"], "stage_title": stage["title"],
                "stop_id": stop["id"], **f0, "suggestion": suggestion,
            }

    # Per-stop release order (high attachment first).
    release_steps = []
    for rank, stop in enumerate(state["stops"]):
        keep = {r for r in range(rank, len(state["stops"]))}
        leg = _leg_state(state, keep)
        box_map = {b["id"]: b for b in boxes_from(leg)}
        resolved, _ = _resolve_endpoints(leg, box_map)
        releases = []
        for lash, ends, ok in resolved:
            case_ends = [e for e in ends if e and e["kind"] == "case"]
            unload_now = [e for e in case_ends
                          if box_map.get(e["id"], {}).get("case", {}).get("stop_id") == stop["id"]]
            if unload_now:
                high = max(e["point"][2] for e in unload_now)
                releases.append((high, lash, ends))
        releases.sort(key=lambda q: -q[0])
        release_steps.append({
            "stop_id": stop["id"], "city": stop["city"],
            "lashings": [{
                "id": lash["id"], "label": lash.get("label", lash["id"]),
                "locked": bool(lash["locked"]),
                "case_labels": [e["label"] for e in ends if e and e["kind"] == "case"],
            } for _, lash, ends in releases],
        })

    return {
        **departure,
        "issues": departure_issues,
        "case_issues": case_issues,
        "stages": stages,
        "first_failure": first_failure,
        "release_steps": release_steps,
    }


def lashing_version_diff(old_state: Dict[str, Any], new_state: Dict[str, Any]) -> Dict[str, Any]:
    """Precise connection-level diff used when saving a revision.

    Only connections whose geometry/force signature changed are returned, so
    adjusting the stop order or moving one box marks exactly the straps that
    must be re-reviewed.
    """
    old, new = norm_state(old_state), norm_state(new_state)
    old_sigs, new_sigs = scheme_version(old)[1], scheme_version(new)[1]
    affected = sorted(lid for lid in new_sigs if lid in old_sigs and old_sigs[lid] != new_sigs[lid])
    added = sorted(lid for lid in new_sigs if lid not in old_sigs)
    removed = sorted(lid for lid in old_sigs if lid not in new_sigs)
    return {"affected_lashing_ids": affected, "added": added, "removed": removed,
            "old_scheme": scheme_version(old)[0], "new_scheme": new_sigs and scheme_version(new)[0]}
