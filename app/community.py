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
     column becomes `[F…]`; with no household column (or an empty household
     cell, such as the covered part of a merged one), a FAMILY_ID column is
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
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from . import familygraph
from .logging_setup import get_logger

log = get_logger("community")

TABULAR_SUFFIXES = {"xlsx", "xls", "ods", "csv"}
# Family Graph's per-request limits (roster.js LIMITS). Checked here too so a
# big workbook gets a plain message up front instead of a 400 from the server.
MAX_ROWS = 20000                 # across all sheets
MAX_SHEETS = 25
MAX_COLUMNS = 300
MAX_CELL_CHARS = 4000
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
    # off | ready | error | skipped | committed | none | commit_unknown
    # commit_unknown: a commit was sent and no answer came back (timeout,
    # dropped connection, 5xx). Family Graph may have written it. The file's
    # choices are frozen: only the same request (same idempotency key) may be
    # sent again, or the file cancelled.
    status: str = "off"
    reason: str = ""             # why it is off / none
    error: Optional[str] = None
    category: str = "other"
    sheets: list = field(default_factory=list)      # what was sent, with sheet coordinates
    plan: Optional[dict] = None
    decisions: dict = field(default_factory=dict)
    result: Optional[dict] = None                   # the commit response
    registered: int = 0                             # guaranteed catches added to the registry
    commit_error: Optional[str] = None              # why the last commit did not happen
    # Name cells the operator kept as original text: key -> {"action": "skip"}.
    # Sent with the decisions so those people get no community id.
    kept: dict = field(default_factory=dict)
    commit_body: Optional[dict] = None              # the exact request, while commit_unknown
    # Fingerprint of the Family Graph address and key the frozen request went
    # out under. Family Graph replays only for the same caller, so a resend
    # under other settings can't be matched to the first send.
    commit_scope: Optional[str] = None


class CommunityError(RuntimeError):
    pass


# Decision targets (Family Graph roster contract): a community id, or - for
# someone first seen earlier in this same upload, who has no id yet - that
# person's or household's key on this upload.
_ID_TARGET_RE = re.compile(r"([IF])([0-9A-F]{16}|[0-9A-F]{8})", re.I)
_PERSON_REF_RE = re.compile(r"([0-9]{1,6}):([0-9]{1,7}):([0-9]{1,3})")
_FAMILY_REF_RE = re.compile(r"([0-9]{1,6}):([0-9]{1,7}):family", re.I)


def normalize_target(target, kind: str) -> Optional[str]:
    """The canonical form of an attach target for a person or household, or
    None when it is not one. Ids are case-insensitive (sent upper case);
    sheet refs are sent exactly as Family Graph keys them ("0:3:1",
    "0:3:family")."""
    t = target.strip() if isinstance(target, str) else ""
    m = _ID_TARGET_RE.fullmatch(t)
    if m:
        letter = m.group(1).upper()
        if letter != ("I" if kind == "person" else "F"):
            return None
        return letter + m.group(2).upper()
    if kind == "person":
        m = _PERSON_REF_RE.fullmatch(t)
        return f"{int(m.group(1))}:{int(m.group(2))}:{int(m.group(3))}" if m else None
    m = _FAMILY_REF_RE.fullmatch(t)
    return f"{int(m.group(1))}:{int(m.group(2))}:family" if m else None


def _offered_targets(item: dict) -> set:
    """Every target the review offered for this item: each candidate's id
    and, for someone new earlier in this upload, their sheet ref."""
    out = set()
    for c in list(item.get("candidates") or []) + ([item["matched"]] if item.get("matched") else []):
        for ref in (c.get("community_id"), c.get("sheet_ref")):
            n = normalize_target(ref, item["kind"])
            if n:
                out.add(n)
    return out


COMMIT_FROZEN_NOTE = (
    "This file's choices are now locked. Press CONFIRM AND SCRUB to send the same request "
    "again - if Family Graph already wrote it, it returns that result instead of writing "
    "anyone twice - or cancel the file.")


SCOPE_CHANGED_NOTE = (
    "Family Graph's address or API key changed after this file's commit went out, so a resend "
    "can't be matched to it. Nothing was sent. Cancel this file and upload it again: anyone "
    "the first send wrote comes back as known, with the same ids.")
