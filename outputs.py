"""Human-readable exports and dependency-free SVG rendering."""
from __future__ import annotations

import html
import math
from typing import Any, Dict, List, Optional, Tuple

from planning import boxes_from, loading_sequence, norm_state, now_iso

STOP_COLORS = ["#5087e8", "#f59e0b", "#10b981", "#a855f7", "#ef4444", "#06b6d4"]

# Strap status colours shared by every SVG view.
STRAP_COLORS = {"ok": "#059669", "over": "#dc2626", "warn": "#d97706",
                "draft": "#64748b", "pending": "#7c3aed"}
ANCHOR_OVER = "#dc2626"
ANCHOR_WARN = "#d97706"
ANCHOR_OK = "#2563eb"


def stop_color(index: int) -> str:
    return STOP_COLORS[index % len(STOP_COLORS)]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _strap_color(row: Dict[str, Any]) -> str:
    if not row.get("locked", False):
        return STRAP_COLORS["draft"]
    if row.get("pending"):
        return STRAP_COLORS["pending"]
    if row.get("utilization", 0.0) > 1.0 or "LASH_OVERLOAD" in row.get("codes", []):
        return STRAP_COLORS["over"]
    if row.get("codes") or row.get("utilization", 0.0) >= 0.9:
        return STRAP_COLORS["warn"]
    return STRAP_COLORS["ok"]


def _anchor_color(anchor_rows: Dict[str, Any], anchor_id: str) -> str:
    row = anchor_rows.get(anchor_id)
    if not row:
        return ANCHOR_OK
    cap = row.get("capacity_kg", 0.0)
    load = row.get("load_kg", 0.0)
    if cap and load > cap + EPS_LASH:
        return ANCHOR_OVER
    if cap and load / cap >= 0.9:
        return ANCHOR_WARN
    return ANCHOR_OK


EPS_LASH = 1e-6


