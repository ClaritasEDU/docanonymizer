"""Community identifiers on rosters (owner decision 2026-09-28).

A school roster or parishioner list surfaces real community members. Each
of them should appear in the anonymized spreadsheet as the SAME identifier
they have everywhere else - `[I3A4F9C2B1D0E7F21]` for an individual,
`[F9B0C11D2E3F4A5B6]` for a household - so an analysis in any AI tool can
join this year's roster with last year's, or the school list with the
parish list, without a single name leaving the building.

Family Graph issues and remembers those ids and does the matching. This
module is the bridge:

  1. After detection, the sheet's rows go to Family Graph for a PLAN. Nothing
     is written there. Every person and household comes back as matched
     (an existing id), new, or REVIEW (a human must decide).
  2. The preview shows the review items. The operator decides each one:
     same person as a candidate, a different (new) person, or not a person.
     CONFIRM AND SCRUB stays blocked until every item is decided.
  3. On confirm, the rows and decisions go to Family Graph as a COMMIT. If
     anything new needs a decision Family Graph refuses and writes nothing;
     the new items come back to the preview.
  4. Each person's name cell becomes their `[I…]` token (a couple or a list
     of children in one cell becomes their tokens joined). The household
     column becomes `[F…]`; with no household column, a FAMILY_ID column is
     appended. Everything else - surnames, emails, phones, addresses, free
     text - keeps the per-value tokens from the normal pipeline.
  5. The key file records every rewritten cell and its original text, so
     restoring the anonymized file puts back exactly what was there, and
     restoring an AI answer turns `[I…]` into the person's name.

Only spreadsheets (xlsx / xls / ods / csv) get this layer. Documents keep
the normal per-value tokens. If Family Graph is not configured the whole
layer is off and nothing changes.

Never logs names, cell values, or identifiers - counts only.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from . import familygraph
from .logging_setup import get_logger

log = get_logger("community")

TABULAR_SUFFIXES = {"xlsx", "xls", "ods", "csv"}
MAX_ROWS = 20000                 # Family Graph's per-request limit
APPENDED_HEADER = "FAMILY_ID"

# The two community token shapes as written into a file.
COMMUNITY_TOKEN_RE = re.compile(r"\[([IF])([0-9A-F]{16}|[0-9A-F]{8})\]")

# A header row names its columns; a title row ("2026 Kindergarten Roster")
# above it does not. Used to find the real header row.
_HEADER_WORD_RE = re.compile(
    r"\b(name|first|last|given|surname|student|child|children|parent|guardian|"
    r"family|household|head|spouse|email|e-mail|phone|mobile|cell|address|grade)\b", re.I)


@dataclass
class CommunityState:
    status: str = "off"          # off | ready | error | skipped | committed | none
    reason: str = ""             # why it is off / none
    error: Optional[str] = None
    category: str = "other"
    sheets: list = field(default_factory=list)      # what was sent, with sheet coordinates
    plan: Optional[dict] = None
    decisions: dict = field(default_factory=dict)
    result: Optional[dict] = None                   # the commit response
    registered: int = 0                             # guaranteed catches added to the registry


class CommunityError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------

def tables_of(extract) -> Optional[list]:
    if extract is None:
        return None
    if extract.original_suffix not in TABULAR_SUFFIXES:
        return None
    tables = extract.payload.get("tables")
    if tables is None and extract.payload.get("rows") is not None:
        tables = [extract.payload["rows"]]
    return tables


def _header_row(table: list) -> Optional[int]:
    first_wide = None
    for i, row in enumerate(table[:25]):
        filled = [c for c in row if (c or "").strip()]
        if len(filled) < 2:
            continue
        if first_wide is None:
            first_wide = i
        if any(_HEADER_WORD_RE.search(c) for c in filled):
            return i
    return first_wide


def build_sheets(tables: list) -> list:
    sheets = []
    total = 0
    for ti, table in enumerate(tables):
        if not table:
            continue
        hr = _header_row(table)
        if hr is None:
            continue
        width = max(len(r) for r in table)
        pad = lambda r: [(c if c is not None else "") for c in r] + [""] * (width - len(r))
        row_numbers = list(range(hr + 1, len(table)))
        total += len(row_numbers)
        sheets.append({
            "table": ti,
            "header_row": hr,
            "width": width,
            "headers": pad(table[hr]),
            "rows": [pad(table[r]) for r in row_numbers],
            "row_numbers": row_numbers,
        })
    if total > MAX_ROWS:
        raise CommunityError(f"this file has {total:,} rows; community ids handle up to {MAX_ROWS:,} per file")
    return sheets


def _body(sess, state: CommunityState, with_decisions: bool) -> dict:
    body = {
        "sheets": [{"headers": s["headers"], "rows": s["rows"]} for s in state.sheets],
        "source": "docanonymizer",
        "source_ref": f"docanonymizer:{sess.id}",
        "category": state.category,
    }
    if with_decisions and state.decisions:
        body["decisions"] = dict(state.decisions)
    return body


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def plan_for_session(sess) -> CommunityState:
    """Run after detection. Never raises: problems land on state.error and the
    run carries on (the operator can retry or continue without ids)."""
    state = CommunityState()
    sess.community = state
    tables = tables_of(sess.extract)
    if tables is None:
        state.reason = "not a spreadsheet"
        return state
    if not familygraph.is_configured():
        state.reason = "Family Graph is not connected"
        return state
    if sess.allowed_tags and "PERSON" not in sess.allowed_tags:
        state.reason = "names (PERSON) are not being replaced"
        return state
    state.category = familygraph.settings().get("category") or "other"
    try:
        state.sheets = build_sheets(tables)
        if not state.sheets:
            state.status, state.reason = "none", "no rows found"
            return state
        _run_plan(sess, state)
    except (CommunityError, familygraph.FamilyGraphError) as exc:
        state.status = "error"
        state.error = str(exc)
        log.warning("session %s community plan failed: %s", sess.id, type(exc).__name__)
        return state
    _register_guaranteed_catches(sess, state)
    return state


def _run_plan(sess, state: CommunityState) -> None:
    data = familygraph.plan(_body(sess, state, with_decisions=True))
    state.plan = data
    state.error = None
    people = sum(len(r.get("persons") or []) for s in data.get("sheets") or [] for r in s.get("rows") or [])
    if not people:
        state.status, state.reason = "none", "no name columns recognized"
    else:
        state.status = "ready"
    summ = data.get("summary") or {}
    p, f = summ.get("persons") or {}, summ.get("families") or {}
    log.info(
        "session %s community plan: rows=%s persons matched=%s new=%s review=%s "
        "families matched=%s new=%s review=%s pending=%d",
        sess.id, summ.get("rows"), p.get("matched"), p.get("new"), p.get("review"),
        f.get("matched"), f.get("new"), f.get("review"), len(pending(state)),
    )


def retry(sess) -> CommunityState:
    state = sess.community or CommunityState()
    keep = dict(state.decisions)
    state = plan_for_session(sess)
    # Decisions still apply to the same rows; drop any whose key vanished.
    valid = {i["key"] for i in _items(state)}
    state.decisions = {k: v for k, v in keep.items() if k in valid}
    return state


def _register_guaranteed_catches(sess, state: CommunityState) -> None:
    """Family Graph knows which cells hold names. Surname cells keep the
    per-value tokens (a surname is shared by a household), so they must be
    in the replacement map even if the model missed them. Full names that
    also appear elsewhere in the text (a notes column) are caught too, and
    so is the household label."""
    reg = sess.registry
    if reg is None or not state.plan:
        return
    text = sess.extract.text if sess.extract else ""
    tables = tables_of(sess.extract) or []
    added = 0

    # Text of every cell that will NOT become a community token. A first name
    # found there as a whole word ("Ann called" in a notes column) would ship
    # untouched, so it becomes a guaranteed catch now, visible in the preview.
    name_coords = set()
    for si, sheet in enumerate(state.plan.get("sheets") or []):
        meta = state.sheets[si] if si < len(state.sheets) else None
        if meta is None:
            continue
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            rn = meta["row_numbers"][row["index"]]
            for p in row.get("persons") or []:
                c = _name_cell(p.get("name_cells") or [])
                if c is not None:
                    name_coords.add((meta["table"], rn, c["col"]))
            fam = row.get("family") or {}
            if fam.get("cell"):
                name_coords.add((meta["table"], rn, fam["cell"]["col"]))
    other_text = "\n".join(
        "\t".join(("" if v is None else str(v)) for ci, v in enumerate(r) if (ti, ri, ci) not in name_coords)
        for ti, table in enumerate(tables) for ri, r in enumerate(table)
    )

    def add(value: str) -> None:
        nonlocal added
        v = (value or "").strip()
        if len(v) < 2 or not any(c.isalpha() for c in v) or v in reg.replacements:
            return
        try:
            reg.add(v, "PERSON")
            added += 1
        except (ValueError, RuntimeError):
            pass

    for si, sheet in enumerate(state.plan.get("sheets") or []):
        meta = state.sheets[si] if si < len(state.sheets) else None
        if meta is None:
            continue
        table = tables[meta["table"]]
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            rn = meta["row_numbers"][row["index"]]
            for p in row.get("persons") or []:
                for c in p.get("name_cells") or []:
                    if c.get("part") == "family":
                        add(_cell(table, rn, c["col"]))
                full = " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x)
                if full and " " in full and full in text:
                    add(full)
                given = (p.get("given_name") or "").strip()
                if given and re.search(rf"(?<!\w){re.escape(given)}(?!\w)", other_text):
                    add(given)
            fam = row.get("family") or {}
            if fam.get("cell"):
                add(_cell(table, rn, fam["cell"]["col"]))
    state.registered = added
    if added:
        log.info("session %s community names added as guaranteed catches: %d", sess.id, added)


def _cell(table: list, r: int, c: int) -> str:
    if r >= len(table) or c >= len(table[r]):
        return ""
    v = table[r][c]
    return v if isinstance(v, str) else ("" if v is None else str(v))


# ---------------------------------------------------------------------------
# Review items and decisions
# ---------------------------------------------------------------------------

def _items(state: CommunityState) -> list:
    """Every person and household in the plan, flattened for the UI."""
    out = []
    data = state.result or state.plan or {}
    for si, sheet in enumerate(data.get("sheets") or []):
        meta = state.sheets[si] if si < len(state.sheets) else None
        if meta is None:
            continue
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            sheet_row = meta["row_numbers"][row["index"]] + 1      # 1-based, as in Excel
            for p in row.get("persons") or []:
                out.append({
                    "key": p["key"], "kind": "person", "table": meta["table"], "sheet_row": sheet_row,
                    "label": " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x) or "(no name)",
                    "role": p.get("role"), "date_of_birth": p.get("date_of_birth"),
                    "action": p.get("action"), "community_id": p.get("community_id"),
                    "same_as": p.get("same_as"), "matched": p.get("matched"),
                    "candidates": p.get("candidates") or [], "review_reasons": p.get("review_reasons") or [],
                })
            fam = row.get("family")
            if fam:
                out.append({
                    "key": fam["key"], "kind": "family", "table": meta["table"], "sheet_row": sheet_row,
                    "label": fam.get("display_name") or "(household)",
                    "action": fam.get("action"), "community_id": fam.get("community_id"),
                    "same_as": fam.get("same_as"),
                    "candidates": fam.get("candidates") or [], "review_reasons": fam.get("review_reasons") or [],
                })
    return out


def pending(state: Optional[CommunityState]) -> list:
    if state is None or state.status != "ready":
        return []
    return [i["key"] for i in _items(state) if i["action"] == "review" and i["key"] not in state.decisions]


def decide(state: CommunityState, key: str, action: str, target: Optional[str] = None) -> None:
    items = {i["key"]: i for i in _items(state)}
    if key not in items:
        raise ValueError("unknown item")
    item = items[key]
    if action == "undo":
        state.decisions.pop(key, None)
        return
    allowed = ("attach", "create", "skip") if item["kind"] == "person" else ("attach", "create")
    if action not in allowed:
        raise ValueError(f"action must be one of {', '.join(allowed)}")
    if action == "attach":
        refs = {c.get("community_id") or c.get("sheet_ref") for c in item["candidates"]}
        if item.get("matched"):
            refs.add(item["matched"].get("community_id") or item["matched"].get("sheet_ref"))
        if not target or target not in refs:
            raise ValueError("attach must name one of the listed candidates")
        state.decisions[key] = {"action": "attach", "target": target}
    else:
        state.decisions[key] = {"action": action}


def view(state: Optional[CommunityState]) -> dict:
    if state is None:
        return {"status": "off", "reason": "not planned"}
    out = {
        "status": state.status,
        "reason": state.reason,
        "error": state.error,
        "category": state.category,
        "pending": len(pending(state)),
        "decisions": state.decisions,
    }
    data = state.result or state.plan
    if data:
        out["summary"] = data.get("summary")
        out["items"] = _items(state)
    return out


# ---------------------------------------------------------------------------
# Commit, and what it means for the output file
# ---------------------------------------------------------------------------

def commit_for_session(sess) -> bool:
    """True when Family Graph wrote everything. False when it refused because
    new items need a decision (state.plan now holds them). Raises
    FamilyGraphError when Family Graph can't be reached or says no."""
    state = sess.community
    committed, data = familygraph.commit(_body(sess, state, with_decisions=True))
    if not committed:
        state.plan = data
        log.warning("session %s community commit refused: %d item(s) need a decision",
                    sess.id, len(pending(state)))
        return False
    state.result = data
    state.status = "committed"
    summ = data.get("summary") or {}
    log.info("session %s community commit: import_runs=%s persons=%s families=%s",
             sess.id, ",".join(data.get("import_runs") or []), summ.get("persons"), summ.get("families"))
    return True


