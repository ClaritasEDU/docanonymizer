"""LLM-driven PII detection orchestrator.

Per PRD 5.5: prompts the local model with strict JSON-output instructions,
parses results per chunk, accumulates into the entity registry. Tolerant
to fenced code blocks and stray prose around the JSON array (some local
models leak prefixes despite instruction).

Public surface:
    detect_pii(text, allowed_tags, on_chunk=...) -> EntityRegistry
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Iterable, Optional

from . import endpoints as endpoints_mod
from . import ids
from . import llm
from .chunker import chunk_text, count_tokens
from .logging_setup import get_logger
from .mapper import TAG_ORDER, VALID_TAGS, EntityRegistry

log = get_logger("detector")

_CHUNK_ATTEMPTS = 3          # LLM tries per chunk before the run is aborted
_REGISTRY_PROMPT_CAP = 40    # most-recent entities listed in each chunk prompt


PROMPT_TEMPLATE = """\
You are a PII (personally identifiable information) extractor. Read the document chunk below and return every distinct PII value as a JSON array.

OUTPUT RULES (STRICT):
- Return ONLY a JSON array. No prose. No markdown fences. No explanation.
- Each item: {{"text": "exact text as it appears", "type": "TAG"}}
- "type" MUST be one of: {tags}
- List each distinct value ONCE - repeats are replaced automatically. Match text exactly as it appears in the chunk.
- Optional: add "linked_to": "<12-character id from the registry below>" when the item clearly belongs to an entity already there (for example the email of a person already listed).
- Do NOT invent entities. Do NOT paraphrase the text.
- Money amounts, prices, totals, quantities, counts, percentages, years, and row numbers are NOT PII - never tag them. Only tag a number when it identifies a person or account (SSN, phone, account, student or employee ID) or is an individual's grade or score.
- Column headers and labels ("Name", "Email", "Gift 2025") are NOT PII.
- If a chunk has no PII, return [].

ALLOWED TAGS (only these; ignore everything else):
{tag_table}

ENTITY REGISTRY (already-known entities from previous chunks; use linked_to to relate):
{registry}

