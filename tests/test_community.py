"""Community identifiers on rosters, end to end through the Flask API.

A stub Family Graph on a loopback port serves responses captured from the
real Family Graph roster engine (tests/fixtures/fg_*.json, produced by its
roster code on the same sheet), so these tests hold docanonymizer to the
actual wire contract.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

FIX = Path(__file__).parent / "fixtures"
SHEET = json.loads((FIX / "fg_request_sheet.json").read_text())
PLAN = json.loads((FIX / "fg_plan.json").read_text())
COMMIT = json.loads((FIX / "fg_commit.json").read_text())
REFUSED = json.loads((FIX / "fg_commit_refused.json").read_text())
DECISION = json.loads((FIX / "fg_decision.json").read_text())
API_KEY = "sk_test_roster_key_0123456789abcdef"

NAMES = ["Emma", "Liam", "Jane", "Ava", "Marie"]


# ---------------------------------------------------------------------------
# Stub Family Graph
# ---------------------------------------------------------------------------

class StubFG:
    def __init__(self):
        self.requests: list[dict] = []
        self.mode = "normal"            # normal | always_refuse | forbidden
        # What it serves (defaults: the captured fixtures) and the decisions a
        # commit needs before it writes (anything else is refused, as Family
        # Graph refuses a commit with undecided review items).
        self.plan_body = PLAN
        self.commit_body = COMMIT
        self.refused_body = REFUSED
        self.expect = {DECISION["key"]: {"action": "attach", "target": DECISION["target"]}}
        # Seconds to wait before answering, by last path segment
        # ("plan", "commit", "health") - a slow machine or a huge roster.
        self.delay: dict[str, float] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return self.headers.get("authorization") == f"Bearer {API_KEY}"

            def _wait(self):
                delay = stub.delay.get(self.path.rsplit("/", 1)[-1], 0)
                if delay:
                    time.sleep(delay)

            def do_GET(self):
                self._wait()
                if self.path == "/api/health":
                    return self._send(200, {"status": "ok"})
                if self.path.startswith("/api/identity/roster/lookup/"):
                    return self._send(404 if self._authed() else 401, {"error": "x"})
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                self._wait()
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if stub.mode == "forbidden":
                    return self._send(403, {"error": "forbidden"})
                if self.path == "/api/identity/roster/plan":
                    return self._send(200, stub.plan_body)
                if self.path == "/api/identity/roster/commit":
                    got = body.get("decisions") or {}
                    if stub.mode == "always_refuse" or any(got.get(k) != v for k, v in stub.expect.items()):
                        return self._send(409, stub.refused_body)
                    return self._send(201, stub.commit_body)
                self._send(404, {})

            def handle(self):
                # A client that timed out has hung up; answering it is not an error.
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return H


@pytest.fixture
def fg():
    stub = StubFG()
    yield stub
    stub.close()


def _client():
    from app.server import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _connect(c, url):
    r = c.post("/api/familygraph", json={"base_url": url, "api_key": API_KEY, "category": "school"})
    assert r.status_code == 200, r.get_json()
    return r


def _roster_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(SHEET["headers"])
    w.writerows(SHEET["rows"])
    return buf.getvalue().encode()


# The model finds most names but misses some: Liam, Ava and Marie are never
# flagged. The community layer must still cover their cells.
LLM = json.dumps([
    {"text": "Emma", "type": "PERSON"}, {"text": "Smith", "type": "PERSON"},
    {"text": "Jane", "type": "PERSON"}, {"text": "Garcia", "type": "PERSON"},
    {"text": "jane@example.org", "type": "EMAIL"}, {"text": "Ann", "type": "PERSON"},
])


def _detect(c, data: bytes, name: str, llm: str = LLM):
    from app import detector
    with patch.object(detector.llm, "llm_call", return_value=llm):
        r = c.post("/api/anonymize/upload", data={
            "file": (io.BytesIO(data), name), "tags": "ALL",
        }, content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        sid = r.get_json()["session_id"]
        for _ in range(200):
            s = c.get(f"/api/anonymize/{sid}/status").get_json()
            if s.get("detection_complete") or s.get("error"):
                break
            time.sleep(0.05)
    assert s.get("detection_complete"), s
    return sid, s


def _wait_results(c, sid):
    for _ in range(300):
        r = c.get(f"/api/anonymize/{sid}/results").get_json()
        if r.get("verify_result") or r.get("error"):
            return r
        time.sleep(0.05)
    raise AssertionError("scrub did not finish")


def _run_roster(c, data, name):
    sid, _ = _detect(c, data, name)
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    assert view["status"] == "ready" and view["pending"] == 1
    r = c.post(f"/api/anonymize/{sid}/community/decide",
               json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    assert r.status_code == 200 and r.get_json()["pending"] == 0
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 200, r.get_json()
    return sid, _wait_results(c, sid)


def _ids(result, given):
    for row in result["sheets"][0]["rows"]:
        for p in row.get("persons") or []:
            if p["given_name"] == given:
                return p["community_id"]
    raise KeyError(given)


# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------

def test_settings_never_return_the_key_and_file_is_private(fg):
    from app.config import FAMILYGRAPH_FILE
    c = _client()
    _connect(c, fg.url)
    got = c.get("/api/familygraph").get_json()
    assert got == {"configured": True, "base_url": fg.url, "key_set": True, "category": "school"}
    assert API_KEY not in json.dumps(got)
    assert (FAMILYGRAPH_FILE.stat().st_mode & 0o777) == 0o600
    # Saving again without a key keeps the stored one.
    assert c.post("/api/familygraph", json={"base_url": fg.url, "category": "church"}).status_code == 200
    assert json.loads(FAMILYGRAPH_FILE.read_text())["api_key"] == API_KEY
    assert c.post("/api/familygraph/test").get_json() == {"status": "ok"}
    assert c.delete("/api/familygraph").status_code == 204
    assert c.get("/api/familygraph").get_json()["configured"] is False


def test_a_non_local_family_graph_is_refused_with_no_override():
    c = _client()
    for url in ("http://8.8.8.8:3500", "https://example.com", "http://127.0.0.1:3500/api"):
        r = c.post("/api/familygraph", json={"base_url": url, "api_key": API_KEY, "allow_nonlocal": True})
        assert r.status_code == 400, url


def test_familygraph_json_is_gitignored():
    root = Path(__file__).resolve().parent.parent
    assert "familygraph.json" in (root / ".gitignore").read_text().split()


def test_wrong_key_is_reported_plainly(fg):
    c = _client()
    c.post("/api/familygraph", json={"base_url": fg.url, "api_key": "sk_wrong_key_0123456789abc"})
    assert c.post("/api/familygraph/test").get_json() == {"status": "err", "error": "Family Graph rejected the API key"}


# ---------------------------------------------------------------------------
# The roster flow
# ---------------------------------------------------------------------------

def test_csv_roster_end_to_end(fg):
    from app.config import KEYS_DIR, OUTPUT_DIR
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")

    # Confirm is refused while a person still needs a decision.
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 409 and "decision" in r.get_json()["error"]
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    review = [i for i in view["items"] if i["action"] == "review"]
    assert [i["key"] for i in review] == [DECISION["key"]]
    assert review[0]["label"] == "Marie Garcia"
    assert review[0]["candidates"][0]["community_id"] == DECISION["target"]

    # A target that is not a listed candidate is refused.
    bad = c.post(f"/api/anonymize/{sid}/community/decide",
                 json={"key": DECISION["key"], "action": "attach", "target": "I0000000000000000"})
    assert bad.status_code == 400
    c.post(f"/api/anonymize/{sid}/community/decide",
           json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"], res
    assert res["community"]["status"] == "committed"

    # What reached Family Graph: the rows, the decision, our key and actor.
    plan_req = next(x for x in fg.requests if x["path"].endswith("/plan"))
    commit_req = [x for x in fg.requests if x["path"].endswith("/commit")][-1]
    assert plan_req["headers"]["x-family-graph-actor"] == "docanonymizer"
    assert plan_req["body"]["sheets"][0]["rows"] == SHEET["rows"]
    assert plan_req["body"]["source_ref"] == f"docanonymizer:{sid}"
    assert commit_req["body"]["decisions"] == {DECISION["key"]: {"action": "attach", "target": DECISION["target"]}}

    out = OUTPUT_DIR / res["output_filename"]
    rows = list(csv.reader(out.read_text(encoding="utf-8").splitlines()))
    assert rows[0][-1] == "FAMILY_ID", "no household column on this sheet - one is appended"
    emma, jane = _ids(COMMIT, "Emma"), _ids(COMMIT, "Jane")
    assert rows[1][0] == f"[{emma}]"
    assert rows[1][3] == f"[{jane}]"
    assert rows[2][3] == f"[{jane}]", "one person, one id, every row"
    assert rows[3][3] == f"[{DECISION['target']}]", "the operator's decision is the id used"
    fam = COMMIT["sheets"][0]["rows"][0]["family"]["community_id"]
    assert rows[1][-1] == rows[2][-1] == f"[{fam}]"
    assert rows[3][-1] != rows[1][-1]
    # Names the model missed are still gone; surnames carry per-value tokens.
    body = out.read_text(encoding="utf-8")
    for name in NAMES + ["Smith", "Garcia"]:
        assert not re.search(rf"\b{name}\b", body), name
    assert re.fullmatch(r"\[PERSON_[0-9A-F]{12}\]", rows[1][1])

    key = json.loads((KEYS_DIR / res["key_filename"]).read_text())
    assert key["identity_registry"][jane] == {"kind": "person", "display": "Jane Smith", "family": fam}
    assert key["identity_registry"][fam]["kind"] == "family"
    assert {"sheet": 0, "row": 1, "col": 0, "written": f"[{emma}]", "original": "Emma"} in key["identity_cells"]
    assert key["identity_columns"] == [{"sheet": 0, "col": 7, "header_row": 0, "header": "FAMILY_ID"}]
    assert key["community_source"]["import_runs"] == COMMIT["import_runs"]
    # Community ids never enter this app's issued-id ledger.
    from app import ids
    assert not any(jane.strip("I") in i for i in ids.load_reserved())

    # Restoring the anonymized file gives back the original exactly.
    r = c.post("/api/unanonymize", data={
        "file": (io.BytesIO(out.read_bytes()), out.name),
        "keys": json.dumps([res["key_filename"]]),
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    restored = (OUTPUT_DIR / r.get_json()["output_filename"]).read_text(encoding="utf-8")
    assert list(csv.reader(restored.splitlines())) == [SHEET["headers"]] + SHEET["rows"]


def test_xlsx_roster_end_to_end(fg):
    from openpyxl import Workbook, load_workbook
    from app.config import KEYS_DIR, OUTPUT_DIR
    wb = Workbook()
    ws = wb.active
    ws.title = "Roster"
    ws.append(SHEET["headers"])
    for row in SHEET["rows"]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    c = _client()
    _connect(c, fg.url)
    sid, res = _run_roster(c, buf.getvalue(), "roster.xlsx")
    assert res["verify_result"]["passed"], res
    out = OUTPUT_DIR / res["output_filename"]
    ws2 = load_workbook(out).active
    assert ws2.cell(row=1, column=8).value == "FAMILY_ID"
    assert ws2.cell(row=2, column=1).value == f"[{_ids(COMMIT, 'Emma')}]"
    assert ws2.cell(row=4, column=4).value == f"[{DECISION['target']}]"

    r = c.post("/api/unanonymize", data={
        "file": (io.BytesIO(out.read_bytes()), out.name),
        "keys": json.dumps([res["key_filename"]]),
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    ws3 = load_workbook(OUTPUT_DIR / r.get_json()["output_filename"]).active
    got = [[("" if v is None else str(v)) for v in row] for row in ws3.iter_rows(values_only=True)]
    assert got == [SHEET["headers"]] + SHEET["rows"]
    assert ws3.max_column == len(SHEET["headers"]), "the appended column is gone"


def test_ai_answer_with_community_ids_restores_to_names(fg):
    c = _client()
    _connect(c, fg.url)
    _, res = _run_roster(c, _roster_csv(), "roster.csv")
    jane, emma = _ids(COMMIT, "Jane"), _ids(COMMIT, "Emma")
    fam = COMMIT["sheets"][0]["rows"][0]["family"]["community_id"]
    answer = (f"Top volunteers: [{jane}], {emma.lower()}, \\[{jane}\\], PARENT_{jane}, "
              f"and household [{fam}] (bare hex {fam[1:]}). Unknown [I0123456789ABCDEF].")
    r = c.post("/api/unanonymize/text", json={"text": answer, "keys": [res["key_filename"]]})
    body = r.get_json()
    assert body["text"] == ("Top volunteers: Jane Smith, Emma Smith, Jane Smith, Jane Smith, "
                            "and household Smith Family (bare hex Smith Family). Unknown [I0123456789ABCDEF].")
    assert body["report"]["by_tag"]["INDIVIDUAL"] == 4
    assert body["report"]["by_tag"]["FAMILY"] == 2
    assert body["report"]["unresolved"] == ["[I0123456789ABCDEF]"]


def test_commit_that_finds_new_items_returns_to_review(fg):
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    c.post(f"/api/anonymize/{sid}/community/decide",
           json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    fg.mode = "always_refuse"
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 409
    assert "new items" in r.get_json()["error"]
    assert c.get(f"/api/anonymize/{sid}/results").get_json()["scrub_steps"] == [], "nothing was scrubbed"


def test_family_graph_down_blocks_until_the_operator_chooses(fg):
    from app.config import OUTPUT_DIR
    c = _client()
    _connect(c, fg.url)
    fg.close()                                   # Family Graph goes away
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    assert view["status"] == "error" and "not reachable" in view["error"]
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 409 and "Family Graph unavailable" in r.get_json()["error"]
    # Explicitly continuing without community ids: the normal pipeline runs.
    assert c.post(f"/api/anonymize/{sid}/community/skip", json={"skip": True}).status_code == 200
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"]
    assert res["community"]["status"] == "skipped"
    assert "[I" not in (OUTPUT_DIR / res["output_filename"]).read_text(encoding="utf-8")


def test_scope_error_is_explained(fg):
    c = _client()
    _connect(c, fg.url)
    fg.mode = "forbidden"
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    assert view["status"] == "error" and "scope" in view["error"]


def test_keeping_names_as_original_skips_community_ids(fg):
    from app.config import OUTPUT_DIR
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": [], "deselected_types": ["PERSON"]})
    assert r.status_code == 200
    res = _wait_results(c, sid)
    assert res["community"]["status"] == "skipped"
    assert not any(x["path"].endswith("/commit") for x in fg.requests), "nothing was written to Family Graph"
    assert "Emma" in (OUTPUT_DIR / res["output_filename"]).read_text(encoding="utf-8")


def test_documents_and_unconfigured_runs_are_untouched(fg):
    c = _client()
    sid, _ = _detect(c, _roster_csv(), "roster.csv")        # not connected
    assert c.get(f"/api/anonymize/{sid}/community").get_json()["status"] == "off"
    _connect(c, fg.url)
    sid, _ = _detect(c, b"Jane Smith wrote to Emma.", "memo.txt")
    assert c.get(f"/api/anonymize/{sid}/community").get_json()["status"] == "off"
    assert fg.requests == []


def test_a_name_the_model_missed_in_free_text_is_still_caught(fg):
    """Liam's first name also appears in the notes column. The model never
    flagged it; the community layer registers it before the preview."""
    c = _client()
    _connect(c, fg.url)
    sheet = json.loads(json.dumps(SHEET))
    sheet["rows"][1][6] = "Liam rides the bus"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(sheet["headers"])
    w.writerows(sheet["rows"])
    sid, status = _detect(c, buf.getvalue().encode(), "roster.csv")
    assert any(s["original"] == "Liam" for s in status["preview"]["spans"])


# ---------------------------------------------------------------------------
# Engine-level guarantees
# ---------------------------------------------------------------------------

def test_short_values_are_never_written_inside_a_community_token():
    from app.replacer import LiteralReplacer, apply
    token = "[IA4F9C2B1D0E7F21A]"
    r = LiteralReplacer({"A4": "[SID_111111111111]", "94": "[GRADE_222222222222]", "F9C2": "[ID_333333333333]"},
                        extra_protected={token, token.strip("[]")})
    assert apply(f"{token} A4 94", r) == f"{token} [SID_111111111111] [GRADE_222222222222]"


def test_restorer_community_forms_and_variants():
    from app.restorer import build_index, restore_text
    k1 = {"session_id": "aaaa1111", "original_filename": "a.xlsx", "replacement_map": {}, "created_at": "2026-01-01T00:00:00Z",
          "identity_registry": {"I00000000000000AA": {"kind": "person", "display": "Robert Jones"}}}
    k2 = {"session_id": "bbbb2222", "original_filename": "b.xlsx", "replacement_map": {}, "created_at": "2026-09-01T00:00:00Z",
          "identity_registry": {"I00000000000000AA": {"kind": "person", "display": "Bob Jones"}}}
    idx = build_index([("a", k1), ("b", k2)])
    assert idx.community_variants == 1
    text, rep = restore_text("[I00000000000000AA] / [f00000000000000aa] / 00000000000000AA", idx)
    assert text == "Bob Jones / Bob Jones / Bob Jones", "newest spelling wins; hex alone restores"
    assert rep.relabeled == 1 and rep.untagged == 1
    # Order of keys does not change the winner.
    idx2 = build_index([("b", k2), ("a", k1)])
    assert restore_text("[I00000000000000AA]", idx2)[0] == "Bob Jones"


def test_log_holds_no_names_emails_or_key(fg):
    from app.config import LOG_FILE
    c = _client()
    _connect(c, fg.url)
    _run_roster(c, _roster_csv(), "roster.csv")
    log = LOG_FILE.read_text(encoding="utf-8")
    for s in NAMES + ["Smith", "Garcia", "jane@example.org", API_KEY, DECISION["target"]]:
        assert s not in log, s
    assert "community commit" in log


# ---------------------------------------------------------------------------
# Slow Family Graph: long roster timeouts, clean failures, one call at a time
# ---------------------------------------------------------------------------

def test_roster_calls_wait_long_and_quick_calls_do_not(fg, monkeypatch):
    """Plan and commit get FAMILYGRAPH_ROSTER_TIMEOUT_S (600 s by default);
    health / [ TEST ] / lookup keep FAMILYGRAPH_TIMEOUT_S (10 s)."""
    from app import familygraph
    seen = []
    real = familygraph._session

    class Spy:
        def __init__(self):
            self.s = real()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.s.close()

        def request(self, method, url, **kw):
            seen.append((url.split("/api/", 1)[-1].split("/")[-1], kw["timeout"]))
            return self.s.request(method, url, **kw)

    monkeypatch.setattr(familygraph, "_session", Spy)
    c = _client()
    _connect(c, fg.url)
    assert c.post("/api/familygraph/test").get_json() == {"status": "ok"}
    _run_roster(c, _roster_csv(), "roster.csv")
    got = dict(seen)
    assert got["health"] == (5, 10) and got["I0000000000000000"] == (5, 10)
    assert got["plan"] == (5, 600) and got["commit"] == (5, 600)


def test_a_slow_plan_is_waited_for_and_shown_as_the_active_step(fg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_TIMEOUT_S", "1")
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "10")
    fg.delay["health"] = 1.5
    c = _client()
    _connect(c, fg.url)
    # The quick check gives up on a slow answer...
    r = c.post("/api/familygraph/test").get_json()
    assert r["status"] == "err" and "did not answer within 1 seconds" in r["error"]
    # ...the roster plan does not, and the status poll says what is running.
    fg.delay = {"plan": 1.5}
    from app import detector
    with patch.object(detector.llm, "llm_call", return_value=LLM):
        sid = c.post("/api/anonymize/upload", data={"file": (io.BytesIO(_roster_csv()), "roster.csv"), "tags": "ALL"},
                     content_type="multipart/form-data").get_json()["session_id"]
        busy = []
        for _ in range(200):
            s = c.get(f"/api/anonymize/{sid}/status").get_json()
            if s["community_busy"]:
                busy.append((s["community_busy"], s["community_rows"], s["stage"]))
            if s.get("detection_complete"):
                break
            time.sleep(0.05)
    assert ("plan", 3, "community") in busy
    assert s["stage"] == "done" and s["community_busy"] == ""
    assert s["preview"]["community"]["status"] == "ready"


def test_a_plan_timeout_blocks_confirm_until_retry_or_opt_out(fg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")       # before app.config is imported
    from app.config import KEYS_DIR, OUTPUT_DIR
    fg.delay["plan"] = 2.5
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    assert view["status"] == "error" and view["busy"] == ""
    assert "did not answer within 1 seconds" in view["error"] and "Nothing was written" in view["error"]
    # Never a silent fallback to per-value tokens.
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 409 and "Family Graph unavailable" in r.get_json()["error"]
    res = c.get(f"/api/anonymize/{sid}/results").get_json()
    assert res["scrub_steps"] == [] and not res["scrub_started"] and res["verify_result"] is None
    assert list(OUTPUT_DIR.iterdir()) == [] and not list(KEYS_DIR.glob("*.key.json"))
    # Family Graph is back: ask again, decide, and the roster run completes.
    fg.delay.clear()
    view = c.post(f"/api/anonymize/{sid}/community/retry").get_json()
    assert view["status"] == "ready" and view["pending"] == 1
    c.post(f"/api/anonymize/{sid}/community/decide",
           json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"] and res["community"]["status"] == "committed"


def test_a_commit_timeout_leaves_no_partial_state(fg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")       # before app.config is imported
    from app.config import KEYS_DIR, LOG_FILE, OUTPUT_DIR
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    c.post(f"/api/anonymize/{sid}/community/decide",
           json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    fg.delay["commit"] = 2.5
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 504
    body = r.get_json()
    assert "did not answer within 1 seconds" in body["error"] and "Nothing was scrubbed" in body["error"]
    view = body["community"]
    # Family Graph may have written it: the choices are locked until the same
    # request is sent again (tests/test_community_flow_fixes.py).
    assert view["status"] == "commit_unknown" and view["busy"] == "" and "did not answer" in view["commit_error"]
    assert view["decisions"] == {DECISION["key"]: {"action": "attach", "target": DECISION["target"]}}
    res = c.get(f"/api/anonymize/{sid}/results").get_json()
    assert res["scrub_steps"] == [] and not res["scrub_started"] and res["verify_result"] is None
    assert res["community"] is None and res["error"] is None
    assert list(OUTPUT_DIR.iterdir()) == [] and not list(KEYS_DIR.glob("*.key.json"))
    assert "timed out" in LOG_FILE.read_text(encoding="utf-8")
    # The operator presses CONFIRM again once Family Graph is answering.
    fg.delay.clear()
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"] and res["community"]["status"] == "committed"


def test_one_family_graph_call_per_file_at_a_time(fg):
    from app import pipeline
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    sess = pipeline.get_session(sid)
    assert sess.lock.acquire(blocking=False)      # a commit or retry is running
    try:
        r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
        assert r.status_code == 409 and "still working" in r.get_json()["error"]
        assert r.get_json()["community"]["busy"] == "working"
        for path, payload in (("decide", {"key": DECISION["key"], "action": "create"}),
                              ("retry", {}), ("skip", {"skip": True})):
            assert c.post(f"/api/anonymize/{sid}/community/{path}", json=payload).status_code == 409, path
        assert c.get(f"/api/anonymize/{sid}/results").get_json()["community_busy"] == "working"
    finally:
        sess.lock.release()
    assert c.get(f"/api/anonymize/{sid}/community").get_json()["decisions"] == {}
    _decide = c.post(f"/api/anonymize/{sid}/community/decide",
                     json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    assert _decide.status_code == 200
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    _wait_results(c, sid)
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 409
    assert len([x for x in fg.requests if x["path"].endswith("/commit")]) == 1


def test_cancel_during_a_slow_commit_writes_nothing(fg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "10")      # before app.config is imported
    from app.config import KEYS_DIR, OUTPUT_DIR
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _roster_csv(), "roster.csv")
    c.post(f"/api/anonymize/{sid}/community/decide",
           json={"key": DECISION["key"], "action": "attach", "target": DECISION["target"]})
    fg.delay["commit"] = 1.0
    out = {}
    c2 = c.application.test_client()
    t = threading.Thread(target=lambda: out.setdefault(
        "r", c2.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})))
    t.start()
    for _ in range(100):
        if c.get(f"/api/anonymize/{sid}/results").get_json()["community_busy"] == "commit":
            break
        time.sleep(0.02)
    c.post(f"/api/anonymize/{sid}/cancel")
    t.join(10)
    assert out["r"].status_code == 410
    time.sleep(0.2)
    assert list(OUTPUT_DIR.iterdir()) == [] and not list(KEYS_DIR.glob("*.key.json"))


# ---------------------------------------------------------------------------
# Review labels (static/app.js)
# ---------------------------------------------------------------------------

FG_REVIEW_REASONS = [
    "no_first_name", "placeholder_name", "initial_only", "looks_like_organization",
    "same_name_twice_in_household", "several_strong_candidates", "household_disagrees",
    "candidate_archived", "several_household_namesakes", "possible_match", "role_mismatch",
    "linked_record_used_twice", "linked_record_archived", "linked_record_changed",
    "prior_link_used_twice", "prior_link_disagrees", "prior_link_unconfirmed",
    "same_person_as_another_record", "same_household_as_another_record",
    "members_in_several_households", "members_in_different_households", "household_archived",
    "same_address_no_known_members", "linked_household_archived", "linked_household_disagrees",
    "shared_contact_other_surname", "several_households_share_contact",
    "several_households_at_address", "new_adult_with_known_children",
]


def test_every_family_graph_review_reason_has_a_plain_label():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const REVIEW_WHY = \{(.*?)\n  \};", js, re.S).group(1)
    labels = dict(re.findall(r'^\s*([a-z_]+): "([^"]+)",', block, re.M))
    missing = [r for r in FG_REVIEW_REASONS if r not in labels]
    assert not missing, missing
    assert all(labels[r].strip() and "_" not in labels[r] for r in FG_REVIEW_REASONS)
    # An unknown reason still renders, underscores turned into spaces.
    assert 'REVIEW_WHY[r] || String(r).replace(/_/g, " ")' in js
    # A candidate first seen earlier in this file has no id yet; it is named by row.
    assert "(new in this file, row ${hit.sheet_row})" in js


# ---------------------------------------------------------------------------
# Provisional candidates and one person in two slots of one row
# ---------------------------------------------------------------------------

TWIN_HEADERS = ["Student First Name", "Student Last Name", "Parent 1 First Name", "Parent 1 Last Name",
                "Parent 2 First Name", "Parent 2 Last Name", "Notes"]
TWIN_ROWS = [
    ["Emma", "Smith", "Jane", "Smith", "Jane", "Smith", ""],
    ["Liam", "Smith", "Jane", "Smith", "Jane", "Smith", ""],
    ["Ava", "Garcia", "Marie", "Garcia", "", "", ""],
]
JANE, EMMA, LIAM, AVA = "I153422DFDD5CA946", "I4AD4825DB3958878", "IE73AAC749C2913BA", "I77C8C5C16567B8B3"
MARIE, SMITHS, GARCIAS = DECISION["target"], "F3D6B57AFBDADAEBD", "FB63F4C7E988BE81B"


def _p(key, role, given, family, cols, action, cid, **extra):
    return {"key": key, "slot": int(key.split(":")[2]), "role": role, "given_name": given, "family_name": family,
            "date_of_birth": None, "name_cells": [{"col": cols[0], "part": "given"}, {"col": cols[1], "part": "family"}],
            "action": action, "community_id": cid, **extra}


def _jane_cand(ref, cid=None):
    return {"community_id": cid, "sheet_ref": ref, "status": "active", "given_name": "Jane", "family_name": "Smith",
            "suffix": None, "date_of_birth": None, "role": "adult", "grade": None, "confidence": 0.9,
            "reasons": ["same_name_on_row"],
            "family": {"community_id": None if cid is None else SMITHS, "sheet_ref": "0:0:family",
                       "display_name": "Smith Family"}}


def _twin_result(committed: bool) -> dict:
    """Family Graph's answer for TWIN_ROWS. Parent 2 on rows 1 and 2 is Jane
    again (the same name twice in a household); in a plan she is new in this
    upload, so her candidate has no id - only a sheet ref."""
    ids = (lambda x: x) if committed else (lambda x: None)
    marie_cand = dict(PLAN["sheets"][0]["rows"][2]["persons"][0]["candidates"][0])
    rows = [
        {"index": 0, "persons": [
            _p("0:0:0", "parent", "Jane", "Smith", (2, 3), "new", ids(JANE)),
            _p("0:0:1", "parent", "Jane", "Smith", (4, 5), "review", ids(JANE),
               candidates=[_jane_cand("0:0:0")], review_reasons=["same_name_twice_in_household"]),
            _p("0:0:2", "child", "Emma", "Smith", (0, 1), "new", ids(EMMA), told_apart=1)],
         "family": {"key": "0:0:family", "display_name": "Smith Family", "cell": None, "action": "new",
                    "community_id": ids(SMITHS)}},
        {"index": 1, "persons": [
            _p("0:1:0", "parent", "Jane", "Smith", (2, 3), "matched", ids(JANE), same_as="0:0:0",
               matched=_jane_cand("0:0:0")),
            _p("0:1:1", "parent", "Jane", "Smith", (4, 5), "review", ids(JANE),
               candidates=[_jane_cand("0:0:0")], review_reasons=["same_name_twice_in_household"]),
            _p("0:1:2", "child", "Liam", "Smith", (0, 1), "new", ids(LIAM))],
         "family": {"key": "0:1:family", "display_name": "Smith Family", "cell": None, "action": "review",
                    "community_id": ids(SMITHS), "review_reasons": ["members_in_several_households"],
                    "candidates": [{"community_id": None, "sheet_ref": "0:0:family", "status": "active",
                                    "display_name": "Smith Family",
                                    "members": [{"community_id": None, "sheet_ref": "0:0:0",
                                                 "name": "Jane Smith", "role": "adult"}]}]}},
        {"index": 2, "persons": [
            _p("0:2:0", "parent", "Marie", "Garcia", (2, 3), "review", ids(MARIE),
               candidates=[marie_cand], review_reasons=["possible_match"]),
            _p("0:2:1", "child", "Ava", "Garcia", (0, 1), "new", ids(AVA))],
         "family": {"key": "0:2:family", "display_name": "Garcia Family", "cell": None, "action": "new",
                    "community_id": ids(GARCIAS)}},
    ]
    out = {"mode": "commit" if committed else "plan", "committed": committed,
           "summary": {"sheets": 1, "rows": 3, "persons": {"matched": 1, "new": 4, "review": 3, "skipped": 0},
                       "families": {"matched": 0, "new": 2, "review": 1}, "told_apart": 1},
           "pending": [] if committed else ["0:0:1", "0:1:1", "0:1:family", "0:2:0"],
           "sheets": [{"index": 0, "name": None, "skipped": None, "columns": TWIN_HEADERS, "rows": rows}]}
    if committed:
        out["import_runs"] = ["imp_twin0000000001"]
    return out


TWIN_DECISIONS = {
    "0:0:1": {"action": "attach", "target": "0:0:0"},
    "0:1:1": {"action": "attach", "target": "0:0:0"},
    "0:1:family": {"action": "attach", "target": "0:0:family"},
    "0:2:0": {"action": "attach", "target": MARIE},
}


def _twin_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TWIN_HEADERS)
    w.writerows(TWIN_ROWS)
    return buf.getvalue().encode()


def _twin_state():
    from app import community
    st = community.CommunityState(status="ready", plan=_twin_result(False))
    st.sheets = community.build_sheets([[TWIN_HEADERS] + TWIN_ROWS])
    return st


def test_attach_targets_are_validated_by_shape_kind_and_offer():
    from app import community
    st = _twin_state()
    ok = [("0:0:1", "0:0:0", "0:0:0"),                               # provisional person: sheet ref
          ("0:1:family", "0:0:FAMILY", "0:0:family"),                # provisional household, any case
          ("0:2:0", MARIE.lower(), MARIE),                           # I id, any case
          ("0:1:0", " 0:0:0 ", "0:0:0")]                             # an automatic match's own target
    for key, target, stored in ok:
        community.decide(st, key, "attach", target)
        assert st.decisions[key] == {"action": "attach", "target": stored}, key
    bad = [("0:0:1", "0:0:2", "listed candidates"),                  # right shape, not offered
           ("0:0:1", SMITHS, "I… id"),                               # a household id for a person
           ("0:1:family", "0:0:0", "F… id"),                         # a person ref for a household
           ("0:0:1", "0:0", "I… id"), ("0:0:1", "0:0:x", "I… id"), ("0:0:1", None, "I… id"),
           ("0:2:0", "I0000000000000000", "listed candidates")]
    for key, target, msg in bad:
        with pytest.raises(ValueError, match=msg):
            community.decide(st, key, "attach", target)
    with pytest.raises(ValueError):
        community.decide(st, "0:1:family", "skip")                   # households cannot be skipped


def test_decisions_the_plan_no_longer_offers_are_dropped():
    from app import community
    st = _twin_state()
    for k, d in TWIN_DECISIONS.items():
        community.decide(st, k, d["action"], d["target"])
    newer = _twin_result(False)
    newer["sheets"][0]["rows"][0]["persons"][1]["candidates"] = [_jane_cand("0:0:0", cid=None) | {"sheet_ref": "0:1:0"}]
    st.plan = newer
    community._prune_decisions(type("S", (), {"id": "t"})(), st)
    assert "0:0:1" not in st.decisions and set(st.decisions) == set(TWIN_DECISIONS) - {"0:0:1"}
    assert community.pending(st) == ["0:0:1"]


def test_same_person_in_two_slots_of_one_row(fg):
    """Parent 1 and Parent 2 are Jane, entered twice, on two rows. The
    operator picks the provisional candidate (new earlier in this file, no id
    yet): the decision carries the sheet ref, Family Graph gives both slots
    one id, and every layer handles one [I…] in two cells of a row."""
    from app.config import KEYS_DIR, OUTPUT_DIR
    fg.plan_body = _twin_result(False)
    fg.commit_body = _twin_result(True)
    fg.refused_body = {"error": "review_incomplete", "plan": _twin_result(False)}
    fg.expect = TWIN_DECISIONS
    c = _client()
    _connect(c, fg.url)
    sid, _ = _detect(c, _twin_csv(), "twins.csv")
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    assert view["pending"] == 4
    assert view["summary"]["told_apart"] == 1
    assert {i["key"]: i["told_apart"] for i in view["items"] if i["kind"] == "person"}["0:0:2"] == 1
    twin = next(i for i in view["items"] if i["key"] == "0:0:1")
    assert twin["candidates"][0]["community_id"] is None and twin["candidates"][0]["sheet_ref"] == "0:0:0"
    assert twin["review_reasons"] == ["same_name_twice_in_household"]
    for key, target in (("0:0:1", "0:0:0"), ("0:1:1", "0:0:0"), ("0:1:family", "0:0:FAMILY"),
                        ("0:2:0", MARIE.lower())):
        r = c.post(f"/api/anonymize/{sid}/community/decide", json={"key": key, "action": "attach", "target": target})
        assert r.status_code == 200, (key, r.get_json())
    assert c.post(f"/api/anonymize/{sid}/community/decide",
                  json={"key": "0:0:1", "action": "attach", "target": SMITHS}).status_code == 400
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"], res
    commit_req = [x for x in fg.requests if x["path"].endswith("/commit")][-1]
    assert commit_req["body"]["decisions"] == TWIN_DECISIONS, "sheet refs and ids go out canonical"

    out = OUTPUT_DIR / res["output_filename"]
    rows = list(csv.reader(out.read_text(encoding="utf-8").splitlines()))
    assert rows[0][-1] == "FAMILY_ID"
    for r in (1, 2):
        assert rows[r][2] == rows[r][4] == f"[{JANE}]", "one person, one id, in both slots"
        assert rows[r][-1] == f"[{SMITHS}]"
    assert rows[1][0] == f"[{EMMA}]" and rows[2][0] == f"[{LIAM}]" and rows[3][2] == f"[{MARIE}]"
    assert rows[3][4] == rows[3][5] == ""
    body = out.read_text(encoding="utf-8")
    for name in ("Jane", "Emma", "Liam", "Ava", "Marie", "Smith", "Garcia"):
        assert not re.search(rf"\b{name}\b", body), name

    key = json.loads((KEYS_DIR / res["key_filename"]).read_text())
    assert key["identity_registry"][JANE] == {"kind": "person", "display": "Jane Smith", "family": SMITHS}
    assert len(key["identity_registry"]) == 7            # 5 people + 2 households, Jane once
    jane_cells = sorted((c_["row"], c_["col"]) for c_ in key["identity_cells"] if c_["written"] == f"[{JANE}]")
    assert jane_cells == [(1, 2), (1, 4), (2, 2), (2, 4)]
    assert all(c_["original"] == "Jane" for c_ in key["identity_cells"] if c_["written"] == f"[{JANE}]")

    # The anonymized file restores exactly; an AI answer restores to the name.
    r = c.post("/api/unanonymize", data={
        "file": (io.BytesIO(out.read_bytes()), out.name), "keys": json.dumps([res["key_filename"]]),
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    restored = (OUTPUT_DIR / r.get_json()["output_filename"]).read_text(encoding="utf-8")
    assert list(csv.reader(restored.splitlines())) == [TWIN_HEADERS] + TWIN_ROWS
    r = c.post("/api/unanonymize/text", json={"text": f"[{JANE}] is both parents; [{JANE.lower()}] again.",
                                              "keys": [res["key_filename"]]})
    assert r.get_json()["text"] == "Jane Smith is both parents; Jane Smith again."


def test_same_person_twice_in_one_list_cell_restores_exactly(tmp_path):
    """A children column like "Emma, Emma" where both are one child: the
    cell gets the id twice (mirroring the cell) and restores to the exact
    original text."""
    from app import community
    from app.extractors import ExtractResult
    table = [["Parent", "Children"], ["Jane Smith", "Emma, Emma"]]
    st = community.CommunityState(status="committed")
    st.sheets = community.build_sheets([table])
    cells = [{"col": 1, "part": "list"}]
    st.result = {"sheets": [{"rows": [{"index": 0, "persons": [
        {"key": "0:0:0", "slot": 0, "given_name": "Jane", "family_name": "Smith", "community_id": JANE,
         "action": "new", "name_cells": [{"col": 0, "part": "full"}]},
        {"key": "0:0:1", "slot": 1, "given_name": "Emma", "family_name": None, "community_id": EMMA,
         "action": "new", "name_cells": cells},
        {"key": "0:0:2", "slot": 2, "given_name": "Emma", "family_name": "Smith", "community_id": EMMA,
         "action": "matched", "name_cells": cells}],
        "family": {"key": "0:0:family", "community_id": SMITHS, "display_name": "Smith Family", "cell": None}}]}]}
    ex = ExtractResult(text="", char_count=0, original_suffix="csv", payload={"rows": table})
    sess = type("S", (), {"community": st, "extract": ex})()
    ov = community.overrides_for(sess)
    assert ov.cells[(0, 1, 1)] == ("Emma, Emma", f"[{EMMA}], [{EMMA}]")
    assert ov.registry[EMMA] == {"kind": "person", "display": "Emma Smith", "family": SMITHS}, \
        "the fuller spelling names the person"
    assert {f"[{EMMA}]", EMMA, f"[{JANE}]", JANE, f"[{SMITHS}]", SMITHS} <= ov.tokens


# ---------------------------------------------------------------------------
# Community ids touch ONLY tabular files (owner requirement 2026-09-28)
# ---------------------------------------------------------------------------

# A document whose text happens to hold community-shaped and 16-hex strings.
DOC_LINES = [
    "Jane Smith emailed jane@example.org about badge [I0123456789ABCDEF].",
    "Serial FEDCBA9876543210 is on file.",
    "Household ref F0011223344556677, token [I1111222233334444], hash AABBCCDDEEFF0011.",
]
DOC_TEXT = "\n".join(DOC_LINES)
DOC_LLM = json.dumps([
    {"text": "Jane Smith", "type": "PERSON"}, {"text": "jane@example.org", "type": "EMAIL"},
    {"text": "I0123456789ABCDEF", "type": "ID"}, {"text": "FEDCBA9876543210", "type": "ID"},
])


def _pdf(lines) -> bytes:
    """A minimal one-page text PDF (no PDF library in the venv)."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "\n".join(["BT", "/F1 11 Tf", "14 TL", "72 720 Td"] + [f"({esc(x)}) Tj T*" for x in lines]
                       + ["ET"]).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def _docx(lines) -> bytes:
    from docx import Document
    doc = Document()
    for x in lines:
        doc.add_paragraph(x)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pptx(lines) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches
    pres = Presentation()
    slide = pres.slides.add_slide(pres.slide_layouts[6])
    tf = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(3)).text_frame
    tf.text = lines[0]
    for x in lines[1:]:
        tf.add_paragraph().text = x
    buf = io.BytesIO()
    pres.save(buf)
    return buf.getvalue()


