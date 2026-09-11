"""Human-readable exports and dependency-free SVG rendering."""
from __future__ import annotations

import html
import math
from typing import Any, Dict, List

from planning import boxes_from, loading_sequence, norm_state, now_iso

STOP_COLORS = ["#5087e8", "#f59e0b", "#10b981", "#a855f7", "#ef4444", "#06b6d4"]


def stop_color(index: int) -> str:
    return STOP_COLORS[index % len(STOP_COLORS)]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _svg_header(width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Arial, sans-serif">'
    )


def _svg_defs() -> str:
    return '''
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#334155"/>
      </marker>
      <pattern id="hatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="8" stroke="#cbd5e1" stroke-width="3"/>
      </pattern>
    </defs>'''


def render_top_svg(state: Dict[str, Any], report: Dict[str, Any] | None = None, scale: int = 90) -> str:
    state = norm_state(state)
    truck = state["truck"]
    stops = state["stops"]
    stop_rank = {s["id"]: i for i, s in enumerate(stops)}
    boxes = boxes_from(state)
    margin_l, margin_t = 70, 50
    width = int(math.ceil(truck["length"] * scale)) + margin_l + 30
    height = int(math.ceil(truck["width"] * scale)) + margin_t + 70
    case_issues = (report or {}).get("case_issues", {})
    parts = [_svg_header(width, height), _svg_defs()]
    parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc"/>')
    rx, ry = margin_l, margin_t
    parts.append(f'<rect x="{rx}" y="{ry}" width="{truck["length"] * scale:.1f}" '
                 f'height="{truck["width"] * scale:.1f}" fill="#fff" stroke="#0f172a" stroke-width="2"/>')
    door_y0 = ry + (truck["width"] - truck["door"]["width"]) / 2 * scale
    parts.append(f'<line x1="{rx}" y1="{door_y0:.1f}" x2="{rx}" '
                 f'y2="{door_y0 + truck["door"]["width"] * scale:.1f}" stroke="#059669" stroke-width="7"/>')
    parts.append(f'<text x="{rx+8}" y="{ry-14}" font-size="16" font-weight="700" fill="#0f172a">尾门</text>')
    parts.append(f'<line x1="{rx+truck["length"]*scale-8}" y1="{ry+truck["width"]*scale/2}" '
                 f'x2="{rx+truck["length"]*scale-25}" y2="{ry+truck["width"]*scale/2}" '
                 f'stroke="#334155" stroke-width="2" marker-end="url(#arrow)"/>')
    parts.append(f'<text x="{rx+truck["length"]*scale-70}" y="{ry-14}" font-size="14">车头</text>')

    # Draw low boxes before higher stacks; add subtle z offset not to obscure.
    for b in sorted(boxes, key=lambda q: q["z"]):
        rank = stop_rank.get(b["stop_id"], 0)
        x = rx + b["x"] * scale + b["z"] * 4
        y = ry + b["y"] * scale - b["z"] * 3
        problems = case_issues.get(b["id"], {})
        stroke = "#dc2626" if problems.get("errors") else ("#d97706" if problems.get("warnings") else "#1e293b")
        sw = 2.5 if stroke != "#1e293b" else 1
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{b["dx"]*scale:.1f}" height="{b["dy"]*scale:.1f}" '
            f'rx="3" fill="{stop_color(rank)}" fill-opacity="0.72" stroke="{stroke}" stroke-width="{sw}"/>'
        )
        label = esc(b["label"])
        if b["dx"] * scale > 65 and b["dy"] * scale > 25:
            parts.append(f'<text x="{x+5:.1f}" y="{y+19:.1f}" font-size="12" fill="#0f172a" font-weight="700">{label}</text>')
            parts.append(f'<text x="{x+5:.1f}" y="{y+35:.1f}" font-size="11" fill="#334155">z={b["z"]:.2f}</text>')
    # Grid/legend
    for i, stop in enumerate(stops):
        parts.append(f'<rect x="{rx+i*150}" y="{height-35}" width="14" height="14" fill="{stop_color(i)}"/>')
        parts.append(f'<text x="{rx+i*150+20}" y="{height-23}" font-size="13">{esc(stop["city"])}</text>')
    parts.append('</svg>')
    return "".join(parts)