@dataclass
class Overrides:
    cells: dict = field(default_factory=dict)          # (table, row, col) -> (expected, new)
    append_columns: list = field(default_factory=list)
    tokens: set = field(default_factory=set)           # every token written (protected from the scrub)
    registry: dict = field(default_factory=dict)       # id -> {kind, display, family?}
    identity_cells: list = field(default_factory=list)
    identity_columns: list = field(default_factory=list)


def overrides_for(sess) -> Overrides:
    """Cell rewrites for a committed roster. Built from the commit result."""
    state = sess.community
    ov = Overrides()
    if state is None or state.status != "committed" or not state.result:
        return ov
    tables = tables_of(sess.extract) or []
    for si, sheet in enumerate(state.result.get("sheets") or []):
        meta = state.sheets[si]
        ti = meta["table"]
        table = tables[ti]
        appended: dict = {}
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            rn = meta["row_numbers"][row["index"]]
            fam = row.get("family") or {}
            fid = fam.get("community_id")
            by_cell: dict = {}
            for p in row.get("persons") or []:
                pid = p.get("community_id")
                if p.get("action") == "skip" or not pid:
                    continue
                ov.registry[pid] = {
                    "kind": "person",
                    "display": " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x),
                    **({"family": fid} if fid else {}),
                }
                cell = _name_cell(p.get("name_cells") or [])
                if cell is None:
                    continue
                by_cell.setdefault(cell["col"], []).append((p.get("slot", 0), f"[{pid}]", cell["part"]))
            for col, items in by_cell.items():
                items.sort()
                sep = ", " if items[0][2] == "list" else " & "
                written = sep.join(tok for _, tok, _ in dict.fromkeys(items))
                _override(ov, ti, rn, col, _cell(table, rn, col), written)
            if fid:
                ov.registry[fid] = {"kind": "family", "display": fam.get("display_name") or ""}
                token = f"[{fid}]"
                if fam.get("cell"):
                    col = fam["cell"]["col"]
                    if (ti, rn, col) not in ov.cells:
                        _override(ov, ti, rn, col, _cell(table, rn, col), token)
                else:
                    appended[rn] = token
                    ov.tokens.add(token)
        if appended:
            spec = {"sheet": ti, "col": meta["width"], "header_row": meta["header_row"],
                    "header": APPENDED_HEADER, "values": appended}
            ov.append_columns.append(spec)
            ov.identity_columns.append({k: spec[k] for k in ("sheet", "col", "header_row", "header")})
    for t in list(ov.tokens):
        ov.tokens.add(t.strip("[]"))           # bare form too
    return ov