def _norm(text: str) -> str:
    return re.sub(r"\[([A-Z]+)_[0-9A-F]{12}\]", r"[\1_x]", text)


def _doc_run(c, data, name):
    sid, _ = _detect(c, data, name, llm=DOC_LLM)
    view = c.get(f"/api/anonymize/{sid}/community").get_json()
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    res = _wait_results(c, sid)
    assert res["verify_result"]["passed"], res
    assert res["community"] is None
    text = c.get(f"/api/anonymize/{sid}/text").get_json()["text"]
    from app.config import KEYS_DIR
    key = json.loads((KEYS_DIR / res["key_filename"]).read_text())
    return view, res, text, key


@pytest.mark.parametrize("name,make", [
    ("memo.txt", lambda: DOC_TEXT.encode()), ("letter.docx", lambda: _docx(DOC_LINES)),
    ("report.pdf", lambda: _pdf(DOC_LINES)), ("slides.pptx", lambda: _pptx(DOC_LINES)),
])
def test_documents_are_identical_with_family_graph_on_or_off(fg, name, make):
    c = _client()
    off_view, _, off_text, off_key = _doc_run(c, make(), name)          # not connected
    _connect(c, fg.url)
    on_view, on_res, on_text, on_key = _doc_run(c, make(), name)        # connected
    assert fg.requests == [], "a document never reaches Family Graph"
    assert off_view["status"] == on_view["status"] == "off"
    assert _norm(on_text) == _norm(off_text)
    assert set(on_key) == set(off_key)
    for field_ in ("identity_registry", "identity_cells", "identity_columns", "community_source"):
        assert field_ not in on_key, field_
    assert "FAMILY_ID" not in on_text
    # Community-shaped PII the model flagged is replaced like any other
    # value; community-shaped text it did not flag is left exactly as is.
    assert "I0123456789ABCDEF" not in on_text and "FEDCBA9876543210" not in on_text
    assert on_text.count("[ID_") == 2
    for kept in ("F0011223344556677", "[I1111222233334444]", "AABBCCDDEEFF0011"):
        assert kept in on_text, kept


