"""Anonymize pipeline orchestrator.

Wires extract -> detect -> preview -> confirm -> scrub -> verify -> output
into a session object that the Flask routes drive step-by-step. Sessions
live in memory only - this is a single-user local tool.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import endpoints as endpoints_mod
from .config import KEYS_DIR, OUTPUT_DIR, UPLOADS_DIR
from .detector import detect_pii
from .extractors import ExtractResult, extract
from .key_files import new_session_id, save_key_file
from .logging_setup import get_logger
from .mapper import EntityRegistry
from .scrubber import scrub_csv, scrub_docx, scrub_text, scrub_xlsx
from .verifier import verify_output

log = get_logger("pipeline")


_SESSIONS: dict[str, "Session"] = {}
_SESSIONS_LOCK = threading.Lock()


@dataclass
class Session:
    id: str
    upload_path: Path
    original_filename: str
    endpoint: dict
    allowed_tags: list[str] = field(default_factory=list)
    custom_terms: list[tuple[str, str]] = field(default_factory=list)  # (tag, text)
    extract: Optional[ExtractResult] = None
    registry: Optional[EntityRegistry] = None
    detection_progress: list[dict] = field(default_factory=list)
    detection_complete: bool = False
    scrub_steps: list[dict] = field(default_factory=list)
    output_path: Optional[Path] = None
    key_path: Optional[Path] = None
    verify_result: Optional[dict] = None
    formula_warnings: list[str] = field(default_factory=list)
    preview: Optional[dict] = None     # built once after detection; polled often
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)


def new_session(upload_path: Path, original_filename: str, allowed_tags: list[str],
                endpoint: Optional[dict] = None,
                custom_terms: Optional[list[tuple[str, str]]] = None) -> Session:
    sid = new_session_id()
    ep = endpoint or endpoints_mod.get_active() or {}
    sess = Session(
        id=sid,
        upload_path=upload_path,
        original_filename=original_filename,
        endpoint=ep,
        allowed_tags=allowed_tags,
        custom_terms=custom_terms or [],
    )
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = sess
    log.info("session created: id=%s file=%s", sid, original_filename)
    return sess


def get_session(sid: str) -> Optional[Session]:
    with _SESSIONS_LOCK:
        return _SESSIONS.get(sid)


def discard_session(sid: str) -> None:
    with _SESSIONS_LOCK:
        sess = _SESSIONS.pop(sid, None)
    if not sess:
        return
    cleanup_session_files(sess, remove_output=True)
    log.info("session discarded: id=%s", sid)


def cleanup_session_files(sess: Session, remove_output: bool = False) -> None:
    """Delete every temp file a session created (CLAUDE.md temp hygiene, H1).

    Runs on every terminal state - success, failure, cancel. Removes the
    upload, any LibreOffice conversion directory, staged scrub files, and
    (when `remove_output` is set, i.e. failure/cancel) the output file too.
    The key file and a verified output are the only artifacts that survive.
    """
    try:
        if sess.upload_path.exists() and UPLOADS_DIR in sess.upload_path.parents:
            sess.upload_path.unlink()
            log.info("upload deleted: session=%s", sess.id)
    except OSError as exc:
        log.warning("upload cleanup failed: %s", exc)

    # LibreOffice conversion dir (working_path lives in a docanon-conv-* tempdir).
    if sess.extract and sess.extract.working_path:
        wp = Path(sess.extract.working_path)
        if wp != sess.upload_path and wp.parent.name.startswith("docanon-conv-"):
            shutil.rmtree(wp.parent, ignore_errors=True)
            log.info("conversion tempdir removed: session=%s", sess.id)

    if sess.output_path:
        # Staged intermediates from the scrubber, if a crash left them behind.
        for staged in (sess.output_path.with_suffix(".staged.docx"),
                       sess.output_path.with_suffix(".staged.xlsx")):
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        if remove_output:
            try:
                if sess.output_path.exists():
                    sess.output_path.unlink()
                    log.info("output removed: session=%s", sess.id)
            except OSError as exc:
                log.warning("output cleanup failed: %s", exc)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def run_extract_and_detect(sess: Session) -> Session:
    """Extract text from the upload, then run LLM detection across chunks.

    Any failure is terminal: the temp upload (and any conversion dir) is
    deleted immediately - PII must not linger on disk after a failed run.
    """
    try:
        sess.extract = extract(sess.upload_path)
    except Exception as exc:  # bubble up clean error to UI
        log.error("session %s extract failed: %s", sess.id, exc)
        cleanup_session_files(sess)
        # Set the error last - it is the completion signal the UI polls, and
        # cleanup must already be done when it appears.
        sess.error = f"extraction failed: {exc}"
        return sess

    def on_chunk(info: dict) -> None:
        sess.detection_progress.append(info)

    try:
        sess.registry = detect_pii(
            sess.extract.text,
            allowed_tags=sess.allowed_tags or None,
            on_chunk=on_chunk,
            endpoint=sess.endpoint,
        )
    except Exception as exc:
        log.error("session %s detect failed: %s", sess.id, exc)
        cleanup_session_files(sess)
        sess.error = f"detection failed: {exc}"
        return sess

    # Operator-supplied custom terms are guaranteed catches - registered after
    # detection so an LLM tag on the same text wins (first tag is stable).
    for tag, term in sess.custom_terms:
        if term and term in sess.extract.text:
            try:
                sess.registry.add(term, tag)
            except (ValueError, RuntimeError) as exc:
                log.warning("custom term skipped: tag=%s reason=%s", tag, exc)

    sess.detection_complete = True
    counts = sess.registry.counts_per_type()
    log.info(
        "session %s detect complete: entities=%d replacements=%d %s",
        sess.id,
        sess.registry.total_entities(),
        sess.registry.total_replacements(),
        " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )
    return sess


def confirm_and_scrub(sess: Session, deselected: Optional[list[str]] = None) -> Session:
    """Apply replacements + deep scrub + verification.

    `deselected` is a list of original PII strings the operator removed in
    the preview panel.
    """
    if sess.registry is None or sess.extract is None:
        sess.error = "session not detected yet"
        return sess
    if sess.verify_result is not None:
        # Already scrubbed - source temp files are gone, nothing to redo.
        log.warning("session %s confirm ignored: already scrubbed", sess.id)
        return sess

    for original in deselected or []:
        sess.registry.drop(original)

    rmap = sess.registry.as_replacement_map()
    if not rmap:
        sess.error = "no replacements to apply (registry empty)"
        return sess

    suffix = sess.extract.original_suffix
    out_ext = sess.extract.output_ext
    out_name = f"{Path(sess.original_filename).stem}_anon_{sess.id}{out_ext}"
    out_path = OUTPUT_DIR / out_name

    sess.scrub_steps.append({"step": "applying replacements", "status": "active"})
    log.info("session %s scrub start: out=%s", sess.id, out_name)

    try:
        if suffix in ("xlsx", "xls", "ods"):
            result = scrub_xlsx(sess.extract.working_path, rmap, out_path)
        elif suffix in ("docx", "doc", "odt"):
            result = scrub_docx(sess.extract.working_path, rmap, out_path)
        elif suffix == "csv":
            result = scrub_csv(sess.extract, rmap, out_path)
        else:
            result = scrub_text(sess.extract, rmap, out_path)
    except Exception as exc:
        log.error("session %s scrub failed: %s", sess.id, exc)
        sess.output_path = out_path  # so cleanup can remove partials/staged
        cleanup_session_files(sess, remove_output=True)
        sess.output_path = None
        sess.error = f"scrub failed: {exc}"
        return sess

    sess.scrub_steps[-1]["status"] = "done"
    for layer in result.layers_cleaned:
        sess.scrub_steps.append({"step": f"deep scrub: {layer}", "status": "done"})
    sess.formula_warnings = result.formula_warnings
    sess.output_path = result.output_path

    sess.scrub_steps.append({"step": "verification scan running", "status": "active"})
    try:
        verify = verify_output(result.output_path, rmap)
        sess.scrub_steps[-1]["status"] = "done" if verify.passed else "err"
        if verify.passed:
            sess.key_path = save_key_file(
                session_id=sess.id,
                original_filename=sess.original_filename,
                endpoint=sess.endpoint,
                pii_types_scrubbed=sorted(sess.registry.counts_per_type().keys()),
                registry=sess.registry,
                sheet_titles=result.sheet_titles,
            )
    except Exception as exc:
        # An output that can't be verified is never released. Without this,
        # an exception here killed the worker thread silently and the UI sat
        # on "verification scan running" forever.
        log.exception("session %s verify failed: %s", sess.id, type(exc).__name__)
        sess.scrub_steps[-1]["status"] = "err"
        cleanup_session_files(sess, remove_output=True)
        sess.error = f"verification could not run ({type(exc).__name__}): {exc}"
        return sess

    if verify.passed:
        sess.scrub_steps.append({"step": "output ready", "status": "done"})
        # Success: the upload (and any conversion dir) has served its purpose.
        cleanup_session_files(sess)
    else:
        sess.scrub_steps.append({"step": "output blocked - verification failed", "status": "err"})
        # The failed output still contains PII by definition - quarantine it.
        cleanup_session_files(sess, remove_output=True)

    # Set last - verify_result is the completion signal the UI polls, and all
    # cleanup must already be done when it appears.
    sess.verify_result = {
        "passed": verify.passed,
        "map_match_types": verify.map_match_types,
        "warnings": verify.regex_match_types,
        "total_matches": verify.total_matches,
    }

    log.info(
        "session %s scrub complete: verified=%s out=%s",
        sess.id, verify.passed, result.output_path.name,
    )
    return sess


def cleanup_upload(sess: Session) -> None:
    """Delete the temp upload after successful processing."""
    try:
        if sess.upload_path.exists() and UPLOADS_DIR in sess.upload_path.parents:
            sess.upload_path.unlink()
            log.info("upload deleted: %s", sess.upload_path.name)
    except OSError as exc:
        log.warning("upload delete failed: %s", exc)


def anonymized_text(sess: Session) -> str:
    """Return the anonymized text from the session output, for on-screen copy/paste.

    For text outputs (.txt / .csv / .html) we read the file directly. For
    DOCX / XLSX / PDF / PPTX we re-run the extractor against the scrubbed
    output so the user gets readable text without having to open the file
    in another app. The file itself is still the authoritative deliverable
    that downstream tools should consume.
    """
    if sess.output_path is None or not sess.output_path.exists():
        raise RuntimeError("no output available yet")
    if sess.verify_result is None or not sess.verify_result.get("passed"):
        raise RuntimeError("verification has not passed - text view blocked")

    suffix = sess.output_path.suffix.lower().lstrip(".")
    if suffix in ("txt", "md", "csv", "html", "htm", "rtf"):
        return sess.output_path.read_text(encoding="utf-8", errors="replace")
    # Binary formats - re-extract.
    from .extractors import extract  # local import to avoid bootstrap cycles
    return extract(sess.output_path).text
