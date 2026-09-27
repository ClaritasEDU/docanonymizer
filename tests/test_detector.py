"""Detector tests with a mocked LLM call."""

from __future__ import annotations

import json
from unittest.mock import patch


def test_extract_json_array_direct():
    from app.detector import _extract_json_array
    assert _extract_json_array('[{"text":"x","type":"PERSON"}]') == [{"text": "x", "type": "PERSON"}]


def test_extract_json_array_in_fence():
    from app.detector import _extract_json_array
    raw = "Sure, here:\n```json\n[{\"text\":\"x\",\"type\":\"PERSON\"}]\n```"
    out = _extract_json_array(raw)
    assert out == [{"text": "x", "type": "PERSON"}]


def test_extract_json_array_with_prose():
    from app.detector import _extract_json_array
    raw = "Some preamble: [{\"text\":\"x\",\"type\":\"EMAIL\"}] then more"
    assert _extract_json_array(raw) == [{"text": "x", "type": "EMAIL"}]


def test_extract_json_array_empty():
    from app.detector import _extract_json_array
    assert _extract_json_array("") == []
    assert _extract_json_array("nothing here") == []


def test_detect_pii_uses_llm_call_and_builds_registry():
    """End-to-end with a stubbed `llm_call`. No network."""
    from app import detector

    fake_response = json.dumps([
        {"text": "Jane Smith", "type": "PERSON", "linked_to": None},
        {"text": "jane@x.org", "type": "EMAIL", "linked_to": "Jane Smith"},
    ])

    with patch.object(detector.llm, "llm_call", return_value=fake_response):
        # Fake an active endpoint so detect_pii can be called.
        with patch.object(detector.endpoints_mod, "get_active",
                          return_value={"chunk_tokens": 2000, "api_style": "ollama",
                                        "base_url": "http://localhost:1", "model": "m",
                                        "nickname": "test"}):
            r = detector.detect_pii("Jane Smith met jane@x.org")
    assert r.total_replacements() == 2
    # Every value has its own 12-char ID; the link is recorded, not shared.
    hex_ids = {ph.split("_", 1)[1].rstrip("]") for ph in r.replacements.values()}
    assert len(hex_ids) == 2
    assert all(len(h) == 12 for h in hex_ids)
    person = r.text_to_hex["Jane Smith"]
    email = r.text_to_hex["jane@x.org"]
    assert r.as_serializable()[email]["linked_to"] == person


def test_detect_pii_never_reissues_ids_from_saved_keys(monkeypatch):
    """IDs already in /keys (or the ledger) are reserved for detection."""
    from app import detector, ids
    from app.config import KEYS_DIR

    (KEYS_DIR / "old_11111111.key.json").write_text(json.dumps({
        "session_id": "11111111", "original_filename": "old.xlsx",
        "replacement_map": {"Someone": "[PERSON_ABCDEF123456]"},
    }))
    ids.LEDGER_FILE.write_text("0A1B2C3D4E5F\n")
    proposals = iter(["ABCDEF123456", "0A1B2C3D4E5F", "1234567890AB"])
    monkeypatch.setattr(ids, "_random_id", lambda: next(proposals))

    fake = json.dumps([{"text": "Jane Smith", "type": "PERSON"}])
    with patch.object(detector.llm, "llm_call", return_value=fake):
        with patch.object(detector.endpoints_mod, "get_active",
                          return_value={"chunk_tokens": 2000, "api_style": "ollama",
                                        "base_url": "http://localhost:1", "model": "m",
                                        "nickname": "test"}):
            r = detector.detect_pii("Jane Smith")
    assert r.replacements["Jane Smith"] == "[PERSON_1234567890AB]"


def test_prompt_describes_12_char_ids():
    from app.detector import _build_prompt
    from app.mapper import EntityRegistry
    prompt = _build_prompt("x", ["PERSON"], EntityRegistry())
    assert "12-character" in prompt and "4-character" not in prompt


def test_detector_retries_transient_llm_error():
    """A transient chunk failure is retried and the run completes."""
    from unittest.mock import patch as _patch
    from app import detector

    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise detector.llm.LLMError("boom")
        return json.dumps([{"text": "Jane Smith", "type": "PERSON"}])

    with patch.object(detector.llm, "llm_call", side_effect=flaky), \
         _patch.object(detector.time, "sleep"):
        with patch.object(detector.endpoints_mod, "get_active",
                          return_value={"chunk_tokens": 200, "api_style": "ollama",
                                        "base_url": "http://localhost:1", "model": "m",
                                        "nickname": "test"}):
            r = detector.detect_pii("Jane Smith. " * 1000)
    assert r.total_replacements() >= 1
    assert call_count["n"] >= 2  # the failed attempt was retried


def test_detector_aborts_when_chunk_permanently_fails():
    """CODE_REVIEW C2: an unscannable chunk must fail the run, never skip.

    Silently skipping a chunk means PII in it ships unscrubbed - names and
    addresses have no regex safety net.
    """
    import pytest
    from unittest.mock import patch as _patch
    from app import detector

    def always_fail(*args, **kwargs):
        raise detector.llm.LLMError("endpoint down")

    progress = []
    with patch.object(detector.llm, "llm_call", side_effect=always_fail), \
         _patch.object(detector.time, "sleep"):
        with patch.object(detector.endpoints_mod, "get_active",
                          return_value={"chunk_tokens": 2000, "api_style": "ollama",
                                        "base_url": "http://localhost:1", "model": "m",
                                        "nickname": "test"}):
            with pytest.raises(detector.llm.LLMError, match="could not be scanned"):
                detector.detect_pii("Jane Smith met Bob.", on_chunk=progress.append)
    # The chunk error was surfaced to the progress stream before aborting.
    assert any(p.get("error") for p in progress)