def test_restoring_a_document_never_rewrites_16_hex_text(fg):
    from app.config import OUTPUT_DIR
    c = _client()
    _connect(c, fg.url)
    _, res, _, _ = _doc_run(c, DOC_TEXT.encode(), "memo.txt")
    out = OUTPUT_DIR / res["output_filename"]
    r = c.post("/api/unanonymize", data={
        "file": (io.BytesIO(out.read_bytes()), out.name), "keys": json.dumps([res["key_filename"]]),
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["report"]["unresolved"] == []
    assert (OUTPUT_DIR / r.get_json()["output_filename"]).read_text(encoding="utf-8") == DOC_TEXT


def test_community_grammar_only_with_an_identity_registry():
    from app.restorer import build_index, restore_text
    doc_key = {"session_id": "cccc3333", "original_filename": "memo.txt", "created_at": "2026-09-01T00:00:00Z",
               "id_format": "hex12", "replacement_map": {"Jane Smith": "[PERSON_3A4F9C2B1D0E]"}}
    text = ("[PERSON_3A4F9C2B1D0E] / [I0123456789ABCDEF] / i0123456789abcdef / STUDENT_I0123456789ABCDEF"
            " / 0123456789ABCDEF / [F01234567] / \\[F0123456789ABCDEF\\]")
    out, rep = restore_text(text, build_index([("memo", doc_key)]))
    assert out == text.replace("[PERSON_3A4F9C2B1D0E]", "Jane Smith"), "nothing else is touched"
    assert rep.restored == 1 and rep.unresolved == [] and rep.unresolved_count == 0
    # With a roster key in the selection the tolerant community forms apply.
    roster_key = {"session_id": "dddd4444", "original_filename": "roster.xlsx", "created_at": "2026-09-02T00:00:00Z",
                  "replacement_map": {}, "identity_registry": {"I0123456789ABCDEF": {"kind": "person", "display": "Ann Lee"}}}
    out2, rep2 = restore_text(text, build_index([("memo", doc_key), ("roster", roster_key)]))
    assert out2.startswith("Jane Smith / Ann Lee / Ann Lee / Ann Lee / Ann Lee / [F01234567]")
    assert rep2.unresolved == ["[F01234567]"]


def test_scrub_and_verify_protect_only_the_runs_own_community_tokens(tmp_path):
    from app.replacer import PLACEHOLDER_RE, LiteralReplacer, apply
    from app.verifier import verify_output
    rmap = {"I0123456789ABCDEF": "[ID_0A1B2C3D4E5F]", "F0123456789ABCDEF": "[ID_1A1B2C3D4E5F]",
            "0123456789ABCDEFAB": "[ID_2A1B2C3D4E5F]"}
    src = "badge [I0123456789ABCDEF], F0123456789ABCDEF, 0123456789ABCDEFAB"
    assert apply(src, LiteralReplacer(rmap)) == "badge [[ID_0A1B2C3D4E5F]], [ID_1A1B2C3D4E5F], [ID_2A1B2C3D4E5F]"
    assert not PLACEHOLDER_RE.search("[I0123456789ABCDEF] [F0123456789ABCDEF] [F01234567]")
    p = tmp_path / "out.txt"
    p.write_text(src, encoding="utf-8")
    v = verify_output(p, rmap)
    assert not v.passed and v.total_matches == 3, "community-shaped PII is still residue"
    # Only when this run itself wrote "[I0123456789ABCDEF]" is it protected.
    v = verify_output(p, rmap, extra_protected={"[I0123456789ABCDEF]", "I0123456789ABCDEF"})
    assert v.total_matches == 2