STALE_RESEND_NOTE = (
    "Family Graph refused the resend because part of this roster looks already written by the "
    "first send. The choices stay locked. Cancel this file and upload it again: anyone the "
    "first send wrote comes back as known, with the same ids.")


def _settings_scope() -> str:
    """A fingerprint of the Family Graph address and key (never logged, never
    stored outside this session)."""
    s = familygraph.settings()
    raw = f"{(s.get('base_url') or '').strip().rstrip('/').lower()}\n{s.get('api_key') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def explain(exc: Exception, what: str) -> str:
    """Operator-facing text for a failed Family Graph call. `what` is "plan"
    (nothing is ever written) or "commit"."""
    if isinstance(exc, familygraph.FamilyGraphError) and exc.timed_out:
        if what == "commit":
            return (f"{exc} while writing this roster. Nothing was scrubbed and no file was written. "
                    f"Family Graph may still finish on its side. {COMMIT_FROZEN_NOTE} "
                    "For a very large roster, raise FAMILYGRAPH_ROSTER_TIMEOUT_S in .env.")
        return (f"{exc} while matching this roster. Nothing was written. For a very large "
                "roster, raise FAMILYGRAPH_ROSTER_TIMEOUT_S in .env.")
    return str(exc)


def _sentence(exc: Exception) -> str:
    m = str(exc).strip()
    return m if m.endswith((".", "?", "!")) else m + "."


def outcome_unknown(exc: Exception) -> bool:
    """True when a commit may have been written even though it failed here:
    no answer (timeout, connection dropped after sending) or a server error.
    A clean 4xx means Family Graph answered and wrote nothing, and a request
    that never left this machine (not configured, bad URL, no connection
    made) wrote nothing either."""
    if getattr(exc, "not_sent", False):
        return False
    status = getattr(exc, "status", 0) or 0
    return status == 0 or status >= 500


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


# The only headers that make a one-column list a list of people. A header
# word alone is not enough there: "Ministry Name", "Room Name" or "Family
# notes" over a column of text would mint a person per line.
_PERSON_LIST_HEADER_RE = re.compile(
    r"(?:(?:student|child|member|parishioner|person|parent|guardian)(?:'s)?\s+)?"
    r"(?:full\s+|legal\s+)?names?", re.I)


def _header_row(table: list) -> Optional[int]:
    hr = _wide_header_row(table)
    return hr if hr is not None else _single_column_header(table)


def _wide_header_row(table: list) -> Optional[int]:
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


def _single_column_header(table: list) -> Optional[int]:
    """A one-column list ("Name" over a list of names): the FIRST row whose
    one cell is exactly a person-name header. A title above it ("Student
    Roster") is not one, and neither is a data row under it that happens to
    hold a header word ("Emma Child"). Every person such a sheet yields goes
    to review (see _items): one column is too little to be certain."""
    for i, row in enumerate(table[:25]):
        filled = [c for c in row if (c or "").strip()]
        if len(filled) == 1 and _PERSON_LIST_HEADER_RE.fullmatch(filled[0].strip()):
            return i
    return None


def build_sheets(tables: list) -> list:
    sheets = []
    for ti, table in enumerate(tables):
        if not table:
            continue
        hr, one_column = _wide_header_row(table), False
        if hr is None:
            hr, one_column = _single_column_header(table), True
        if hr is None:
            continue
        width = max(len(r) for r in table)
        pad = lambda r: [(c if c is not None else "") for c in r] + [""] * (width - len(r))
        row_numbers = list(range(hr + 1, len(table)))
        sheets.append({
            "table": ti,
            "header_row": hr,
            "width": width,
            "headers": pad(table[hr]),
            "rows": [pad(table[r]) for r in row_numbers],
            "row_numbers": row_numbers,
            "one_column": one_column,
        })
    if len(sheets) > MAX_SHEETS:
        # Monthly tabs, totals, lookups: a sheet whose header names no person,
        # household or contact column can't hold a roster. Drop those first.
        sheets = [s for s in sheets if any(_HEADER_WORD_RE.search(h or "") for h in s["headers"])]
        if len(sheets) > MAX_SHEETS:
            raise CommunityError(f"this file has {len(sheets)} roster sheets; community ids handle "
                                 f"up to {MAX_SHEETS} per file")
    total = sum(len(s["row_numbers"]) for s in sheets)
    if total > MAX_ROWS:
        raise CommunityError(f"this file has {total:,} rows; community ids handle up to {MAX_ROWS:,} per file")
    for s in sheets:
        if s["width"] > MAX_COLUMNS:
            raise CommunityError(f"sheet {s['table'] + 1} has {s['width']:,} columns; community ids handle "
                                 f"up to {MAX_COLUMNS} per sheet")
        if any(len(c) > MAX_CELL_CHARS for r in [s["headers"]] + s["rows"] for c in r if isinstance(c, str)):
            raise CommunityError(f"sheet {s['table'] + 1} has a cell longer than {MAX_CELL_CHARS:,} characters; "
                                 f"community ids handle cells up to {MAX_CELL_CHARS:,}")
    return sheets


