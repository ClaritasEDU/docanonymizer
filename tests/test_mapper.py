"""Entity registry / replacement map tests.

Owner rule 2026-09-27: every distinct value gets its own 12-char hex ID;
IDs are never shared between values. Model-reported relationships are kept
as `linked_to` in the key file, not by sharing an ID.
"""

import re

import pytest

PH12 = r"\[(?P<tag>[A-Z]+)_(?P<hex>[0-9A-F]{12})\]"


def _hex(ph: str) -> str:
    return re.fullmatch(PH12, ph).group("hex")


def test_basic_add_assigns_12_char_hex():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    p = r.add("Jane Smith", "PERSON")
    assert re.fullmatch(PH12, p)
    assert "Jane Smith" in r.replacements


def test_id_always_has_a_letter_and_a_digit():
    """So an ID can never be mistaken for a plain number or word in AI output."""
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    for i in range(2000):
        h = _hex(r.add(f"v{i}", "PERSON"))
        assert any(c.isdigit() for c in h) and any(c in "ABCDEF" for c in h)


def test_linked_values_get_their_own_ids():
    """Jane's name, email, and phone are three values -> three distinct IDs."""
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    person = r.add("Jane Smith", "PERSON")
    email = r.add("jane@x.org", "EMAIL", linked_to=_hex(person))
    phone = r.add("555-0001", "PHONE", linked_to="Jane Smith")
    hexes = {_hex(person), _hex(email), _hex(phone)}
    assert len(hexes) == 3
    assert len(r.entities) == 3


def test_link_recorded_in_key_file_not_in_id():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    person = _hex(r.add("Jane Smith", "PERSON"))
    email = _hex(r.add("jane@x.org", "EMAIL", linked_to=person))
    by_text = _hex(r.add("555-0001", "PHONE", linked_to="Jane Smith"))
    out = r.as_serializable()
    assert out[email]["linked_to"] == person
    assert out[by_text]["linked_to"] == person
    assert "linked_to" not in out[person]


def test_unknown_link_is_ignored():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    e = _hex(r.add("jane@x.org", "EMAIL", linked_to="ABCDEF123456"))
    assert "linked_to" not in r.as_serializable()[e]


def test_repeat_text_returns_existing_placeholder():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    a = r.add("Bob", "PERSON")
    b = r.add("Bob", "PERSON")
    assert a == b
    assert r.total_replacements() == 1


def test_replacement_map_sorted_longest_first():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    r.add("Jane", "PERSON")
    r.add("Jane Smith", "PERSON")
    keys = list(r.as_replacement_map().keys())
    assert keys == ["Jane Smith", "Jane"]


def test_drop_removes_replacement_entity_and_links():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    person = _hex(r.add("Jane Smith", "PERSON"))
    email = _hex(r.add("jane@x.org", "EMAIL", linked_to=person))
    assert r.drop("Jane Smith") is True
    assert "Jane Smith" not in r.replacements
    assert person not in r.entities
    # The email survives but no longer points at a deleted ID.
    assert "linked_to" not in r.as_serializable()[email]
    assert r.drop("not there") is False


def test_invalid_tag_raises():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    with pytest.raises(ValueError):
        r.add("x", "NOPE")


def test_merge_chunks_skips_invalid():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    r.merge_chunks([
        {"text": "Jane Smith", "type": "PERSON", "linked_to": None},
        {"text": "", "type": "PERSON"},                       # empty, skipped
        {"text": "x", "type": "BOGUS"},                       # bad tag, skipped
        {"text": 42, "type": "PERSON"},                       # non-string, skipped
        "garbage",                                            # not a dict, skipped
        {"text": "jane@x.org", "type": "EMAIL", "linked_to": "Jane Smith"},
    ])
    assert r.total_replacements() == 2
    assert r.total_entities() == 2


def test_hex_uniqueness_under_load():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    placeholders = {r.add(f"person_{i}", "PERSON") for i in range(5000)}
    assert len(r.entities) == 5000
    assert len({_hex(p) for p in placeholders}) == 5000


def test_reserved_ids_are_never_issued(monkeypatch):
    """Force the RNG to propose a reserved ID first - it must be skipped."""
    from app import ids
    from app.mapper import EntityRegistry
    proposals = iter(["ABCDEF123456", "ABCDEF123456", "0A1B2C3D4E5F"])
    monkeypatch.setattr(ids, "_random_id", lambda: next(proposals))
    r = EntityRegistry(reserved={"ABCDEF123456"})
    assert _hex(r.add("Jane Smith", "PERSON")) == "0A1B2C3D4E5F"


def test_ids_unique_across_registries_in_one_process(monkeypatch):
    """Two live sessions can never be handed the same ID."""
    from app import ids
    from app.mapper import EntityRegistry
    proposals = iter(["ABCDEF123456", "ABCDEF123456", "0A1B2C3D4E5F"])
    monkeypatch.setattr(ids, "_random_id", lambda: next(proposals))
    a = EntityRegistry()
    b = EntityRegistry()
    assert _hex(a.add("Jane Smith", "PERSON")) == "ABCDEF123456"
    assert _hex(b.add("Bob Torres", "PERSON")) == "0A1B2C3D4E5F"


def test_serializable_shape():
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    r.add("Jane Smith", "PERSON")
    r.add("jane@x.org", "EMAIL", linked_to="Jane Smith")
    out = r.as_serializable()
    assert len(out) == 2
    values = {tuple(rec["types"]): rec["values"] for rec in out.values()}
    assert values[("PERSON",)] == {"PERSON": "Jane Smith"}
    assert values[("EMAIL",)] == {"EMAIL": "jane@x.org"}


def test_same_text_second_tag_keeps_first_placeholder():
    """CODE_REVIEW M1: the placeholder must not flip when the LLM tags the
    same string differently in a later chunk. First tag wins; the extra tag
    is still recorded on the entity for the key file."""
    from app.mapper import EntityRegistry
    r = EntityRegistry()
    first = r.add("St. Theresa", "ORG")
    second = r.add("St. Theresa", "PERSON")
    assert first == second
    assert r.as_replacement_map()["St. Theresa"] == first
    rec = next(iter(r.as_serializable().values()))
    assert set(rec["types"]) == {"ORG", "PERSON"}
