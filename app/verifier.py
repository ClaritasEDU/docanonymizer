"""Post-scrub verification (PRD 5.10, policy revised per CODE_REVIEW C1).

Two scans over the output:

  1. Replacement-map scan - any original PII string still present anywhere in
     the output is a HARD FAIL. The output is not released. This scan covers
     both the format-native extraction (what a reader sees) and, for
     DOCX/XLSX, the raw XML of every part in the archive - so formula
     literals, alt text, and metadata are checked even when the extractor
     can't see them.

  2. Regex residue scan - generic shapes (email / phone / SSN / IP / card).
     These are WARNINGS, not failures. The patterns cannot tell a missed
     phone number from an invoice number, and a hard gate here dead-ends
     clean documents (confirmed false positives: any 10-digit number,
     version strings). Warnings are surfaced to the operator for review.

Placeholders are excluded from the map scan. A short original ("94", "2B")
can legitimately occur inside a 12-char ID like [GRADE_3A94F2B1C0DE]; that is
not leaked PII, and counting it would dead-end clean documents.

Logs pass/fail and match types only - never matched values.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .extractors import extract
from .logging_setup import get_logger
from .replacer import PLACEHOLDER_RE, LiteralReplacer

log = get_logger("verify")


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


def _version_like(ip: str) -> bool:
    """All-single-digit octets read as version strings (1.2.3.4), not hosts."""
    return all(len(o) == 1 for o in ip.split("."))


# Residue patterns. Tightened per CODE_REVIEW C1: phone requires separators,
# IP octets are range-checked, card candidates must pass Luhn.
PATTERNS: dict[str, re.Pattern] = {
    "EMAIL":       re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
    "PHONE":       re.compile(r"(?:\+?1[-. ])?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
    "SSN":         re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "IP":          re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                              r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d{4}[\s\-]){3}\d{4}\b|\b\d{15,16}\b"),
}

_PLACEHOLDER_RE = PLACEHOLDER_RE
_TAG_RE = re.compile(r"<[^>]+>")
_SHEET_PART_RE = re.compile(r"xl/worksheets/sheet\d*\.xml")
_V_RE = re.compile(r"<v>[^<]*</v>")
# A number-only text node with fewer than 7 digits is XML bookkeeping (word
# counts, offsets, indexes), not document content - real content numbers are
# covered by the format-native extraction above. Matches the scrubber's rule.
_SHORT_NUMERIC_NODE_RE = re.compile(r">([\s\d.,:+\-]+)<")


# Formula text (cells, defined names, chart refs): only "string literals" can
# hold PII. The rest is references and operators - a PII value like "B7"
# legitimately remains there as a cell reference and must not fail the scan.
_FORMULA_EL_RE = re.compile(
    r"(<(?:\w+:)?(f|definedName)(?:\s[^>]*)?(?<!/)>)(.*?)(</(?:\w+:)?\2>)", re.DOTALL)
_STRING_LIT_RE = re.compile(r'"(?:[^"]|"")*"')


def _formula_literals_only(xml: str) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(3).replace("&quot;", '"')
        lits = [x[1:-1].replace('""', '"') for x in _STRING_LIT_RE.findall(inner)]
        return m.group(1) + "\n".join(lits) + m.group(4)
    return _FORMULA_EL_RE.sub(repl, xml)


def _blank_short_numbers(xml: str) -> str:
    def repl(m: re.Match) -> str:
        node = m.group(1)
        return ">" + (node if sum(c.isdigit() for c in node) >= 7 else "") + "<"
    return _SHORT_NUMERIC_NODE_RE.sub(repl, xml)
_ZIP_SUFFIXES = {".docx", ".xlsx"}
_TEXT_PART_RE = re.compile(r"\.(xml|rels|txt|vml)$", re.IGNORECASE)


@dataclass
class VerifyResult:
    passed: bool
    map_match_types: list[str] = field(default_factory=list)
    regex_match_types: list[str] = field(default_factory=list)   # warnings
    total_matches: int = 0                                       # map matches only


def _tag_of(placeholder: str) -> str:
    body = placeholder.strip("[]")
    return body.split("_", 1)[0] if "_" in body else "UNKNOWN"


def _xml_unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def _deep_zip_text(path: Path) -> str:
    """Tag-stripped text of every XML part in a DOCX/XLSX archive.

    Tags are replaced with newlines so text from adjacent elements can't fuse
    into a false match. This catches PII the format-native extractor can't
    see: formula literals, alt text, metadata fields, stray parts.
    """
    chunks: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not _TEXT_PART_RE.search(name):
                    continue
                try:
                    raw = zf.read(name).decode("utf-8")
                except (UnicodeDecodeError, KeyError):
                    continue
                if _SHEET_PART_RE.fullmatch(name):
                    raw = _V_RE.sub("", raw)     # numbers + shared-string indexes
                raw = _formula_literals_only(raw)
                raw = _blank_short_numbers(raw)
                chunks.append(_xml_unescape(_TAG_RE.sub("\n", raw)))
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("deep zip scan unavailable: %s", type(exc).__name__)
    return "\n".join(chunks)


def verify_output(output_path: Path, replacement_map: dict[str, str]) -> VerifyResult:
    """Run the verification pass against the freshly scrubbed file."""
    extracted = extract(output_path)
    texts = [extracted.text]
    if output_path.suffix.lower() in _ZIP_SUFFIXES:
        texts.append(_deep_zip_text(output_path))

    # One pass per text with the same engine the scrubber used. Placeholder
    # regions are protected, so only text outside them can count as residue.
    map_match_types: set[str] = set()
    map_total = 0
    finder = LiteralReplacer(replacement_map, protect_placeholders=True)
    for text in texts:
        for _, _, placeholder in finder.find(text):
            map_match_types.add(_tag_of(placeholder))
            map_total += 1

    # Regex residue - warnings only. Strip placeholders first (replaced with a
    # newline so surrounding digits can't fuse into a false phone/card match).
    regex_match_types: set[str] = set()
    scrubbed = _PLACEHOLDER_RE.sub("\n", "\n".join(texts))
    for label, pat in PATTERNS.items():
        for m in pat.finditer(scrubbed):
            v = m.group(0)
            if label == "IP" and _version_like(v):
                continue
            if label == "CREDIT_CARD" and not _luhn_ok(re.sub(r"\D", "", v)):
                continue
            regex_match_types.add(label)
            break  # one hit flags the type

    passed = map_total == 0

    log.info(
        "verify result: passed=%s map_types=%s warnings=%s map_matches=%d",
        passed,
        ",".join(sorted(map_match_types)) or "-",
        ",".join(sorted(regex_match_types)) or "-",
        map_total,
    )

    return VerifyResult(
        passed=passed,
        map_match_types=sorted(map_match_types),
        regex_match_types=sorted(regex_match_types),
        total_matches=map_total,
    )
