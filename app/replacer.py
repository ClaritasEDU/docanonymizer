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
not match inside 1945, 94.5, 1,945, A94, or a row index; it does match each
item of a comma list like 94,87,100. Short numbers are matched after the
letter-bearing originals, in the gaps they leave. Long numbers (phones,
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

# The generic shape of a placeholder: current 12-hex IDs or legacy 4-hex IDs,
# bracketed or (where brackets are illegal - sheet names and formula
# references to them) bare. Used only to strip tokens before the generic
# regex-residue warning scan. Replacement protection is NOT shape-based: it
# covers only the exact placeholders in the map, so PII that merely looks
# like a token ("MRN_000123456789") is still replaced and still verified.
PLACEHOLDER_RE = re.compile(
    r"\[[A-Z]+_(?:[0-9A-F]{12}|[0-9A-F]{4})\]"
    r"|(?<![A-Za-z0-9_])[A-Z]+_[0-9A-F]{12}(?![A-Za-z0-9_])"
)
_BRACKETED_PH_RE = re.compile(r"\[([A-Z]+_(?:[0-9A-F]{12}|[0-9A-F]{4}))\]")

Span = tuple[int, int, str]

_SHORT_NUMBER_DIGITS = 7
_NUM_CHARS = frozenset("0123456789.,")
# One number: 1,945 / 1,945.50 / 94.5 / 94
_ONE_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def is_short_number(s: str) -> bool:
    """No letters, fewer than 7 digits, starts or ends with a digit."""
    return (
        bool(s)
        and not any(c.isalpha() for c in s)
        and sum(c.isdigit() for c in s) < _SHORT_NUMBER_DIGITS
        and (s[0].isdigit() or s[-1].isdigit())
    )


def _glued(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _number_boundary_ok(text: str, s: int, e: int) -> bool:
    """True when text[s:e] (a short number) stands on its own as a whole value.

    Not whole: glued to letters/digits/$/_ (1945, A94, $B$94, 94th), part of
    one formatted number (1,945 / 94.5 / 10.0.94.1). Whole: surrounded by
    spaces or punctuation, or one item of a comma list (204518,78704 and
    94,87,100 are lists - their groups are not all 3 digits).
    """
    if (s > 0 and _glued(text[s - 1])) or (e < len(text) and _glued(text[e])):
        return False
    a = s
    while a > 0 and text[a - 1] in _NUM_CHARS:
        a -= 1
    b = e
    while b < len(text) and text[b] in _NUM_CHARS:
        b += 1
    left, right = text[a:s], text[e:b]
    if (left and left[-1].isdigit()) or (right and right[0].isdigit()):
        return False
    core = text[s:e]
    if not all(c in _NUM_CHARS for c in core):
        # e.g. "12/05", "(512)": only a direct decimal continuation matters
        if len(left) > 1 and left[-1] == "." and left[-2].isdigit():
            return False
        if len(right) > 1 and right[0] == "." and right[1].isdigit():
            return False
        return True
    run = text[a:b].strip(".,")
    if run == core.strip(".,"):
        return True
    if _ONE_NUMBER_RE.fullmatch(run):
        return False                     # core is only part of one number
    i0 = text.rfind(",", a, s)
    i0 = a if i0 == -1 else i0 + 1
    i1 = text.find(",", e, b)
    i1 = b if i1 == -1 else i1
    return text[i0:i1].strip(".") == core


class Replacer(Protocol):
    def find(self, text: str) -> list[Span]: ...
    def for_raw_xml(self) -> "Replacer": ...


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


_END = ""  # trie key marking "a word ends here"


def _build_trie(words) -> dict:
    root: dict = {}
    for w in words:
        node = root
        for ch in w:
            node = node.setdefault(ch, {})
        node[_END] = True
    return root


def _trie_pattern(words: list[str]) -> str:
    """Regex matching any of `words`, preferring the longest at a position.

    A greedy optional group tries the longer continuation first and only
    backtracks to the shorter word if the longer one fails - so the result
    is longest-match without relying on alternation order.
    """
    def emit(node: dict) -> str:
        parts: list[str] = []
        # Walk straight chains iteratively (keeps recursion to branch points).
        while True:
            kids = [k for k in node if k != _END]
            if len(kids) == 1 and _END not in node:
                parts.append(re.escape(kids[0]))
                node = node[kids[0]]
                continue
            break
        kids = sorted(k for k in node if k != _END)
        if not kids:
            return "".join(parts)
        alts = [re.escape(k) + emit(node[k]) for k in kids]
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        if _END in node:
            body = "(?:" + body + ")?" if len(alts) == 1 else body + "?"
        return "".join(parts) + body

    return emit(_build_trie(words))


def _words_at(trie: dict, text: str, p: int) -> list[int]:
    """End offsets of every trie word starting at text[p], longest first."""
    ends: list[int] = []
    node = trie
    i = p
    while i < len(text):
        node = node.get(text[i])
        if node is None:
            break
        i += 1
        if _END in node:
            ends.append(i)
    ends.reverse()
    return ends


class LiteralReplacer:
    """Exact originals -> replacement strings, one pass.

    Matching is two-stage. (1) Originals containing a letter (and long
    numbers) match leftmost-longest. (2) Short numbers then fill the gaps,
    each only where it stands as a whole number.

    `protect_placeholders` (default on) makes this map's own placeholders
    (bracketed and bare) untouchable, so a pass over already-anonymized text
    can never rewrite part of one.
    """

    def __init__(self, mapping: dict[str, str], protect_placeholders: bool = True):
        self.mapping = {k: v for k, v in mapping.items() if k}
        self.protect = protect_placeholders
        words = sorted(self.mapping, key=len, reverse=True)
        self._texty = [w for w in words if not is_short_number(w)]
        self._numbers = [w for w in words if is_short_number(w)]
        self._protected: list[str] = []
        if self.protect:
            ph = set()
            for v in self.mapping.values():
                m = _BRACKETED_PH_RE.fullmatch(v)
                if m:
                    ph.add(v)
                    ph.add(m.group(1))
            # A placeholder that is itself an original must stay matchable.
            self._protected = sorted(ph - set(self.mapping), key=len, reverse=True)
        self._regex: Optional[re.Pattern] = None
        self._num_regex: Optional[re.Pattern] = None
        self._num_trie = _build_trie(self._numbers) if self._numbers else None
        self._fallback = False
        try:
            alts = []
            if self._protected:
                alts.append(f"(?P<ph>{_trie_pattern(self._protected)})")
            if self._texty:
                alts.append(f"(?P<o>{_trie_pattern(self._texty)})")
            if alts:
                self._regex = re.compile("|".join(alts))
            if self._numbers:
                self._num_regex = re.compile(f"(?=(?:{_trie_pattern(self._numbers)}))")
        except (RecursionError, re.error, OverflowError, MemoryError) as exc:
            # Pathological originals (hundreds of nested branch points).
            # Correct but slower scan below.
            log.warning("trie regex unavailable (%s) - using fallback scan",
                        type(exc).__name__)
            self._fallback = True

    def find(self, text: str) -> list[Span]:
        if not text or not self.mapping:
            return []
        occupied = bytearray(len(text))
        spans: list[Span] = []
        if self._fallback:
            self._texty_fallback(text, occupied, spans)
        elif self._regex is not None:
            for m in self._regex.finditer(text):
                occupied[m.start():m.end()] = b"\x01" * (m.end() - m.start())
                if m.lastgroup == "o":
                    spans.append((m.start(), m.end(), self.mapping[m.group("o")]))
        if self._numbers:
            if self._fallback or self._num_regex is None:
                starts = sorted({i for w in self._numbers for i in _find_all(text, w)})
            else:
                starts = [m.start() for m in self._num_regex.finditer(text)]
            for p in starts:
                if occupied[p]:
                    continue
                for e in _words_at(self._num_trie, text, p):
                    if 1 in occupied[p:e] or not _number_boundary_ok(text, p, e):
                        continue
                    occupied[p:e] = b"\x01" * (e - p)
                    spans.append((p, e, self.mapping[text[p:e]]))
                    break
        spans.sort()
        return spans

    def _texty_fallback(self, text: str, occupied: bytearray, spans: list[Span]) -> None:
        # Leftmost-longest over protected + texty words: collect candidates,
        # take them in (start asc, length desc) order, skipping overlaps.
        cands = [(i, i + len(w), True) for w in self._protected for i in _find_all(text, w)]
        cands += [(i, i + len(w), False) for w in self._texty for i in _find_all(text, w)]
        cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
        for s0, e0, is_ph in cands:
            if 1 in occupied[s0:e0]:
                continue
            occupied[s0:e0] = b"\x01" * (e0 - s0)
            if not is_ph:
                spans.append((s0, e0, self.mapping[text[s0:e0]]))

    def for_raw_xml(self) -> "LiteralReplacer":
        """Variant for raw XML parts: also match XML-escaped originals."""
        out: dict[str, str] = {}
        for original, repl in self.mapping.items():
            out[original] = repl
            esc = xml_escape(original)
            if esc != original:
                out[esc] = xml_escape(repl)
        return LiteralReplacer(out, protect_placeholders=self.protect)


def overlap_unions(text: str, originals) -> list[tuple[str, list[str]]]:
    """Stretches of `text` where detected values overlap without nesting.

    "Patient Jane" and "Jane Smith" both found in "Patient Jane Smith":
    whichever wins, part of the other ("Smith") would survive, and the
    verifier can't see a fragment. Returns (union_text, member_originals)
    for each such stretch so the caller can register the union as one value
    (its own ID - fully reversible). Unions spanning a tab or newline are
    skipped: they cross spreadsheet cells or lines and can't be one value.
    """
    words = sorted({w for w in originals if w and not is_short_number(w)}, key=len, reverse=True)
    if len(words) < 2:
        return []
    try:
        pat = re.compile(f"(?=({_trie_pattern(words)}))")
    except (RecursionError, re.error, OverflowError, MemoryError):
        return []
    word_set = set(words)
    out: list[tuple[str, list[str]]] = []
    cs = ce = -1
    members: list[str] = []

    def flush() -> None:
        if len(members) > 1:
            union = text[cs:ce]
            if union not in word_set and "\t" not in union and "\n" not in union:
                out.append((union, list(members)))

    for m in pat.finditer(text):
        s0, w = m.start(), m.group(1)
        if s0 < ce:
            ce = max(ce, s0 + len(w))
            members.append(w)
        else:
            flush()
            cs, ce, members = s0, s0 + len(w), [w]
    flush()
    seen: set[str] = set()
    return [(u, ms) for u, ms in out if not (u in seen or seen.add(u))]


def _find_all(text: str, w: str):
    i = text.find(w)
    while i != -1:
        yield i
        i = text.find(w, i + 1)


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
