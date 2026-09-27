import tempfile
import unittest
from pathlib import Path

import pymupdf

from engine_worker import estimate_pdf, preflight


def make_pdf(path: str, text: str | None = "Sample paper text for preflight checks. " * 8, encrypt: bool = False, rotation: int = 0) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_textbox(pymupdf.Rect(72, 72, 540, 700), text, fontsize=12)
    if rotation:
        page.set_rotation(rotation)
    if encrypt:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="secret", user_pw="secret")
    else:
        doc.save(path)
    doc.close()


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_pdf_passes(self):
        path = self.dir / "ok.pdf"
        make_pdf(str(path))
        errors, warnings = preflight(str(path), ocr_enabled=False)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_corrupt_pdf_fails(self):
        path = self.dir / "broken.pdf"
        path.write_bytes(b"this is not a pdf at all")
        errors, _ = preflight(str(path), ocr_enabled=False)
        self.assertEqual(len(errors), 1)
        self.assertIn("损坏", errors[0])

    def test_encrypted_pdf_fails(self):
        path = self.dir / "locked.pdf"
        make_pdf(str(path), encrypt=True)
        errors, _ = preflight(str(path), ocr_enabled=False)
        self.assertEqual(len(errors), 1)
        self.assertIn("加密", errors[0])

    def test_missing_text_layer_fails_without_ocr(self):
        path = self.dir / "scan.pdf"
        make_pdf(str(path), text=None)
        errors, _ = preflight(str(path), ocr_enabled=False)
        self.assertEqual(len(errors), 1)
        self.assertIn("文字层", errors[0])

    def test_missing_text_layer_warns_with_ocr(self):
        path = self.dir / "scan.pdf"
        make_pdf(str(path), text=None)
        errors, warnings = preflight(str(path), ocr_enabled=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("OCR", warnings[0])

    def test_rotated_page_warns(self):
        path = self.dir / "rotated.pdf"
        make_pdf(str(path), rotation=90)
        errors, warnings = preflight(str(path), ocr_enabled=False)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("旋转", warnings[0])


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_estimate_reports_pages_chars_tokens(self):
        path = self.dir / "paper.pdf"
        make_pdf(str(path), text="Deep learning transforms natural language processing. " * 20)
        result = estimate_pdf(str(path))
        self.assertEqual(result["pages"], 1)
        self.assertGreater(result["chars"], 400)
        # tiktoken 精确分词（PDF 提取含页间空白,量级一致即可,不等于字符数粗估）
        self.assertGreater(result["estimatedTokens"], 100)
        self.assertLess(result["estimatedTokens"], result["chars"] // 2)
        self.assertEqual(result["tokenizer"], "o200k_base")
        self.assertEqual(result["errors"], [])

    def test_estimate_tokens_fallback_without_special_token_error(self):
        # 文本包含 <|...|> 形式的特殊 token 时不应报错
        path = self.dir / "special.pdf"
        make_pdf(str(path), text="<|endoftext|> special token handling test " * 10)
        result = estimate_pdf(str(path))
        self.assertEqual(result["errors"], [])
        self.assertGreater(result["estimatedTokens"], 0)

    def test_estimate_includes_encryption_error(self):
        path = self.dir / "locked.pdf"
        make_pdf(str(path), encrypt=True)
        result = estimate_pdf(str(path))
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("加密", result["errors"][0])

    def test_estimate_scan_pdf_warns_not_errors(self):
        path = self.dir / "scan.pdf"
        make_pdf(str(path), text=None)
        result = estimate_pdf(str(path))
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["estimatedTokens"], 0)

    def test_estimate_garbage_file_errors(self):
        path = self.dir / "broken.pdf"
        path.write_bytes(b"not a pdf")
        result = estimate_pdf(str(path))
        self.assertEqual(len(result["errors"]), 1)


class CountTokensTest(unittest.TestCase):
    def test_exact_token_count(self):
        from engine_worker import count_tokens
        import tiktoken
        text = "The attention mechanism allows transformers to capture long-range dependencies efficiently."
        count, name = count_tokens(text)
        self.assertEqual(name, "o200k_base")
        self.assertEqual(count, len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=())))

    def test_special_tokens_are_ignored_not_error(self):
        from engine_worker import count_tokens
        count, _ = count_tokens("hello <|endoftext|> world")
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
