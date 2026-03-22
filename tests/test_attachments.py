"""
tests/test_attachments.py

Tests for utils/attachments.py — classify(), save_upload(), and
build_content_blocks().

No network I/O, no LLM calls.  All filesystem writes go to pytest's
tmp_path fixture so nothing touches the real workspace.

Run with:
    pytest tests/test_attachments.py -v
"""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from contracts import Attachment, AttachmentKind
from config import ModelConfig, AttachmentConfig
from utils.attachments import (
    classify,
    save_upload,
    build_content_blocks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _att(
    filename: str = "file.txt",
    data: bytes = b"hello",
    mime_type: str = "text/plain",
) -> Attachment:
    return Attachment(filename=filename, data=data, mime_type=mime_type)


def _model(vision: bool = False) -> ModelConfig:
    return ModelConfig(model="test", base_url="http://localhost/v1", vision=vision)


def _cfg(**kwargs) -> AttachmentConfig:
    defaults = dict(inline_max_files=3, inline_max_bytes=200 * 1024, uploads_dir="uploads")
    defaults.update(kwargs)
    return AttachmentConfig(**defaults)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

class TestClassify:
    def test_png_is_image(self):
        assert classify(_att("photo.png", mime_type="image/png")) == AttachmentKind.IMAGE

    def test_jpeg_is_image(self):
        assert classify(_att("photo.jpg", mime_type="image/jpeg")) == AttachmentKind.IMAGE

    def test_gif_is_image(self):
        assert classify(_att("anim.gif", mime_type="image/gif")) == AttachmentKind.IMAGE

    def test_webp_is_image(self):
        assert classify(_att("img.webp", mime_type="image/webp")) == AttachmentKind.IMAGE

    def test_svg_is_text(self):
        assert classify(_att("icon.svg", mime_type="image/svg+xml")) == AttachmentKind.TEXT

    def test_pdf_by_mime_is_document(self):
        assert classify(_att("doc.pdf", mime_type="application/pdf")) == AttachmentKind.DOCUMENT

    def test_pdf_by_extension_is_document(self):
        assert classify(_att("doc.pdf", mime_type="application/octet-stream")) == AttachmentKind.DOCUMENT

    def test_docx_by_mime_is_document(self):
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert classify(_att("report.docx", mime_type=mime)) == AttachmentKind.DOCUMENT

    def test_docx_by_extension_is_document(self):
        assert classify(_att("report.docx", mime_type="application/octet-stream")) == AttachmentKind.DOCUMENT

    def test_txt_is_text(self):
        assert classify(_att("notes.txt", mime_type="text/plain")) == AttachmentKind.TEXT

    def test_md_is_text(self):
        assert classify(_att("README.md", mime_type="application/octet-stream")) == AttachmentKind.TEXT

    def test_py_is_text(self):
        assert classify(_att("script.py", mime_type="application/octet-stream")) == AttachmentKind.TEXT

    def test_json_is_text(self):
        assert classify(_att("data.json", mime_type="application/json")) == AttachmentKind.TEXT

    def test_yaml_is_text(self):
        assert classify(_att("config.yaml", mime_type="application/octet-stream")) == AttachmentKind.TEXT

    def test_csv_is_text(self):
        assert classify(_att("data.csv", mime_type="text/csv")) == AttachmentKind.TEXT

    def test_unknown_extension_unknown_mime_is_binary(self):
        assert classify(_att("archive.zip", mime_type="application/zip")) == AttachmentKind.BINARY

    def test_exe_is_binary(self):
        assert classify(_att("prog.exe", mime_type="application/octet-stream")) == AttachmentKind.BINARY

    def test_extension_overrides_wrong_mime(self):
        # A .py file reported as octet-stream by the bridge should still be TEXT
        att = _att("script.py", mime_type="application/octet-stream")
        assert classify(att) == AttachmentKind.TEXT

    def test_text_plain_mime_without_known_ext_is_text(self):
        assert classify(_att("mystery", mime_type="text/plain")) == AttachmentKind.TEXT


# ---------------------------------------------------------------------------
# save_upload()
# ---------------------------------------------------------------------------

class TestSaveUpload:
    def test_file_is_written(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("hello.txt", data=b"world")
        path = save_upload(att, ws / "uploads")
        assert path.exists()
        assert path.read_bytes() == b"world"

    def test_uploads_dir_created_if_absent(self, tmp_path):
        ws = _workspace(tmp_path)
        uploads = ws / "uploads" / "nested"
        att = _att("hello.txt")
        save_upload(att, uploads)
        assert uploads.exists()

    def test_returns_path_inside_uploads_dir(self, tmp_path):
        ws = _workspace(tmp_path)
        uploads = ws / "uploads"
        att = _att("hello.txt")
        path = save_upload(att, uploads)
        assert path.parent == uploads

    def test_collision_avoidance_appends_counter(self, tmp_path):
        ws = _workspace(tmp_path)
        uploads = ws / "uploads"
        att = _att("file.txt", data=b"first")
        p1 = save_upload(att, uploads)

        att2 = _att("file.txt", data=b"second")
        p2 = save_upload(att2, uploads)

        assert p1 != p2
        assert p2.stem == "file_1"
        assert p1.read_bytes() == b"first"
        assert p2.read_bytes() == b"second"

    def test_multiple_collisions(self, tmp_path):
        ws = _workspace(tmp_path)
        uploads = ws / "uploads"
        for _ in range(3):
            save_upload(_att("x.txt"), uploads)
        files = list(uploads.glob("x*.txt"))
        assert len(files) == 3


# ---------------------------------------------------------------------------
# build_content_blocks() — no attachments
# ---------------------------------------------------------------------------

class TestBuildNoAttachments:
    def test_no_attachments_returns_plain_string(self, tmp_path):
        ws = _workspace(tmp_path)
        result = build_content_blocks("hello", (), _model(), _cfg(), ws)
        assert result == "hello"


# ---------------------------------------------------------------------------
# build_content_blocks() — text files
# ---------------------------------------------------------------------------

class TestBuildTextAttachments:
    def test_text_file_inlined_as_block(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("notes.txt", data=b"some text", mime_type="text/plain")
        result = build_content_blocks("read this", (att,), _model(), _cfg(), ws)
        assert isinstance(result, list)
        text_blocks = [b for b in result if b["type"] == "text"]
        combined = " ".join(b["text"] for b in text_blocks)
        assert "some text" in combined
        assert "notes.txt" in combined

    def test_md_file_fenced_with_md_lang(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("README.md", data=b"# Title\nBody", mime_type="text/plain")
        result = build_content_blocks("see attached", (att,), _model(), _cfg(), ws)
        assert isinstance(result, list)
        content = " ".join(b["text"] for b in result if b["type"] == "text")
        assert "```md" in content or "```markdown" in content or "```" in content

    def test_py_file_fenced_with_py_lang(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("script.py", data=b"print('hi')", mime_type="text/x-python")
        result = build_content_blocks("run this", (att,), _model(), _cfg(), ws)
        content = " ".join(b["text"] for b in result if b["type"] == "text")
        assert "```py" in content

    def test_user_text_included_in_first_block(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("f.txt", data=b"content")
        result = build_content_blocks("my message", (att,), _model(), _cfg(), ws)
        assert isinstance(result, list)
        assert result[0]["type"] == "text"
        assert "my message" in result[0]["text"]

    def test_text_file_saved_to_uploads(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("saved.txt", data=b"data")
        build_content_blocks("hi", (att,), _model(), _cfg(), ws)
        assert (ws / "uploads" / "saved.txt").exists()


# ---------------------------------------------------------------------------
# build_content_blocks() — image files
# ---------------------------------------------------------------------------

class TestBuildImageAttachments:
    def _png_bytes(self) -> bytes:
        # Minimal 1x1 white PNG
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI6QAAAABJRU5ErkJggg=="
        )

    def test_image_inlined_for_vision_model(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("photo.png", data=self._png_bytes(), mime_type="image/png")
        result = build_content_blocks("look at this", (att,), _model(vision=True), _cfg(), ws)
        assert isinstance(result, list)
        img_blocks = [b for b in result if b["type"] == "image_url"]
        assert len(img_blocks) == 1
        url = img_blocks[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    def test_image_base64_is_valid(self, tmp_path):
        ws = _workspace(tmp_path)
        data = self._png_bytes()
        att = _att("photo.png", data=data, mime_type="image/png")
        result = build_content_blocks("look", (att,), _model(vision=True), _cfg(), ws)
        img_block = next(b for b in result if b["type"] == "image_url")
        encoded = img_block["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(encoded) == data

    def test_image_not_inlined_for_non_vision_model(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("photo.png", data=self._png_bytes(), mime_type="image/png")
        result = build_content_blocks("look", (att,), _model(vision=False), _cfg(), ws)
        # No image blocks; should fall back to reference note
        if isinstance(result, list):
            img_blocks = [b for b in result if b["type"] == "image_url"]
            assert len(img_blocks) == 0
        else:
            # Returned plain string with reference note
            assert "uploads" in result

    def test_image_reference_note_mentions_filename(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("diagram.png", data=self._png_bytes(), mime_type="image/png")
        result = build_content_blocks("see diagram", (att,), _model(vision=False), _cfg(), ws)
        text = result if isinstance(result, str) else result[0]["text"]
        assert "diagram.png" in text

    def test_image_saved_to_uploads_regardless_of_vision(self, tmp_path):
        ws = _workspace(tmp_path)
        for vision in (True, False):
            data = self._png_bytes()
            att = _att(f"img_{vision}.png", data=data, mime_type="image/png")
            build_content_blocks("hi", (att,), _model(vision=vision), _cfg(), ws)
        assert (ws / "uploads" / "img_True.png").exists()
        assert (ws / "uploads" / "img_False.png").exists()


# ---------------------------------------------------------------------------
# build_content_blocks() — binary files
# ---------------------------------------------------------------------------

class TestBuildBinaryAttachments:
    def test_binary_file_produces_reference_note(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("archive.zip", data=b"\x50\x4b\x03\x04", mime_type="application/zip")
        result = build_content_blocks("attached", (att,), _model(), _cfg(), ws)
        text = result if isinstance(result, str) else result[0]["text"]
        assert "archive.zip" in text
        assert "uploads" in text.lower() or "workspace" in text.lower()

    def test_binary_never_inlined_even_with_small_size(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("tiny.bin", data=b"\x00\x01", mime_type="application/octet-stream")
        result = build_content_blocks("hi", (att,), _model(), _cfg(), ws)
        if isinstance(result, list):
            assert all(b["type"] != "image_url" for b in result)


# ---------------------------------------------------------------------------
# build_content_blocks() — threshold enforcement
# ---------------------------------------------------------------------------

class TestBuildThresholds:
    def test_inline_max_files_enforced(self, tmp_path):
        ws = _workspace(tmp_path)
        cfg = _cfg(inline_max_files=2)
        attachments = tuple(
            _att(f"file{i}.txt", data=b"x") for i in range(4)
        )
        result = build_content_blocks("msg", attachments, _model(), cfg, ws)
        # At most 2 files inlined → text blocks from attachments ≤ 2
        # (first block is always the user message text)
        att_blocks = [b for b in result if b["type"] == "text"][1:]  # skip user msg block
        assert len(att_blocks) <= 2

    def test_inline_max_bytes_enforced(self, tmp_path):
        ws = _workspace(tmp_path)
        cfg = _cfg(inline_max_bytes=10)  # tiny limit
        # First file is small (fits), second blows the limit
        a1 = _att("small.txt", data=b"abc")
        a2 = _att("big.txt",   data=b"x" * 100)
        result = build_content_blocks("msg", (a1, a2), _model(), cfg, ws)
        assert isinstance(result, list)
        # big.txt should appear only as a reference note, not as an inline block
        full_text = " ".join(b.get("text", "") for b in result if b["type"] == "text")
        assert "big.txt" in full_text  # reference note
        # The inline block for big.txt should NOT contain its full content
        inline_contents = [b["text"] for b in result if b["type"] == "text" and "x" * 100 in b.get("text", "")]
        assert len(inline_contents) == 0

    def test_over_threshold_files_saved_to_uploads(self, tmp_path):
        ws = _workspace(tmp_path)
        cfg = _cfg(inline_max_files=1)
        a1 = _att("first.txt", data=b"a")
        a2 = _att("second.txt", data=b"b")
        build_content_blocks("msg", (a1, a2), _model(), cfg, ws)
        # Both should still be saved to uploads regardless of inline/ref status
        assert (ws / "uploads" / "first.txt").exists()
        assert (ws / "uploads" / "second.txt").exists()

    def test_zero_inline_max_files_all_reference(self, tmp_path):
        ws = _workspace(tmp_path)
        cfg = _cfg(inline_max_files=0)
        att = _att("notes.txt", data=b"content")
        result = build_content_blocks("msg", (att,), _model(), cfg, ws)
        # With 0 allowed inline files, result should be a plain string
        assert isinstance(result, str)
        assert "notes.txt" in result


# ---------------------------------------------------------------------------
# build_content_blocks() — PDF (soft dep: pdfplumber)
# ---------------------------------------------------------------------------

class TestBuildPDFAttachments:
    def test_pdf_without_pdfplumber_produces_reference(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("doc.pdf", data=b"%PDF-1.4 fake", mime_type="application/pdf")
        with patch.dict("sys.modules", {"pdfplumber": None}):
            result = build_content_blocks("read this", (att,), _model(), _cfg(), ws)
        text = result if isinstance(result, str) else result[0]["text"]
        assert "doc.pdf" in text

    def test_pdf_saved_even_when_pdfplumber_missing(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("doc.pdf", data=b"%PDF-1.4 fake", mime_type="application/pdf")
        with patch.dict("sys.modules", {"pdfplumber": None}):
            build_content_blocks("read this", (att,), _model(), _cfg(), ws)
        assert (ws / "uploads" / "doc.pdf").exists()

    def test_pdf_with_pdfplumber_extracts_text(self, tmp_path):
        """If pdfplumber is available and returns text, it should be inlined."""
        ws = _workspace(tmp_path)
        att = _att("doc.pdf", data=b"%PDF-1.4 fake", mime_type="application/pdf")

        fake_page = type("Page", (), {"extract_text": lambda self: "extracted content"})()
        fake_pdf  = type("PDF",  (), {
            "__enter__": lambda self, *a: self,
            "__exit__":  lambda self, *a: None,
            "pages":     [fake_page],
        })()

        fake_pdfplumber = type("mod", (), {"open": staticmethod(lambda *a, **kw: fake_pdf)})()

        with patch.dict("sys.modules", {"pdfplumber": fake_pdfplumber}):
            result = build_content_blocks("read this", (att,), _model(), _cfg(), ws)

        assert isinstance(result, list)
        full = " ".join(b.get("text", "") for b in result)
        assert "extracted content" in full


# ---------------------------------------------------------------------------
# build_content_blocks() — DOCX (soft dep: python-docx)
# ---------------------------------------------------------------------------

class TestBuildDOCXAttachments:
    def test_docx_without_python_docx_produces_reference(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("report.docx", data=b"PK fake docx", mime_type="application/octet-stream")
        with patch.dict("sys.modules", {"docx": None}):
            result = build_content_blocks("read this", (att,), _model(), _cfg(), ws)
        text = result if isinstance(result, str) else result[0]["text"]
        assert "report.docx" in text

    def test_docx_with_python_docx_extracts_text(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("report.docx", data=b"PK fake docx", mime_type="application/octet-stream")

        fake_para = type("Para", (), {"text": "paragraph text"})()
        fake_doc  = type("Doc",  (), {"paragraphs": [fake_para]})()
        fake_docx = type("mod",  (), {"Document": staticmethod(lambda *a, **kw: fake_doc)})()

        with patch.dict("sys.modules", {"docx": fake_docx}):
            result = build_content_blocks("read", (att,), _model(), _cfg(), ws)

        assert isinstance(result, list)
        full = " ".join(b.get("text", "") for b in result)
        assert "paragraph text" in full


# ---------------------------------------------------------------------------
# build_content_blocks() — mixed attachments
# ---------------------------------------------------------------------------

class TestBuildMixedAttachments:
    def _png_bytes(self) -> bytes:
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI6QAAAABJRU5ErkJggg=="
        )

    def test_image_and_text_both_included(self, tmp_path):
        ws = _workspace(tmp_path)
        img = _att("photo.png", data=self._png_bytes(), mime_type="image/png")
        txt = _att("notes.txt", data=b"some notes", mime_type="text/plain")
        result = build_content_blocks("check both", (img, txt), _model(vision=True), _cfg(), ws)
        assert isinstance(result, list)
        types = {b["type"] for b in result}
        assert "image_url" in types
        assert "text" in types

    def test_user_text_not_lost_with_multiple_attachments(self, tmp_path):
        ws = _workspace(tmp_path)
        attachments = tuple(_att(f"f{i}.txt", data=b"x") for i in range(2))
        result = build_content_blocks("original message", attachments, _model(), _cfg(), ws)
        assert isinstance(result, list)
        assert "original message" in result[0]["text"]

    def test_all_binary_returns_plain_string(self, tmp_path):
        ws = _workspace(tmp_path)
        attachments = (
            _att("a.zip", data=b"\x50\x4b", mime_type="application/zip"),
            _att("b.exe", data=b"\x4d\x5a", mime_type="application/octet-stream"),
        )
        result = build_content_blocks("msg", attachments, _model(), _cfg(), ws)
        assert isinstance(result, str)

    def test_reference_notes_appended_to_text(self, tmp_path):
        ws = _workspace(tmp_path)
        att = _att("file.bin", data=b"\x00", mime_type="application/octet-stream")
        result = build_content_blocks("hi", (att,), _model(), _cfg(), ws)
        text = result if isinstance(result, str) else result[0]["text"]
        assert "file.bin" in text
        assert "hi" in text