def _name_cell(cells: list) -> Optional[dict]:
    for part in ("full", "list", "given"):
        for c in cells:
            if c.get("part") == part and c.get("col") is not None:
                return c
    return None


def _override(ov: Overrides, ti: int, rn: int, col: int, original: str, written: str) -> None:
    ov.cells[(ti, rn, col)] = (original, written)
    ov.identity_cells.append({"sheet": ti, "row": rn, "col": col, "written": written, "original": original})
    for m in COMMUNITY_TOKEN_RE.finditer(written):
        ov.tokens.add(m.group(0))


def key_fields(sess, ov: Overrides) -> dict:
    """What the key file records about the community layer."""
    state = sess.community
    if not ov.registry:
        return {}
    base = (familygraph.settings().get("base_url") or "")
    return {
        "identity_registry": ov.registry,
        "identity_cells": ov.identity_cells,
        "identity_columns": ov.identity_columns,
        "community_source": {
            "familygraph_host": urlparse(base).hostname or "",
            "import_runs": (state.result or {}).get("import_runs") or [],
            "category": state.category,
            "committed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def summary_for_results(state: Optional[CommunityState]) -> Optional[dict]:
    if state is None or state.status not in ("committed", "skipped", "error", "none"):
        return None
    out = {"status": state.status}
    if state.status == "committed" and state.result:
        s = state.result.get("summary") or {}
        out["persons"] = s.get("persons")
        out["families"] = s.get("families")
        out["import_runs"] = state.result.get("import_runs") or []
    if state.error:
        out["error"] = state.error
    return out
