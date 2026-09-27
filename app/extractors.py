"""File extractors per PRD 5.6.

Single entry point: `extract(path: Path) -> ExtractResult`.

Handles:
  Tier 1 (format-preserving output target):  PDF, XLSX, XLS, CSV
  Tier 2 (format-preserving output target):  DOCX, DOC
  Tier 3 (text-only output):                 PPTX, PPT, ODT, ODS, ODP, TXT, MD, RTF, HTML

DOC, PPT, ODT, ODS, ODP require LibreOffice to convert into a supported
format first. If LibreOffice is missing the extractor returns an error so
the caller can surface a "format not available" message in the UI.

NEVER log document text. Only metadata: char count, page/sheet count.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .logging_setup import get_logger

log = get_logger("extractor")


# ---------------------------------------------------------------------------
# Result types and tiers
# ---------------------------------------------------------------------------

# Map suffix (lowercase, no dot) -> output file extension and human note.
OUTPUT_NOTES: dict[str, tuple[str, str]] = {
    "pdf":  (".txt",  "text only - PDF rebuild not supported in v1"),
    "xlsx": (".xlsx", "formatting preserved"),
    "xls":  (".xlsx", "formatting preserved (converted to xlsx)"),
    "csv":  (".csv",  "format preserved"),
    "docx": (".docx", "formatting preserved"),
    "doc":  (".docx", "formatting preserved (converted to docx)"),
    "pptx": (".txt",  "text only - PPTX rebuild not supported in v1"),
    "ppt":  (".txt",  "text only - PPTX rebuild not supported in v1"),
    "odt":  (".docx", "formatting preserved (converted to docx)"),
    "ods":  (".xlsx", "formatting preserved (converted to xlsx)"),
    "odp":  (".txt",  "text only (converted via libreoffice)"),
    "txt":  (".txt",  "format preserved"),
    "md":   (".md",   "format preserved"),
    "rtf":  (".txt",  "text only"),
    "html": (".txt",  "text only"),
    "htm":  (".txt",  "text only"),
}

SUPPORTED = set(OUTPUT_NOTES)


@dataclass
class ExtractResult:
    text: str
    char_count: int
    page_count: int = 0
    sheet_count: int = 0
    output_ext: str = ".txt"
    output_note: str = ""
    # Path to the file that downstream writers should consume.
    # For DOC -> converted DOCX, this points at the converted file.
    working_path: Optional[Path] = None
    # Original suffix the user uploaded (for audit/log).
    original_suffix: str = ""
    # Per-format payloads. Only populated for the formats whose writers consume them.
    payload: dict = field(default_factory=dict)


class ExtractError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def extract(path: Path) -> ExtractResult:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in SUPPORTED:
        raise ExtractError(f"unsupported file type: .{suffix}")
    out_ext, note = OUTPUT_NOTES[suffix]

    # Convert legacy / OpenDocument formats to a primary format first.
    work = path
    if suffix in ("doc", "odt"):
        work = _libreoffice_convert(path, "docx")
        result = _extract_docx(work)
    elif suffix == "ods":
        work = _libreoffice_convert(path, "xlsx")
        result = _extract_xlsx(work)
    elif suffix == "ppt":
        work = _libreoffice_convert(path, "pptx")
        result = _extract_pptx(work)
    elif suffix == "odp":
        # LibreOffice can convert to .txt directly for simplicity.
        work = _libreoffice_convert(path, "txt")
        result = _extract_text(work)
    elif suffix == "pdf":
        result = _extract_pdf(path)
    elif suffix == "xlsx":
        result = _extract_xlsx(path)
    elif suffix == "xls":
        work = _libreoffice_convert(path, "xlsx")
        result = _extract_xlsx(work)
    elif suffix == "csv":
        result = _extract_csv(path)
    elif suffix == "docx":
        result = _extract_docx(path)
    elif suffix == "pptx":
        result = _extract_pptx(path)
    elif suffix in ("txt", "md", "rtf", "html", "htm"):
        result = _extract_text(path)
        if suffix == "rtf":
            result.text = _strip_rtf(result.text)
            result.char_count = len(result.text)
        elif suffix in ("html", "htm"):
            result.text = _strip_html(result.text)
            result.char_count = len(result.text)
    else:
        raise ExtractError(f"unhandled suffix: .{suffix}")

    result.output_ext = out_ext
    result.output_note = note
    result.working_path = work
    result.original_suffix = suffix
    log.info(
        "extract complete: suffix=%s chars=%d pages=%d sheets=%d",
        suffix, result.char_count, result.page_count, result.sheet_count,
    )
    return result


# ---------------------------------------------------------------------------
# Per-format extractors
# ---------------------------------------------------------------------------

def _extract_text(path: Path) -> ExtractResult:
    raw = path.read_bytes()
    # Heuristic decode: try utf-8, fall back to latin-1.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    return ExtractResult(text=text, char_count=len(text))


def _strip_html(text: str) -> str:
    # Rough HTML to text. PRD calls HTML "text only", so we don't need fidelity.
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    # Collapse extreme whitespace runs but keep newlines.
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_RTF_CTRL = re.compile(r"\\[a-zA-Z]+-?\d* ?|\\['][0-9a-fA-F]{2}|[{}]")


def _strip_rtf(text: str) -> str:
    return _RTF_CTRL.sub("", text)


def _extract_csv(path: Path) -> ExtractResult:
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin-1", errors="replace")
    rows: list[list[str]] = []
    text_parts: list[str] = []
    reader = csv.reader(io.StringIO(decoded))
    for row in reader:
        rows.append(row)
        text_parts.append(",".join(row))
    text = "\n".join(text_parts)
    return ExtractResult(
        text=text,
        char_count=len(text),
        sheet_count=1,
        payload={"rows": rows},
    )


def _extract_xlsx(path: Path) -> ExtractResult:
    from openpyxl import load_workbook  # local import keeps cold-start fast

    wb = load_workbook(filename=str(path), data_only=True)
    text_parts: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            text_parts.append("\t".join(cells))
        text_parts.append("")  # blank line between sheets
    text = "\n".join(text_parts)
    return ExtractResult(
        text=text,
        char_count=len(text),
        sheet_count=len(wb.worksheets),
    )


_TXBX_RE = re.compile(r"<w:txbxContent>.*?</w:txbxContent>", re.DOTALL)
_WT_TEXT_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_DOCX_HDRFTR_RE = re.compile(r"word/(?:header|footer)\d*\.xml")


def _xml_unescape_min(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def _wt_paragraph_texts(xml: str) -> list[str]:
    out = []
    for para in xml.split("</w:p>"):
        runs = _WT_TEXT_RE.findall(para)
        if runs:
            out.append(_xml_unescape_min("".join(runs)))
    return out


def _docx_hidden_layer_text(path: Path) -> str:
    """Text python-docx cannot see: footnotes, endnotes, text boxes.

    Without this, PII living only in those layers never reaches detection,
    so it is never mapped, scrubbed, or verified (CODE_REVIEW M5).
    """
    import zipfile
    chunks: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for part in ("word/footnotes.xml", "word/endnotes.xml"):
                if part in names:
                    xml = zf.read(part).decode("utf-8", errors="ignore")
                    chunks.extend(_wt_paragraph_texts(xml))
            for part in names:
                if part == "word/document.xml" or _DOCX_HDRFTR_RE.fullmatch(part):
                    xml = zf.read(part).decode("utf-8", errors="ignore")
                    for box in _TXBX_RE.findall(xml):
                        chunks.extend(_wt_paragraph_texts(box))
    except Exception as exc:  # hidden layers are additive - never fail extraction
        log.warning("hidden-layer scan skipped: %s", type(exc).__name__)
        return ""
    return "\n".join(c for c in chunks if c.strip())


def _extract_docx(path: Path) -> ExtractResult:
    from docx import Document  # python-docx

    doc = Document(str(path))
    text_parts: list[str] = []

    # Header / footer text (each section's headers/footers).
    for section in doc.sections:
        for hdr in (section.header, section.footer):
            for p in hdr.paragraphs:
                if p.text:
                    text_parts.append(p.text)

    for p in doc.paragraphs:
        if p.text:
            text_parts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if p.text:
                        text_parts.append(p.text)

    hidden = _docx_hidden_layer_text(path)
    if hidden:
        text_parts.append(hidden)

    text = "\n".join(text_parts)
    return ExtractResult(
        text=text,
        char_count=len(text),
        page_count=max(1, sum(1 for p in doc.paragraphs if p.text) // 25 or 1),
    )


def _extract_pdf(path: Path) -> ExtractResult:
    import pdfplumber

    text_parts: list[str] = []
    page_count = 0
    with pdfplumber.open(str(path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            t = page.extract_text() or ""
            text_parts.append(t)
    text = "\n".join(text_parts)
    if page_count and not text.strip():
        # A scanned/image PDF would otherwise sail through with "no PII found"
        # and release an empty output as verified (CODE_REVIEW M6).
        raise ExtractError(
            "this PDF has no extractable text - it looks like a scanned or "
            "image-only PDF. OCR is not supported in v1."
        )
    return ExtractResult(text=text, char_count=len(text), page_count=page_count)


def _extract_pptx(path: Path) -> ExtractResult:
    from pptx import Presentation

    pres = Presentation(str(path))
    text_parts: list[str] = []
    slide_count = 0
    for slide in pres.slides:
        slide_count += 1
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if run.text:
                            text_parts.append(run.text)
        # Speaker notes - separate XML in PPTX, equally important to scrub.
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
            if notes:
                text_parts.append(notes)
    text = "\n".join(text_parts)
    return ExtractResult(text=text, char_count=len(text), page_count=slide_count)


# ---------------------------------------------------------------------------
# LibreOffice conversion (system dependency, optional)
# ---------------------------------------------------------------------------

def libreoffice_available() -> bool:
    return shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def _libreoffice_convert(src: Path, target_ext: str) -> Path:
    """Convert `src` to `target_ext` using LibreOffice. Returns the new path.

    Output written into a temp directory next to the upload root and not
    deleted by us - the caller owns lifecycle. We do *not* leave the file
    in `/uploads` because it would survive normal cleanup logic.
    """
    if not libreoffice_available():
        raise ExtractError("libreoffice not installed; .doc/.ppt/.odf formats unavailable")

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    out_dir = Path(tempfile.mkdtemp(prefix="docanon-conv-"))
    cmd = [soffice, "--headless", "--convert-to", target_ext, "--outdir", str(out_dir), str(src)]
    log.info("libreoffice convert: %s -> %s", src.suffix, target_ext)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise ExtractError("libreoffice conversion timed out") from exc
    if proc.returncode != 0:
        raise ExtractError(f"libreoffice failed: {proc.stderr.decode(errors='ignore')[:200]}")

    # LibreOffice writes "<basename>.<target_ext>" into out_dir.
    converted = out_dir / f"{src.stem}.{target_ext}"
    if not converted.exists():
        # Some LibreOffice versions emit different casing.
        for cand in out_dir.iterdir():
            if cand.suffix.lower() == f".{target_ext}":
                converted = cand
                break
    if not converted.exists():
        raise ExtractError("libreoffice produced no output")
    return converted
