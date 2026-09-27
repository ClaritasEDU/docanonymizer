"""Deterministic detection that runs after the LLM.

Measured on real llama3.2 with a 25-row pledge sheet: the model alone found
91 of 112 PII values. It missed phones written 512-555-2202, a run of street
addresses, and names in the Donor column. The verifier cannot catch a name
or address that was never detected, so recall has to be won here.

Two layers, both deterministic:

1. Patterns - high-precision shapes the model has no excuse to miss: email,
   phone, SSN, card number (Luhn-checked), IPv4, US street address.

2. Column consensus (spreadsheets) - if the model tagged at least half of a
   column's values with one type, the column IS that type: every other value
   in it gets the same tag. The first non-empty cell of a column is treated
   as its header and never added. Amount-like values are never added unless
   the type is one where a bare number is PII (SID, GRADE).

Everything added here shows up in the mandatory preview like any other
detection. Logs counts only, never values.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from .logging_setup import get_logger

log = get_logger("backstop")

# Types where a bare short number can itself be PII.
NUMERIC_PII_TAGS = {"SID", "GRADE"}


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_STREET = (r"St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Rd|Road|Ct|Court|Way|"
           r"Pl|Place|Cir|Circle|Pkwy|Parkway|Ter|Terrace|Trl|Trail|Loop|Hwy|Highway")

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("FINANCIAL", re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b|\b\d{15,16}\b")),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+?1[-. ])?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")),
    ("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                      r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")),
    ("ADDRESS", re.compile(
        rf"\b\d{{1,6}}[ \t]+(?:[A-Z][A-Za-z']*[ \t]+){{0,4}}(?:{_STREET})\b\.?"
        r"(?:,?[ \t]*(?:Apt|Apartment|Suite|Ste|Unit|#)\.?[ \t]*[\w-]+)?"
        r"(?:,[ \t]*[A-Z][A-Za-z]+(?:[ \t][A-Z][A-Za-z]+)*,?[ \t]+[A-Z]{2}(?:[ \t]+\d{5}(?:-\d{4})?)?)?")),
]


def pattern_hits(text: str, allowed: Iterable[str]) -> list[tuple[str, str]]:
    """(value, tag) for every pattern match whose tag is allowed."""
    allowed = set(allowed)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag, pat in PATTERNS:
        if tag not in allowed:
            continue
        for m in pat.finditer(text):
            v = m.group(0).strip().rstrip(".,")
            if tag == "FINANCIAL" and not _luhn_ok(re.sub(r"\D", "", v)):
                continue
            if tag == "IP" and all(len(o) == 1 for o in v.split(".")):
                continue                               # version string 1.2.3.4
            if v and v not in seen:
                seen.add(v)
                out.append((v, tag))
    return out


def column_consensus(tables: list[list[list[str]]], detected: dict[str, str],
                     is_amount) -> list[tuple[str, str]]:
    """Values to add because their column is predominantly one PII type.

    `tables` is a list of sheets, each a list of rows of cell strings.
    `detected` maps already-detected originals to their tag.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rows in tables:
        if len(rows) < 3:
            continue
        ncols = max((len(r) for r in rows), default=0)
        for j in range(ncols):
            cells = [r[j].strip() for r in rows if j < len(r) and r[j] and r[j].strip()]
            if len(cells) < 3:
                continue
            header, values = cells[0], cells[1:]
            if header in detected:                 # no header row - keep it in play
                values = cells
            tags = Counter(detected[v] for v in values if v in detected)
            if not tags:
                continue
            tag, hits = tags.most_common(1)[0]
            if hits < 2 or hits / len(values) < 0.5:
                continue
            for v in values:
                if v in detected or v in seen or len(v) > 120:
                    continue
                if tag not in NUMERIC_PII_TAGS and is_amount(v):
                    continue
                seen.add(v)
                out.append((v, tag))
    return out
