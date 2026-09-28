"""Regression tests for the community-id flow fixes (2026-09-28 review).

  - A commit whose outcome is unknown (timeout, dropped connection) freezes
    the file's choices; the only way forward is the same request with the
    same idempotency key, or cancelling the file.
  - Family Graph settings can't be changed by another web page, and a new
    Family Graph URL never inherits the stored key.
  - A session under review or mid-commit is never expired as abandoned.
  - Keeping a name as original text carries into the community layer.
  - The results table counts people by what actually happened to them.
  - Temp settings files are gitignored.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_community import (
    API_KEY, COMMIT, DECISION, PLAN, REFUSED, SHEET, _client, _connect, _detect, _roster_csv, _wait_results,
)

ATTACH = {"action": "attach", "target": DECISION["target"]}


# ---------------------------------------------------------------------------
# A stub Family Graph that implements the idempotency contract
# ---------------------------------------------------------------------------

def _canonical_hash(body: dict) -> str:
    rest = {k: v for k, v in body.items() if k != "idempotency_key"}
    raw = json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdemFG:
    """Like tests/test_community.StubFG, plus Family Graph's roster commit
    idempotency: a stored key with the same request replays (200,
    "replayed": true) and writes nothing; a refusal is never stored. The
    commit is written BEFORE any delay, like a real server that finishes
    after the client gave up. `drop` hangs up without answering."""

    def __init__(self):
        self.requests: list[dict] = []
        self.writes = 0                      # commits that actually wrote
        self.keys: dict[str, tuple] = {}
        self.plan_body = PLAN
        self.commit_body = COMMIT
        self.expect = {DECISION["key"]: ATTACH}
        self.delay: dict[str, float] = {}
        self.drop = False
        self.commit_status = None            # force a status (e.g. 403) on commit
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def commits(self):
        return [r["body"] for r in self.requests if r["path"].endswith("/commit")]

    def _result_for(self, decisions: dict) -> dict:
        out = copy.deepcopy(self.commit_body)
        for row in out["sheets"][0]["rows"]:
            for p in row.get("persons") or []:
                if (decisions.get(p["key"]) or {}).get("action") == "skip":
                    p["action"], p["community_id"] = "skip", None
        return out

    def _handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/health":
                    return self._send(200, {"status": "ok"})
                if self.path.startswith("/api/identity/roster/lookup/"):
                    return self._send(404, {"error": "x"})
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({"path": self.path, "body": body})
                if self.headers.get("authorization") != f"Bearer {API_KEY}":
                    return self._send(401, {"error": "unauthorized"})
                if self.path == "/api/identity/roster/plan":
                    return self._send(200, stub.plan_body)
                if self.path != "/api/identity/roster/commit":
                    return self._send(404, {})
                if stub.commit_status:
                    return self._send(stub.commit_status, {"error": "forbidden"})
                key, h = body.get("idempotency_key"), _canonical_hash(body)
                if key and key in stub.keys:
                    if stub.keys[key][0] != h:
                        return self._send(409, {"error": "idempotency_conflict"})
                    return self._send(200, {**stub.keys[key][1], "replayed": True})
                got = body.get("decisions") or {}
                if any(got.get(k) != v for k, v in stub.expect.items()):
                    status, answer = 409, REFUSED
                else:
                    stub.writes += 1
                    status, answer = 201, stub._result_for(got)
                    if key:
                        stub.keys[key] = (h, answer)
                delay = stub.delay.get("commit", 0)
                if delay:
                    time.sleep(delay)
                if stub.drop:
                    self.close_connection = True
                    return                                   # hang up, no answer
                self._send(status, answer)

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return H


@pytest.fixture
def ifg():
    stub = IdemFG()
    yield stub
    stub.close()


def _decided(c, fg, csv_bytes=None):
    _connect(c, fg.url)
    sid, _ = _detect(c, csv_bytes or _roster_csv(), "roster.csv")
    r = c.post(f"/api/anonymize/{sid}/community/decide", json={"key": DECISION["key"], **ATTACH})
    assert r.status_code == 200
    return sid


def _csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(SHEET["headers"])
    w.writerows(rows)
    return buf.getvalue().encode()


# ---------------------------------------------------------------------------
# Finding 0: a commit with an unknown outcome is frozen, resent with the same key
# ---------------------------------------------------------------------------

def test_commit_carries_an_idempotency_key_of_the_canonical_body(ifg):
    c = _client()
    sid = _decided(c, ifg)
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    assert _wait_results(c, sid)["verify_result"]["passed"]
    [body] = ifg.commits()
    key = body["idempotency_key"]
    assert re.fullmatch(rf"docanon:{sid}:[0-9a-f]{{32}}", key)
    assert key.rsplit(":", 1)[1] == _canonical_hash(body)[:32]
    assert 8 <= len(key) <= 200 and re.fullmatch(r"[A-Za-z0-9._:-]+", key)


def test_a_commit_timeout_freezes_the_file_and_the_resend_replays(ifg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")       # before app.config is imported
    from app.config import KEYS_DIR, LOG_FILE, OUTPUT_DIR
    c = _client()
    sid = _decided(c, ifg)
    ifg.delay["commit"] = 2.5                    # Family Graph writes, then answers too late
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 504
    msg = r.get_json()["error"]
    assert "comes back as known" not in msg
    assert "same" in msg and "cancel" in msg.lower()
    view = r.get_json()["community"]
    assert view["status"] == "commit_unknown" and view["busy"] == ""
    assert ifg.writes == 1

    # Nothing may change what the next commit says.
    for path, payload in (("decide", {"key": DECISION["key"], "action": "create"}),
                          ("decide", {"key": DECISION["key"], "action": "undo"}),
                          ("retry", {}), ("skip", {"skip": True})):
        rr = c.post(f"/api/anonymize/{sid}/community/{path}", json=payload)
        assert rr.status_code == 409, (path, payload, rr.get_json())
    assert c.get(f"/api/anonymize/{sid}/community").get_json()["decisions"] == {DECISION["key"]: ATTACH}
    rr = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": [], "deselected_types": ["PERSON"]})
    assert rr.status_code == 409 and c.get(f"/api/anonymize/{sid}/community").get_json()["status"] == "commit_unknown"
    # Keeping a name now would change which people the commit writes.
    rr = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": ["Emma"]})
    assert rr.status_code == 409
    assert len(ifg.commits()) == 1
    assert list(OUTPUT_DIR.iterdir()) == [] and not list(KEYS_DIR.glob("*.key.json"))

    # CONFIRM again: the same body and key go out and Family Graph replays.
    ifg.delay.clear()
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"] and res["community"]["status"] == "committed"
    first, second = ifg.commits()
    assert first == second
    assert ifg.writes == 1, "one commit written - no second id for anyone"
    log = LOG_FILE.read_text(encoding="utf-8")
    assert "replayed" in log and first["idempotency_key"] not in log


def test_a_dropped_connection_freezes_the_file_too(ifg):
    c = _client()
    sid = _decided(c, ifg)
    ifg.drop = True
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502
    assert r.get_json()["community"]["status"] == "commit_unknown"
    assert c.post(f"/api/anonymize/{sid}/community/decide",
                  json={"key": DECISION["key"], "action": "create"}).status_code == 409
    ifg.drop = False
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    assert _wait_results(c, sid)["verify_result"]["passed"]
    assert ifg.writes == 1 and ifg.commits()[0] == ifg.commits()[1]


def test_a_frozen_resend_that_family_graph_refuses_unfreezes_for_review(ifg, monkeypatch):
    """A refusal is never stored by Family Graph, so a refused resend proves
    the first commit wrote nothing: the operator may decide again."""
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")
    c = _client()
    sid = _decided(c, ifg)
    ifg.expect = {DECISION["key"]: {"action": "create"}}          # Family Graph wants something else
    ifg.delay["commit"] = 2.5
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 504
    ifg.delay.clear()
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 409 and r.get_json()["community"]["status"] == "ready"
    assert ifg.writes == 0
    assert c.post(f"/api/anonymize/{sid}/community/decide",
                  json={"key": DECISION["key"], "action": "create"}).status_code == 200


def test_a_clean_refusal_by_status_does_not_freeze(ifg):
    c = _client()
    sid = _decided(c, ifg)
    ifg.commit_status = 403
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502 and r.get_json()["community"]["status"] == "ready"
    assert c.post(f"/api/anonymize/{sid}/community/decide",
                  json={"key": DECISION["key"], "action": "undo"}).status_code == 200


def test_an_idempotency_conflict_is_explained_and_stays_frozen(ifg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")
    from app import pipeline
    c = _client()
    sid = _decided(c, ifg)
    ifg.delay["commit"] = 2.5
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 504
    ifg.delay.clear()
    # Family Graph holds this key for a different request (should never happen).
    key = pipeline.get_session(sid).community.commit_body["idempotency_key"]
    ifg.keys[key] = ("different", {})
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502 and "different" in r.get_json()["error"]
    assert r.get_json()["community"]["status"] == "commit_unknown"


# ---------------------------------------------------------------------------
# Finding 12: cross-site requests can't change settings; a new URL needs the key again
# ---------------------------------------------------------------------------

def _stored():
    from app.config import FAMILYGRAPH_FILE
    return json.loads(FAMILYGRAPH_FILE.read_text())


def test_another_web_page_cannot_change_family_graph_settings(ifg):
    c = _client()
    _connect(c, ifg.url)
    before = _stored()
    evil = json.dumps({"base_url": "http://192.168.1.66:3500"})
    attempts = [
        dict(data=evil, content_type="text/plain", headers={"Origin": "http://evil.example"}),
        dict(data=evil, content_type="application/json", headers={"Origin": "http://evil.example"}),
        dict(data=evil, content_type="application/json", headers={"Origin": "null"}),
        dict(data=evil, content_type="application/json", headers={"Referer": "http://evil.example/x"}),
        dict(data=evil, content_type="application/json", headers={"Sec-Fetch-Site": "cross-site"}),
        # DNS rebinding: the page's own origin, but not this app's host name.
        dict(data=evil, content_type="application/json",
             headers={"Host": "evil.example:5000", "Origin": "http://evil.example:5000"}),
    ]
    for kw in attempts:
        r = c.post("/api/familygraph", **kw)
        assert r.status_code == 403, kw
    assert c.delete("/api/familygraph", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/familygraph/test", headers={"Origin": "http://evil.example"}).status_code == 403
    # A form-style body with no Origin at all is not JSON: refused.
    assert c.post("/api/familygraph", data=evil, content_type="text/plain").status_code == 415
    assert _stored() == before
    # The app's own page still works.
    r = c.post("/api/familygraph", json={"base_url": ifg.url, "category": "church"},
               headers={"Origin": "http://localhost"})
    assert r.status_code == 200 and _stored()["category"] == "church"


def test_every_state_changing_api_route_checks_the_origin():
    c = _client()
    for path in ("/api/endpoints", "/api/github", "/api/github/push", "/api/keys/import",
                 "/api/unanonymize/text", "/api/anonymize/abc/confirm", "/api/anonymize/abc/community/decide"):
        r = c.post(path, json={}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403, path
    assert c.get("/api/health", headers={"Host": "evil.example:5000"}).status_code == 403
    assert c.get("/api/health", headers={"Host": "127.0.0.1:5000"}).status_code == 200


def test_a_new_family_graph_url_never_inherits_the_stored_key(ifg):
    c = _client()
    _connect(c, ifg.url)
    before = _stored()
    other = f"http://127.0.0.2:{ifg.port}"
    r = c.post("/api/familygraph", json={"base_url": other})
    assert r.status_code == 400 and "key" in r.get_json()["error"].lower()
    assert _stored() == before
    # Same URL, no key: kept (the settings form leaves the key blank).
    assert c.post("/api/familygraph", json={"base_url": ifg.url + "/", "category": "other"}).status_code == 200
    # New URL with the key entered again: fine.
    assert c.post("/api/familygraph", json={"base_url": other, "api_key": API_KEY}).status_code == 200
    assert _stored()["base_url"] == other


# ---------------------------------------------------------------------------
# Finding 20: a session in review or mid-commit is not abandoned
# ---------------------------------------------------------------------------

def test_a_busy_or_active_session_is_never_expired(ifg):
    from app import pipeline
    c = _client()
    sid = _decided(c, ifg)
    sess = pipeline.get_session(sid)
    old = time.monotonic() - pipeline.ABANDONED_AFTER_S - 60
    sess.detected_at = old
    sess.last_activity = old

    assert sess.lock.acquire(blocking=False)             # a commit is running
    try:
        assert pipeline.expire_abandoned() == 0 and pipeline.get_session(sid) is sess
    finally:
        sess.lock.release()
    sess.fg_busy = "commit"
    assert pipeline.expire_abandoned() == 0 and pipeline.get_session(sid) is sess
    sess.fg_busy = ""

    # A decision counts as activity.
    assert c.post(f"/api/anonymize/{sid}/community/decide",
                  json={"key": DECISION["key"], **ATTACH}).status_code == 200
    assert pipeline.expire_abandoned() == 0 and pipeline.get_session(sid) is sess

    # Truly idle: discarded, upload gone.
    sess.detected_at = sess.last_activity = old
    assert pipeline.expire_abandoned() == 1 and pipeline.get_session(sid) is None
    assert not sess.upload_path.exists()


def test_a_session_in_a_long_commit_survives_the_health_poll(ifg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "10")
    from app import pipeline
    c = _client()
    sid = _decided(c, ifg)
    sess = pipeline.get_session(sid)
    ifg.delay["commit"] = 1.0
    out = {}
    c2 = c.application.test_client()
    t = threading.Thread(target=lambda: out.setdefault(
        "r", c2.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})))
    t.start()
    for _ in range(100):
        if sess.fg_busy == "commit":
            break
        time.sleep(0.02)
    sess.detected_at = sess.last_activity = time.monotonic() - pipeline.ABANDONED_AFTER_S - 60
    c.post("/api/endpoints/health", json={"id": "nope"})     # the page's 30 s poll
    t.join(10)
    assert out["r"].status_code == 200, out["r"].get_json()
    assert _wait_results(c, sid)["verify_result"]["passed"]


# ---------------------------------------------------------------------------
# Finding 21: keeping a name as original text carries into the community layer
# ---------------------------------------------------------------------------

def test_a_name_kept_as_original_is_not_given_a_community_id(ifg):
    from app.config import KEYS_DIR, OUTPUT_DIR
    rows = copy.deepcopy(SHEET["rows"])
    rows[1][6] = "Emma Smith helps Liam"
    c = _client()
    sid = _decided(c, ifg, _csv(rows))
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": ["Emma", "Emma Smith"]})
    assert r.status_code == 200, r.get_json()
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"], res
    [body] = ifg.commits()
    assert body["decisions"]["0:0:1"] == {"action": "skip"}
    out = (OUTPUT_DIR / res["output_filename"]).read_text(encoding="utf-8")
    table = list(csv.reader(io.StringIO(out)))
    assert table[1][0] == "Emma", "her name cell keeps the text the operator chose"
    # Kept in the notes too; Liam (not kept) is still replaced there.
    assert table[2][6].startswith("Emma ") and "helps" in table[2][6] and "Liam" not in table[2][6]
    key = json.loads((KEYS_DIR / res["key_filename"]).read_text())
    assert all(v.get("display") != "Emma Smith" for v in key["identity_registry"].values())
    assert "I4AD4825DB3958878" not in out


def test_keeping_a_name_the_file_does_not_hold_changes_nothing(ifg):
    c = _client()
    sid = _decided(c, ifg)
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": ["Nobody Here"]}).status_code == 200
    assert _wait_results(c, sid)["verify_result"]["passed"]
    assert ifg.commits()[0]["decisions"] == {DECISION["key"]: ATTACH}


# ---------------------------------------------------------------------------
# Finding 30: results count by outcome, not by verdict
# ---------------------------------------------------------------------------

def test_results_count_people_decided_new_as_new(ifg):
    result = copy.deepcopy(COMMIT)
    for row in result["sheets"][0]["rows"]:
        for p in row.get("persons") or []:
            if p["key"] == DECISION["key"]:
                p.update(community_id="I0A1B2C3D4E5F6071", code_state="new", decided=True)
    ifg.commit_body = result
    ifg.expect = {DECISION["key"]: {"action": "create"}}
    c = _client()
    _connect(c, ifg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    c.post(f"/api/anonymize/{sid}/community/decide", json={"key": DECISION["key"], "action": "create"})
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    cm = _wait_results(c, sid)["community"]
    # Five distinct people, all minted by this commit; Jane appears twice.
    assert cm["people"] == {"known": 0, "new": 5, "skipped": 0}
    assert cm["households"] == {"known": 0, "new": 2}


def test_results_table_uses_the_outcome_counts():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "(p.matched || 0) + (p.review || 0)" not in js
    assert "cm.people" in js and "cm.households" in js


# ---------------------------------------------------------------------------
# Finding 29: temp settings files never reach git
# ---------------------------------------------------------------------------

def test_temp_settings_files_are_gitignored():
    root = Path(__file__).resolve().parent.parent
    for name in ("familygraph.tmp", "github.tmp", "endpoints.tmp"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=root)
        assert r.returncode == 0, name


def test_a_failed_settings_write_leaves_no_temp_file(monkeypatch):
    from app import familygraph
    from app.config import FAMILYGRAPH_FILE

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(familygraph.json, "dump", boom)
    with pytest.raises(OSError):
        familygraph._write({"base_url": "http://127.0.0.1:3500", "api_key": API_KEY})
    assert not FAMILYGRAPH_FILE.with_suffix(".tmp").exists()
