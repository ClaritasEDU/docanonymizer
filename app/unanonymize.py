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
restores to `donors_restored.xlsx`).
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

_ANON_SUFFIX_RE = re.compile(r"_anon_[0-9a-f]{8}$")


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


def restore_file(input_path: Path, index: KeyIndex, display_name: str = "") -> RestoreResult:
    """Restore `input_path` using the merged key `index`."""
    if index.size == 0:
        raise ValueError("the selected key(s) contain no identifiers")

    extracted = extract(input_path)
    suffix = extracted.original_suffix
    out_ext = extracted.output_ext

    stem = _ANON_SUFFIX_RE.sub("", Path(display_name or input_path.name).stem) or "document"
    out_path = _unique_path(OUTPUT_DIR / f"{stem}_restored{out_ext}")

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
                                sheet_restore=index.sheet_titles)
        elif suffix in ("docx", "doc", "odt"):
            result = scrub_docx(extracted.working_path, restorer, out_path, deep_clean=False)
        elif suffix == "csv":
            result = scrub_csv(extracted, restorer, out_path)
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
