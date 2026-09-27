"""Single-pass replacement engine shared by anonymize, verify, and preview.

Why this exists (two bugs in the old per-entry `str.replace` loop):

1. Placeholder corruption. Applying the map one entry at a time lets a later,
   shorter original match INSIDE a placeholder an earlier entry just wrote.
   `[PERSON_3A4F9C2B1D0E]` contains "2B1D", "4F9", "SON"... a short SID or
   grade value would silently garble the placeholder and break reversal.
   Here every match is found against the ORIGINAL text in one scan, and any
   placeholder already present in the text is protected.

2. Quadratic time. The loop cost (values x cells); 1,000 spreadsheet rows took
   3s, 5,000 rows ~80s. All originals are compiled into one trie-shaped regex
   so each text is scanned once.

Matching semantics: leftmost-longest. Scanning left to right, at each
position the longest original that starts there wins ("Jane Smith" beats
"Jane"). Case-sensitive, exact substring - same as PRD 5.3 - with one
exception: a SHORT NUMBER (no letters, fewer than 7 digits - a score, room
number, ZIP, 5-digit student ID) matches only as a whole number. "94" must
not match inside 1945, 94.5, 1,945, or row index 94. Long numbers (phones,
SSNs, account numbers) keep plain substring matching, the safer choice when
a detected phone appears inside a longer form like +15125550101.

Public surface:
    Replacer                      - protocol: .find(text) -> [(start, end, repl)]
    LiteralReplacer(mapping)      - original -> placeholder (forward direction)
    apply(text, replacer)         - build the replaced string
    splice(text, spans)           - write precomputed spans into text
    as_replacer(map_or_replacer)  - accept either a dict or a Replacer
"""

from __future__ import annotations

import re
from typing import Optional, Protocol, Union

from .logging_setup import get_logger

log = get_logger("replacer")

# A placeholder this app writes: current 12-hex IDs, or legacy 4-hex IDs.
# The bracket-free form (PERSON_3A4F9C2B1D0E) is what goes where brackets are
# illegal - spreadsheet sheet names and the formula references to them.
PLACEHOLDER_RE = re.compile(
    r"\[[A-Z]+_(?:[0-9A-F]{12}|[0-9A-F]{4})\]"
    r"|(?<![A-Za-z0-9_])[A-Z]+_[0-9A-F]{12}(?![A-Za-z0-9_])"
)

Span = tuple[int, int, str]

_SHORT_NUMBER_DIGITS = 7
# Whole-number boundaries: no digit (or digit+separator) directly around it.
_NUM_BEFORE = r"(?<!\d)(?<!\d[.,])"
_NUM_AFTER = r"(?!\d)(?![.,]\d)"


def is_short_number(s: str) -> bool:
    """No letters, fewer than 7 digits, starts or ends with a digit."""
    return (
        bool(s)
        and not any(c.isalpha() for c in s)
        and sum(c.isdigit() for c in s) < _SHORT_NUMBER_DIGITS
        and (s[0].isdigit() or s[-1].isdigit())
    )


def _number_boundary_ok(text: str, s: int, e: int) -> bool:
    if s > 0 and text[s - 1].isdigit():
        return False
    if s > 1 and text[s - 1] in ".," and text[s - 2].isdigit():
        return False
    if e < len(text) and text[e].isdigit():
        return False
    if e + 1 < len(text) and text[e] in ".," and text[e + 1].isdigit():
        return False
    return True


class Replacer(Protocol):
    def find(self, text: str) -> list[Span]: ...
    def for_raw_xml(self) -> "Replacer": ...


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _trie_pattern(words: list[str]) -> str:
    """Regex matching any of `words`, preferring the longest at a position.

    A greedy optional group tries the longer continuation first and only
    backtracks to the shorter word if the longer one fails - so the result
    is longest-match without relying on alternation order.
    """
    END = ""  # key marking "a word ends here"
    root: dict = {}
    for w in words:
        node = root
        for ch in w:
            node = node.setdefault(ch, {})
        node[END] = True

    def emit(node: dict) -> str:
        parts: list[str] = []
        # Walk straight chains iteratively (keeps recursion to branch points).
        while True:
            kids = [k for k in node if k != END]
            if len(kids) == 1 and END not in node:
                parts.append(re.escape(kids[0]))
                node = node[kids[0]]
                continue
            break
        kids = sorted(k for k in node if k != END)
        if not kids:
            return "".join(parts)
        alts = [re.escape(k) + emit(node[k]) for k in kids]
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        if END in node:
            body = "(?:" + body + ")?" if len(alts) == 1 else body + "?"
        return "".join(parts) + body

    return emit(root)


