"""Temp-file hygiene + pipeline robustness tests (CODE_REVIEW H1, C2, custom terms).

CLAUDE.md mandate: files in /uploads must be deleted immediately after
processing completes OR fails - not on a schedule, not only on download.
"""

from __future__ import annotations

import io
import json
import time
from unittest.mock import patch


def _client():
    from app.server import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _wait(cond, timeout_s=4.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    return cond()


def _run_to_results(c, body: bytes, name="memo.txt", custom_terms=""):
    r = c.post("/api/anonymize/upload", data={
        "file": (io.BytesIO(body), name),
        "tags": "ALL",
        "custom_terms": custom_terms,
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["session_id"]
    _wait(lambda: c.get(f"/api/anonymize/{sid}/status").get_json().get("detection_complete")
                  or c.get(f"/api/anonymize/{sid}/status").get_json().get("error"))
    return sid


FAKE = json.dumps([{"text": "Jane Smith", "type": "PERSON", "linked_to": None}])


def test_upload_deleted_after_successful_run():
    from app import detector
    from app.config import UPLOADS_DIR

    c = _client()
    with patch.object(detector.llm, "llm_call", return_value=FAKE):
        sid = _run_to_results(c, b"Hello Jane Smith")
        c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
        res = _wait(lambda: (c.get(f"/api/anonymize/{sid}/results").get_json() or {}).get("verify_result")
                    and c.get(f"/api/anonymize/{sid}/results").get_json())
    assert res["verify_result"]["passed"] is True
    # The upload is gone the moment the run succeeds - no download required.
    assert list(UPLOADS_DIR.iterdir()) == []


def test_upload_deleted_when_detection_fails():
    from app import detector
    from app.config import UPLOADS_DIR

    def always_fail(*args, **kwargs):
        raise detector.llm.LLMError("endpoint down")

    c = _client()
    with patch.object(detector.llm, "llm_call", side_effect=always_fail), \
         patch.object(detector.time, "sleep"):
        sid = _run_to_results(c, b"Hello Jane Smith")
        status = c.get(f"/api/anonymize/{sid}/status").get_json()
    assert status["error"]
    assert "detection failed" in status["error"]
    assert list(UPLOADS_DIR.iterdir()) == []


def test_upload_deleted_when_extraction_fails():
    from app.config import UPLOADS_DIR

    c = _client()
    # A corrupt xlsx: right extension, garbage bytes -> extractor raises.
    sid = _run_to_results(c, b"this is not a zip", name="broken.xlsx")
    status = c.get(f"/api/anonymize/{sid}/status").get_json()
    assert status["error"]
    assert list(UPLOADS_DIR.iterdir()) == []


def test_failed_verification_quarantines_output():
    """A failed-verify output still contains PII - it must not stay on disk."""
    from app import detector, verifier
    from app.config import OUTPUT_DIR, UPLOADS_DIR

    fail = verifier.VerifyResult(passed=False, total_matches=1, map_match_types=["PERSON"])
    c = _client()
    with patch.object(detector.llm, "llm_call", return_value=FAKE), \
         patch("app.pipeline.verify_output", return_value=fail):
        sid = _run_to_results(c, b"Hello Jane Smith")
        c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
        res = _wait(lambda: (c.get(f"/api/anonymize/{sid}/results").get_json() or {}).get("verify_result")
                    and c.get(f"/api/anonymize/{sid}/results").get_json())
    assert res["verify_result"]["passed"] is False
    assert list(OUTPUT_DIR.iterdir()) == []
    assert list(UPLOADS_DIR.iterdir()) == []
    # Download reads as blocked, not merely missing.
    assert c.get(f"/api/anonymize/{sid}/download/file").status_code == 403


def test_cancel_removes_upload():
    from app import detector
    from app.config import UPLOADS_DIR

    c = _client()
    with patch.object(detector.llm, "llm_call", return_value=FAKE):
        sid = _run_to_results(c, b"Hello Jane Smith")
        c.post(f"/api/anonymize/{sid}/cancel")
    assert list(UPLOADS_DIR.iterdir()) == []


def test_custom_terms_are_guaranteed_catches():
    """Operator terms are registered even when the LLM misses them entirely."""
    from app import detector

    c = _client()
    # LLM finds nothing at all.
    with patch.object(detector.llm, "llm_call", return_value="[]"):
        sid = _run_to_results(
            c, b"Report prepared by Maria Gonzalez for St. Theresa.",
            custom_terms="Maria Gonzalez\nORG: St. Theresa",
        )
        status = c.get(f"/api/anonymize/{sid}/status").get_json()
    preview = status["preview"]
    originals = {s["original"]: s["placeholder"] for s in preview["spans"]}
    assert "Maria Gonzalez" in originals
    assert originals["Maria Gonzalez"].startswith("[PERSON_")
    assert originals["St. Theresa"].startswith("[ORG_")


def test_double_confirm_is_ignored():
    """After a successful scrub the source temp files are gone - a second
    confirm must be a no-op, not a crash."""
    from app import detector

    c = _client()
    with patch.object(detector.llm, "llm_call", return_value=FAKE):
        sid = _run_to_results(c, b"Hello Jane Smith")
        c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
        res = _wait(lambda: (c.get(f"/api/anonymize/{sid}/results").get_json() or {}).get("verify_result")
                    and c.get(f"/api/anonymize/{sid}/results").get_json())
        assert res["verify_result"]["passed"] is True
        # Second confirm - must not error the session or touch the output.
        c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
        time.sleep(0.2)
        res2 = c.get(f"/api/anonymize/{sid}/results").get_json()
    assert res2["verify_result"]["passed"] is True
    assert not res2["error"]
    assert c.get(f"/api/anonymize/{sid}/download/file").status_code == 200


def test_verify_crash_surfaces_error_and_quarantines_output(tmp_path):
    """An exception during verification must end the run with an error the UI
    can show - never a silently dead worker - and nothing is released."""
    import json as _json
    from unittest.mock import patch as _patch
    from app import detector, pipeline
    from app.config import OUTPUT_DIR, UPLOADS_DIR

    up = UPLOADS_DIR / "memo.txt"
    up.write_text("Hello Jane Smith")
    sess = pipeline.new_session(up, "memo.txt", ["PERSON"],
                                endpoint={"base_url": "http://localhost:1", "model": "m",
                                          "api_style": "ollama", "nickname": "t"})
    fake = _json.dumps([{"text": "Jane Smith", "type": "PERSON"}])
    with _patch.object(detector.llm, "llm_call", return_value=fake):
        pipeline.run_extract_and_detect(sess)
    with _patch("app.pipeline.verify_output", side_effect=ValueError("bad workbook")):
        pipeline.confirm_and_scrub(sess)
    assert sess.error and "verification could not run" in sess.error
    assert sess.key_path is None
    assert list(OUTPUT_DIR.iterdir()) == []
    assert not up.exists()


def test_startup_purges_uploads_left_by_an_unfinished_run():
    """Anything in uploads/ at startup is an original document from a run
    that never finished (crash, restart, closed tab) - delete it."""
    import tempfile
    from pathlib import Path
    from app.config import UPLOADS_DIR
    (UPLOADS_DIR / "donors.xlsx").write_bytes(b"original document bytes")
    (UPLOADS_DIR / "memo_1.txt").write_text("Jane Smith")
    conv = Path(tempfile.mkdtemp(prefix="docanon-conv-"))
    (conv / "x.docx").write_bytes(b"converted copy")
    from app.server import create_app
    create_app()
    assert list(UPLOADS_DIR.iterdir()) == []
    assert not conv.exists()


def test_session_abandoned_at_preview_is_discarded():
    import json as _json
    from unittest.mock import patch as _patch
    from app import detector, pipeline
    from app.config import UPLOADS_DIR
    up = UPLOADS_DIR / "memo.txt"
    up.write_text("Hello Jane Smith")
    sess = pipeline.new_session(up, "memo.txt", ["PERSON"],
                                endpoint={"base_url": "http://localhost:1", "model": "m",
                                          "api_style": "ollama", "nickname": "t"})
    with _patch.object(detector.llm, "llm_call",
                       return_value=_json.dumps([{"text": "Jane Smith", "type": "PERSON"}])):
        pipeline.run_extract_and_detect(sess)
    assert pipeline.expire_abandoned() == 0          # fresh: kept
    assert up.exists()
    sess.detected_at -= pipeline.ABANDONED_AFTER_S + 1
    assert pipeline.expire_abandoned() == 1          # stale: discarded
    assert not up.exists()
    assert pipeline.get_session(sess.id) is None


def test_expiry_never_touches_running_or_finished_sessions():
    from pathlib import Path
    from app import pipeline
    from app.config import UPLOADS_DIR
    running = pipeline.new_session(UPLOADS_DIR / "a.txt", "a.txt", ["PERSON"], endpoint={})
    running.upload_path.write_text("x")                  # detection never finished
    done = pipeline.new_session(UPLOADS_DIR / "b.txt", "b.txt", ["PERSON"], endpoint={})
    done.detected_at = 0.0
    done.verify_result = {"passed": True}                # finished long ago
    assert pipeline.expire_abandoned(max_idle_s=0) == 0
    assert running.upload_path.exists()


def test_upload_route_survives_worker_finishing_first():
    """Race found under load: a corrupt file's worker fails and deletes the
    upload before the route builds its response - which then crashed (500)
    reading the file size. Force the worker to finish first."""
    import io as _io
    from unittest.mock import patch as _patch
    from app.config import UPLOADS_DIR

    class Inline:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    c = _client()
    with _patch("app.server.threading.Thread", Inline):
        r = c.post("/api/anonymize/upload", data={"file": (_io.BytesIO(b"not a zip"), "broken.xlsx"),
                                                  "tags": "ALL"}, content_type="multipart/form-data")
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["size"] == 9
    status = c.get(f"/api/anonymize/{body['session_id']}/status").get_json()
    assert status["error"] and list(UPLOADS_DIR.iterdir()) == []