def render_side_svg(state: Dict[str, Any], report: Dict[str, Any] | None = None, scale: int = 90) -> str:
    state = norm_state(state)
    truck, stops = state["truck"], state["stops"]
    boxes = boxes_from(state)
    stop_rank = {s["id"]: i for i, s in enumerate(stops)}
    margin_l, margin_t = 70, 50
    width = int(truck["length"] * scale) + margin_l + 30
    height = int(truck["height"] * scale) + margin_t + 85
    case_issues = (report or {}).get("case_issues", {})
    parts = [_svg_header(width, height), _svg_defs()]
    parts.append(f'<rect width="{width}" height="{height}" fill="#f8fafc"/>')
    rx, ry = margin_l, margin_t
    parts.append(f'<rect x="{rx}" y="{ry}" width="{truck["length"]*scale:.1f}" height="{truck["height"]*scale:.1f}" fill="url(#hatch)" fill-opacity=".25" stroke="#0f172a" stroke-width="2"/>')
    door = truck["door"]
    dy0, dz0 = ry + 20, ry + (truck["height"] - door["sill"] - door["height"]) * scale
    parts.append(f'<rect x="{rx-4}" y="{dz0:.1f}" width="8" height="{door["height"]*scale:.1f}" fill="#059669"/>')
    for axle in truck["axles"]:
        ax = rx + axle["position"] * scale
        parts.append(f'<circle cx="{ax:.1f}" cy="{ry+truck["height"]*scale+28}" r="13" fill="none" stroke="#334155" stroke-width="3"/>')
        parts.append(f'<text x="{ax-18:.1f}" y="{ry+truck["height"]*scale+58}" font-size="12">{esc(axle["name"])}轴</text>')
    cg = (report or {}).get("metrics", {}).get("x")
    if cg is not None:
        cgx = rx + cg * scale
        parts.append(f'<line x1="{cgx:.1f}" y1="{ry-5}" x2="{cgx:.1f}" y2="{ry+truck["height"]*scale+5}" stroke="#dc2626" stroke-dasharray="5 4" stroke-width="2"/>')
        parts.append(f'<text x="{cgx-24:.1f}" y="{ry-12}" font-size="12" fill="#dc2626">重心</text>')
    # Side projection: draw highest first with transparency so low/far boxes show.
    for b in sorted(boxes, key=lambda q: -q["z"]):
        rank = stop_rank.get(b["stop_id"], 0)
        x, y = rx + b["x"] * scale, ry + (truck["height"] - b["z"] - b["dz"]) * scale
        problems = case_issues.get(b["id"], {})
        stroke = "#dc2626" if problems.get("errors") else ("#d97706" if problems.get("warnings") else "#1e293b")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{b["dx"]*scale:.1f}" height="{b["dz"]*scale:.1f}" '
                     f'fill="{stop_color(rank)}" fill-opacity=".58" stroke="{stroke}" stroke-width="1.4"/>')
    parts.append('</svg>')
    return "".join(parts)


def render_tail_svg(state: Dict[str, Any], report: Dict[str, Any] | None = None, scale: int = 120) -> str:
    state = norm_state(state)
    truck, stops = state["truck"], state["stops"]
    boxes = boxes_from(state)
    stop_rank = {s["id"]: i for i, s in enumerate(stops)}
    margin_l, margin_t = 70, 50
    width = int(truck["width"] * scale) + margin_l + 40
    height = int(truck["height"] * scale) + margin_t + 80
    case_issues = (report or {}).get("case_issues", {})
    parts = [_svg_header(width, height), _svg_defs()]
    parts.append(f'<rect width="{width}" height="{height}" fill="#f8fafc"/>')
    rx, ry = margin_l, margin_t
    parts.append(f'<rect x="{rx}" y="{ry}" width="{truck["width"]*scale:.1f}" height="{truck["height"]*scale:.1f}" fill="#fff" stroke="#0f172a" stroke-width="2"/>')
    door = truck["door"]
    y0 = rx + (truck["width"] - door["width"]) / 2 * scale
    z0 = ry + (truck["height"] - door["sill"] - door["height"]) * scale
    parts.append(f'<rect x="{y0:.1f}" y="{z0:.1f}" width="{door["width"]*scale:.1f}" height="{door["height"]*scale:.1f}" fill="#d1fae5" stroke="#059669" stroke-width="3" stroke-dasharray="8 5"/>')
    parts.append(f'<text x="{y0+8:.1f}" y="{z0+22:.1f}" font-size="14" fill="#047857" font-weight="700">门洞</text>')
    # Tail projection uses nearest (smallest x) box on top.
    for b in sorted(boxes, key=lambda q: -q["x"]):
        rank = stop_rank.get(b["stop_id"], 0)
        opacity = 0.22 + 0.55 * max(0.0, min(1.0, 1 - b["x"] / truck["length"]))
        x = rx + b["y"] * scale
        y = ry + (truck["height"] - b["z"] - b["dz"]) * scale
        problems = case_issues.get(b["id"], {})
        stroke = "#dc2626" if problems.get("errors") else ("#d97706" if problems.get("warnings") else "#334155")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{b["dy"]*scale:.1f}" height="{b["dz"]*scale:.1f}" '
                     f'fill="{stop_color(rank)}" fill-opacity="{opacity:.2f}" stroke="{stroke}" stroke-width="1.4"/>')
        if b["dy"] * scale > 70 and b["dz"] * scale > 34:
            parts.append(f'<text x="{x+5:.1f}" y="{y+20:.1f}" font-size="12" font-weight="700">{esc(b["label"])}</text>')
    parts.append(f'<text x="{rx}" y="{height-30}" font-size="13" fill="#475569">颜色越实，箱体越靠近尾门；红色/琥珀色描边表示错误/余量告警。</text>')
    parts.append('</svg>')
    return "".join(parts)


