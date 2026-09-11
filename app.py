from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Tuple

from flask import Flask, Response, g, jsonify, render_template, request, send_file
import io

from outputs import (
    layers_markdown,
    loading_markdown,
    render_layer_svgs,
    unloading_markdown,
)
from planning import affected_cases, analyze, auto_arrange, norm_state, now_iso, recompute_json, uid
from sample_data import BAD_STATE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TOUR_LOAD_DB", os.path.join(BASE_DIR, "tour_load.sqlite3"))

app = Flask(__name__, static_folder="static", template_folder="templates")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            current_state TEXT NOT NULL,
            confirmed_state TEXT,
            confirmed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_versions (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            version_no INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL,
            affected_case_ids TEXT NOT NULL DEFAULT '[]',
            report_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_versions_plan ON plan_versions(plan_id, version_no)")
    existing = db.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    if not existing:
        state = norm_state(BAD_STATE)
        plan_id, now = "sample-tour", now_iso()
        db.execute(
            "INSERT INTO plans (id,name,status,current_state,confirmed_state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (plan_id, state["name"], "draft", json.dumps(state, ensure_ascii=False), None, now, now),
        )
        report = analyze(state)
        db.execute(
            "INSERT INTO plan_versions (id,plan_id,version_no,status,reason,state_json,affected_case_ids,report_json,created_at) "
            "VALUES (?,?,1,'draft','内置示例：前轴超载与卸货阻挡',?,?,?,?,?)",
            (uid(), plan_id, json.dumps(state, ensure_ascii=False), json.dumps([], ensure_ascii=False),
             json.dumps(report, ensure_ascii=False), now),
        )
    db.commit()
    db.close()


def parse_state() -> Dict[str, Any]:
    if not request.is_json:
        raise ValueError("请求体必须是 JSON")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("JSON 必须是对象")
    state = data.get("state", data)
    return norm_state(state)


def fetch_plan(plan_id: str) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise KeyError("方案不存在")
    return row


def row_payload(row: sqlite3.Row) -> Dict[str, Any]:
    current = json.loads(row["current_state"])
    confirmed = json.loads(row["confirmed_state"]) if row["confirmed_state"] else None
    latest = get_db().execute(
        "SELECT version_no,status,reason,affected_case_ids,created_at FROM plan_versions "
        "WHERE plan_id=? ORDER BY version_no DESC, created_at DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "state": current,
        "confirmed_state": confirmed,
        "confirmed_at": row["confirmed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest_version": dict(latest) if latest else None,
    }


def insert_version(
    db: sqlite3.Connection, plan_id: str, version_no: int, status: str, reason: str,
    state: Dict[str, Any], affected: list[str] | None = None, report: Dict[str, Any] | None = None,
) -> None:
    report = report or analyze(state)
    db.execute(
        "INSERT INTO plan_versions (id,plan_id,version_no,status,reason,state_json,affected_case_ids,report_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (uid(), plan_id, version_no, status, reason, json.dumps(state, ensure_ascii=False),
         json.dumps(affected or [], ensure_ascii=False), json.dumps(report, ensure_ascii=False), now_iso()),
    )


def save_state_to_plan(plan_id: str, state: Dict[str, Any], reason: str = "手工编辑") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    db = get_db()
    row = fetch_plan(plan_id)
    current = json.loads(row["current_state"])
    report = analyze(state)
    now = now_iso()

    if row["status"] == "confirmed":
        confirmed = json.loads(row["confirmed_state"]) if row["confirmed_state"] else current
        affected = affected_cases(confirmed, state, report)
        version_no = db.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM plan_versions WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]
        db.execute(
            "UPDATE plans SET name=?, status='draft', current_state=?, confirmed_state=?, updated_at=? WHERE id=?",
            (state["name"], json.dumps(state, ensure_ascii=False),
             json.dumps(confirmed, ensure_ascii=False), now, plan_id),
        )
        insert_version(db, plan_id, version_no, "draft", reason, state, affected, report)
    else:
        latest = db.execute(
            "SELECT version_no FROM plan_versions WHERE plan_id=? ORDER BY version_no DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        version_no = (latest["version_no"] if latest else 0) + 1
        db.execute(
            "UPDATE plans SET name=?, current_state=?, updated_at=? WHERE id=?",
            (state["name"], json.dumps(state, ensure_ascii=False), now, plan_id),
        )
        insert_version(db, plan_id, version_no, "draft", reason, state, [], report)
    db.commit()
    return row_payload(fetch_plan(plan_id)), report


@app.errorhandler(ValueError)
def value_error(exc: ValueError):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(KeyError)
def key_error(exc: KeyError):
    return jsonify({"error": str(exc).strip("'")}), 404


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.route("/api/plans")
def list_plans():
    rows = get_db().execute("SELECT * FROM plans ORDER BY updated_at DESC").fetchall()
    return jsonify({"plans": [row_payload(r) for r in rows]})


@app.route("/api/plans/<plan_id>")
def get_plan(plan_id: str):
    return jsonify(row_payload(fetch_plan(plan_id)))


@app.route("/api/plans", methods=["POST"])
def create_plan():
    body = request.get_json(silent=True) or {}
    state = norm_state(body.get("state") or BAD_STATE)
    name = body.get("name") or state["name"]
    state["name"] = name
    plan_id = body.get("id") or uid()
    now = now_iso()
    report = analyze(state)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO plans (id,name,status,current_state,confirmed_state,created_at,updated_at) "
            "VALUES (?,?, 'draft', ?, NULL, ?, ?)",
            (plan_id, name, json.dumps(state, ensure_ascii=False), now, now),
        )
    except sqlite3.IntegrityError:
        raise ValueError("方案 ID 已存在")
    insert_version(db, plan_id, 1, "draft", "创建方案", state, [], report)
    db.commit()
    return jsonify(row_payload(fetch_plan(plan_id))), 201


