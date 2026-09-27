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
You are a PII (personally identifiable information) extractor. Read the document chunk below and return EVERY occurrence of PII as a JSON array.

OUTPUT RULES (STRICT):
- Return ONLY a JSON array. No prose. No markdown fences. No explanation.
- Each item: {{"text": "exact text as it appears", "type": "TAG", "linked_to": "<hex_id or null>"}}
- "type" MUST be one of: {tags}
- Include EVERY occurrence, including repeats. Match text exactly as it appears in the chunk.
- "linked_to" is a 12-character uppercase hex id from the registry below if this PII item clearly belongs to an entity already there (for example the email of a person already listed). Otherwise null.
- Do NOT invent entities. Do NOT paraphrase the text.
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
    "ID":          "SSN, EIN, ITIN, passport numbers",
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


def _extract_json_array(raw: str) -> list:
    """Pull the first JSON array out of `raw`. Tolerates prose / fences."""
    if raw is None:
        return []
    s = raw.strip()
    # 1. Direct parse
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass
    # 2. Inside a fence
    m = _FENCE_RE.search(s)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, list):
                return v
        except json.JSONDecodeError:
            pass
    # 3. First "[" .. matching "]" at top level
    start = s.find("[")
    if start == -1:
        return []
    depth = 0
    for i, ch in enumerate(s[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                fragment = s[start : i + 1]
                try:
                    v = json.loads(fragment)
                    if isinstance(v, list):
                        return v
                except json.JSONDecodeError:
                    return []
                break
    return []


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
    for idx, chunk in enumerate(chunks, start=1):
        prompt = _build_prompt(chunk, allowed, registry)
        tokens = count_tokens(prompt)
        started = time.monotonic()
        # A chunk that can't be scanned means PII may ship unscrubbed - the
        # regex verifier can't catch names or addresses. Retry, then fail the
        # whole run rather than silently skipping (CODE_REVIEW C2).
        raw = None
        last_exc: Optional[llm.LLMError] = None
        for attempt in range(1, _CHUNK_ATTEMPTS + 1):
            try:
                raw = llm.llm_call(prompt, endpoint=ep)
                break
            except llm.LLMError as exc:
                last_exc = exc
                log.warning("chunk %d/%d LLM error (attempt %d/%d): %s",
                            idx, len(chunks), attempt, _CHUNK_ATTEMPTS, exc)
                if attempt < _CHUNK_ATTEMPTS:
                    time.sleep(attempt)  # 1s, 2s backoff
        if raw is None:
            log.error("chunk %d/%d failed after %d attempts - aborting detection",
                      idx, len(chunks), _CHUNK_ATTEMPTS)
            if on_chunk:
                on_chunk({"index": idx, "total": len(chunks), "tokens": tokens,
                          "found": 0, "elapsed_s": time.monotonic() - started,
                          "error": str(last_exc)})
            raise llm.LLMError(
                f"chunk {idx}/{len(chunks)} could not be scanned after "
                f"{_CHUNK_ATTEMPTS} attempts ({last_exc}). Detection aborted - "
                "an unscanned chunk would ship PII unscrubbed."
            )
        items = _extract_json_array(raw)
        registry.merge_chunks(items)
        elapsed = time.monotonic() - started
        log.info(
            "chunk %d/%d: tokens=%d pii=%d t=%.2fs",
            idx, len(chunks), tokens, len(items), elapsed,
        )
        if on_chunk:
            on_chunk({"index": idx, "total": len(chunks), "tokens": tokens,
                      "found": len(items), "elapsed_s": elapsed})

    counts = registry.counts_per_type()
    log.info(
        "detection complete: entities=%d replacements=%d types=%s",
        registry.total_entities(),
        registry.total_replacements(),
        ",".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )
    return registry
