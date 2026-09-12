"""Sample dataset.

The initial snapshot is deliberately unsafe:
- six Amsterdam first-stop cases are pushed toward the front wall;
- later Berlin wardrobe/merchandise blocks the rear tailgate lanes;
- a FOH rack is stacked/adjacent in a way that violates pressure/forbidden
  neighbour assumptions;
- the resulting front-axle load exceeds the configured capacity;
- one strap overloads a 500 kg side anchor, one runs diagonally through
  another case, and a case-to-case link survives only until Amsterdam.
"""
from __future__ import annotations

from copy import deepcopy

def _access(kind, path=None, obstacles=None, **kw):
    """Venue access registration for a stop (tail lift / ramp + venue plan)."""
    acc = {
        "kind": kind,
        "width": 2.3, "length": 2.2, "capacity_kg": 1500,
        "slope_pct": 0, "max_slope_pct": 8, "threshold_mm": 10,
        "clear_height": 2.5, "edge_load_ratio": 0.55, "parallel_slots": 1,
        "turn_zone": {"width": 3.0, "depth": 3.0},
        "venue": {
            "width": 14.0, "depth": 9.0,
            "dock": {"x": 1.0, "y": 4.5},
            "staging": {"x": 10.5, "y": 5.5, "dx": 3.0, "dy": 3.0, "label": "暂存区"},
            "obstacles": obstacles or [],
        },
        "path": path or [{"x": 1.0, "y": 4.5}, {"x": 12.0, "y": 4.5}, {"x": 12.0, "y": 7.0}],
        "confirmed_steps": {},
    }
    acc.update(kw)
    return acc


STOPS = [
    {"id": "ams", "city": "Amsterdam", "venue": "Melkweg",
     "trips": [],
     "access": _access("lift", width=2.4, length=2.2, capacity_kg=1500,
                       threshold_mm=10, clear_height=2.5,
                       obstacles=[{"x": 6.5, "y": 1.0, "dx": 0.9, "dy": 0.9, "label": "立柱"}])},
    {"id": "ber", "city": "Berlin", "venue": "Columbiahalle",
     "trips": [],
     "access": _access("lift", width=2.2, length=2.0, capacity_kg=1200,
                       threshold_mm=20, clear_height=2.4,
                       turn_zone={"width": 2.8, "depth": 2.8},
                       obstacles=[{"x": 5.0, "y": 6.4, "dx": 2.4, "dy": 2.0, "label": "储物间"},
                                  {"x": 9.0, "y": 1.8, "dx": 0.8, "dy": 0.8, "label": "立柱"}])},
    {"id": "par", "city": "Paris", "venue": "Le Bataclan",
     "trips": [],
     "access": _access("ramp", width=2.3, length=3.0, capacity_kg=2000,
                       slope_pct=4, max_slope_pct=8, threshold_mm=15, clear_height=2.6,
                       obstacles=[{"x": 4.5, "y": 6.8, "dx": 2.0, "dy": 1.4, "label": "吧台"}])},
]

