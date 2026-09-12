"""Exercise app.py weighing endpoints with a tiny in-process Flask shim.

The sandbox has no Flask installed; the shim implements only the surface the
application uses (routing, ``request`` JSON, ``g``, ``jsonify``, error handlers
and teardown), so the real route functions, validation and SQLite persistence
run unchanged.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import uuid

# ------------------------------------------------------------------ shim
class G(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e

    def __setattr__(self, k, v):
        self[k] = v


class Request:
    def __init__(self):
        self.method = "GET"
        self.path = ""
        self._json = None
        self.args = {}
        self.is_json = False

    def get_json(self, silent=False):
        return self._json


class Response:
    def __init__(self, body, status=200, headers=None, mimetype=None):
        if isinstance(body, Response) and status == 200:
            body, status = body.body, body.status_code
        elif isinstance(body, Response):
            body = body.body
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.mimetype = mimetype


def jsonify(obj):
    return Response(obj, 200, mimetype="application/json")


class FakeFlask:
    def __init__(self, name, static_folder=None, template_folder=None):
        self.routes = []
        self.error_handlers = {}
        self.teardown_funcs = []

    def route(self, rule, **options):
        methods = options.get("methods", ["GET"])

        def deco(fn):
            self.routes.append((rule, methods, fn))
            return fn
        return deco

    def teardown_appcontext(self, fn):
        self.teardown_funcs.append(fn)

    def errorhandler(self, exc):
        def deco(fn):
            self.error_handlers[exc] = fn
            return fn
        return deco

    def test_client(self):
        return Client(self)


class Client:
    def __init__(self, app):
        self.app = app

    def open(self, path, method="GET", json_body=None, query=None):
        import re
        req.method = method
        req.path = path
        req.args = query or {}
        req._json = json_body
        req.is_json = json_body is not None
        for rule, methods, fn in self.app.routes:
            if method not in methods:
                continue
            pattern = "^" + re.sub(r"<(?:int:)?(\w+)>", r"(?P<\1>[^/]+)", rule) + "$"
            m = re.match(pattern, path)
            if not m:
                continue
            kwargs = {k: (int(v) if f"<int:{k}>" in rule else v)
                      for k, v in m.groupdict().items()}
            try:
                rv = fn(**kwargs)
            except tuple(self.app.error_handlers) as exc:
                rv = self.app.error_handlers[type(exc)](exc)
            if isinstance(rv, tuple) and len(rv) >= 2 and isinstance(rv[1], int):
                rv = Response(rv[0], rv[1])
            elif not isinstance(rv, Response):
                rv = Response(rv)
            for f in self.app.teardown_funcs:
                f(None)
            return rv
        return Response({"error": "not found"}, 404)

    def get(self, path, query=None):
        return self.open(path, "GET", query=query)

    def post(self, path, json_body=None):
        return self.open(path, "POST", json_body=json_body)

    def put(self, path, json_body=None):
        return self.open(path, "PUT", json_body=json_body)

    def delete(self, path, query=None):
        return self.open(path, "DELETE", query=query)


flask_mod = types.ModuleType("flask")
flask_mod.Flask = FakeFlask
flask_mod.Response = Response
flask_mod.g = G()
flask_mod.jsonify = jsonify
flask_mod.request = Request()
req = flask_mod.request


def render_template(_name, **_kw):
    return "<html></html>"


flask_mod.render_template = render_template


def send_file(*_a, **_k):
    return Response("")


flask_mod.send_file = send_file
sys.modules["flask"] = flask_mod

# ------------------------------------------------------------------ tests
import app as app_module  # noqa: E402
from planning import norm_state  # noqa: E402
from sample_data import BAD_STATE  # noqa: E402
from weighing import predicted_readings  # noqa: E402

tmp = tempfile.mkdtemp()
app_module.DB_PATH = os.path.join(tmp, "test.sqlite3")
app_module.init_db()
flask_app = app_module.app
client = flask_app.test_client()
plan_id = "sample-tour"
state = norm_state(BAD_STATE)


def must_ok(rv, code=200):
    assert rv.status_code == code, (rv.status_code, rv.body[:400])
    return json.loads(rv.body)


# 1. sheets list is initially empty
data = must_ok(client.get(f"/api/plans/{plan_id}/weigh/sheets"))
assert data["stages"][0] == "departure" and len(data["stages"]) == 4
assert data["sheets"] == []

# 2. departure ticket reconciles with predicted readings
pred = predicted_readings(state, "departure", {"fuel_l": 100, "crew_count": 2})
readings = {
    "gross_kg": round(pred["gross_kg"]),
    "axle_kg": [round(a["total_kg"]) for a in pred["axles"]],
    "tolerance_kg": 20, "scale_max_kg": 20000,
    "fuel_l": 100, "crew_count": 2, "weighed_at": "2026-09-01T08:00",
}
ev = must_ok(client.post(f"/api/plans/{plan_id}/weigh/departure/evaluate",
                         {"readings": readings}))
assert ev["verdict"] == "within_tolerance", ev["gaps"]

# 3. evidence gap: axle sum != gross -> no diagnosis
bad = dict(readings, gross_kg=readings["gross_kg"] + 600)
ev = must_ok(client.post(f"/api/plans/{plan_id}/weigh/departure/evaluate",
                         {"readings": bad}))
assert ev["verdict"] == "evidence_gap"
assert "AXLE_SUM_MISMATCH" in [g["code"] for g in ev["gaps"]]
assert ev["candidates"] == []

# 4. save draft, freeze without reviewer fails, then freeze reconciled ticket
saved = must_ok(client.put(f"/api/plans/{plan_id}/weigh/departure",
                           {"readings": readings}))
assert saved["sheet"]["status"] == "draft"
rv = client.post(f"/api/plans/{plan_id}/weigh/departure/freeze",
                 {"reviewer": "", "readings": readings})
assert rv.status_code == 400, rv.body
frozen = must_ok(client.post(f"/api/plans/{plan_id}/weigh/departure/freeze",
                             {"reviewer": "张三", "readings": readings, "force": True}))
assert frozen["sheet"]["status"] == "frozen"
assert frozen["sheet"]["reviewer"] == "张三"

# 5. frozen ticket cannot be overwritten without force
rv = client.put(f"/api/plans/{plan_id}/weigh/departure", {"readings": readings})
assert rv.status_code == 409, rv.status_code

# 6. after-ams ticket with AMP (Paris box) wrongly unloaded: diagnosis via delta
dep_read = readings
ams_actual = norm_state(BAD_STATE)
ranks = {s["id"]: i for i, s in enumerate(ams_actual["stops"])}
ams_actual["placements"] = [
    p for p in ams_actual["placements"]
    if not (p["case_id"] == "AMP" or
            ranks[next(c for c in ams_actual["cases"] if c["id"] == p["case_id"])["stop_id"]] < 1)
]
pa = predicted_readings(ams_actual, "after-ams", {"fuel_l": 80, "crew_count": 2})
ams_read = {
    "gross_kg": round(pa["gross_kg"]),
    "axle_kg": [round(a["total_kg"]) for a in pa["axles"]],
    "tolerance_kg": 20, "fuel_l": 80, "crew_count": 2,
    "weighed_at": "2026-09-02T10:00",
}
ev = must_ok(client.post(f"/api/plans/{plan_id}/weigh/after-ams/evaluate",
                         {"readings": ams_read}))
assert ev["verdict"] == "out_of_tolerance"
top = ev["candidates"][0]
assert top["events"][0]["case_id"] == "AMP"

# 7. freeze with the candidate events -> derives an actual loading version
events = [{"kind": e["kind"], "case_id": e["case_id"],
           "present_prev": e["present_prev"], "dx_m": e.get("dx_m", 0),
           "weight_delta_kg": e.get("weight_delta_kg", 0)}
          for e in top["events"]]
res = must_ok(client.post(f"/api/plans/{plan_id}/weigh/after-ams/freeze",
                          {"reviewer": "李四", "readings": ams_read, "events": events}))
assert res["derived_version_no"] >= 2
derived_no = res["derived_version_no"]
assert res["sheet"]["derived_version_no"] == derived_no
# AMP removed from the derived current state
cur = must_ok(client.get(f"/api/plans/{plan_id}"))
assert all(p["case_id"] != "AMP" for p in cur["state"]["placements"])
# A new "actual" version exists, and the confirmed snapshot still exists
versions = must_ok(client.get(f"/api/plans/{plan_id}/versions"))["versions"]
v = next(v for v in versions if v["version_no"] == derived_no)
assert v["status"] == "actual" and "称重复核" in v["reason"]

# 8. moving a case after freezing marks the associated frozen ticket stale
sheets = must_ok(client.get(f"/api/plans/{plan_id}/weigh/sheets"))["sheets"]
dep_sheet = next(s for s in sheets if s["stage"] == "departure")
assert dep_sheet["status"] in ("frozen", "stale")
new_state = cur["state"]
# Move a Paris case (still on board) and save a new draft version
pamp = next(p for p in new_state["placements"] if p["case_id"] == "DRUM")
pamp["x"] = round(pamp["x"] + 0.2, 2)
must_ok(client.put(f"/api/plans/{plan_id}",
                   {"state": new_state, "reason": "现场调整鼓箱位置"}))
sheets = must_ok(client.get(f"/api/plans/{plan_id}/weigh/sheets"))["sheets"]
dep_after = next(s for s in sheets if s["stage"] == "departure")
assert dep_after["stale"] is True and dep_after["status"] == "stale", dep_after["status"]

# 9. a stale frozen ticket may be re-recorded (force implicit once signature differs)
upd = dict(ams_read, gross_kg=ams_read["gross_kg"], weighed_at="2026-09-02T11:00")
saved2 = must_ok(client.put(f"/api/plans/{plan_id}/weigh/after-ams",
                            {"readings": upd}))
assert saved2["sheet"]["status"] == "draft"

# 10. chronology: an earlier time on a later stage is an evidence gap
early = dict(ams_read, weighed_at="2026-08-30T08:00")
ev = must_ok(client.post(f"/api/plans/{plan_id}/weigh/after-ams/evaluate",
                         {"readings": early}))
assert "TIME_ORDER" in [g["code"] for g in ev["gaps"]]

# 11. defect 2: a reconciled ticket freezes (reviewer) WITHOUT a saved draft and
#     WITHOUT events -> no actual version is derived.
pred_dep = predicted_readings(state, "departure", {"fuel_l": 200, "crew_count": 3})
recon_read = {
    "gross_kg": round(pred_dep["gross_kg"]),
    "axle_kg": [round(a["total_kg"]) for a in pred_dep["axles"]],
    "tolerance_kg": 20, "scale_max_kg": 20000,
    "fuel_l": 200, "crew_count": 3, "weighed_at": "2026-09-01T09:30",
}
before_max = max(v["version_no"] for v in
                 must_ok(client.get(f"/api/plans/{plan_id}/versions"))["versions"])
# overwrite the previously frozen departure (signature already changed by the
# AMP-derived version, so the stale ticket is re-recordable with force)
fz = must_ok(client.post(f"/api/plans/{plan_id}/weigh/departure/freeze",
                         {"reviewer": "王五", "readings": recon_read,
                          "events": [], "force": True}))
assert fz["sheet"]["status"] == "frozen"
assert fz["sheet"]["reviewer"] == "王五"
assert fz["derived_version_no"] is None, fz["derived_version_no"]
assert fz["sheet"]["derived_version_no"] is None
assert fz["sheet"]["resolution"] == []
after_max = max(v["version_no"] for v in
                must_ok(client.get(f"/api/plans/{plan_id}/versions"))["versions"])
assert after_max == before_max, "reconciled freeze must not create a version"

# 12. defect 3: freeze an OUT-OF-TOLERANCE ticket directly from a candidate
#     (no draft ever saved).  The confirmed events must land in resolution_json
#     AND match the derived actual version.
# Use a fresh plan so no after-ber sheet exists yet.
new_plan = must_ok(client.post("/api/plans", {"name": "直冻结核对", "state": state}), 201)
new_id = new_plan["id"]
pber = predicted_readings(state, "after-ber", {"fuel_l": 60, "crew_count": 2})
from weighing import axle_fractions
b = next(b for b in __import__("planning").boxes_from(state) if b["id"] == "BER-LIGHT")
fr = axle_fractions(state["truck"], b["x"] + b["dx"] / 2)
ber_read = {
    "gross_kg": round(pber["gross_kg"] + b["weight"]),
    "axle_kg": [round(pber["axles"][i]["total_kg"] + b["weight"] * fr[i]) for i in range(2)],
    "tolerance_kg": 20, "scale_max_kg": 20000,
    "fuel_l": 60, "crew_count": 2, "weighed_at": "2026-09-03T10:00",
}
ber_ev = must_ok(client.post(f"/api/plans/{new_id}/weigh/after-ber/evaluate",
                             {"readings": ber_read}))
assert ber_ev["verdict"] == "out_of_tolerance"
cand = ber_ev["candidates"][0]
assert cand["events"][0]["kind"] == "extra" and cand["events"][0]["case_id"] == "BER-LIGHT"
# no draft saved -> fetching the sheet before freeze must 404
rv = client.get(f"/api/plans/{new_id}/weigh/sheets")
assert all(s["stage"] != "after-ber" for s in json.loads(rv.body)["sheets"])
events = [{"kind": e["kind"], "case_id": e["case_id"],
           "present_prev": e["present_prev"], "dx_m": e.get("dx_m", 0),
           "weight_delta_kg": e.get("weight_delta_kg", 0)} for e in cand["events"]]
ber_fz = must_ok(client.post(f"/api/plans/{new_id}/weigh/after-ber/freeze",
                             {"reviewer": "赵六", "readings": ber_read, "events": events}))
assert ber_fz["derived_version_no"] is not None
dno = ber_fz["derived_version_no"]
# resolution persisted on the (previously nonexistent) sheet
sheet = next(s for s in must_ok(client.get(f"/api/plans/{new_id}/weigh/sheets"))["sheets"]
             if s["stage"] == "after-ber")
assert sheet["status"] == "frozen"
assert sheet["reviewer"] == "赵六"
assert [e["case_id"] for e in sheet["resolution"]] == ["BER-LIGHT"]
assert {e["kind"] for e in sheet["resolution"]} == {"extra"}
assert sheet["derived_version_no"] == dno
# the derived version reflects the same event: BER-LIGHT deferred to next stop
ver = must_ok(client.get(f"/api/plans/{new_id}/versions/{dno}"))
assert ver["status"] == "actual"
ber_light = next(c for c in ver["state"]["cases"] if c["id"] == "BER-LIGHT")
assert ber_light["stop_id"] == "par", ber_light["stop_id"]
# and it is now the current (actual) state; the confirmed/plan snapshot is untouched
cur = must_ok(client.get(f"/api/plans/{new_id}"))
cur_light = next(c for c in cur["state"]["cases"] if c["id"] == "BER-LIGHT")
assert cur_light["stop_id"] == "par"
print("\nALL FLASK WEIGH-ENDPOINT TESTS PASS")
