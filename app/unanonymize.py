"""Unanonymize pipeline (PRD 5.8, extended 2026-09-27 for AI round trips).

Restores a file - the anonymized file itself, or anything an AI tool gave
back after analyzing it (.txt .md .csv .xlsx .docx ...) - using one or more
key files. Identifier matching is tolerant of how AI tools mangle tokens
(see restorer.py) and uses the same format-aware writers as anonymize, so
formatting survives and identifiers split across Word runs are still found.

After writing, the output is re-extracted and scanned again. Any identifier
that the keys could resolve but that is still present is counted as
`residual` in the report, so a partial restore is never silent.

Output filename: `{input_name}_restored.{ext}` (an `_anon_<session>` suffix
from our own anonymized files is dropped, so `donors_anon_ab12cd34.xlsx`
restores to `donors_restored.xlsx`). The filename is cosmetic: the exact
roster layout is chosen from the file's cells, never its name.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from .config import OUTPUT_DIR
from .extractors import extract
from .logging_setup import get_logger
from .restorer import KeyIndex, RestoreReport, TokenRestorer, build_index, new_report
from .scrubber import scrub_csv, scrub_docx, scrub_text, scrub_xlsx

log = get_logger("unanon")

# Our own suffix, plus the copy suffix a browser adds to a second download
# ("x_anon_ab12cd34 (1).csv" arrives as "x_anon_ab12cd34_1.csv").
_ANON_SUFFIX_RE = re.compile(r"_anon_([0-9a-f]{8})(?:_\d+)?$")


@dataclass
class RestoreResult:
    output_path: Path
    report: RestoreReport
    text: str            # re-extracted text of the restored output (for the UI)


def _unique_path(base: Path) -> Path:
    """Never silently overwrite an earlier restore."""
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = base.with_name(f"{base.stem}_{i}{base.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _tables(extracted) -> list:
    tables = extracted.payload.get("tables")
    if tables is None and extracted.payload.get("rows") is not None:
        tables = [extracted.payload["rows"]]
    return tables or []


def _cell_text(tables: list, key: tuple) -> str:
    t, r, c = key
    if t >= len(tables) or r >= len(tables[t]) or c >= len(tables[t][r]):
        return ""
    v = tables[t][r][c]
    return "" if v is None else str(v)


def _exact_layout(index: KeyIndex, name: str, tables: list):
    """Cell overrides that put roster cells back exactly, for our own
    anonymized output. A community id restores to a person's name; the cell
    may have held "Smith, John & Mary" - this puts back exactly that. Only
    cells that still hold exactly what was written are touched; anything the
    AI or a person changed falls back to the token restore.

    The layout is picked by the file's own cells, not its name: a renamed
    download ("roster_anon_ab12cd34 (1).csv", "final.csv") restores exactly
    too. The key whose written tokens sit at the most of their recorded
    coordinates wins; the filename only breaks a tie. Two keys that fit
    equally but disagree on an original are never guessed between."""
    scored = []
    for sid, layout in index.identity_layout.items():
        overrides = {}
        for c in layout["cells"]:
            try:
                overrides[(int(c["sheet"]), int(c["row"]), int(c["col"]))] = (str(c["written"]), str(c["original"]))
            except (KeyError, TypeError, ValueError):
                continue
        hits = {k: v for k, v in overrides.items() if _cell_text(tables, k) == v[0]}
        if hits:
            scored.append((len(hits), sid, overrides, hits, list(layout["columns"])))
    if not scored:
        return None, None
    best = max(s[0] for s in scored)
    top = [s for s in scored if s[0] == best]
    m = _ANON_SUFFIX_RE.search(Path(name).stem)
    named = next((s for s in top if m and s[1] == m.group(1)), None)
    if named is None and len(top) > 1:
        first = top[0]
        if any(s[3] != first[3] or s[4] != first[4] for s in top[1:]):
            log.warning("unanonymize: %d keys fit this file's roster cells equally and disagree; "
                        "roster cells restored by name instead", len(top))
            return None, None
    pick = named or top[0]
    return pick[2], pick[4]


def restore_file(input_path: Path, index: KeyIndex, display_name: str = "") -> RestoreResult:
    """Restore `input_path` using the merged key `index`."""
    if index.size == 0:
        raise ValueError("the selected key(s) contain no identifiers")

    extracted = extract(input_path)
    suffix = extracted.original_suffix
    out_ext = extracted.output_ext

    stem = _ANON_SUFFIX_RE.sub("", Path(display_name or input_path.name).stem) or "document"
    out_path = _unique_path(OUTPUT_DIR / f"{stem}_restored{out_ext}")
    overrides, columns = _exact_layout(index, display_name or input_path.name, _tables(extracted))
    if overrides:
        log.info("unanonymize: exact roster layout for %d cell(s), %d appended column(s)",
                 len(overrides), len(columns or []))

    restorer = TokenRestorer(index)
    # Tally from the text the operator actually sees, once - the writers make
    # several passes and would double count.
    report = new_report(index)
    restorer.scan(extracted.text, report)

    log.info(
        "unanonymize start: input_suffix=%s output=%s keys=%d ids=%d",
        suffix, out_path.name, len(index.key_names), index.size,
    )

    try:
        if suffix in ("xlsx", "xls", "ods"):
            result = scrub_xlsx(extracted.working_path, restorer, out_path, deep_clean=False,
                                sheet_restore=index.sheet_titles,
                                cell_overrides=overrides, remove_columns=columns)
        elif suffix in ("docx", "doc", "odt"):
            result = scrub_docx(extracted.working_path, restorer, out_path, deep_clean=False)
        elif suffix == "csv":
            result = scrub_csv(extracted, restorer, out_path,
                               cell_overrides=overrides, remove_columns=columns)
        else:  # txt / md / rtf / html / pdf / pptx - text output
            result = scrub_text(extracted, restorer, out_path)
    finally:
        # LibreOffice conversion dirs must not outlive the run (temp hygiene).
        wp = extracted.working_path
        if wp and wp != input_path and Path(wp).parent.name.startswith("docanon-conv-"):
            shutil.rmtree(Path(wp).parent, ignore_errors=True)

    # Post-restore check: nothing the keys can resolve may remain.
    restored_text = extract(out_path).text
    residual = RestoreReport()
    restorer.scan(restored_text, residual)
    report.residual = residual.restored

    log.info(
        "unanonymize complete: out=%s bytes=%d restored=%d relabeled=%d untagged=%d "
        "unresolved=%d residual=%d",
        out_path.name, result.bytes_written, report.restored, report.relabeled,
        report.untagged, report.unresolved_count, report.residual,
    )
    return RestoreResult(output_path=result.output_path, report=report, text=restored_text)


def unanonymize_file(input_path: Path, key: Union[dict, KeyIndex]) -> Path:
    """Restore a file with a single key payload (or a prebuilt index)."""
    index = key if isinstance(key, KeyIndex) else build_index(
        [(key.get("original_filename") or "key", key)]
    )
    return restore_file(input_path, index).output_path
