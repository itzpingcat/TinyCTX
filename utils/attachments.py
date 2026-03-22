"""
utils/attachments.py — Attachment classification, saving, and LLM content-block assembly.

This is a pure utility module (not an agent module).  It has no tools, hooks, or
prompts.  Bridges and the gateway call it directly.

Pipeline
--------
1. classify(attachment) → sets Attachment.kind based on mime_type / filename extension
2. save_upload(attachment, uploads_dir) → writes bytes to disk, returns Path
3. build_content_blocks(text, attachments, model_cfg, att_cfg, workspace)
     → list[dict]  (OpenAI-compat content block list for a user message)

Content block strategies
------------------------
image/*      + vision model  → {"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}
image/*      + no vision     → reference note in text, saved to uploads/
text/* + md/py/json/etc.     → {"type": "text", "text": "<fenced code block>"}
application/pdf              → {"type": "text", "text": "<extracted or stub>"}  (pdfplumber if available)
.docx                        → {"type": "text", "text": "<extracted>"}  (python-docx if available)
binary / unknown             → reference note in text, saved to uploads/

The inline-vs-reference decision respects AttachmentConfig thresholds:
  inline_max_files — max number of files to inline per message (default 3)
  inline_max_bytes — max total raw bytes to inline (default ~200 KB)
Once either threshold is hit, remaining files are reference-only regardless of kind.

Soft dependencies
-----------------
pdfplumber  — PDF text extraction.  If absent, PDFs get a stub reference note.
python-docx — DOCX text extraction. If absent, DOCX files get a stub reference note.
Neither is required; attachments.py degrades gracefully.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from contracts import Attachment, AttachmentKind
from config import ModelConfig, AttachmentConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension → MIME overrides for types mimetypes may not know
# ---------------------------------------------------------------------------
_EXT_MIME: dict[str, str] = {
    ".md":   "text/markdown",
    ".py":   "text/x-python",
    ".ts":   "text/typescript",
    ".tsx":  "text/typescript",
    ".jsx":  "text/javascript",
    ".yaml": "text/yaml",
    ".yml":  "text/yaml",
    ".toml": "text/toml",
    ".sh":   "text/x-shellscript",
    ".json": "application/json",
    ".csv":  "text/csv",
    ".xml":  "text/xml",
    ".html": "text/html",
    ".htm":  "text/html",
    ".sql":  "text/x-sql",
    ".rs":   "text/x-rust",
    ".go":   "text/x-go",
    ".java": "text/x-java",
    ".c":    "text/x-c",
    ".cpp":  "text/x-c++",
    ".h":    "text/x-c",
}

# Extensions we treat as text regardless of reported MIME
_TEXT_EXTENSIONS: frozenset[str] = frozenset(_EXT_MIME.keys()) | {
    ".txt", ".log", ".ini", ".cfg", ".conf", ".env",
    ".r", ".rb", ".php", ".swift", ".kt", ".cs",
}

# Image MIME prefixes we can inline
_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(attachment: Attachment) -> AttachmentKind:
    """
    Derive the AttachmentKind for an Attachment from its mime_type and filename.
    The returned kind is not stored back onto the (frozen) dataclass — callers
    use the return value directly.
    """
    mime  = (attachment.mime_type or "").lower().split(";")[0].strip()
    ext   = Path(attachment.filename).suffix.lower()

    # Prefer extension overrides for text types (bridges often lie about MIME)
    if ext in _TEXT_EXTENSIONS:
        return AttachmentKind.TEXT

    if mime in _IMAGE_MIMES:
        return AttachmentKind.IMAGE

    if mime == "image/svg+xml":
        return AttachmentKind.TEXT  # SVG is XML text

    if mime == "application/pdf" or ext == ".pdf":
        return AttachmentKind.DOCUMENT

    if mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) or ext == ".docx":
        return AttachmentKind.DOCUMENT

    if mime.startswith("text/"):
        return AttachmentKind.TEXT

    if mime == "application/json":
        return AttachmentKind.TEXT

    return AttachmentKind.BINARY


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def save_upload(attachment: Attachment, uploads_dir: Path) -> Path:
    """
    Write attachment bytes to uploads_dir/<filename>.
    Creates the directory if it doesn't exist.
    Returns the absolute path of the saved file.
    If writing fails, logs the error and returns the intended path anyway
    so the caller can still include a reference note.
    """
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / attachment.filename
    # Avoid silent overwrites: append a counter suffix if the name is taken.
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = uploads_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    try:
        dest.write_bytes(attachment.data)
        logger.debug("Saved upload: %s (%d bytes)", dest.name, len(attachment.data))
    except OSError as exc:
        logger.error("Failed to save upload %s: %s", dest, exc)
    return dest


# ---------------------------------------------------------------------------
# Text extraction helpers (soft deps)
# ---------------------------------------------------------------------------

def _extract_pdf_text(data: bytes) -> str | None:
    """Extract text from a PDF using pdfplumber.  Returns None if unavailable."""
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n\n".join(p for p in pages if p.strip()) or None
    except ImportError:
        return None
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return None


def _extract_docx_text(data: bytes) -> str | None:
    """Extract text from a DOCX using python-docx.  Returns None if unavailable."""
    try:
        import io
        from docx import Document
        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs) or None
    except ImportError:
        return None
    except Exception as exc:
        logger.warning("DOCX text extraction failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Content block assembly
# ---------------------------------------------------------------------------

def build_content_blocks(
    text:        str,
    attachments: tuple[Attachment, ...],
    model_cfg:   ModelConfig,
    att_cfg:     AttachmentConfig,
    workspace:   Path,
) -> list[dict] | str:
    """
    Build an OpenAI-compat content block list for a user message that includes
    one or more attachments.

    Returns a list[dict] if any attachment is inlineable (so the caller must
    send content as a list rather than a plain string), or a plain str with
    reference notes appended if every attachment is reference-only.

    Args:
        text:        The user's text message.
        attachments: Tuple of Attachment objects from InboundMessage.
        model_cfg:   ModelConfig of the currently active LLM.
        att_cfg:     AttachmentConfig thresholds from Config.
        workspace:   Resolved workspace root Path.

    The function:
      1. Saves every attachment to workspace/uploads/ (permanent record).
      2. Decides inline vs reference per file, respecting thresholds.
      3. Builds content blocks for inlineable files.
      4. Appends reference notes for non-inlineable files.
    """
    if not attachments:
        return text

    uploads_dir = workspace / att_cfg.uploads_dir

    inline_count = 0
    inline_bytes = 0
    blocks: list[dict] = []
    ref_notes: list[str] = []

    for att in attachments:
        kind = classify(att)
        saved_path = save_upload(att, uploads_dir)
        saved_rel  = saved_path.name  # just the filename for the reference note

        # --- threshold check ---
        over_count = inline_count >= att_cfg.inline_max_files
        over_bytes = (inline_bytes + len(att.data)) > att_cfg.inline_max_bytes
        force_ref  = over_count or over_bytes

        # --- strategy selection ---
        if force_ref or kind == AttachmentKind.BINARY:
            ref_notes.append(
                f"[File uploaded to workspace/uploads/{saved_rel}: {att.filename}]"
            )
            continue

        if kind == AttachmentKind.IMAGE:
            if not model_cfg.vision:
                ref_notes.append(
                    f"[Image uploaded to workspace/uploads/{saved_rel}: {att.filename}"
                    " — model does not support vision, use filesystem tools to inspect]"
                )
                continue
            # Inline as image_url block
            b64 = base64.b64encode(att.data).decode()
            mime = att.mime_type.split(";")[0].strip()
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
            inline_count += 1
            inline_bytes += len(att.data)
            continue

        if kind == AttachmentKind.TEXT:
            try:
                content = att.data.decode("utf-8", errors="replace")
            except Exception:
                content = att.data.decode("latin-1", errors="replace")
            ext  = Path(att.filename).suffix.lstrip(".")
            lang = ext if ext else "text"
            block_text = f"**{att.filename}**\n```{lang}\n{content}\n```"
            blocks.append({"type": "text", "text": block_text})
            inline_count += 1
            inline_bytes += len(att.data)
            continue

        if kind == AttachmentKind.DOCUMENT:
            ext = Path(att.filename).suffix.lower()
            extracted: str | None = None

            if ext == ".pdf" or att.mime_type == "application/pdf":
                extracted = _extract_pdf_text(att.data)
                if extracted is None:
                    ref_notes.append(
                        f"[PDF uploaded to workspace/uploads/{saved_rel}: {att.filename}"
                        " — install pdfplumber to extract text, or use filesystem tools]"
                    )
                    continue
            elif ext == ".docx":
                extracted = _extract_docx_text(att.data)
                if extracted is None:
                    ref_notes.append(
                        f"[DOCX uploaded to workspace/uploads/{saved_rel}: {att.filename}"
                        " — install python-docx to extract text, or use filesystem tools]"
                    )
                    continue

            if extracted:
                block_text = f"**{att.filename}** (extracted text)\n```\n{extracted}\n```"
                blocks.append({"type": "text", "text": block_text})
                inline_count += 1
                inline_bytes += len(att.data)
            else:
                ref_notes.append(
                    f"[Document uploaded to workspace/uploads/{saved_rel}: {att.filename}]"
                )

    # --- assemble final content ---
    if not blocks and not ref_notes:
        return text

    full_text = text
    if ref_notes:
        full_text = (text + "\n\n" + "\n".join(ref_notes)).strip()

    if not blocks:
        # All reference — return plain string, no content block list needed
        return full_text

    # Mix: text block first, then attachment blocks
    result: list[dict] = [{"type": "text", "text": full_text}] + blocks
    return result
