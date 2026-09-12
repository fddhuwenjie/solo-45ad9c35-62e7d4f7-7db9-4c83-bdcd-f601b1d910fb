from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Optional, Tuple

from flask import Flask, Response, g, jsonify, render_template, request, send_file
import io

from outputs import (
    layers_markdown,
    lashing_markdown,
    loading_markdown,
    render_layer_svgs,
    unloading_markdown,
)
from planning import affected_cases, analyze, auto_arrange, norm_state, now_iso, recompute_json, uid
from lashing import lashing_signature, lashing_version_diff
from weighing import (
    apply_resolution,
    evaluate_sheet,
    predicted_readings,
    sheet_signature,
    stage_sequence,
)
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
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS weigh_sheets (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            readings_json TEXT NOT NULL,
            eval_json TEXT,
            signature TEXT NOT NULL DEFAULT '',
            derived_version_no INTEGER,
            resolution_json TEXT NOT NULL DEFAULT '[]',
            reviewer TEXT NOT NULL DEFAULT '',
            frozen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(plan_id, stage),
            FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_weigh_plan ON weigh_sheets(plan_id, stage)")
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
            "VALUES (?,?,1,'draft',?,?,'[]',?,?)",
            (uid(), plan_id, "内置示例：前轴超载与卸货阻挡",
             json.dumps(state, ensure_ascii=False),
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


# ----------------------------- weigh sheets -----------------------------

def fetch_sheet(plan_id: str, stage: str) -> sqlite3.Row:
    row = get_db().execute(
        "SELECT * FROM weigh_sheets WHERE plan_id=? AND stage=?", (plan_id, stage)
    ).fetchone()
    if row is None:
        raise KeyError("称重单不存在")
    return row


def _chronology_rows(plan_id: str, stage: str) -> list[dict]:
    """Earlier, already recorded tickets used for the TIME_ORDER check."""
    rows = get_db().execute(
        "SELECT stage, readings_json FROM weigh_sheets WHERE plan_id=? ORDER BY updated_at",
        (plan_id,),
    ).fetchall()
    state = json.loads(fetch_plan(plan_id)["current_state"])
    ranks = {s: i for i, s in enumerate(stage_sequence(state))}
    out = []
    for r in rows:
        if r["stage"] == stage or r["stage"] not in ranks:
            continue
        readings = json.loads(r["readings_json"])
        out.append({"stage": r["stage"], "weighed_at": readings.get("weighed_at", ""),
                    "label": _stage_label(state, r["stage"])})
    return out


def _stage_label(state: Dict[str, Any], stage: str) -> str:
    if stage == "departure":
        return "发车前（满载）"
    sid = stage[len("after-"):] if stage.startswith("after-") else ""
    stop = next((s for s in state.get("stops", []) if s["id"] == sid), None)
    return f"{stop['city']} 卸货后" if stop else stage


def _previous_sheet(plan_id: str, stage: str) -> Optional[sqlite3.Row]:
    """The chronologically adjacent recorded ticket (any status)."""
    state = json.loads(fetch_plan(plan_id)["current_state"])
    seq = stage_sequence(state)
    if stage not in seq:
        return None
    rank = seq.index(stage)
    candidates = [s for s in seq[:rank]]
    rows = get_db().execute(
        "SELECT * FROM weigh_sheets WHERE plan_id=?", (plan_id,)
    ).fetchall()
    by_stage = {r["stage"]: r for r in rows}
    for prev_stage in reversed(candidates):
        if prev_stage in by_stage:
            return by_stage[prev_stage]
    return None


def _sheet_payload(row: sqlite3.Row, state: Dict[str, Any]) -> Dict[str, Any]:
    sig_now = ""
    try:
        sig_now = sheet_signature(state, row["stage"])
    except ValueError:
        sig_now = ""
    stale = row["status"] == "frozen" and bool(row["signature"]) and sig_now != row["signature"]
    return {
        "id": row["id"],
        "stage": row["stage"],
        "stage_title": _stage_label(state, row["stage"]),
        "status": "stale" if stale else row["status"],
        "readings": json.loads(row["readings_json"]),
        "evaluation": json.loads(row["eval_json"]) if row["eval_json"] else None,
        "signature": row["signature"],
        "signature_current": sig_now,
        "stale": stale,
        "derived_version_no": row["derived_version_no"],
        "resolution": json.loads(row["resolution_json"] or "[]"),
        "reviewer": row["reviewer"],
        "frozen_at": row["frozen_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def evaluate_for_plan(plan_id: str, stage: str, readings: Dict[str, Any]) -> Dict[str, Any]:
    state = json.loads(fetch_plan(plan_id)["current_state"])
    chronology = _chronology_rows(plan_id, stage)
    prev_row = _previous_sheet(plan_id, stage)
    prev_sheet = None
    if prev_row is not None:
        prev_sheet = {"stage": prev_row["stage"], "status": prev_row["status"],
                      "readings": json.loads(prev_row["readings_json"]),
                      "weighed_at": json.loads(prev_row["readings_json"]).get("weighed_at", ""),
                      "label": _stage_label(state, prev_row["stage"])}
    return evaluate_sheet(state, stage, readings, chronology=chronology,
                          prev_sheet=prev_sheet)


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
    lashing_diff = lashing_version_diff(confirmed, state)
    return jsonify({"plan": row_payload(fetch_plan(plan_id)), "report": report,
                    "affected_case_ids": affected,
                    "affected_lashing_ids": lashing_diff["affected_lashing_ids"],
                    "lashing_diff": lashing_diff})


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


@app.route("/api/plans/<plan_id>/weigh/sheets")
def weigh_sheets(plan_id: str):
    """All tickets for a plan with fresh staleness flags (computed on read)."""
    state = json.loads(fetch_plan(plan_id)["current_state"])
    rows = get_db().execute(
        "SELECT * FROM weigh_sheets WHERE plan_id=? ORDER BY created_at", (plan_id,)
    ).fetchall()
    sheets = [_sheet_payload(r, state) for r in rows]
    return jsonify({
        "stages": stage_sequence(state),
        "stage_titles": {s: _stage_label(state, s) for s in stage_sequence(state)},
        "sheets": sheets,
    })


@app.route("/api/plans/<plan_id>/weigh/<stage>/evaluate", methods=["POST"])
def weigh_evaluate(plan_id: str, stage: str):
    """Dry-run reconciliation for the edited ticket; nothing is persisted."""
    fetch_plan(plan_id)
    body = request.get_json(silent=True) or {}
    readings = body.get("readings") or {}
    if body.get("state"):
        state = norm_state(body["state"])
        seq = stage_sequence(state)
        if stage not in seq:
            raise ValueError("当前站序中不存在该称重阶段")
        prev_sheet = None
        if body.get("prev_sheet"):
            prev_sheet = body["prev_sheet"]
        chronology = body.get("chronology") or []
        result = evaluate_sheet(state, stage, readings,
                                chronology=chronology, prev_sheet=prev_sheet)
        result["predicted"] = predicted_readings(state, stage, readings)
        return jsonify(result)
    result = evaluate_for_plan(plan_id, stage, readings)
    return jsonify(result)


@app.route("/api/plans/<plan_id>/weigh/<stage>", methods=["PUT", "POST"])
def weigh_save(plan_id: str, stage: str):
    """Create/update a draft (or overwrite a stale frozen ticket after recheck)."""
    fetch_plan(plan_id)
    body = request.get_json(silent=True) or {}
    readings = body.get("readings") or {}
    result = evaluate_for_plan(plan_id, stage, readings)
    db = get_db()
    now = now_iso()
    existing = db.execute(
        "SELECT * FROM weigh_sheets WHERE plan_id=? AND stage=?", (plan_id, stage)
    ).fetchone()
    if existing is not None and existing["status"] == "frozen":
        state = json.loads(fetch_plan(plan_id)["current_state"])
        if existing["signature"] and not body.get("force") and \
                existing["signature"] == sheet_signature(state, stage):
            return jsonify({"error": "该称重单已冻结；计划相关内容修改后才允许复核重录，或使用 force=true。",
                            "sheet": _sheet_payload(existing, state)}), 409
    if existing is None:
        sheet_id = uid()
        db.execute(
            "INSERT INTO weigh_sheets (id,plan_id,stage,status,readings_json,eval_json,"
            "signature,derived_version_no,resolution_json,reviewer,frozen_at,"
            "created_at,updated_at) VALUES (?,?,?,'draft',?,?,'',NULL,'[]','',NULL,?,?)",
            (sheet_id, plan_id, stage,
             json.dumps(readings, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now, now),
        )
    else:
        db.execute(
            "UPDATE weigh_sheets SET status='draft', readings_json=?, eval_json=?, "
            "signature='', reviewer='', frozen_at=NULL, derived_version_no=NULL, "
            "resolution_json='[]', updated_at=? WHERE id=?",
            (json.dumps(readings, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), now, existing["id"]),
        )
    db.commit()
    row = fetch_sheet(plan_id, stage)
    state = json.loads(fetch_plan(plan_id)["current_state"])
    return jsonify({"sheet": _sheet_payload(row, state), "evaluation": result})


@app.route("/api/plans/<plan_id>/weigh/<stage>/freeze", methods=["POST"])
def weigh_freeze(plan_id: str, stage: str):
    """Confirm the on-site recheck and freeze the ticket.

    With ``events`` the findings are applied to derive a new actual-loading
    version (re-running axle and lashing checks); the confirmed/plan snapshot is
    never overwritten.  A ticket may also be frozen as-is when it reconciles.
    """
    fetch_plan(plan_id)
    body = request.get_json(silent=True) or {}
    row = get_db().execute(
        "SELECT * FROM weigh_sheets WHERE plan_id=? AND stage=?", (plan_id, stage)
    ).fetchone()
    if row is not None and row["status"] == "frozen" and not body.get("force"):
        raise ValueError("称重单已冻结")
    plan_row = fetch_plan(plan_id)
    state = json.loads(plan_row["current_state"])
    readings = body.get("readings") or (
        json.loads(row["readings_json"]) if row is not None else None)
    if not readings:
        raise ValueError("缺少称重读数")
    events = body.get("events") or []
    reviewer = str(body.get("reviewer", "")).strip()
    if not reviewer:
        raise ValueError("请填写现场复核人姓名")
    result = evaluate_for_plan(plan_id, stage, readings)
    if result["gaps"]:
        raise ValueError("存在证据缺口，先校正称重单后再冻结："
                         + "；".join(g["message"] for g in result["gaps"][:3]))
    if events and result["verdict"] != "out_of_tolerance":
        # Findings only accompany an actually reconciled discrepancy.
        events = []

    derived_no = None
    next_state = state
    if events:
        next_state = apply_resolution(state, stage, events)
        next_state = norm_state(next_state)
        next_report = analyze(next_state)
        db = get_db()
        version_no = db.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM plan_versions WHERE plan_id=?",
            (plan_id,),
        ).fetchone()[0]
        confirmed = json.loads(plan_row["confirmed_state"]) if plan_row["confirmed_state"] else state
        affected = affected_cases(confirmed, next_state, next_report)
        reason = f"称重复核（{_stage_label(state, stage)}）派生实际装载版本"
        insert_version(db, plan_id, version_no, "actual", reason, next_state,
                       affected, next_report)
        db.execute("UPDATE plans SET current_state=?, status='draft', updated_at=? WHERE id=?",
                   (json.dumps(next_state, ensure_ascii=False), now_iso(), plan_id))
        derived_no = version_no

    signature = sheet_signature(next_state, stage)
    db = get_db()
    now = now_iso()
    if row is None:
        sheet_id = uid()
        db.execute(
            "INSERT INTO weigh_sheets (id,plan_id,stage,status,readings_json,eval_json,"
            "signature,derived_version_no,resolution_json,reviewer,frozen_at,"
            "created_at,updated_at) VALUES (?,?,?,'frozen',?,?,?,?,?,?,?,?,?)",
            (sheet_id, plan_id, stage,
             json.dumps(readings, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), signature,
             derived_no, json.dumps(events, ensure_ascii=False), reviewer, now, now, now),
        )
    else:
        db.execute(
            "UPDATE weigh_sheets SET status='frozen', readings_json=?, eval_json=?, signature=?, "
            "resolution_json=?, reviewer=?, frozen_at=?, derived_version_no=?, updated_at=? "
            "WHERE id=?",
            (json.dumps(readings, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), signature,
             json.dumps(events, ensure_ascii=False), reviewer, now, derived_no, now, row["id"]),
        )
    db.commit()
    saved = fetch_sheet(plan_id, stage)
    return jsonify({"sheet": _sheet_payload(saved, next_state),
                    "evaluation": result,
                    "derived_version_no": derived_no,
                    "plan": row_payload(fetch_plan(plan_id))})


@app.route("/api/plans/<plan_id>/weigh/<stage>", methods=["DELETE"])
def weigh_delete(plan_id: str, stage: str):
    row = fetch_sheet(plan_id, stage)
    if row["status"] == "frozen" and not request.args.get("force"):
        raise ValueError("已冻结的称重单不能删除，只能复核重录")
    get_db().execute("DELETE FROM weigh_sheets WHERE id=?", (row["id"],))
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    return jsonify(analyze(parse_state()))


@app.route("/api/lashing/lock", methods=["POST"])
def lashing_lock():
    """Lock/unlock straps.  The review signature is stamped server-side from
    the current geometry so a client cannot fake a re-reviewed connection."""
    body = request.get_json(silent=True) or {}
    state = norm_state(body.get("state"))
    lock = bool(body.get("lock", True))
    targets = body.get("ids")
    if isinstance(targets, str):
        targets = [targets]
    changed = []
    for lash in state.get("lashings", []):
        if targets and lash["id"] not in targets:
            continue
        lash["locked"] = lock
        lash["review_signature"] = lashing_signature(state, lash) if lock else ""
        changed.append(lash["id"])
    report = analyze(state)
    return jsonify({"state": state, "report": report, "changed": changed,
                    "scheme_version": report["lashing"]["scheme_version"]})


@app.route("/api/lashing/suggest", methods=["POST"])
def lashing_suggest():
    """Return the first failing connection per leg with an add-strap hint."""
    state = norm_state((request.get_json(silent=True) or {}).get("state"))
    return jsonify(analyze(state)["lashing"])


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
    if kind == "lashing":
        return Response(lashing_markdown(state, report), mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-lashing.md"'})
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
