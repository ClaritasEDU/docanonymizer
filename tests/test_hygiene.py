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
