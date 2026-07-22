"""
tests/test_attachments.py

Tests for utils/attachments.py — attachment classification, saving, and
content-block assembly. Focuses on pure/testable logic (classify, save_upload
dedup, build_content_blocks thresholds) using small synthetic byte strings.
Heavy binary decoding (real image/PDF parsing) is not exercised — Pillow/
pdfplumber/python-docx are soft deps and this module degrades gracefully
without them, which is exactly what these tests check.

Run with:
    pytest tests/
"""
from __future__ import annotations

from pathlib import Path

import pytest

from TinyCTX.contracts import Attachment, AttachmentKind
from TinyCTX.config import ModelConfig, AttachmentConfig
from TinyCTX.utils.attachments import classify, save_upload, build_content_blocks


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

class TestClassify:
    def test_image_mime(self):
        a = Attachment(filename="pic.png", data=b"x", mime_type="image/png")
        assert classify(a) == AttachmentKind.IMAGE

    def test_pdf_by_mime(self):
        a = Attachment(filename="doc", data=b"x", mime_type="application/pdf")
        assert classify(a) == AttachmentKind.DOCUMENT

    def test_pdf_by_extension(self):
        a = Attachment(filename="doc.pdf", data=b"x", mime_type="application/octet-stream")
        assert classify(a) == AttachmentKind.DOCUMENT

    def test_docx_by_extension(self):
        a = Attachment(filename="report.docx", data=b"x", mime_type="application/octet-stream")
        assert classify(a) == AttachmentKind.DOCUMENT

    def test_text_extension_override(self):
        """Extension text types win even if mime_type lies (bridges often mislabel)."""
        a = Attachment(filename="script.py", data=b"x", mime_type="application/octet-stream")
        assert classify(a) == AttachmentKind.TEXT

    def test_text_mime(self):
        a = Attachment(filename="note", data=b"x", mime_type="text/plain")
        assert classify(a) == AttachmentKind.TEXT

    def test_svg_treated_as_text(self):
        a = Attachment(filename="icon.svg", data=b"x", mime_type="image/svg+xml")
        assert classify(a) == AttachmentKind.TEXT

    def test_json_mime_treated_as_text(self):
        a = Attachment(filename="data", data=b"x", mime_type="application/json")
        assert classify(a) == AttachmentKind.TEXT

    def test_unknown_binary(self):
        a = Attachment(filename="blob.bin", data=b"x", mime_type="application/octet-stream")
        assert classify(a) == AttachmentKind.BINARY

    def test_mime_with_charset_suffix(self):
        a = Attachment(filename="note.unknown", data=b"x", mime_type="text/plain; charset=utf-8")
        assert classify(a) == AttachmentKind.TEXT


# ---------------------------------------------------------------------------
# save_upload()
# ---------------------------------------------------------------------------

class TestSaveUpload:
    def test_saves_file(self, tmp_path):
        a = Attachment(filename="hello.txt", data=b"hello world", mime_type="text/plain")
        dest = save_upload(a, tmp_path)
        assert dest.exists()
        assert dest.read_bytes() == b"hello world"

    def test_dedup_same_content_returns_existing(self, tmp_path):
        a = Attachment(filename="hello.txt", data=b"same bytes", mime_type="text/plain")
        first = save_upload(a, tmp_path)
        b = Attachment(filename="hello.txt", data=b"same bytes", mime_type="text/plain")
        second = save_upload(b, tmp_path)
        assert first == second

    def test_different_content_same_name_gets_suffixed(self, tmp_path):
        a = Attachment(filename="hello.txt", data=b"content A", mime_type="text/plain")
        b = Attachment(filename="hello.txt", data=b"content B", mime_type="text/plain")
        first = save_upload(a, tmp_path)
        second = save_upload(b, tmp_path)
        assert first != second
        assert first.read_bytes() == b"content A"
        assert second.read_bytes() == b"content B"

    def test_path_traversal_filename_sanitized(self, tmp_path):
        a = Attachment(filename="../../etc/passwd", data=b"x", mime_type="text/plain")
        dest = save_upload(a, tmp_path)
        assert dest.parent == tmp_path.resolve() or str(dest.resolve()).startswith(str(tmp_path.resolve()))

    def test_filename_colliding_with_cache_sidecar_renamed(self, tmp_path):
        a = Attachment(filename="cache.json", data=b"x", mime_type="text/plain")
        dest = save_upload(a, tmp_path)
        assert dest.name == "cache.json.upload"

    def test_creates_missing_directory(self, tmp_path):
        target = tmp_path / "nested" / "uploads"
        a = Attachment(filename="f.txt", data=b"x", mime_type="text/plain")
        dest = save_upload(a, target)
        assert dest.exists()


