"""12-char ID rules and the uniqueness ledger (ids.py)."""

from __future__ import annotations

import json


def test_id_format():
    from app import ids
    for _ in range(3000):
        i = ids.new_id()
        assert ids.is_valid_id(i)
        assert len(i) == 12 and i == i.upper()


def test_is_valid_id_rejects_bad_shapes():
    from app.ids import is_valid_id
    assert is_valid_id("3A4F9C2B1D0E")
    assert not is_valid_id("123456789012")     # digits only - looks like a number
    assert not is_valid_id("ABCDEFABCDEF")     # letters only
    assert not is_valid_id("3a4f9c2b1d0e")     # lowercase
    assert not is_valid_id("3A4F9C2B1D0")      # 11 chars
    assert not is_valid_id("3A4F")             # legacy length
    assert not is_valid_id(None)


def test_load_reserved_reads_ledger_and_key_files():
    from app import ids
    from app.config import KEYS_DIR
    ids.LEDGER_FILE.write_text("ABCDEF123456\njunk\n0a1b2c3d4e5f\n")
    (KEYS_DIR / "a_11111111.key.json").write_text(json.dumps({
        "session_id": "11111111", "original_filename": "a.xlsx",
        "entity_registry": {"1234567890AB": {}},
        "replacement_map": {"Jane": "[PERSON_FEDCBA654321]", "old": "[PERSON_3A4F]"},
    }))
    (KEYS_DIR / "broken.key.json").write_text("{not json")
    reserved = ids.load_reserved()
    assert reserved == {"ABCDEF123456", "0A1B2C3D4E5F", "1234567890AB", "FEDCBA654321"}


def test_save_key_file_records_ids_in_ledger():
    from app import ids
    from app.key_files import save_key_file
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    r.add("Jane Smith", "PERSON")
    r.add("jane@x.org", "EMAIL")
    path = save_key_file("abcd1234", "donors.xlsx", {}, ["PERSON", "EMAIL"], r)
    payload = json.loads(path.read_text())
    assert payload["id_format"] == "hex12"
    ledger = set(ids.LEDGER_FILE.read_text().split())
    assert ledger == set(r.entities)


def test_ledger_survives_key_file_deletion():
    """A released ID stays reserved even after its key file is moved away."""
    from app import ids
    from app.key_files import save_key_file
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    r.add("Jane Smith", "PERSON")
    path = save_key_file("abcd1234", "donors.xlsx", {}, ["PERSON"], r)
    path.unlink()
    assert set(r.entities) <= ids.load_reserved()


def test_new_id_raises_when_everything_is_taken(monkeypatch):
    import pytest
    from app import ids
    monkeypatch.setattr(ids, "_random_id", lambda: "ABCDEF123456")
    with pytest.raises(RuntimeError):
        ids.new_id(lambda c: True)