CASES = [
    # Amsterdam / stop 1: four 1.2 m wide bass/control systems.
    {"id": "BASS-A", "label": "Bass Head A", "dims": [1.25, 1.2, 0.95], "weight_kg": 380,
     "stop_id": "ams", "allowed_orientations": ["LWH", "WLH", "LHW"], "max_stack_kg": 260,
     "forbidden_neighbors": ["FOH-L"], "notes": "功放，重心低；禁止与 FOH 贴邻。"},
    {"id": "BASS-B", "label": "Bass Head B", "dims": [1.25, 1.2, 0.95], "weight_kg": 370,
     "stop_id": "ams", "allowed_orientations": ["LWH", "WLH", "LHW"], "max_stack_kg": 260,
     "forbidden_neighbors": [], "notes": ""},
    {"id": "FOH-L", "label": "FOH Left", "dims": [1.25, 1.2, 1.05], "weight_kg": 290,
     "stop_id": "ams", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 0,
     "forbidden_neighbors": ["BASS-A"], "notes": "顶部禁压；尾门优先取出。"},
    {"id": "FOH-R", "label": "FOH Right", "dims": [1.25, 1.2, 1.05], "weight_kg": 250,
     "stop_id": "ams", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 0,
     "forbidden_neighbors": [], "notes": "顶部禁压。"},
    {"id": "CON-MON", "label": "Monitor Console", "dims": [1.2, 0.9, 0.7], "weight_kg": 180,
     "stop_id": "ams", "allowed_orientations": ["LWH", "WLH", "LHW", "WHL"], "max_stack_kg": 120,
     "forbidden_neighbors": [], "notes": "可多向翻转，但屏幕面不得重压。"},
    {"id": "CON-CAT", "label": "Cat Snake", "dims": [0.9, 0.7, 0.55], "weight_kg": 90,
     "stop_id": "ams", "allowed_orientations": ["LWH", "WLH", "LHW", "WHL", "HLW", "HWL"],
     "max_stack_kg": 150, "forbidden_neighbors": [], "notes": "线缆箱，可任意方向。"},

    # Berlin / stop 2.
    {"id": "BER-LIGHT", "label": "Berlin Lighting Rack", "dims": [1.25, 0.9, 1.7], "weight_kg": 270,
     "stop_id": "ber", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 0,
     "forbidden_neighbors": [], "notes": "灯控立架，不可倒置。"},
    {"id": "BER-WARD", "label": "Berlin Wardrobe", "dims": [1.5, 1.2, 1.9], "weight_kg": 260,
     "stop_id": "ber", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 0,
     "forbidden_neighbors": [], "notes": "服装立箱，不可平放。"},
    {"id": "MERCH", "label": "Merchandise Cube", "dims": [1.2, 1.2, 1.15], "weight_kg": 150,
     "stop_id": "ber", "allowed_orientations": ["LWH", "WLH", "LHW"], "max_stack_kg": 90,
     "forbidden_neighbors": [], "notes": "可承压但重心较高。"},
    {"id": "BACKLINE", "label": "Berlin Backline", "dims": [1.5, 0.9, 1.0], "weight_kg": 320,
     "stop_id": "ber", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 220,
     "forbidden_neighbors": [], "notes": ""},

    # Paris / stop 3.
    {"id": "VIDEOWALL", "label": "Video Wall", "dims": [2.1, 1.25, 1.0], "weight_kg": 470,
     "stop_id": "par", "allowed_orientations": ["LWH", "WLH", "LHW"], "max_stack_kg": 180,
     "forbidden_neighbors": [], "notes": "大屏箱，长度或高度可交换。"},
    {"id": "FOH-PAR", "label": "Paris FOH Rack", "dims": [1.25, 1.0, 1.55], "weight_kg": 350,
     "stop_id": "par", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 0,
     "forbidden_neighbors": [], "notes": "立架，顶部禁压。"},
    {"id": "AMP", "label": "Power Amp Rack", "dims": [1.2, 0.8, 1.1], "weight_kg": 400,
     "stop_id": "par", "allowed_orientations": ["LWH", "WLH"], "max_stack_kg": 240,
     "forbidden_neighbors": [], "notes": "重型功放。"},
    {"id": "DRUM", "label": "Drum Hardware", "dims": [1.4, 0.8, 0.85], "weight_kg": 260,
     "stop_id": "par", "allowed_orientations": ["LWH", "WLH", "LHW"], "max_stack_kg": 160,
     "forbidden_neighbors": [], "notes": ""},
    {"id": "SPARE", "label": "Spare Parts", "dims": [0.8, 0.6, 0.5], "weight_kg": 70,
     "stop_id": "par", "allowed_orientations": ["LWH", "WLH", "LHW", "WHL", "HLW", "HWL"],
     "max_stack_kg": 200, "forbidden_neighbors": [], "notes": "小件。"},
]

