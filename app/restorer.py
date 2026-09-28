"""Restore AI output - map identifiers back to the original values.

The round trip this serves: anonymize a file -> paste it into an AI tool ->
bring the AI's answer back here -> every identifier becomes the real value
again, with no chance of mixing up two people or two phone numbers.

AI tools rarely hand identifiers back byte-for-byte. Seen in the wild:
    [PERSON_3A4F9C2B1D0E]        exact
    [person_3a4f9c2b1d0e]        case changed
    \\[PERSON\\_3A4F9C2B1D0E\\]  markdown-escaped
    PERSON_3A4F9C2B1D0E          brackets dropped
    [DONOR_3A4F9C2B1D0E]         label changed by the AI
    3A4F9C2B1D0E                 only the ID survived
Because every 12-char ID is unique to one value (ids.py), the hex alone is
enough to restore safely - so all of the above resolve. When the label the
AI used differs from the key's label, the value is still restored by ID and
the report counts it as "relabeled" so the operator can glance at it.

Anything that looks like one of our identifiers but is NOT in the selected
keys (a typo, a truncated ID, a key that wasn't selected) is left untouched
and listed as unresolved. Nothing is guessed. As a final safety net, any
known ID still present after the scan (glued inside some other token) is
also listed, so a miss is never silent.

Legacy (v1.3) keys used 4-char IDs shared across an entity's tags. Those are
matched only with their tag present ([PERSON_3A4F] / PERSON_3A4F) - a bare
4-char hex is far too common in ordinary text to restore on its own.

Community identifiers (2026-09-28): roster cells carry Family Graph ids,
`[I…]` for an individual and `[F…]` for a household - one id per HUMAN, so
unlike the per-value ids they restore to the person's name (or the
household's label), not to one exact spelling. Tolerated forms:
    [I3A4F9C2B1D0E7F21]  [i3a4f9c2b1d0e7f21]  \\[I3A4F9C2B1D0E7F21\\]
    I3A4F9C2B1D0E7F21    STUDENT_I3A4F9C2B1D0E7F21    3A4F9C2B1D0E7F21
    FAMILY_9B0C11D2E3F4A5B6    [PERSON_3A4F9C2B1D0E7F21]   (letter dropped, any label)
A household saved with no label is reported unresolved, never restored to
an empty string.
The same id in two keys (the same person on two rosters) is not a conflict;
if the keys spell the name differently the newest key's spelling is used and
the report counts it. Restoring the anonymized file itself puts each cell's
exact original text back by position (unanonymize.py), not the name.

Never logs original values. Only counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .ids import HEX_LEN, LEGACY_HEX_LEN
from .logging_setup import get_logger
from .mapper import VALID_TAGS
from .replacer import Span, splice, xml_escape

log = get_logger("restore")


class KeyConflictError(ValueError):
    """Two selected keys give the same identifier different values."""


_COMMUNITY_TAG = {"I": "INDIVIDUAL", "F": "FAMILY"}


_KEY_PLACEHOLDER_RE = re.compile(r"\[([A-Z]+)_([0-9A-F]+)\]")

# Tolerant token grammar. Alternatives are tried left to right at each
# position; the regex scans the text once.
#   br - bracketed:  [TAG_HEX]  \[TAG\_HEX\]  [ tag_hex ]
#   nb - no brackets: TAG_HEX   TAG\_HEX   __TAG_HEX__ (markdown)   TAG_HEX_email
#   xh - bare 12-char hex
_TAG = r"[A-Za-z][A-Za-z_]{1,30}"
# Community ids: [I…] / [F…], optionally tag-prefixed or escaped, then bare
# I…/F… with 16 hex, then a bare 16-hex (the letter dropped). Used ONLY when
# a selected key carries an identity_registry (a roster with community ids):
# for any other key a 16-hex string or an I…/F…-shaped word is ordinary text
# and is neither rewritten nor reported.
_COMMUNITY_ALTS = (
    rf"(?P<cb>\\?\[[ \t]*(?:[A-Za-z][A-Za-z]{{1,30}}\\?_)?(?P<cbl>[IiFf])(?P<cbh>[0-9A-Fa-f]{{16}}|[0-9A-Fa-f]{{8}})[ \t]*\\?\])"
    rf"|(?<![0-9A-Za-z])(?P<cn>(?:[A-Za-z][A-Za-z]{{1,30}}\\?_)?(?P<cnl>[IiFf])(?P<cnh>[0-9A-Fa-f]{{16}}))(?![0-9A-Za-z])"
    rf"|(?<![0-9A-Za-z])(?P<cx>[0-9A-Fa-f]{{16}})(?![0-9A-Za-z])"
)
_VALUE_ALTS = (
    rf"(?P<br>\\?\[[ \t]*(?P<bt>{_TAG})\\?_(?P<bh>[0-9A-Fa-f]{{4,16}})[ \t]*\\?\])"
    rf"|(?<![0-9A-Za-z])(?P<nb>(?P<nt>{_TAG})\\?_(?P<nh>[0-9A-Fa-f]{{4,16}}))(?![0-9A-Fa-f])"
    rf"|(?<![0-9A-Za-z])(?P<xh>[0-9A-Fa-f]{{{HEX_LEN}}})(?![0-9A-Za-z])"
)
_TOKEN_RE = re.compile(_COMMUNITY_ALTS + "|" + _VALUE_ALTS)     # keys with community ids
_VALUE_TOKEN_RE = re.compile(_VALUE_ALTS)                       # every other key
_ANY_ID_RE = re.compile(rf"(?=([0-9A-Fa-f]{{{HEX_LEN}}}))")
_ANY_COMMUNITY_HEX_RE = re.compile(r"(?=([0-9A-Fa-f]{16}))")
_SHEET_ID_RE = re.compile(r"SHEET_([0-9A-F]{12})")
_COMMUNITY_ID_RE = re.compile(r"([IF])([0-9A-F]{16}|[0-9A-F]{8})")


@dataclass
class KeyIndex:
    by_hex: dict[str, tuple[str, str]] = field(default_factory=dict)           # HEX12 -> (tag, original)
    legacy: dict[tuple[str, str], str] = field(default_factory=dict)          # (TAG, HEX4) -> original
    key_names: list[str] = field(default_factory=list)
    legacy_ambiguous: int = 0      # v1.3 placeholders that pointed at 2+ values
    sheet_titles: dict[str, str] = field(default_factory=dict)   # anonymized -> original
    # Community ids: HEX (16 or 8, upper) -> (letter I/F, display name)
    community: dict[str, tuple[str, str]] = field(default_factory=dict)
    community_variants: int = 0    # same id spelled differently across keys
    # session id -> {"cells": [...], "columns": [...]} for exact file restores
    identity_layout: dict[str, dict] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.by_hex) + len(self.legacy) + len(self.community)


def build_index(keys: Iterable[tuple[str, dict]]) -> KeyIndex:
    """Merge one or more (name, key_payload) pairs into a lookup index.

    Raises KeyConflictError if two keys assign the same identifier to
    different values - restoring through that would risk a mix-up, so we
    refuse and say which keys disagree.
    """
    idx = KeyIndex()
    hex_owner: dict[str, str] = {}
    legacy_owner: dict[tuple[str, str], str] = {}
    community_when: dict[str, str] = {}
    for name, payload in keys:
        rmap = payload.get("replacement_map") or {}
        if not isinstance(rmap, dict):
            raise ValueError(f"key {name}: replacement_map is not an object")
        idx.key_names.append(name)
        created = str(payload.get("created_at") or "")
        registry = payload.get("identity_registry") or {}
        if isinstance(registry, dict):
            for cid, rec in registry.items():
                m = _COMMUNITY_ID_RE.fullmatch(cid) if isinstance(cid, str) else None
                if not m or not isinstance(rec, dict):
                    continue
                hx, display = m.group(2).upper(), str(rec.get("display") or "").strip()
                prev = idx.community.get(hx)
                if prev is not None and prev[1] != display:
                    if not display:
                        continue            # no label never replaces a real one
                    if prev[1]:
                        idx.community_variants += 1
                        if created < community_when.get(hx, ""):
                            continue        # an older spelling never wins
                idx.community[hx] = (m.group(1).upper(), display)
                community_when[hx] = created
        sid = payload.get("session_id")
        cells = payload.get("identity_cells")
        if isinstance(sid, str) and isinstance(cells, list):
            idx.identity_layout[sid] = {
                "cells": [c for c in cells if isinstance(c, dict)],
                "columns": [c for c in (payload.get("identity_columns") or []) if isinstance(c, dict)],
            }
        titles = payload.get("sheet_titles") or {}
        if isinstance(titles, dict):
            for new, old in titles.items():
                if not isinstance(new, str) or not isinstance(old, str):
                    continue
                if idx.sheet_titles.get(new, old) != old:
                    raise KeyConflictError(
                        f"sheet '{new}' has different original titles in two keys "
                        f"(one is {name}). Select only the key(s) this output came from."
                    )
                idx.sheet_titles[new] = old
                m = _SHEET_ID_RE.fullmatch(new)
                if m and m.group(1) not in idx.by_hex:
                    # "SHEET_<id>" in an AI answer restores to the real title.
                    idx.by_hex[m.group(1)] = ("SHEET", old)
                    hex_owner[m.group(1)] = name
        # Longest original first, so a legacy placeholder shared by
        # "Jane Smith" and "Jane" resolves to the fuller value.
        for original, placeholder in sorted(rmap.items(), key=lambda kv: -len(kv[0])):
            if not isinstance(original, str) or not isinstance(placeholder, str) or not original:
                continue
            m = _KEY_PLACEHOLDER_RE.fullmatch(placeholder)
            if not m:
                continue
            tag, hx = m.group(1), m.group(2)
            if len(hx) == HEX_LEN:
                prev = idx.by_hex.get(hx)
                if prev is not None and prev[1] != original:
                    raise KeyConflictError(
                        f"identifier {hx} means different values in "
                        f"{hex_owner[hx]} and {name}. Select only the key(s) "
                        "this output came from."
                    )
                if prev is None:
                    idx.by_hex[hx] = (tag, original)
                    hex_owner[hx] = name
            elif len(hx) == LEGACY_HEX_LEN:
                k = (tag, hx)
                prev = idx.legacy.get(k)
                if prev is not None and prev != original:
                    if legacy_owner[k] != name:
                        raise KeyConflictError(
                            f"[{tag}_{hx}] means different values in "
                            f"{legacy_owner[k]} and {name} (old 4-character "
                            "keys reuse IDs). Select only the key this output came from."
                        )
                    idx.legacy_ambiguous += 1
                    continue
                if prev is None:
                    idx.legacy[k] = original
                    legacy_owner[k] = name
    log.info(
        "key index built: keys=%d ids=%d legacy_ids=%d legacy_ambiguous=%d "
        "community_ids=%d community_variants=%d",
        len(idx.key_names), len(idx.by_hex), len(idx.legacy), idx.legacy_ambiguous,
        len(idx.community), idx.community_variants,
    )
    return idx


@dataclass
class RestoreReport:
    restored: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)
    relabeled: int = 0          # AI changed the label; restored by ID
    untagged: int = 0           # only the bare hex survived; restored by ID
    unresolved: list[str] = field(default_factory=list)   # distinct tokens, in order seen
    unresolved_count: int = 0
    residual: int = 0           # file restores: tokens still present after writing
    legacy_ambiguous: int = 0
    community_variants: int = 0   # one person spelled differently across keys
    keys_used: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "restored": self.restored,
            "by_tag": dict(sorted(self.by_tag.items())),
            "relabeled": self.relabeled,
            "untagged": self.untagged,
            "unresolved": self.unresolved,
            "unresolved_count": self.unresolved_count,
            "residual": self.residual,
            "legacy_ambiguous": self.legacy_ambiguous,
            "community_variants": self.community_variants,
            "keys_used": self.keys_used,
        }


class TokenRestorer:
    """Replacer (see replacer.py) that maps identifiers back to originals."""

    restoring = True   # scrubber: a token is never a cell reference

    def __init__(self, index: KeyIndex, escape_xml: bool = False):
        self.index = index
        self.escape_xml = escape_xml

    def _resolve(self, m: re.Match) -> tuple[Optional[str], str, dict]:
        """Returns (original or None, key_tag, flags)."""
        if "cb" in m.re.groupindex and (
                m.group("cb") is not None or m.group("cn") is not None or m.group("cx") is not None):
            return self._resolve_community(m)
        if m.group("xh") is not None:
            hx = m.group("xh").upper()
            hit = self.index.by_hex.get(hx)
            if hit is None:
                return None, "", {"ignore": True}   # ordinary hex in the text
            return hit[1], hit[0], {"untagged": True}

        bracketed = m.group("br") is not None
        tag = (m.group("bt") if bracketed else m.group("nt")).upper()
        hx = (m.group("bh") if bracketed else m.group("nh")).upper()
        tag_known = tag in VALID_TAGS

        if self.index.community and len(hx) in (16, 8) and hx in self.index.community:
            # A community id relabeled with its I/F dropped: FAMILY_9B0C...,
            # [PERSON_3A4F...]. The hex alone names one human or household.
            return self._community_hit(self.index.community[hx], relabeled=True)
        if len(hx) == HEX_LEN:
            hit = self.index.by_hex.get(hx)
            if hit is not None:
                return hit[1], hit[0], {"relabeled": hit[0] != tag}
            return None, "", {"report": True}
        if len(hx) == LEGACY_HEX_LEN:
            original = self.index.legacy.get((tag, hx))
            if original is not None:
                return original, tag, {}
            return None, "", {"report": bracketed and tag_known}
        # Wrong length: a truncated or padded ID. Report it if it clearly
        # was meant to be one of ours - with community ids in the key, any
        # tagged 16-hex is (whatever label the AI chose).
        report = (tag_known and (bracketed or len(hx) >= 8)) or (bool(self.index.community) and len(hx) == 16)
        return None, "", {"report": report}

    @staticmethod
    def _community_hit(hit: tuple[str, str], **flags) -> tuple[Optional[str], str, dict]:
        # A household saved with no label must not restore to "" - that would
        # delete the reference from the AI answer and count it as restored.
        if not hit[1]:
            return None, "", {"report": True}
        return hit[1], _COMMUNITY_TAG[hit[0]], flags

    def _resolve_community(self, m: re.Match) -> tuple[Optional[str], str, dict]:
        if m.group("cx") is not None:
            hit = self.index.community.get(m.group("cx").upper())
            if hit is None:
                return None, "", {"ignore": True}     # some other 16-hex string
            return self._community_hit(hit, untagged=True)
        bracketed = m.group("cb") is not None
        letter = (m.group("cbl") if bracketed else m.group("cnl")).upper()
        hx = (m.group("cbh") if bracketed else m.group("cnh")).upper()
        hit = self.index.community.get(hx)
        if hit is None:
            return None, "", {"report": True}
        return self._community_hit(hit, relabeled=hit[0] != letter)

    def find(self, text: str) -> list[Span]:
        return self._scan(text, None)

    def scan(self, text: str, report: RestoreReport) -> list[Span]:
        """find() that also tallies what happened into `report`."""
        return self._scan(text, report)

    def _scan(self, text: str, report: Optional[RestoreReport]) -> list[Span]:
        if not text or self.index.size == 0:
            return []
        spans: list[Span] = []
        seen_unresolved = set(report.unresolved) if report else set()
        covered = bytearray(len(text)) if report is not None else None

        def note_unresolved(tok: str) -> None:
            report.unresolved_count += 1
            if tok not in seen_unresolved:
                seen_unresolved.add(tok)
                report.unresolved.append(tok)

        grammar = _TOKEN_RE if self.index.community else _VALUE_TOKEN_RE
        for m in grammar.finditer(text):
            original, key_tag, flags = self._resolve(m)
            if covered is not None and (original is not None or flags.get("report")):
                covered[m.start():m.end()] = b"\x01" * (m.end() - m.start())
            if original is None:
                if report is not None and flags.get("report"):
                    note_unresolved(m.group(0))
                continue
            spans.append((m.start(), m.end(),
                          xml_escape(original) if self.escape_xml else original))
            if report is not None:
                report.restored += 1
                report.by_tag[key_tag] = report.by_tag.get(key_tag, 0) + 1
                if flags.get("relabeled"):
                    report.relabeled += 1
                if flags.get("untagged"):
                    report.untagged += 1
        if report is not None:
            # Safety net: a known ID glued inside some other token
            # ("xPERSON_3A4F9C2B1D0E9") was not restored - say so.
            for m in _ANY_ID_RE.finditer(text):
                p0 = m.start()
                if covered[p0] or self.index.by_hex.get(m.group(1).upper()) is None:
                    continue
                covered[p0:p0 + HEX_LEN] = b"\x01" * HEX_LEN
                note_unresolved(m.group(1))
            if self.index.community:
                for m in _ANY_COMMUNITY_HEX_RE.finditer(text):
                    p0 = m.start()
                    if covered[p0] or m.group(1).upper() not in self.index.community:
                        continue
                    covered[p0:p0 + 16] = b"\x01" * 16
                    note_unresolved(m.group(1))
        return spans

    def for_raw_xml(self) -> "TokenRestorer":
        return TokenRestorer(self.index, escape_xml=True)


def new_report(index: KeyIndex) -> RestoreReport:
    return RestoreReport(keys_used=list(index.key_names),
                         legacy_ambiguous=index.legacy_ambiguous,
                         community_variants=index.community_variants)


def restore_text(text: str, index: KeyIndex) -> tuple[str, RestoreReport]:
    """Restore a block of pasted text. Returns (restored_text, report)."""
    report = new_report(index)
    out = splice(text, TokenRestorer(index).scan(text, report))
    log.info(
        "text restore: chars_in=%d restored=%d relabeled=%d untagged=%d unresolved=%d",
        len(text), report.restored, report.relabeled, report.untagged, report.unresolved_count,
    )
    return out, report