class LiteralReplacer:
    """Exact originals -> replacement strings, leftmost-longest, one pass.

    `protect_placeholders` (default on) makes any existing `[TAG_HEX]` token
    in the text untouchable, so a pass over already-anonymized text can never
    rewrite part of a placeholder.
    """

    def __init__(self, mapping: dict[str, str], protect_placeholders: bool = True):
        self.mapping = {k: v for k, v in mapping.items() if k}
        self.protect = protect_placeholders
        self._regex: Optional[re.Pattern] = None
        self._fallback = False
        if self.mapping:
            words = sorted(self.mapping, key=len, reverse=True)
            texty = [w for w in words if not is_short_number(w)]
            numbers = [w for w in words if is_short_number(w)]
            try:
                alts = []
                if self.protect:
                    alts.append(f"(?P<ph>{PLACEHOLDER_RE.pattern})")
                # Texty words first: at any position where both could match,
                # the texty one is the longer (it contains a letter).
                if texty:
                    alts.append(f"(?P<o>{_trie_pattern(texty)})")
                if numbers:
                    alts.append(f"(?P<n>{_NUM_BEFORE}(?:{_trie_pattern(numbers)}){_NUM_AFTER})")
                self._regex = re.compile("|".join(alts))
            except (RecursionError, re.error, OverflowError, MemoryError) as exc:
                # Pathological originals (hundreds of nested branch points).
                # Correct but slower scan below.
                log.warning("trie regex unavailable (%s) - using fallback scan",
                            type(exc).__name__)
                self._fallback = True
                self._words = words

    def find(self, text: str) -> list[Span]:
        if not text or not self.mapping:
            return []
        if self._fallback:
            return self._find_fallback(text)
        spans: list[Span] = []
        groups = self._regex.groupindex
        for m in self._regex.finditer(text):
            hit = (m.group("o") if "o" in groups else None) or \
                  (m.group("n") if "n" in groups else None)
            if hit:
                spans.append((m.start(), m.end(), self.mapping[hit]))
        return spans

    def _find_fallback(self, text: str) -> list[Span]:
        occupied = bytearray(len(text))
        if self.protect:
            for m in PLACEHOLDER_RE.finditer(text):
                occupied[m.start():m.end()] = b"\x01" * (m.end() - m.start())
        # Leftmost-longest: collect every candidate, then take them in
        # (start asc, length desc) order, skipping overlaps.
        cands: list[tuple[int, int]] = []
        for w in self._words:
            short = is_short_number(w)
            i = text.find(w)
            while i != -1:
                if not short or _number_boundary_ok(text, i, i + len(w)):
                    cands.append((i, i + len(w)))
                i = text.find(w, i + 1)
        cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
        spans: list[Span] = []
        for s, e in cands:
            if 1 in occupied[s:e]:
                continue
            occupied[s:e] = b"\x01" * (e - s)
            spans.append((s, e, self.mapping[text[s:e]]))
        spans.sort()
        return spans

    def for_raw_xml(self) -> "LiteralReplacer":
        """Variant for raw XML parts: also match XML-escaped originals."""
        out: dict[str, str] = {}
        for original, repl in self.mapping.items():
            out[original] = repl
            esc = xml_escape(original)
            if esc != original:
                out[esc] = xml_escape(repl)
        return LiteralReplacer(out, protect_placeholders=self.protect)


def splice(text: str, spans: list[Span]) -> str:
    """Write each (start, end, replacement) span into `text` in one pass."""
    if not spans:
        return text
    out: list[str] = []
    cur = 0
    for s, e, r in spans:
        out.append(text[cur:s])
        out.append(r)
        cur = e
    out.append(text[cur:])
    return "".join(out)


def apply(text: str, replacer: Replacer) -> str:
    return splice(text, replacer.find(text))


def as_replacer(map_or_replacer: Union[dict, Replacer]) -> Replacer:
    if isinstance(map_or_replacer, dict):
        return LiteralReplacer(map_or_replacer)
    return map_or_replacer