# Friction / lashing metadata added to each flight case.  ``lash_faces`` is
# the subset of post-orientation world faces (-x tail side ...) that accept
# hooks; ``no_strap_zones`` are local AABBs where straps must not press
# (vents, screens, handles).
_CASE_EXTRA = {
    "BASS-A": {"friction": 0.40, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "BASS-B": {"friction": 0.40, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "FOH-L": {"friction": 0.35, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "FOH-R": {"friction": 0.35, "lash_faces": ["-x", "-y", "+y"]},  # +x 面无扣
    "CON-MON": {"friction": 0.45, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "CON-CAT": {"friction": 0.45, "lash_faces": ["-x", "+x", "-y", "+y"],
                "no_strap_zones": [{"x": 0.0, "y": 0.30, "z": 0.0, "dx": 0.9, "dy": 0.4, "dz": 0.55,
                                    "label": "网口面板禁压区"}]},
    "BER-LIGHT": {"friction": 0.30, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "BER-WARD": {"friction": 0.35, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "MERCH": {"friction": 0.35, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "BACKLINE": {"friction": 0.40, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "VIDEOWALL": {"friction": 0.35, "lash_faces": ["-x", "+x", "-y", "+y"],
                  "no_strap_zones": [{"x": 0.0, "y": 0.0, "z": 0.7, "dx": 2.1, "dy": 1.25, "dz": 0.3,
                                      "label": "屏幕检修窗禁压区"}]},
    "FOH-PAR": {"friction": 0.30, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "AMP": {"friction": 0.35, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "DRUM": {"friction": 0.35, "lash_faces": ["-x", "+x", "-y", "+y"]},
    "SPARE": {"friction": 0.40, "lash_faces": ["-x", "+x", "-y", "+y"]},
}
for _c in CASES:
    _c.update(deepcopy(_CASE_EXTRA.get(_c["id"], {})))

# Venue-handling metadata: caster mode, min turning radius, registered crew and
# per-person push limit.  Paris has a 4% ramp (effort ×1.5), so the heaviest
# Paris cases register a larger crew.
_HANDLING_EXTRA = {
    "VIDEOWALL": {"min_turn_radius": 1.5, "crew": 4},
    "AMP": {"crew": 3},
    "FOH-PAR": {"crew": 3},
}
for _c in CASES:
    _c["handling"] = {
        "caster_mode": "swivel4",
        "min_turn_radius": 1.2,
        "crew": 2,
        "push_limit_kg": 250,
        **_HANDLING_EXTRA.get(_c["id"], {}),
    }

TRUCK = {
    "id": "truck-18t",
    "name": "18t 巡演卡车",
    "length": 6.0,
    "width": 2.6,
    "height": 2.4,
    "floor_limit_kg_m2": 1200,
    "floor_point_limit_kg": 350,
    "gvw_limit_kg": 12500,
    "door": {"width": 2.5, "height": 2.2, "sill": 0},
    "accel": {"forward": 0.8, "rearward": 0.5, "lateral": 0.5, "up": 0.3, "down": 1.0},
    "strap_defaults": {"capacity_kg": 1000, "pretension_kg": 200},
    "anchors": [
        # Floor D-ring rows (z=0): four x stations × left/right.
        {"id": "F1L", "label": "地板 F1 左", "surface": "floor", "x": 0.25, "y": 0.15, "z": 0.0,
         "capacity_kg": 1000, "group": "", "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F1R", "label": "地板 F1 右", "surface": "floor", "x": 0.25, "y": 2.45, "z": 0.0,
         "capacity_kg": 1000, "group": "", "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F2L", "label": "地板 F2 左", "surface": "floor", "x": 2.0, "y": 0.15, "z": 0.0,
         "capacity_kg": 1000, "group": "rail-mid", "group_capacity_kg": 1800,
         "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F2R", "label": "地板 F2 右", "surface": "floor", "x": 2.0, "y": 2.45, "z": 0.0,
         "capacity_kg": 1000, "group": "rail-mid", "group_capacity_kg": 1800,
         "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F3L", "label": "地板 F3 左", "surface": "floor", "x": 4.0, "y": 0.15, "z": 0.0,
         "capacity_kg": 1000, "group": "", "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F3R", "label": "地板 F3 右", "surface": "floor", "x": 4.0, "y": 2.45, "z": 0.0,
         "capacity_kg": 1000, "group": "", "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F4L", "label": "地板 F4 左", "surface": "floor", "x": 5.75, "y": 0.15, "z": 0.0,
         "capacity_kg": 1000, "group": "", "directions": ["+x", "-x", "+y", "-y", "+z"]},
        {"id": "F4R", "label": "地板 F4 右", "surface": "floor", "x": 5.75, "y": 2.45, "z": 0.0,
         "capacity_kg": 1000, "group": "", "directions": ["+x", "-x", "+y", "-y", "+z"]},
        # Side wall lashing rails, three stations each.
        {"id": "WL1", "label": "左墙轨 1", "surface": "wall_l", "x": 0.8, "y": 0.0, "z": 0.65,
         "capacity_kg": 800, "group": "", "directions": ["-y", "+x", "-x"]},
        {"id": "WL2", "label": "左墙轨 2", "surface": "wall_l", "x": 3.0, "y": 0.0, "z": 0.65,
         "capacity_kg": 800, "group": "", "directions": ["-y", "+x", "-x"]},
        {"id": "WL3", "label": "左墙轨 3", "surface": "wall_l", "x": 5.2, "y": 0.0, "z": 0.65,
         "capacity_kg": 500, "group": "", "directions": ["-y"]},
        {"id": "WR1", "label": "右墙轨 1", "surface": "wall_r", "x": 0.8, "y": 2.6, "z": 0.65,
         "capacity_kg": 800, "group": "", "directions": ["+y", "+x", "-x"]},
        {"id": "WR2", "label": "右墙轨 2", "surface": "wall_r", "x": 3.0, "y": 2.6, "z": 0.65,
         "capacity_kg": 800, "group": "", "directions": ["+y", "+x", "-x"]},
        {"id": "WR3", "label": "右墙轨 3", "surface": "wall_r", "x": 5.2, "y": 2.6, "z": 0.65,
         "capacity_kg": 800, "group": "", "directions": ["+y"]},
        # Front wall lugs near the cab.
        {"id": "FR-L", "label": "前墙左下", "surface": "front", "x": 6.0, "y": 0.5, "z": 0.55,
         "capacity_kg": 1000, "group": "", "directions": ["+x"]},
        {"id": "FR-R", "label": "前墙右下", "surface": "front", "x": 6.0, "y": 2.1, "z": 0.55,
         "capacity_kg": 1000, "group": "", "directions": ["+x"]},
    ],
    "axles": [
        {"name": "后轴", "position": 1.0, "tare_kg": 2500, "capacity_kg": 7500},
        {"name": "前轴", "position": 6.0, "tare_kg": 2500, "capacity_kg": 4000},
    ],
}

# Lanes approximately 1.3 / 0.7 / 0.6 m.  This unsafe plan pushes the first stop
# cargo forward and deliberately leaves Berlin blockers at x=0.
BAD_PLACEMENTS = [
    {"case_id": "BASS-A", "x": 4.30, "y": 0.00, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "BASS-B", "x": 4.30, "y": 1.30, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "FOH-L", "x": 3.05, "y": 0.00, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "FOH-R", "x": 3.05, "y": 2.00, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "CON-MON", "x": 4.30, "y": 0.05, "z": 0.95, "orientation": "LWH", "locked": False},
    {"case_id": "CON-CAT", "x": 5.00, "y": 1.45, "z": 1.05, "orientation": "LWH", "locked": False},

    {"case_id": "BER-WARD", "x": 0.00, "y": 0.00, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "MERCH", "x": 0.00, "y": 1.50, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "BACKLINE", "x": 0.00, "y": 1.30, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "BER-LIGHT", "x": 1.55, "y": 1.50, "z": 0, "orientation": "LWH", "locked": False},

    {"case_id": "VIDEOWALL", "x": 2.60, "y": 0.00, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "FOH-PAR", "x": 1.80, "y": 2.00, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "AMP", "x": 1.50, "y": 0.00, "z": 1.00, "orientation": "LWH", "locked": False},
    {"case_id": "DRUM", "x": 3.00, "y": 1.30, "z": 0, "orientation": "LWH", "locked": False},
    {"case_id": "SPARE", "x": 4.75, "y": 1.30, "z": 0, "orientation": "LWH", "locked": False},
]


def _anchor_end(aid):
    return {"kind": "anchor", "id": aid, "face": "", "u": 0.5, "v": 0.5}


def _case_end(cid, face, u=0.5, v=0.5):
    return {"kind": "case", "id": cid, "face": face, "u": u, "v": v}


# Unlocked sample straps.  They deliberately demonstrate every check once the
# user locks them: L001 overpowers the 500 kg WL3 anchor, L002 threads through
# two later boxes, L003 hooks a face FOH-R does not allow, L004 is a case-to-
# case link whose anchor (FOH-L) is gone after the Amsterdam unload.
BAD_LASHINGS = [
    {"id": "L001", "label": "Bass A → 左墙轨3（超载演示）", "from": _anchor_end("WL3"),
     "to": _case_end("BASS-A", "+y", 0.3, 0.55), "pretension_kg": 700, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
    {"id": "L002", "label": "Bass A 斜拉（路径穿箱演示）", "from": _anchor_end("F1R"),
     "to": _case_end("BASS-A", "+y", 0.2, 0.8), "pretension_kg": 250, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
    {"id": "L003", "label": "FOH-R 禁面挂钩演示", "from": _anchor_end("F4R"),
     "to": _case_end("FOH-R", "+x", 0.5, 0.5), "pretension_kg": 200, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
    {"id": "L004", "label": "FOH-L ↔ Drum 箱间连接",
     "from": _case_end("FOH-L", "+x", 0.5, 0.7), "to": _case_end("DRUM", "-y", 0.5, 0.6),
     "pretension_kg": 250, "capacity_kg": 1000, "locked": False, "review_signature": ""},
    {"id": "L005", "label": "Bass B 右侧拉", "from": _anchor_end("F3R"),
     "to": _case_end("BASS-B", "+y", 0.5, 0.5), "pretension_kg": 200, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
    {"id": "L006", "label": "大屏墙前固定", "from": _anchor_end("FR-R"),
     "to": _case_end("VIDEOWALL", "+x", 0.5, 0.5), "pretension_kg": 200, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
    {"id": "L007", "label": "功放高位下拉", "from": _anchor_end("F2L"),
     "to": _case_end("AMP", "-y", 0.5, 0.9), "pretension_kg": 250, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
    {"id": "L008", "label": "鼓箱草拟绑带", "from": _anchor_end("F2R"),
     "to": _case_end("DRUM", "+y", 0.5, 0.5), "pretension_kg": 200, "capacity_kg": 1000,
     "locked": False, "review_signature": ""},
]

BAD_STATE = {
    "name": "示例：会触发前轴超载与首站阻挡",
    "truck": deepcopy(TRUCK),
    "stops": deepcopy(STOPS),
    "cases": deepcopy(CASES),
    "placements": deepcopy(BAD_PLACEMENTS),
    "lashings": deepcopy(BAD_LASHINGS),
}

EMPTY_STATE = {
    "name": "空方案",
    "truck": deepcopy(TRUCK),
    "stops": deepcopy(STOPS),
    "cases": deepcopy(CASES),
    "placements": [],
    "lashings": [],
}
