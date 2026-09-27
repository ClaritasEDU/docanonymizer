"""Key file save / load (PRD 5.7).

Schema:
{
  "session_id":       "a3f9c1b2",
  "original_filename": "donor_list.xlsx",
  "created_at":       "2026-05-01T14:32:00Z",
  "id_format":        "hex12",
  "llm_endpoint":     "http://localhost:11434",
  "llm_api_style":    "ollama",
  "model_used":       "llama3.2",
  "pii_types_scrubbed": [...],
  "entity_registry":  {"3A4F9C2B1D0E": {"types": ["PERSON"], "values": {...},
                                        "linked_to": "<id>" (optional)}},
  "replacement_map":  {"Jane Smith": "[PERSON_3A4F9C2B1D0E]", ...},
  "sheet_titles":     {"ORG_B9442179E0EE": "Smith Family"}   (XLSX only, optional)
}

`id_format` is "hex12" for current keys (one unique 12-char ID per value).
Keys without it are v1.3 "legacy" keys (4-char IDs shared across an
entity's tags). Both restore; legacy IDs need their tag to match.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
from pathlib import Path
from typing import Optional

from . import ids
from .config import KEYS_DIR
from .logging_setup import get_logger
from .mapper import EntityRegistry

log = get_logger("key")

ID_FORMAT = "hex12"


def new_session_id() -> str:
    return secrets.token_hex(4)  # 8 hex chars


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_key_file(
    session_id: str,
    original_filename: str,
    endpoint: dict,
    pii_types_scrubbed: list[str],
    registry: EntityRegistry,
    sheet_titles: Optional[dict[str, str]] = None,
) -> Path:
    payload = {
        "session_id": session_id,
        "original_filename": original_filename,
        "created_at": _now_iso(),
        "id_format": ID_FORMAT,
        "llm_endpoint": endpoint.get("base_url", ""),
        "llm_api_style": endpoint.get("api_style", ""),
        "model_used": endpoint.get("model", ""),
        "pii_types_scrubbed": sorted(set(pii_types_scrubbed)),
        "entity_registry": registry.as_serializable(),
        "replacement_map": registry.as_replacement_map(),
    }
    if sheet_titles:
        # anonymized sheet title -> original (restore puts the exact title back)
        payload["sheet_titles"] = dict(sheet_titles)
    stem = Path(original_filename).stem or "document"
    name = f"{stem}_{session_id}.key.json"
    path = KEYS_DIR / name
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("key file saved: %s ids=%d", path.name, len(registry.entities))
    # The IDs are now released - make sure they are never issued again, even
    # if this key file is later moved out of /keys.
    ids.record_issued(ids.ids_in_key_payload(payload))
    return path


def load_key_file(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("key file is not a JSON object")
    required = ("session_id", "original_filename", "replacement_map")
    for k in required:
        if k not in payload:
            raise ValueError(f"key file missing required field: {k}")
    if not isinstance(payload["replacement_map"], dict):
        raise ValueError("key file replacement_map is not an object")
    return payload


def saved_key_path(name: str) -> Optional[Path]:
    """Resolve a key name from the UI to a file directly inside /keys, or None.

    Any file name the picker lists is accepted (keys dragged in by hand may
    have spaces or parentheses) - but never a path: no separators, no dot
    prefix, and the resolved file must sit directly in /keys.
    """
    if (not isinstance(name, str) or not name.endswith(".key.json")
            or "/" in name or "\\" in name or "\x00" in name or name.startswith(".")):
        return None
    path = KEYS_DIR / name
    try:
        if path.resolve().parent != KEYS_DIR.resolve() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def key_format(payload: dict) -> str:
    """'hex12' for current keys, 'legacy' for v1.3 4-char keys."""
    if payload.get("id_format") == ID_FORMAT:
        return ID_FORMAT
    return ID_FORMAT if ids.ids_in_key_payload(payload) else "legacy"


def list_recent(limit: int = 1000) -> list[dict]:
    """Directory listing for the unanonymize key picker, newest first."""
    items: list[dict] = []
    if not KEYS_DIR.exists():
        return items
    for p in sorted(KEYS_DIR.glob("*.key.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            stat = p.stat()
            payload = load_key_file(p)
        except (OSError, ValueError):
            continue
        items.append({
            "name": p.name,
            "size": stat.st_size,
            "modified": _fmt_mtime(stat.st_mtime),
            "original_filename": str(payload.get("original_filename") or ""),
            "created_at": str(payload.get("created_at") or ""),
            "ids": len(payload.get("replacement_map") or {}),
            "format": key_format(payload),
        })
        if len(items) >= limit:
            break
    return items


def import_key_file(src: Path, original_name: str) -> Path:
    """Copy an external key file into /keys so it can be selected for restores.

    Validates it first. Never overwrites an existing key.
    """
    payload = load_key_file(src)
    base = Path(original_name).name
    if not base.endswith(".key.json"):
        base = f"{Path(base).stem or 'imported'}.key.json"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    dest = KEYS_DIR / base
    i = 1
    while dest.exists():
        dest = KEYS_DIR / base.replace(".key.json", f"_{i}.key.json")
        i += 1
    with dest.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("key file imported: %s ids=%d format=%s",
             dest.name, len(payload.get("replacement_map") or {}), key_format(payload))
    return dest


def _fmt_mtime(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