def _endpoint_world(end: Optional[Dict[str, Any]], anchor_map: Dict[str, Any],
                    boxes: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    if not end:
        return None
    if end["kind"] == "anchor":
        a = anchor_map.get(end["id"])
        if not a:
            return None
        return (float(a["x"]), float(a["y"]), float(a["z"]))
    b = boxes.get(end["id"])
    if not b:
        return None
    face = end.get("face", "")
    x, y, z = b["x"], b["y"], b["z"]
    u, v = float(end.get("u", 0.5)), float(end.get("v", 0.5))
    if face == "-x":
        return (x, y + u * b["dy"], z + v * b["dz"])
    if face == "+x":
        return (x + b["dx"], y + u * b["dy"], z + v * b["dz"])
    if face == "-y":
        return (x + u * b["dx"], y, z + v * b["dz"])
    if face == "+y":
        return (x + u * b["dx"], y + b["dy"], z + v * b["dz"])
    return (x + b["dx"] / 2, y + b["dy"] / 2, z + b["dz"] / 2)


def _lashing_overlays(state: Dict[str, Any], report: Optional[Dict[str, Any]]):
    """Resolve every strap to world endpoints and a status colour."""
    anchors = {a["id"]: a for a in state["truck"]["anchors"]}
    boxes = {b["id"]: b for b in boxes_from(state)}
    lashing = (report or {}).get("lashing") or {}
    strap_rows = {s["id"]: s for s in lashing.get("straps", [])}
    anchor_rows = {a["id"]: a for a in lashing.get("anchors", [])}
    overlays = []
    for lash in state.get("lashings", []):
        p0 = _endpoint_world(lash["from"], anchors, boxes)
        p1 = _endpoint_world(lash["to"], anchors, boxes)
        if not p0 or not p1:
            continue
        row = strap_rows.get(lash["id"])
        color = _strap_color(row) if row else STRAP_COLORS["draft"]
        overlays.append({"id": lash["id"], "label": lash.get("label", lash["id"]),
                         "locked": bool(lash.get("locked")), "p0": p0, "p1": p1,
                         "color": color})
    return anchors, anchor_rows, overlays


def _scheme_badge(report: Optional[Dict[str, Any]]) -> str:
    lashing = (report or {}).get("lashing") or {}
    ver = lashing.get("scheme_version")
    if not ver:
        return ""
    locked = lashing.get("locked_count", 0)
    total = lashing.get("total_count", 0)
    pending = len(lashing.get("pending_ids", []))
    text = f"系固方案 {ver[:8]} · 锁定 {locked}/{total} 条"
    if pending:
        text += f" · {pending} 条待复核"
    return text

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

    # Anchors and straps share the same scheme version as lists/steps.
    anchors, anchor_rows, overlays = _lashing_overlays(state, report)
    for ov in overlays:
        p0, p1 = ov["p0"], ov["p1"]
        x0, y0 = rx + p0[0] * scale, ry + p0[1] * scale
        x1, y1 = rx + p1[0] * scale, ry + p1[1] * scale
        dash = "" if ov["locked"] else ' stroke-dasharray="7 4"'
        parts.append(
            f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
            f'stroke="{ov["color"]}" stroke-width="2.2" opacity="0.9"{dash}/>'
        )
    for a in anchors.values():
        ax, ay = rx + a["x"] * scale, ry + a["y"] * scale
        color = _anchor_color(anchor_rows, a["id"])
        r = 5 if a.get("surface") == "floor" else 4
        parts.append(f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="{r}" fill="#fff" stroke="{color}" stroke-width="2.5"/>')
        parts.append(f'<text x="{ax+6:.1f}" y="{ay+3:.1f}" font-size="9" fill="{color}">{esc(a["id"])}</text>')
    badge = _scheme_badge(report)
    if badge:
        parts.append(f'<text x="{rx}" y="{height-12}" font-size="11" fill="#475569">{esc(badge)}</text>')
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
    anchors, anchor_rows, overlays = _lashing_overlays(state, report)
    for ov in overlays:
        p0, p1 = ov["p0"], ov["p1"]
        x0, y0 = rx + p0[0] * scale, ry + (truck["height"] - p0[2]) * scale
        x1, y1 = rx + p1[0] * scale, ry + (truck["height"] - p1[2]) * scale
        dash = "" if ov["locked"] else ' stroke-dasharray="7 4"'
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                     f'stroke="{ov["color"]}" stroke-width="2.2" opacity=".9"{dash}/>')
    for a in anchors.values():
        ax, ay = rx + a["x"] * scale, ry + (truck["height"] - a["z"]) * scale
        color = _anchor_color(anchor_rows, a["id"])
        parts.append(f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="4.5" fill="#fff" stroke="{color}" stroke-width="2.5"/>')
    badge = _scheme_badge(report)
    if badge:
        parts.append(f'<text x="{rx}" y="{height-12}" font-size="11" fill="#475569">{esc(badge)}</text>')
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
    anchors, anchor_rows, overlays = _lashing_overlays(state, report)
    for ov in overlays:
        p0, p1 = ov["p0"], ov["p1"]
        x0, y0 = rx + p0[1] * scale, ry + (truck["height"] - p0[2]) * scale
        x1, y1 = rx + p1[1] * scale, ry + (truck["height"] - p1[2]) * scale
        dash = "" if ov["locked"] else ' stroke-dasharray="7 4"'
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                     f'stroke="{ov["color"]}" stroke-width="2.2" opacity=".9"{dash}/>')
    for a in anchors.values():
        ax, ay = rx + a["y"] * scale, ry + (truck["height"] - a["z"]) * scale
        color = _anchor_color(anchor_rows, a["id"])
        parts.append(f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="4.5" fill="#fff" stroke="{color}" stroke-width="2.5"/>')
    parts.append(f'<text x="{rx}" y="{height-30}" font-size="13" fill="#475569">颜色越实，箱体越靠近尾门；红色/琥珀色描边表示错误/余量告警。</text>')
    badge = _scheme_badge(report)
    if badge:
        parts.append(f'<text x="{rx}" y="{height-10}" font-size="11" fill="#475569">{esc(badge)}</text>')
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
    lashing = report.get("lashing", {})
    ver = lashing.get("scheme_version", "")
    lines = [f"# 各站卸货步骤：{state['name']}", "", f"倒箱次数：**{report['metrics']['rehandle_count']}**", ""]
    if ver:
        lines += [f"系固方案版本：`{ver[:8]}`（与系固清单、SVG 同源）", ""]
    current = None
    for step in report["unloading"]["steps"]:
        if step["city"] != current:
            current = step["city"]
            lines += ["", f"## {current}", "", "| 站内步骤 | 航空箱 | 需先临时移开的后站箱 |", "|---:|---|---|"]
        temps = ", ".join(step["temporary_move_case_ids"]) or "—"
        lines.append(f"| {step['step']} | {step['label']} | {temps} |")
    # Per-station strap release steps from the same scheme version.
    if lashing.get("release_steps"):
        lines += ["", "## 分站解带步骤（高位先解）", ""]
        for rs in lashing["release_steps"]:
            lines.append(f"### {rs['city']}")
            if not rs["lashings"]:
                lines.append("- 本站无需解绑带")
            for i, l in enumerate(rs["lashings"], 1):
                lock = "🔒" if l["locked"] else "草稿"
                lines.append(f"{i}. {lock} **{l['label']}**（{l['id']}）→ {', '.join(l['case_labels'])}")
            lines.append("")
    # First failure across legs + suggestion.
    ff = lashing.get("first_failure")
    if ff:
        lines += ["## 首个分站系固失效", "",
                  f"- 阶段：{ff['stage_title']}",
                  f"- 失效连接：{ff['label']}（{ff['id']}）· `{ff['code']}`"]
        sug = ff.get("suggestion")
        if sug:
            lines.append(f"- 增绑建议：{sug['message']}")
        lines.append("")
    if report["issues"]:
        lines += ["", "## 复算问题", ""]
        for item in report["issues"]:
            lines.append(f"- **{item['severity']} / {item['code']}** {item['message']}")
    return "\n".join(lines) + "\n"


def lashing_markdown(state: Dict[str, Any], report: Dict[str, Any]) -> str:
    state = norm_state(state)
    lashing = report.get("lashing", {})
    ver = lashing.get("scheme_version", "")
    lines = [f"# 系固清单：{state['name']}", ""]
    if ver:
        lines += [f"方案版本：`{ver[:8]}`（与分站解带步骤、三视图 SVG 同源）", ""]
    lines += ["## 锚点", "",
              "| ID | 名称 | 位置 x/y/z (m) | 额定 kg | 受力 kg | 共用组 | 可用方向 |",
              "|---|---|---|---:|---:|---|---|"]
    anchor_rows = {a["id"]: a for a in lashing.get("anchors", [])}
    for a in state["truck"]["anchors"]:
        row = anchor_rows.get(a["id"], {})
        lines.append(
            f"| {a['id']} | {a.get('label', a['id'])} | {a['x']:.2f}/{a['y']:.2f}/{a['z']:.2f} | "
            f"{a['capacity_kg']:.0f} | {row.get('load_kg', 0):.0f} | "
            f"{a.get('group', '') or '—'} | {','.join(a.get('directions', []))} |"
        )
    lines += ["", "## 绑带", "",
              "| ID | 名称 | 自 | 至 | 预紧/额定 kg | 夹角° | 工作张力 | 状态 | 问题码 |",
              "|---|---|---|---|---:|---:|---:|---|---|"]
    strap_rows = {s["id"]: s for s in lashing.get("straps", [])}
    cmap = {c["id"]: c for c in state["cases"]}
    amap = {a["id"]: a for a in state["truck"]["anchors"]}
    def _ep(ep):
        if ep["kind"] == "anchor":
            a = amap.get(ep["id"])
            return f"锚点 {a.get('label', ep['id']) if a else ep['id']}"
        c = cmap.get(ep["id"])
        return f"箱体 {c.get('label', ep['id']) if c else ep['id']} {ep.get('face','')}"
    for lash in state.get("lashings", []):
        row = strap_rows.get(lash["id"], {})
        status = "待复核" if row.get("pending") else ("已锁定" if lash.get("locked") else "草稿")
        lines.append(
            f"| {lash['id']} | {lash.get('label', lash['id'])} | {_ep(lash['from'])} | {_ep(lash['to'])} | "
            f"{lash['pretension_kg']:.0f}/{lash['capacity_kg']:.0f} | "
            f"{row.get('angle_deg', 0):.0f} | {row.get('tension_kg', 0):.0f} | {status} | "
            f"{','.join(row.get('codes', [])) or '—'} |"
        )
    # Station legs summary.
    lines += ["", "## 分站系固校核", ""]
    for st in lashing.get("stages", []):
        margins = []
        if st.get("min_slip_margin") is not None:
            margins.append(f"防滑 {st['min_slip_margin']:.2f}")
        if st.get("min_tip_margin") is not None:
            margins.append(f"防倾覆 {st['min_tip_margin']:.2f}")
        if st.get("min_lift_margin") is not None:
            margins.append(f"防跳起 {st['min_lift_margin']:.2f}")
        line = f"- **{st['title']}**（剩余 {st.get('remaining_mass_kg', 0):.0f} kg）："
        line += "、".join(margins) if margins else "无载荷"
        if st.get("failures"):
            line += "；失效：" + "、".join(f"{f['label']} {f['code']}" for f in st["failures"][:5])
        lines.append(line)
    ff = lashing.get("first_failure")
    if ff:
        lines += ["", f"**首个失效**：{ff['stage_title']} · {ff['label']}（{ff['code']}）"]
        if ff.get("suggestion"):
            lines.append(f"增绑建议：{ff['suggestion']['message']}")
    return "\n".join(lines) + "\n"



def layers_markdown(state: Dict[str, Any], report: Dict[str, Any]) -> str:
    state = norm_state(state)
    boxes = boxes_from(state)
    layer_case_ids = {
        round(layer["z"], 6): layer["case_ids"]
        for layer in report.get("layers") or layers(boxes)
    }
    lines = [f"# 逐层 SVG 索引：{state['name']}", "", "每个 `<svg>...</svg>` 块可直接保存为 .svg 文件。", ""]
    for layer in render_layer_svgs(state, report):
        z = round(layer["z"], 6)
        labels = [next((b["label"] for b in boxes if b["id"] == cid), cid)
                  for cid in layer_case_ids.get(z, [])]
        lines += [f"## z = {layer['z']:.2f} m", "", ", ".join(labels), "", layer["svg"], ""]
    return "\n".join(lines) + "\n"