def _body(sess, state: CommunityState, with_decisions: bool) -> dict:
    body = {
        "sheets": [{"headers": s["headers"], "rows": s["rows"]} for s in state.sheets],
        "source": "docanonymizer",
        "source_ref": f"docanonymizer:{sess.id}",
        "category": state.category,
    }
    if with_decisions and (state.decisions or state.kept):
        # A kept name wins over any decision on the same person.
        body["decisions"] = {**state.decisions, **state.kept}
    return body


def commit_body(sess, state: CommunityState) -> dict:
    """The commit request with its idempotency key: session id plus a hash
    of the canonical body, so a resend of the same request is recognized by
    Family Graph and a different request can never reuse the key."""
    body = _body(sess, state, with_decisions=True)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body["idempotency_key"] = f"docanon:{sess.id}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"
    return body


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def plan_for_session(sess, decisions: Optional[dict] = None) -> CommunityState:
    """Run after detection (and on retry, with the decisions made so far).
    Never raises: problems land on state.error and the run carries on - the
    operator must retry or explicitly continue without ids.

    The new state replaces sess.community only when it is complete, so a
    slow plan never exposes a half-built state (a retry keeps showing the
    previous one, marked busy)."""
    state = CommunityState(decisions=dict(decisions or {}))
    try:
        _plan_into(sess, state)
    finally:
        sess.community = state
    return state


def _plan_into(sess, state: CommunityState) -> None:
    tables = tables_of(sess.extract)
    if tables is None:
        state.reason = "not a spreadsheet"
        return
    if not familygraph.is_configured():
        state.reason = "Family Graph is not connected"
        return
    if sess.allowed_tags and "PERSON" not in sess.allowed_tags:
        state.reason = "names (PERSON) are not being replaced"
        return
    state.category = familygraph.settings().get("category") or "other"
    try:
        state.sheets = build_sheets(tables)
        if not state.sheets:
            state.status, state.reason = "none", "no rows found"
            return
        _run_plan(sess, state)
    except (CommunityError, familygraph.FamilyGraphError) as exc:
        state.status = "error"
        state.error = explain(exc, "plan")
        state.plan = None
        log.warning("session %s community plan failed: %s%s", sess.id, type(exc).__name__,
                    " (timed out)" if getattr(exc, "timed_out", False) else "")
        return
    _register_guaranteed_catches(sess, state)


def _run_plan(sess, state: CommunityState) -> None:
    rows = sum(len(s["rows"]) for s in state.sheets)
    log.info("session %s community plan requested: sheets=%d rows=%d decisions=%d",
             sess.id, len(state.sheets), rows, len(state.decisions))
    sess.fg_rows, sess.fg_busy = rows, "plan"
    try:
        data = familygraph.plan(_body(sess, state, with_decisions=True))
    finally:
        sess.fg_busy = ""
    state.plan = data
    state.error = None
    _prune_decisions(sess, state)
    people = sum(len(r.get("persons") or []) for s in data.get("sheets") or [] for r in s.get("rows") or [])
    if not people:
        state.status, state.reason = "none", "no name columns recognized"
    else:
        state.status = "ready"
    summ = data.get("summary") or {}
    p, f = summ.get("persons") or {}, summ.get("families") or {}
    log.info(
        "session %s community plan: rows=%s persons matched=%s new=%s review=%s "
        "families matched=%s new=%s review=%s told_apart=%s pending=%d",
        sess.id, summ.get("rows"), p.get("matched"), p.get("new"), p.get("review"),
        f.get("matched"), f.get("new"), f.get("review"), summ.get("told_apart", 0), len(pending(state)),
    )