@app.route("/api/plans/<plan_id>", methods=["PUT", "POST"])
def update_plan(plan_id: str):
    state = parse_state()
    body = request.get_json(silent=True)
    reason = (body.get("reason") if isinstance(body, dict) else "") or "手工编辑"
    plan, report = save_state_to_plan(plan_id, state, reason)
    return jsonify({"plan": plan, "report": report})


@app.route("/api/plans/<plan_id>/confirm", methods=["POST"])
def confirm_plan(plan_id: str):
    body = request.get_json(silent=True) or {}
    row = fetch_plan(plan_id)
    if body.get("state"):
        state = norm_state(body["state"])
        plan, report = save_state_to_plan(plan_id, state, body.get("reason", "确认前保存"))
        row = fetch_plan(plan_id)
    state = json.loads(row["current_state"])
    report = analyze(state)
    if report["error_count"] and not body.get("force"):
        return jsonify({"error": "仍有安全错误，不能确认；如为教学演示可使用 force=true。", "report": report}), 409
    now = now_iso()
    db = get_db()
    version_no = db.execute(
        "SELECT COALESCE(MAX(version_no),0) FROM plan_versions WHERE plan_id=?", (plan_id,)
    ).fetchone()[0]
    db.execute(
        "UPDATE plan_versions SET status='confirmed', reason=?, report_json=? WHERE plan_id=? AND version_no=?",
        (body.get("reason", "确认快照"), json.dumps(report, ensure_ascii=False), plan_id, version_no),
    )
    db.execute(
        "UPDATE plans SET status='confirmed', confirmed_state=current_state, confirmed_at=?, updated_at=? WHERE id=?",
        (now, now, plan_id),
    )
    db.commit()
    return jsonify({"plan": row_payload(fetch_plan(plan_id)), "report": report})


