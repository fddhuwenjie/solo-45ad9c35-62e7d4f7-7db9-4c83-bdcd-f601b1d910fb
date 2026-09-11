"""Sample dataset.

The initial snapshot is deliberately unsafe:
- six Amsterdam first-stop cases are pushed toward the front wall;
- later Berlin wardrobe/merchandise blocks the rear tailgate lanes;
- a FOH rack is stacked/adjacent in a way that violates pressure/forbidden
  neighbour assumptions;
- the resulting front-axle load exceeds the configured capacity.
"""
from __future__ import annotations

from copy import deepcopy

STOPS = [
    {"id": "ams", "city": "Amsterdam", "venue": "Melkweg"},
    {"id": "ber", "city": "Berlin", "venue": "Columbiahalle"},
    {"id": "par", "city": "Paris", "venue": "Le Bataclan"},
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
    {"id": "BER-LIGHT", "label": "Berlin Lighting Rack", "dims": [1.25, 0.8, 1.7], "weight_kg": 270,
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

BAD_STATE = {
    "name": "示例：会触发前轴超载与首站阻挡",
    "truck": deepcopy(TRUCK),
    "stops": deepcopy(STOPS),
    "cases": deepcopy(CASES),
    "placements": deepcopy(BAD_PLACEMENTS),
}

EMPTY_STATE = {
    "name": "空方案",
    "truck": deepcopy(TRUCK),
    "stops": deepcopy(STOPS),
    "cases": deepcopy(CASES),
    "placements": [],
}