def retry(sess) -> CommunityState:
    """Ask Family Graph again. Decisions made so far still apply to the same
    rows and go with the request; any the new plan no longer supports are
    dropped (the item comes back as needing a decision). Refused while a
    commit's outcome is unknown: a new plan would change the request."""
    if sess.community is not None and sess.community.status == "commit_unknown":
        raise CommunityError("frozen")
    keep = dict((sess.community or CommunityState()).decisions)
    return plan_for_session(sess, decisions=keep)


def _prune_decisions(sess, state: CommunityState) -> None:
    """Drop decisions that no longer fit the plan: the item is gone, or an
    attach target is no longer one of the offered candidates. Never keeps a
    decision Family Graph would apply to someone the operator did not see."""
    if not state.decisions:
        return
    items = {i["key"]: i for i in _items(state)}
    # Decisions Family Graph says the data has overtaken (someone else
    # committed the same person meanwhile, or this file's own earlier
    # commit already wrote them). Kept, every confirm would resend them and
    # be refused again with nothing left to decide.
    stale = set((state.plan or {}).get("stale_decisions") or [])
    kept = {}
    for key, d in state.decisions.items():
        item = items.get(key)
        if item is None or key in stale:
            continue
        if d.get("action") == "attach" and d.get("target") not in _offered_targets(item):
            continue
        if d.get("action") == "skip" and item["kind"] != "person":
            continue
        kept[key] = d
    dropped = len(state.decisions) - len(kept)
    state.decisions = kept
    if dropped:
        log.warning("session %s community decisions dropped (no longer offered): %d", sess.id, dropped)


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
                # Given name and surname alike: a name that sits only inside a
                # full-name or list cell ("Emma Smith") is covered there by the
                # [I...] token, so a bare "Smith" in the notes would ship.
                for part in (p.get("given_name"), p.get("family_name")):
                    part = (part or "").strip()
                    if part and re.search(rf"(?<!\w){re.escape(part)}(?!\w)", other_text):
                        add(part)
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
        # A one-column list could be ministries or rooms under "Name": before
        # the commit every person on it needs a human, never an automatic
        # new id or match (Family Graph cannot tell from one column).
        confirm_each = bool(meta.get("one_column")) and state.result is None
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            sheet_row = meta["row_numbers"][row["index"]] + 1      # 1-based, as in Excel
            for p in row.get("persons") or []:
                if confirm_each and p.get("action") in ("new", "matched"):
                    p = {**p, "action": "review",
                         "candidates": list(p.get("candidates") or []) + ([p["matched"]] if p.get("matched") else []),
                         "review_reasons": list(p.get("review_reasons") or []) + ["one_column_list"]}
                out.append({
                    "key": p["key"], "kind": "person", "table": meta["table"], "sheet_row": sheet_row,
                    "label": " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x) or "(no name)",
                    "role": p.get("role"), "date_of_birth": p.get("date_of_birth"),
                    "action": p.get("action"), "community_id": p.get("community_id"),
                    "same_as": p.get("same_as"), "matched": p.get("matched"),
                    "candidates": p.get("candidates") or [], "review_reasons": p.get("review_reasons") or [],
                    # namesakes Family Graph's rules told apart without asking (a count)
                    "told_apart": p.get("told_apart") if isinstance(p.get("told_apart"), int) else 0,
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
    return [i["key"] for i in _items(state)
            if i["action"] == "review" and i["key"] not in state.decisions and i["key"] not in state.kept]


def kept_for(sess, state: CommunityState, deselected) -> dict:
    """People whose name the operator kept as original text in the preview
    (clicked its placeholder): their name cell equals a kept value, or their
    full name does. They are sent as skip decisions, so their cell keeps the
    text the operator chose and no community id is minted for them. Exact
    matches only - a kept "Ann" never touches "Ann Lee"."""
    kept_values = {d.strip() for d in (deselected or []) if isinstance(d, str) and d.strip()}
    out: dict = {}
    if not kept_values or not state.plan:
        return out
    tables = tables_of(sess.extract) or []
    for si, sheet in enumerate(state.plan.get("sheets") or []):
        meta = state.sheets[si] if si < len(state.sheets) else None
        if meta is None or meta["table"] >= len(tables):
            continue
        table = tables[meta["table"]]
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            rn = meta["row_numbers"][row["index"]]
            for p in row.get("persons") or []:
                c = _name_cell(p.get("name_cells") or [])
                cell = _cell(table, rn, c["col"]).strip() if c else ""
                full = " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x).strip()
                if (cell and cell in kept_values) or (full and full in kept_values):
                    out[p["key"]] = {"action": "skip"}
    return out


def decide(state: CommunityState, key: str, action: str, target: Optional[str] = None) -> None:
    if state.status == "commit_unknown":
        raise ValueError("a commit for this file may already be written - its choices are locked")
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
        norm = normalize_target(target, item["kind"])
        if norm is None:
            shape = ("an I… id or a row reference like 0:3:1" if item["kind"] == "person"
                     else "an F… id or a row reference like 0:3:family")
            raise ValueError(f"attach target must be {shape}")
        if norm not in _offered_targets(item):
            raise ValueError("attach must name one of the listed candidates")
        state.decisions[key] = {"action": "attach", "target": norm}
    else:
        state.decisions[key] = {"action": action}


def view(state: Optional[CommunityState], busy: str = "") -> dict:
    """What the preview shows. `busy` ("plan" / "commit") means a Family
    Graph call for this session is still running."""
    if state is None:
        return {"status": "off", "reason": "not planned", "busy": busy}
    out = {
        "status": state.status,
        "reason": state.reason,
        "error": state.error,
        "category": state.category,
        "pending": len(pending(state)),
        "decisions": state.decisions,
        "busy": busy,
        "commit_error": state.commit_error,
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
    FamilyGraphError when Family Graph can't be reached or says no.

    Every commit carries an idempotency key. When the outcome is unknown
    (no answer, or a server error) the state becomes "commit_unknown" and
    the exact request is kept: the next confirm resends it byte for byte,
    so Family Graph replays what it wrote instead of writing anyone twice."""
    state = sess.community
    state.commit_error = None
    resend = state.status == "commit_unknown" and state.commit_body is not None
    body = state.commit_body if resend else commit_body(sess, state)
    scope = _settings_scope()
    if resend and state.commit_scope and scope != state.commit_scope:
        # Family Graph keys its replay by caller: under a new key or address
        # the resend would be a fresh commit, and a refusal would prove
        # nothing. Stay frozen; a fresh upload is the certain way out.
        state.commit_error = SCOPE_CHANGED_NOTE
        log.error("session %s community resend refused: Family Graph settings changed since the first send",
                  sess.id)
        raise familygraph.FamilyGraphError(SCOPE_CHANGED_NOTE, 0, not_sent=True)
    rows = sum(len(s["rows"]) for s in state.sheets)
    log.info("session %s community commit requested: rows=%d decisions=%d kept=%d resend=%s",
             sess.id, rows, len(state.decisions), len(state.kept), resend)
    sess.fg_rows, sess.fg_busy = rows, "commit"
    try:
        committed, data = familygraph.commit(body)
    except familygraph.FamilyGraphError as exc:
        if resend or outcome_unknown(exc):
            # Family Graph may hold this commit. Freeze until the same
            # request gets a real answer, or the operator cancels.
            if not resend:
                state.commit_scope = scope
            state.status, state.commit_body = "commit_unknown", body
            state.commit_error = (explain(exc, "commit") if exc.timed_out
                                  else f"{_sentence(exc)} Nothing was scrubbed and no file was written, but Family "
                                       f"Graph may have written this roster anyway. {COMMIT_FROZEN_NOTE}")
        else:
            # A clean refusal (key, scope, rate limit): nothing was written.
            state.commit_error = explain(exc, "commit")
        log.error("session %s community commit failed: %s status=%s%s frozen=%s", sess.id, type(exc).__name__,
                  exc.status, " (timed out)" if exc.timed_out else "", state.status == "commit_unknown")
        raise
    finally:
        sess.fg_busy = ""
    if not committed:
        stale = (data or {}).get("stale_decisions") or []
        if resend and stale:
            # A stale decision can mean this file's first send WAS written
            # (Family Graph sees a create that repeats an earlier commit).
            # Unlocking would let the choices change for a written roster.
            state.commit_error = STALE_RESEND_NOTE
            log.error("session %s community resend refused with stale decisions: %d - stays frozen",
                      sess.id, len(stale))
            raise familygraph.FamilyGraphError(STALE_RESEND_NOTE, 409, {"error": "review_incomplete"})
        # Family Graph never stores a refusal, and no decision repeats an
        # earlier commit, so nothing from this file was written: the
        # operator may decide again.
        if resend:
            log.info("session %s community commit unlocked: the resend was refused, nothing was written", sess.id)
        state.status, state.commit_body, state.commit_scope = "ready", None, None
        if data:                        # keep the last plan if the refusal carried none
            state.plan = data
        stale = len((state.plan or {}).get("stale_decisions") or [])
        _prune_decisions(sess, state)
        if pending(state) or not stale:
            state.commit_error = "Family Graph found new items that need a decision"
        else:
            state.commit_error = ("Family Graph's records changed since you decided, so those answers were "
                                  "dropped and it matched those people itself. Check the preview and confirm again.")
        log.warning("session %s community commit refused: %d item(s) need a decision, %d stale decision(s) dropped",
                    sess.id, len(pending(state)), stale)
        return False
    state.result = data
    state.status, state.commit_body, state.commit_scope = "committed", None, None
    summ = data.get("summary") or {}
    log.info("session %s community commit: import_runs=%s persons=%s families=%s replayed=%s",
             sess.id, ",".join(data.get("import_runs") or []), summ.get("persons"), summ.get("families"),
             bool(data.get("replayed")))
    return True


def outcome_counts(state: Optional[CommunityState]) -> Optional[dict]:
    """Distinct people and households by what the commit did to them: known
    (an id that existed before) or new (minted now). Family Graph's summary
    counts a decided review item as "review" even when it minted an id, so
    the results table is built from this instead."""
    if state is None or state.status != "committed" or not state.result:
        return None
    people = {"known": set(), "new": set(), "skipped": 0}
    households = {"known": set(), "new": set()}
    for sheet in state.result.get("sheets") or []:
        for row in sheet.get("rows") or []:
            if row.get("skipped"):
                continue
            for p in row.get("persons") or []:
                if p.get("action") == "skip" or not p.get("community_id"):
                    people["skipped"] += 1
                else:
                    people["new" if p.get("code_state") == "new" else "known"].add(p["community_id"])
            fam = row.get("family") or {}
            if fam.get("community_id"):
                households["new" if fam.get("code_state") == "new" else "known"].add(fam["community_id"])
    # An id minted earlier in this same commit and then matched is new.
    people["known"] -= people["new"]
    households["known"] -= households["new"]
    return {
        "people": {"known": len(people["known"]), "new": len(people["new"]), "skipped": people["skipped"]},
        "households": {"known": len(households["known"]), "new": len(households["new"])},
    }


@dataclass
class Overrides:
    cells: dict = field(default_factory=dict)          # (table, row, col) -> (expected, new)
    append_columns: list = field(default_factory=list)
    tokens: set = field(default_factory=set)           # every token written (protected from the scrub)
    registry: dict = field(default_factory=dict)       # id -> {kind, display, family?}
    identity_cells: list = field(default_factory=list)
    identity_columns: list = field(default_factory=list)
    skipped: set = field(default_factory=set)          # cells the scrub could not rewrite
    left_alone: int = 0                                # shared name cells with a kept person


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
            # Name cells holding someone who gets no id (kept as original
            # text, or not a person). A couple or list cell like that is
            # never rewritten to the others' tokens alone - that would delete
            # the kept name. It keeps the normal per-value treatment.
            left_alone: set = set()
            for p in row.get("persons") or []:
                pid = p.get("community_id")
                if p.get("action") == "skip" or not pid:
                    c = _name_cell(p.get("name_cells") or [])
                    if c is not None:
                        left_alone.add(c["col"])
                    continue
                # One person can fill several cells: many rows (a parent per
                # child) or two slots of one row (Parent 1 and Parent 2 the
                # same human, entered twice). One registry entry; the first
                # full spelling seen names them, the first household sticks.
                display = " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x)
                rec = ov.registry.get(pid)
                if rec is None:
                    ov.registry[pid] = {"kind": "person", "display": display, **({"family": fid} if fid else {})}
                else:
                    if len(display) > len(rec.get("display") or "") and " " not in (rec.get("display") or ""):
                        rec["display"] = display
                    if fid and "family" not in rec:
                        rec["family"] = fid
                cell = _name_cell(p.get("name_cells") or [])
                if cell is None:
                    continue
                by_cell.setdefault(cell["col"], []).append((p.get("slot", 0), f"[{pid}]", cell["part"]))
            for col, items in by_cell.items():
                if col in left_alone:
                    ov.left_alone += 1
                    continue
                items.sort()
                sep = ", " if items[0][2] == "list" else " & "
                written = sep.join(tok for _, tok, _ in dict.fromkeys(items))
                _override(ov, ti, rn, col, _cell(table, rn, col), written)
            if fid:
                # The first real label names the household; a later row with
                # none never blanks it (Family Graph sends null when a row has
                # no household cell and no surname).
                rec = ov.registry.setdefault(fid, {"kind": "family", "display": ""})
                if not rec["display"]:
                    rec["display"] = (fam.get("display_name") or "").strip()
                token = f"[{fid}]"
                col = (fam.get("cell") or {}).get("col")
                if col is not None and (ti, rn, col) in ov.cells:
                    pass
                elif col is not None and _cell(table, rn, col).strip():
                    _override(ov, ti, rn, col, _cell(table, rn, col), token)
                else:
                    # No household column, or this row's cell is empty (often
                    # the covered part of a merged "Smith Family" cell, which
                    # can't be written). The id goes in the appended column.
                    appended[rn] = token
                    ov.tokens.add(token)
        if appended:
            spec = {"sheet": ti, "col": meta["width"], "header_row": meta["header_row"],
                    "header": APPENDED_HEADER, "values": appended}
            ov.append_columns.append(spec)
            ov.identity_columns.append({k: spec[k] for k in ("sheet", "col", "header_row", "header")})
    if ov.left_alone:
        log.info("session %s community name cells left as normal text (a kept person shares them): %d",
                 sess.id, ov.left_alone)
    _label_nameless_households(ov)
    for t in list(ov.tokens):
        ov.tokens.add(t.strip("[]"))           # bare form too
    return ov


def _label_nameless_households(ov: Overrides) -> None:
    """A household with no label would restore to "" in an AI answer - its
    reference silently deleted. Name it after its members instead
    ("Ann & Ben household"); with no members known, leave it empty and the
    restorer reports it as unresolved."""
    for fid, rec in ov.registry.items():
        if rec.get("kind") != "family" or rec.get("display"):
            continue
        members = [r["display"] for r in ov.registry.values()
                   if r.get("kind") == "person" and r.get("family") == fid and r.get("display")]
        members = list(dict.fromkeys(members))
        if members:
            rec["display"] = " & ".join(members) + " household"


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
        # Only rewrites that happened: restore trusts these coordinates.
        "identity_cells": [c for c in ov.identity_cells
                           if (c["sheet"], c["row"], c["col"]) not in ov.skipped],
        "identity_columns": ov.identity_columns,
        "community_source": {
            "familygraph_host": urlparse(base).hostname or "",
            "import_runs": (state.result or {}).get("import_runs") or [],
            "category": state.category,
            "committed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def note_scrub(sess, ov: Overrides, skipped) -> None:
    """After the scrub: cells it could not rewrite (a formula, a merged cell)
    kept their normal per-value treatment and carry no community id. Say so
    in the steps, and keep them out of the key. They stay in
    ov.identity_cells so the residue check still looks for their originals."""
    keys = {tuple(k) for k in skipped or []} & set(ov.cells)
    if not keys:
        return
    ov.skipped |= keys
    log.warning("session %s community cells not rewritten (formula, merged or changed): %d", sess.id, len(keys))
    sess.scrub_steps.append({
        "step": (f"community ids: {len(keys)} roster cell(s) could not take an id (a formula or merged "
                 "cell) - scrubbed as normal text, no [I...] there"),
        "status": "err",
    })


def after_commit_note(sess) -> str:
    """Appended to a scrub failure once Family Graph has committed, so the
    operator knows the ids are safe and a rerun is the fix."""
    state = getattr(sess, "community", None)
    if state is None or state.status != "committed":
        return ""
    log.error("session %s scrub failed after the community commit (import runs kept): %d",
              sess.id, len((state.result or {}).get("import_runs") or []))
    return (" - Family Graph already saved this roster's ids, so nothing is lost there. Upload the "
            "file again: everyone it saved comes back as known, with the same ids.")


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
