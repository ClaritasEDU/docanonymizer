"""Entity registry + replacement map.

Identifier rules (owner decision 2026-09-27, supersedes PRD 5.1-5.2 v1.3):
  - Every distinct original value gets its own 12-char uppercase hex ID:
    `[PERSON_3A4F9C2B1D0E]`, `[EMAIL_7C1B0A94E2D3]`.
  - No ID is ever shared between two values and no ID is ever reused - not
    within a file, not across files (see ids.py for how that is enforced).
    One ID points back to exactly one original, so restoring AI output can
    never mix up people, addresses, or contact details.
  - The same exact value repeated anywhere in the document keeps one ID, so
    counts and joins still work for analysis.
  - When the model says a value belongs with an already-seen one (Jane's
    email -> Jane), that relationship is recorded in the key file as
    `linked_to`. It is never expressed by sharing an ID.
  - Replacement order: longest original first (see replacer.py).

Public surface:
    EntityRegistry(reserved=set())   - in-memory accumulator
    .add(text, tag, linked_to=None)  -> placeholder string
    .as_replacement_map()            -> dict[original -> placeholder]
    .as_serializable()               -> dict for the key file payload
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import ids
from .logging_setup import get_logger

log = get_logger("mapper")


# 17 PII tags from PRD 4.2, in display order (Standard tier first, Sensitive last).
TAG_ORDER = [
    "PERSON", "EMAIL", "PHONE", "ADDRESS", "ID", "ORG",
    "FINANCIAL", "DOB", "SID", "IP", "USERNAME",
    "GRADE", "MEDICAL", "IMMIGRATION", "DEMO", "RELIGION", "GENDER",
]
VALID_TAGS = set(TAG_ORDER)


@dataclass
class EntityRegistry:
    # IDs already spoken for outside this session (key files + ledger).
    reserved: set[str] = field(default_factory=set)
    # hex_id -> {tag: original_text}. One original per ID; a second tag on the
    # same text is recorded here too (first tag drives the placeholder).
    entities: dict[str, dict[str, str]] = field(default_factory=dict)
    # original_text -> placeholder (what the scrubber applies)
    replacements: dict[str, str] = field(default_factory=dict)
    # original_text -> hex_id (so repeats keep their ID)
    text_to_hex: dict[str, str] = field(default_factory=dict)
    # hex_id -> hex_id of the value the model linked it to
    links: dict[str, str] = field(default_factory=dict)

    def _taken(self, candidate: str) -> bool:
        return candidate in self.entities or candidate in self.reserved

    def _resolve_link(self, linked_to: Optional[str]) -> Optional[str]:
        if not linked_to:
            return None
        link = linked_to.strip()
        up = link.upper()
        if up in self.entities:
            return up
        if link in self.text_to_hex:
            return self.text_to_hex[link]
        return None

    def add(self, text: str, tag: str, linked_to: Optional[str] = None) -> str:
        """Register a PII span. Returns the placeholder (e.g. `[PERSON_3A4F9C2B1D0E]`).

        `linked_to` may be an existing ID or a previously-seen text value. It
        is recorded as a relationship only - the new value still gets its own ID.
        """
        if tag not in VALID_TAGS:
            raise ValueError(f"unknown tag: {tag}")
        text = text.strip()
        if not text:
            raise ValueError("empty PII text")

        # Already registered - first tag wins so the placeholder stays stable
        # regardless of LLM output order (CODE_REVIEW M1). The extra tag is
        # still recorded on the entity for the key file.
        if text in self.replacements:
            hex_id = self.text_to_hex[text]
            self.entities.setdefault(hex_id, {}).setdefault(tag, text)
            return self.replacements[text]

        hex_id = ids.new_id(self._taken)
        placeholder = f"[{tag}_{hex_id}]"
        self.entities[hex_id] = {tag: text}
        self.replacements[text] = placeholder
        self.text_to_hex[text] = hex_id

        target = self._resolve_link(linked_to)
        if target and target != hex_id:
            self.links[hex_id] = target
        return placeholder

    def as_replacement_map(self) -> dict[str, str]:
        """Sorted longest-first (the order the key file has always used)."""
        return dict(sorted(self.replacements.items(), key=lambda kv: -len(kv[0])))

    def counts_per_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for placeholder in self.replacements.values():
            tag = placeholder.split("_", 1)[0].lstrip("[")
            out[tag] = out.get(tag, 0) + 1
        return out

    def total_entities(self) -> int:
        return len(self.entities)

    def total_replacements(self) -> int:
        return len(self.replacements)

    def as_serializable(self) -> dict:
        """Build the entity_registry block used in the key file (PRD 5.7)."""
        out: dict[str, dict] = {}
        for hex_id, by_tag in self.entities.items():
            rec: dict = {
                "types": sorted(by_tag.keys()),
                "values": dict(by_tag),
            }
            if hex_id in self.links:
                rec["linked_to"] = self.links[hex_id]
            out[hex_id] = rec
        return out

    def merge_chunks(self, raw_items: list[dict]) -> None:
        """Ingest a list of LLM-returned spans across chunks.

        Each item: {"text": str, "type": str, "linked_to": str | None}
        """
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "")
            tag = (item.get("type") or "")
            if not isinstance(text, str) or not isinstance(tag, str):
                continue
            text = text.strip()
            tag = tag.strip().upper()
            link = item.get("linked_to")
            if not text or tag not in VALID_TAGS:
                continue
            try:
                self.add(text, tag, link if isinstance(link, str) else None)
            except (ValueError, RuntimeError) as exc:
                log.warning("skipped span: tag=%s reason=%s", tag, exc)

    def drop(self, original_text: str) -> bool:
        """Remove a span (used when the operator deselects a false positive)."""
        if original_text not in self.replacements:
            return False
        hex_id = self.text_to_hex.pop(original_text, None)
        self.replacements.pop(original_text, None)
        if hex_id:
            self.entities.pop(hex_id, None)
            self.links.pop(hex_id, None)
            # Nothing may point at an ID that no longer exists.
            for k in [k for k, v in self.links.items() if v == hex_id]:
                del self.links[k]
        return True