@app.route("/api/plans/<plan_id>/revision", methods=["POST"])
def create_revision(plan_id: str):
    row = fetch_plan(plan_id)
    body = request.get_json(silent=True) or {}
    state = norm_state(body.get("state") or json.loads(row["current_state"]))
    confirmed = json.loads(row["confirmed_state"]) if row["confirmed_state"] else json.loads(row["current_state"])
    report = analyze(state)
    affected = affected_cases(confirmed, state, report)
    db = get_db()
    version_no = db.execute(
        "SELECT COALESCE(MAX(version_no),0)+1 FROM plan_versions WHERE plan_id=?", (plan_id,)
    ).fetchone()[0]
    now = now_iso()
    db.execute(
        "UPDATE plans SET name=?, status='draft', current_state=?, confirmed_state=?, updated_at=? WHERE id=?",
        (state["name"], json.dumps(state, ensure_ascii=False),
         json.dumps(confirmed, ensure_ascii=False), now, plan_id),
    )
    insert_version(db, plan_id, version_no, "draft",
                   body.get("reason", "更换卡车或调整站序后的新版本"), state, affected, report)
    db.commit()
    return jsonify({"plan": row_payload(fetch_plan(plan_id)), "report": report,
                    "affected_case_ids": affected})


@app.route("/api/plans/<plan_id>/versions")
def versions(plan_id: str):
    fetch_plan(plan_id)
    rows = get_db().execute(
        "SELECT * FROM plan_versions WHERE plan_id=? ORDER BY version_no DESC, created_at DESC",
        (plan_id,),
    ).fetchall()
    return jsonify({"versions": [{
        "id": r["id"], "version_no": r["version_no"], "status": r["status"],
        "reason": r["reason"], "affected_case_ids": json.loads(r["affected_case_ids"]),
        "report": json.loads(r["report_json"]) if r["report_json"] else None,
        "state": json.loads(r["state_json"]), "created_at": r["created_at"],
    } for r in rows]})


@app.route("/api/plans/<plan_id>/versions/<int:version_no>")
def version_detail(plan_id: str, version_no: int):
    row = get_db().execute(
        "SELECT * FROM plan_versions WHERE plan_id=? AND version_no=?", (plan_id, version_no)
    ).fetchone()
    if row is None:
        raise KeyError("版本不存在")
    return jsonify({
        "id": row["id"], "plan_id": row["plan_id"], "version_no": row["version_no"],
        "status": row["status"], "reason": row["reason"],
        "affected_case_ids": json.loads(row["affected_case_ids"]),
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
        "state": json.loads(row["state_json"]), "created_at": row["created_at"],
    })


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    return jsonify(analyze(parse_state()))


@app.route("/api/auto", methods=["POST"])
def api_auto():
    result = auto_arrange(parse_state())
    return jsonify(result)


def export_response(state: Dict[str, Any], kind: str, plan_name: str = "tour-load") -> Response:
    report = analyze(state)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in plan_name).strip("-") or "tour-load"
    if kind == "loading":
        return Response(loading_markdown(state), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-loading.md"'})
    if kind == "unloading":
        return Response(unloading_markdown(state, report), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-unloading.md"'})
    if kind == "layers":
        return Response(layers_markdown(state, report), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-layers.md"'})
    if kind == "layers-json":
        payload = {"name": state["name"], "layers": render_layer_svgs(state, report)}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                        mimetype="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-layers.json"'})
    if kind == "recompute":
        payload = recompute_json(state)
        return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                        mimetype="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-recompute.json"'})
    raise ValueError("未知导出类型")


@app.route("/api/export/<kind>", methods=["POST"])
def api_export(kind: str):
    state = parse_state()
    return export_response(state, kind, state["name"])


@app.route("/api/plans/<plan_id>/export/<kind>")
def plan_export(plan_id: str, kind: str):
    row = fetch_plan(plan_id)
    return export_response(json.loads(row["current_state"]), kind, row["name"])


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