def render_layer_svgs(state: Dict[str, Any], report: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    state = norm_state(state)
    levels = sorted({round(b["z"], 3) for b in boxes_from(state)})
    output = []
    for z in levels:
        layer = deepcopy_state_at_z(state, z)
        svg = render_top_svg(layer, report, scale=100)
        svg = svg.replace("</svg>", f'<text x="70" y="24" font-size="16" font-weight="700">分层 z={z:.2f} m</text></svg>')
        output.append({"z": z, "svg": svg})
    return output


def deepcopy_state_at_z(state: Dict[str, Any], z: float) -> Dict[str, Any]:
    # Local import avoids requiring copy at module import callers.
    from copy import deepcopy
    copied = deepcopy(state)
    copied["placements"] = [p for p in copied["placements"] if abs(float(p["z"]) - z) < 1e-6]
    return copied


def loading_markdown(state: Dict[str, Any]) -> str:
    state = norm_state(state)
    seq = loading_sequence(state)
    lines = [f"# 装车顺序：{state['name']}", "", f"生成时间：{now_iso()}", "",
             "| 步骤 | 航空箱 | 卸货站 | 方向 | x/y/z (m) | 外廓 (m) |",
             "|---:|---|---|---|---|---|"]
    for row in seq:
        lines.append(
            f"| {row['step']} | {row['label']} | {row['city']} | {row['orientation']} | "
            f"{row['x']:.2f}/{row['y']:.2f}/{row['z']:.2f} | "
            f"{row['dims'][0]:.2f}×{row['dims'][1]:.2f}×{row['dims'][2]:.2f} |"
        )
    return "\n".join(lines) + "\n"


def unloading_markdown(state: Dict[str, Any], report: Dict[str, Any]) -> str:
    state = norm_state(state)
    cmap = {c["id"]: c for c in state["cases"]}
    lines = [f"# 各站卸货步骤：{state['name']}", "", f"倒箱次数：**{report['metrics']['rehandle_count']}**", ""]
    current = None
    for step in report["unloading"]["steps"]:
        if step["city"] != current:
            current = step["city"]
            lines += ["", f"## {current}", "", "| 站内步骤 | 航空箱 | 需先临时移开的后站箱 |", "|---:|---|---|"]
        temps = ", ".join(step["temporary_move_case_ids"]) or "—"
        lines.append(f"| {step['step']} | {step['label']} | {temps} |")
    if report["issues"]:
        lines += ["", "## 复算问题", ""]
        for item in report["issues"]:
            lines.append(f"- **{item['severity']} / {item['code']}** {item['message']}")
    return "\n".join(lines) + "\n"


def layers_markdown(state: Dict[str, Any], report: Dict[str, Any]) -> str:
    state = norm_state(state)
    cmap = {c["id"]: c for c in state["cases"]}
    boxes = boxes_from(state)
    lines = [f"# 逐层 SVG 索引：{state['name']}", "", "每个 `<svg>...</svg>` 块可直接保存为 .svg 文件。", ""]
    for layer in render_layer_svgs(state, report):
        labels = [next((b["label"] for b in boxes if b["id"] == cid), cid)
                  for cid in layer["z"] and next(l["case_ids"] for l in report["layers"] if abs(l["z"] - layer["z"]) < 1e-6)]
        lines += [f"## z = {layer['z']:.2f} m", "", ", ".join(labels), "", layer["svg"], ""]
    return "\n".join(lines) + "\n"
