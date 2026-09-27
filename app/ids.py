"""Identifier generation - 12-character hex IDs, unique for all time.

Owner rule (2026-09-27): every distinct PII value gets its own identifier,
and no identifier is ever issued twice. That makes each ID a one-to-one
pointer back to exactly one original value, which is what lets AI output be
restored without mixing up people, addresses, or contact details - even when
the AI drops the brackets or relabels the tag and only the hex survives.

Uniqueness is enforced, not left to chance. A candidate is rejected if it
appears in:
  1. the current session's registry,
  2. any key file in /keys (so two anonymized files pasted into the same AI
     chat can never collide),
  3. the issued-ID ledger (/keys/issued_ids.ledger) - an append-only list of
     every ID ever released, so uniqueness survives key files being moved or
     deleted,
  4. IDs handed out by this process that have not been saved yet.

Format rules:
  - exactly 12 uppercase hex characters (48 bits)
  - at least one letter A-F AND at least one digit, so an ID can never be
    mistaken for a plain number or a word in AI output
  - never digits-E-digits (123456789E12), which Excel would turn into a number

The ledger holds random IDs only - never PII.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import KEYS_DIR
from .logging_setup import get_logger

log = get_logger("ids")

HEX_LEN = 12
LEGACY_HEX_LEN = 4           # v1.3 key files used 4-char shared suffixes
LEDGER_FILE = KEYS_DIR / "issued_ids.ledger"

_ID_RE = re.compile(r"[0-9A-F]{12}")
_SCI_RE = re.compile(r"\d+E\d+")
# Pulls the hex out of a placeholder like "[PERSON_3A4F9C2B1D0E]".
_PLACEHOLDER_HEX_RE = re.compile(r"_([0-9A-F]{12})\]")
_BARE_HEX_RE = re.compile(r"(?<=_)([0-9A-F]{12})(?![0-9A-F])")

_lock = threading.Lock()
_issued_this_process: set[str] = set()


def is_valid_id(s: str) -> bool:
    """True for a well-formed 12-char ID (uppercase hex, >=1 letter, >=1 digit)."""
    return (
        isinstance(s, str)
        and _ID_RE.fullmatch(s) is not None
        and any(c.isdigit() for c in s)
        and any(c.isalpha() for c in s)
        and _SCI_RE.fullmatch(s) is None
    )


def _random_id() -> str:
    while True:
        candidate = secrets.token_hex(HEX_LEN // 2).upper()
        if is_valid_id(candidate):
            return candidate


def new_id(is_taken: Optional[Callable[[str], bool]] = None) -> str:
    """Return a fresh ID that is not taken anywhere we know of.

    `is_taken` is the caller's check (session registry + reserved set from
    disk). This function adds the process-wide check and records the ID so a
    concurrent session can't receive it.
    """
    with _lock:
        for _ in range(10_000):
            candidate = _random_id()
            if candidate in _issued_this_process:
                continue
            if is_taken is not None and is_taken(candidate):
                continue
            _issued_this_process.add(candidate)
            return candidate
    # 48 bits of space - reaching here means the RNG or the reserved set is broken.
    raise RuntimeError("could not find an unused 12-char id after 10,000 tries")


def ids_in_key_payload(payload: dict) -> set[str]:
    """Every 12-char ID referenced by a key file payload."""
    out: set[str] = set()
    for placeholder in (payload.get("replacement_map") or {}).values():
        if isinstance(placeholder, str):
            out.update(_PLACEHOLDER_HEX_RE.findall(placeholder))
    for hex_id in (payload.get("entity_registry") or {}):
        if isinstance(hex_id, str) and _ID_RE.fullmatch(hex_id):
            out.add(hex_id)
    for title in (payload.get("sheet_titles") or {}):
        if isinstance(title, str):
            out.update(_BARE_HEX_RE.findall(title))
    return out


def load_reserved(keys_dir: Optional[Path] = None) -> set[str]:
    """IDs already spoken for on disk: the ledger plus every key file."""
    keys_dir = keys_dir or KEYS_DIR
    reserved: set[str] = set()
    ledger = keys_dir / LEDGER_FILE.name
    if ledger.exists():
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines():
                s = line.strip().upper()
                if _ID_RE.fullmatch(s):
                    reserved.add(s)
        except OSError as exc:
            log.warning("ledger unreadable: %s", type(exc).__name__)
    key_files = 0
    for p in keys_dir.glob("*.key.json"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            reserved |= ids_in_key_payload(payload)
            key_files += 1
    log.info("reserved ids loaded: %d (from ledger + %d key files)", len(reserved), key_files)
    return reserved


def record_issued(ids: Iterable[str]) -> int:
    """Append released IDs to the ledger. Returns the count written."""
    fresh = sorted({i for i in ids if _ID_RE.fullmatch(i or "")})
    if not fresh:
        return 0
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock, LEDGER_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(fresh) + "\n")
    log.info("ledger updated: %d ids recorded", len(fresh))
    return len(fresh)