CHUNK TEXT:
\"\"\"
{chunk}
\"\"\"
"""


_TAG_HELP = {
    "PERSON":      "personal full names (Jane Smith, Dr. Torres, Coach Mike)",
    "EMAIL":       "any email address",
    "PHONE":       "any phone number including extensions",
    "ADDRESS":     "physical street address, city, state, zip, unit",
    "ID":          "SSN, EIN, ITIN, passport numbers (never amounts or counts)",
    "ORG":         "company, school, church, nonprofit names",
    "FINANCIAL":   "account, routing, credit card numbers",
    "DOB":         "date of birth",
    "SID":         "student / employee / badge id numbers",
    "IP":          "IPv4 or IPv6 address",
    "USERNAME":    "social handles, login usernames",
    "GRADE":       "letter grade, GPA, individual test score",
    "MEDICAL":     "diagnosis, medication, IEP, health condition",
    "IMMIGRATION": "visa type, citizenship status, DACA",
    "DEMO":        "race / ethnicity tied to a person",
    "RELIGION":    "religion tied to a person",
    "GENDER":      "gender / pronouns tied to a named individual",
}


def _build_prompt(chunk: str, allowed: list[str], registry: EntityRegistry) -> str:
    tag_lines = [f"  {t}: {_TAG_HELP[t]}" for t in allowed]
    if registry.entities:
        # Cap the listing so long, name-dense documents can't blow the prompt
        # past the chunk budget (CODE_REVIEW M3). Most recent entities win -
        # they're the likeliest to recur in the next chunk.
        items = list(registry.entities.items())[-_REGISTRY_PROMPT_CAP:]
        reg_lines = []
        for hex_id, by_tag in items:
            ex = next(iter(by_tag.values()), "")
            reg_lines.append(f"  {hex_id}: tags={','.join(sorted(by_tag))} example={ex!r}")
        registry_repr = "\n".join(reg_lines)
        if len(registry.entities) > _REGISTRY_PROMPT_CAP:
            registry_repr += f"\n  (+{len(registry.entities) - _REGISTRY_PROMPT_CAP} earlier entities omitted)"
    else:
        registry_repr = "  (empty)"
    return PROMPT_TEMPLATE.format(
        tags=",".join(allowed),
        tag_table="\n".join(tag_lines),
        registry=registry_repr,
        chunk=chunk,
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Small local models tag money amounts as PII even when told not to
# (llama3.2 turned a donor sheet's whole Gift column into tokens). A value
# that is plainly an amount - currency sign, cents, percent, or a bare number
# under 7 digits - is kept as original text. Real SSNs, account, routing, and
# card numbers are 8+ digits and are still caught. Custom terms are never
# filtered, and the preview reports how many were kept.
_AMOUNT_RE = re.compile(r"[$€£¥]?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s?%?")
# Measured: llama3.2 tagged pledge amounts as FINANCIAL, ID, and even PERSON.
# Every type except those where a bare number is itself PII.
_AMOUNT_EXEMPT_TAGS = {"SID", "GRADE"}


def is_plain_amount(text: str) -> bool:
    t = text.strip()
    if not _AMOUNT_RE.fullmatch(t):
        return False
    marked = bool(re.search(r"[$€£¥%]|\.\d{1,2}$", t))
    return marked or sum(c.isdigit() for c in t) < 7


def _extract_json_array(raw: str) -> Optional[list]:
    """Pull the JSON array out of `raw`. Tolerates prose / fences / a wrapper
    object like {"pii": [...]}.

    Returns None when no array can be read. That is a FAILED scan, not "no
    PII": a garbled or cut-off answer must never let a chunk through
    unscrubbed. Only an actual empty array means the chunk has no PII.
    """
    if not raw:
        return None
    s = raw.strip()

    def as_list(v):
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            lists = [x for x in v.values() if isinstance(x, list)]
            if len(lists) == 1:
                return lists[0]
        return None

    # 1. Direct parse
    try:
        v = as_list(json.loads(s))
        if v is not None:
            return v
    except json.JSONDecodeError:
        pass
    # 2. Inside a fence
    m = _FENCE_RE.search(s)
    if m:
        try:
            v = as_list(json.loads(m.group(1)))
            if v is not None:
                return v
        except json.JSONDecodeError:
            pass
    # 3. First "[" .. matching "]" at top level
    start = s.find("[")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(s[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(s[start : i + 1])
                    return v if isinstance(v, list) else None
                except json.JSONDecodeError:
                    return None
    return None


def _answer_schema(allowed: list[str]) -> dict:
    """JSON schema for Ollama structured output - the answer always parses."""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "type": {"type": "string", "enum": list(allowed)},
                "linked_to": {"type": "string"},
            },
            "required": ["text", "type"],
        },
    }


_MIN_SPLIT_CHARS = 400
_SPLIT_OVERLAP = 80


def _split_chunk(chunk: str) -> Optional[tuple[str, str]]:
    """Halve a chunk whose answer ran out of room. Splits at a line break (a
    spreadsheet row) near the middle, else at a space, with a small overlap
    so a value on the boundary lands whole in one half."""
    if len(chunk) < _MIN_SPLIT_CHARS:
        return None
    mid = len(chunk) // 2
    cut = chunk.rfind("\n", 0, mid + mid // 2)
    if cut <= len(chunk) // 4:
        cut = chunk.rfind(" ", 0, mid)
    if cut <= 0:
        cut = mid
    return chunk[: min(len(chunk), cut + _SPLIT_OVERLAP)], chunk[max(0, cut - _SPLIT_OVERLAP):]


def detect_pii(
    text: str,
    allowed_tags: Optional[Iterable[str]] = None,
    on_chunk: Optional[Callable[[dict], None]] = None,
    endpoint: Optional[dict] = None,
) -> EntityRegistry:
    """Run detection over `text` and return a populated EntityRegistry.

    `on_chunk` is invoked with `{index, total, tokens, found, elapsed_s}` for each
    chunk so the caller can stream UI progress.
    """
    if allowed_tags is None:
        allowed = list(TAG_ORDER)
    else:
        wanted = set(allowed_tags) & VALID_TAGS
        allowed = [t for t in TAG_ORDER if t in wanted]
    if not allowed:
        raise ValueError("no allowed tags supplied")

    ep = endpoint or endpoints_mod.get_active()
    chunk_budget = (ep or {}).get("chunk_tokens") if ep else None

    chunks = chunk_text(text, chunk_tokens=chunk_budget) if chunk_budget else chunk_text(text)
    log.info("detection start: chunks=%d total_chars=%d", len(chunks), len(text))

    # IDs already released in earlier files are off-limits (ids.py).
    registry = EntityRegistry(reserved=ids.load_reserved())
    schema = _answer_schema(allowed)
    nickname = (ep or {}).get("nickname", "-")
    pending = list(chunks)
    done = 0
    while pending:
        chunk = pending.pop(0)
        idx = done + 1
        total = done + 1 + len(pending)
        prompt = _build_prompt(chunk, allowed, registry)
        tokens = count_tokens(prompt)
        started = time.monotonic()
        # A chunk that can't be scanned means PII may ship unscrubbed - the
        # regex verifier can't catch names or addresses. Retry, then fail the
        # whole run rather than silently skipping (CODE_REVIEW C2).
        items: Optional[list] = None
        last_exc: Optional[Exception] = None
        split = False
        for attempt in range(1, _CHUNK_ATTEMPTS + 1):
            try:
                raw = llm.llm_call(prompt, endpoint=ep, schema=schema)
            except llm.LLMTruncated as exc:
                last_exc = exc
                halves = _split_chunk(chunk)
                if halves is None:
                    break
                log.warning("endpoint=%s chunk %d/%d: answer ran out of room - "
                            "splitting into 2 smaller chunks", nickname, idx, total)
                pending[:0] = list(halves)
                split = True
                break
            except llm.LLMError as exc:
                last_exc = exc
            else:
                items = _extract_json_array(raw)
                if items is not None:
                    break
                last_exc = llm.LLMError("the model's answer was not the JSON list it was asked for")
            log.warning("endpoint=%s chunk %d/%d LLM error (attempt %d/%d): %s",
                        nickname, idx, total, attempt, _CHUNK_ATTEMPTS, last_exc)
            if attempt < _CHUNK_ATTEMPTS:
                time.sleep(attempt)  # 1s, 2s backoff
        if split:
            continue
        if items is None:
            log.error("chunk %d/%d failed after %d attempts - aborting detection",
                      idx, total, _CHUNK_ATTEMPTS)
            if on_chunk:
                on_chunk({"index": idx, "total": total, "tokens": tokens,
                          "found": 0, "elapsed_s": time.monotonic() - started,
                          "error": str(last_exc)})
            raise llm.LLMError(
                f"chunk {idx}/{total} could not be scanned after "
                f"{_CHUNK_ATTEMPTS} attempts ({last_exc}). Detection aborted - "
                "an unscanned chunk would ship PII unscrubbed."
            )
        done += 1
        kept = []
        for it in items:
            if (isinstance(it, dict) and isinstance(it.get("text"), str)
                    and str(it.get("type") or "").strip().upper() not in _AMOUNT_EXEMPT_TAGS
                    and is_plain_amount(it["text"])):
                registry.skipped_amounts.add(it["text"].strip())
                continue
            kept.append(it)
        registry.merge_chunks(kept)
        elapsed = time.monotonic() - started
        log.info(
            "endpoint=%s chunk %d/%d: tokens=%d pii=%d t=%.2fs",
            nickname, idx, total, tokens, len(items), elapsed,
        )
        if on_chunk:
            on_chunk({"index": idx, "total": total, "tokens": tokens,
                      "found": len(items), "elapsed_s": elapsed})

    if registry.skipped_amounts:
        log.info("amount-like values kept as original text: %d", len(registry.skipped_amounts))
    counts = registry.counts_per_type()
    log.info(
        "detection complete: entities=%d replacements=%d types=%s",
        registry.total_entities(),
        registry.total_replacements(),
        ",".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )
    return registry