# ---------------------------------------------------------------------------
# build_content_blocks()
# ---------------------------------------------------------------------------

class TestBuildContentBlocks:
    def _model_cfg(self, vision=False):
        return ModelConfig(model="test-model", base_url="http://localhost", vision=vision)

    def _att_cfg(self, **overrides):
        defaults = dict(inline_max_files=3, inline_max_bytes=200 * 1024, uploads_dir="uploads")
        defaults.update(overrides)
        return AttachmentConfig(**defaults)

    def test_no_attachments_returns_text(self, tmp_path):
        result = build_content_blocks("hello", (), self._model_cfg(), self._att_cfg(), tmp_path)
        assert result == "hello"

    def test_text_attachment_inlined_as_block(self, tmp_path):
        att = Attachment(filename="note.txt", data=b"body text", mime_type="text/plain")
        result = build_content_blocks("check this", (att,), self._model_cfg(), self._att_cfg(), tmp_path)
        assert isinstance(result, list)
        assert result[0]["type"] == "text"
        assert "check this" in result[0]["text"]
        assert any("body text" in b.get("text", "") for b in result[1:])

    def test_binary_attachment_reference_only(self, tmp_path):
        att = Attachment(filename="blob.bin", data=b"\x00\x01\x02", mime_type="application/octet-stream")
        result = build_content_blocks("here", (att,), self._model_cfg(), self._att_cfg(), tmp_path)
        assert isinstance(result, str)
        assert "blob.bin" in result
        assert "here" in result

    def test_image_without_vision_support_is_reference_only(self, tmp_path):
        att = Attachment(filename="pic.png", data=b"\x89PNG\r\n", mime_type="image/png")
        result = build_content_blocks("look", (att,), self._model_cfg(vision=False), self._att_cfg(), tmp_path)
        assert isinstance(result, str)
        assert "does not support vision" in result

    def test_inline_max_files_threshold_forces_reference(self, tmp_path):
        atts = tuple(
            Attachment(filename=f"f{i}.txt", data=b"x", mime_type="text/plain")
            for i in range(5)
        )
        result = build_content_blocks("many files", atts, self._model_cfg(), self._att_cfg(inline_max_files=2), tmp_path)
        assert isinstance(result, list)
        text_blocks = [b for b in result if b["type"] == "text"]
        # first block is the message text; remaining inlined text blocks capped at 2
        inlined = [b for b in text_blocks if "```" in b.get("text", "")]
        assert len(inlined) == 2
        assert "f4.txt" in text_blocks[0]["text"]  # reference note for the overflow file

    def test_inline_max_bytes_threshold_forces_reference(self, tmp_path):
        big = b"a" * 1000
        atts = (
            Attachment(filename="big1.txt", data=big, mime_type="text/plain"),
            Attachment(filename="big2.txt", data=big, mime_type="text/plain"),
        )
        result = build_content_blocks("size test", atts, self._model_cfg(), self._att_cfg(inline_max_bytes=1000), tmp_path)
        assert isinstance(result, list)
        inlined = [b for b in result if b["type"] == "text" and "```" in b.get("text", "")]
        assert len(inlined) == 1

    def test_all_reference_returns_plain_string(self, tmp_path):
        atts = (
            Attachment(filename="a.bin", data=b"x", mime_type="application/octet-stream"),
            Attachment(filename="b.bin", data=b"y", mime_type="application/octet-stream"),
        )
        result = build_content_blocks("msg", atts, self._model_cfg(), self._att_cfg(), tmp_path)
        assert isinstance(result, str)
        assert "a.bin" in result and "b.bin" in result

    def test_pdf_without_pdfplumber_falls_back_to_reference(self, tmp_path, monkeypatch):
        """extract_pdf_text returns None when pdfplumber is absent — verified
        indirectly by forcing the extractor to return None."""
        import TinyCTX.utils.attachments as attachments_mod
        monkeypatch.setattr(attachments_mod, "extract_pdf_text", lambda data: None)
        att = Attachment(filename="doc.pdf", data=b"%PDF-1.4", mime_type="application/pdf")
        result = build_content_blocks("pdf test", (att,), self._model_cfg(), self._att_cfg(), tmp_path)
        assert isinstance(result, str)
        assert "doc.pdf" in result
