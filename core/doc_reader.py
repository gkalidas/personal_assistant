"""
Local document text extraction — PDF, DOCX, XLSX, plain text.
No cloud. No telemetry. Runs fully offline.

Usage:
    from core.doc_reader import read_document
    result = read_document("/path/to/file.pdf")
    # result = {"text": "...", "pages": 5, "type": "pdf", "title": "...", "error": None}
"""

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Max characters to extract — keep it LLM-friendly
_MAX_CHARS = 12_000


def read_document(path: str | Path) -> dict[str, Any]:
    """
    Extract text from a document file.

    Returns:
        {
            "text":    str,         # extracted text (truncated to _MAX_CHARS)
            "type":    str,         # "pdf" | "docx" | "xlsx" | "txt" | "csv"
            "pages":   int | None,  # page count if available
            "title":   str | None,  # doc title if available
            "chars":   int,         # total chars extracted
            "truncated": bool,      # True if text was cut off
            "error":   str | None,  # error message if extraction failed
        }
    """
    p = Path(path).expanduser().resolve()
    ext = p.suffix.lower()

    if not p.exists():
        return _err(ext, f"File not found: {path}")

    size_mb = p.stat().st_size / 1e6
    if size_mb > 50:
        return _err(ext, f"File too large ({size_mb:.1f} MB) — limit is 50 MB")

    try:
        if ext == ".pdf":
            return _read_pdf(p)
        elif ext == ".docx":
            return _read_docx(p)
        elif ext == ".xlsx":
            return _read_xlsx(p)
        elif ext in (".txt", ".md", ".csv", ".log", ".json"):
            return _read_text(p, ext.lstrip("."))
        else:
            return _err(ext, f"Unsupported file type '{ext}'. Supported: PDF, DOCX, XLSX, TXT, MD, CSV")
    except Exception as e:
        log.error("doc_reader error for %s: %s", path, e)
        return _err(ext, str(e))


def _read_pdf(p: Path) -> dict:
    try:
        import pypdf
    except ImportError:
        return _err("pdf", "pypdf not installed — run: pip install pypdf")

    pages_text = []
    title = None
    try:
        reader = pypdf.PdfReader(str(p))
        meta = reader.metadata
        if meta and meta.title:
            title = meta.title
        for page in reader.pages:
            pages_text.append(page.extract_text() or "")
    except Exception as e:
        return _err("pdf", f"PDF read error: {e}")

    full = "\n\n".join(pages_text).strip()
    return _result("pdf", full, pages=len(pages_text), title=title)


def _read_docx(p: Path) -> dict:
    try:
        from docx import Document
    except ImportError:
        return _err("docx", "python-docx not installed — run: pip install python-docx")

    try:
        doc = Document(str(p))
    except Exception as e:
        return _err("docx", f"DOCX read error: {e}")

    title = None
    prop = doc.core_properties
    if prop and prop.title:
        title = prop.title

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    # Also extract table content
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
            if row_text:
                paragraphs.append(row_text)

    full = "\n".join(paragraphs).strip()
    return _result("docx", full, title=title)


def _read_xlsx(p: Path) -> dict:
    try:
        import openpyxl
    except ImportError:
        return _err("xlsx", "openpyxl not installed — run: pip install openpyxl")

    try:
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    except Exception as e:
        return _err("xlsx", f"XLSX read error: {e}")

    rows_text = []
    sheet_names = wb.sheetnames
    for name in sheet_names:
        ws = wb[name]
        rows_text.append(f"=== Sheet: {name} ===")
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            if row_count > 500:
                rows_text.append(f"[... {ws.max_row - 500} more rows truncated]")
                break
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                rows_text.append(" | ".join(cells))
            row_count += 1
    wb.close()

    full = "\n".join(rows_text).strip()
    return _result("xlsx", full, title=f"{p.stem} ({len(sheet_names)} sheets)")


def _read_text(p: Path, ext: str) -> dict:
    try:
        text = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        return _err(ext, f"Read error: {e}")
    return _result(ext, text)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _result(typ: str, text: str, pages: int | None = None,
            title: str | None = None) -> dict:
    truncated = len(text) > _MAX_CHARS
    return {
        "text":      text[:_MAX_CHARS],
        "type":      typ,
        "pages":     pages,
        "title":     title,
        "chars":     len(text),
        "truncated": truncated,
        "error":     None,
    }


def _err(typ: str, msg: str) -> dict:
    return {
        "text": "", "type": typ, "pages": None,
        "title": None, "chars": 0, "truncated": False,
        "error": msg,
    }


def describe(result: dict) -> str:
    """One-line summary for display."""
    if result["error"]:
        return f"Error reading document: {result['error']}"
    t = result["type"].upper()
    pg = f"{result['pages']} pages, " if result["pages"] else ""
    trun = " (truncated)" if result["truncated"] else ""
    title = f" · {result['title']}" if result.get("title") else ""
    return f"{t}{title} — {pg}{result['chars']:,} chars{trun}"
