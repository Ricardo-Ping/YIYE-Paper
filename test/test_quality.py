import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from engine_worker import quality_check, translated_page_text


def make_output_pdf(path: str, pages: list[dict]) -> None:
    """pages: [{"cjk": str|None, "latin": str|None, "placeholder": bool}]"""
    doc = pymupdf.open()
    for spec in pages:
        page = doc.new_page()
        if spec.get("cjk"):
            page.insert_textbox(pymupdf.Rect(72, 72, 540, 360), spec["cjk"], fontsize=12, fontname="china-s")
        if spec.get("latin"):
            page.insert_textbox(pymupdf.Rect(72, 380, 540, 700), spec["latin"], fontsize=12)
        if spec.get("placeholder"):
            page.insert_textbox(pymupdf.Rect(72, 720, 540, 780), "see {v1} and <style id='1'> x", fontsize=12)
    doc.save(path)
    doc.close()


class QualityCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "output"
        self.out.mkdir()
        src = pymupdf.open()
        src.new_page()
        src.new_page()
        src.new_page()
        src.new_page()
        src.save(str(self.dir / "input.pdf"))
        src.close()

    def tearDown(self):
        self.tmp.cleanup()

    def input_path(self):
        return str(self.dir / "input.pdf")

    def test_clean_translation_passes(self):
        make_output_pdf(str(self.out / "out.mono.pdf"), [
            {"cjk": "深度学习改变了自然语言处理。" * 3, "latin": "some refs 2024"},
            {"cjk": "注意力机制允许模型捕捉长距离依赖。" * 3},
            {"cjk": "实验结果证明了方法的有效性。" * 3},
            {"cjk": "结论：本文提出了新的框架。" * 3},
        ])
        report = quality_check(self.input_path(), str(self.out), "mono")
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["issueCount"], 0)

    def test_blank_and_untranslated_and_placeholder_flagged(self):
        long_english = "The transformer architecture processes tokens sequentially. " * 8
        make_output_pdf(str(self.out / "out.mono.pdf"), [
            {"cjk": "正常译文页。" * 10},
            {},  # 空白页
            {"latin": long_english},  # 疑似未翻译
            {"cjk": "带占位符的页面。", "placeholder": True},  # 占位符残留
        ])
        report = quality_check(self.input_path(), str(self.out), "mono")
        self.assertFalse(report["ok"])
        entry = report["outputs"][0]
        flagged = {item["page"]: item["issues"] for item in entry["pages"]}
        self.assertIn("空白页", flagged[2])
        self.assertIn("疑似未翻译", flagged[3])
        self.assertIn("占位符残留", flagged[4])
        self.assertEqual(report["issueCount"], 3)

    def test_page_count_mismatch_detected(self):
        make_output_pdf(str(self.out / "out.mono.pdf"), [
            {"cjk": "只有一页译文。" * 10},
        ])
        report = quality_check(self.input_path(), str(self.out), "mono")
        self.assertFalse(report["ok"])
        self.assertTrue(any("页数" in issue for issue in report["outputs"][0]["issues"]))

    def test_alternate_output_falls_back_to_dual_expectation(self):
        # 交替页面已移除:历史 alternate 配置按双栏预期处理,不应崩溃
        make_output_pdf(str(self.out / "out.dual.pdf"), [
            {"cjk": "第一页译文。" * 10},
            {"cjk": "第二页译文。" * 10},
        ])
        report = quality_check(self.input_path(), str(self.out), "dual")
        self.assertTrue("ok" in report)

    def test_report_file_written(self):
        make_output_pdf(str(self.out / "out.mono.pdf"), [{"cjk": "译文。" * 10}])
        quality_check(self.input_path(), str(self.out), "mono")
        report_file = self.out / "quality-report.json"
        self.assertTrue(report_file.exists())
        data = json.loads(report_file.read_text(encoding="utf-8"))
        self.assertIn("issueCount", data)


def make_dual_pdf(path: str, left: str, right: str) -> None:
    """左右对照页:左半英文原文,右半中文译文。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(36, 72, 250, 400), left, fontsize=11)
    page.insert_textbox(pymupdf.Rect(320, 72, 560, 400), right, fontsize=11, fontname="china-s")
    doc.save(path)
    doc.close()


class GlossaryConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "output"
        self.out.mkdir()
        src = pymupdf.open()
        src.new_page()
        src.save(str(self.dir / "input.pdf"))
        src.close()

    def tearDown(self):
        self.tmp.cleanup()

    def input_path(self):
        return str(self.dir / "input.pdf")

    def write_glossary(self, rows: list[tuple[str, str]]) -> str:
        glossary = self.dir / "glossary.csv"
        lines = ["source,target"] + [f"{s},{t}" for s, t in rows]
        glossary.write_text("\n".join(lines), encoding="utf-8")
        return str(glossary)

    def test_applied_suspect_and_unseen_classification(self):
        make_output_pdf(str(self.out / "out.mono.pdf"), [
            {"cjk": "变换器架构采用注意力机制。" * 5, "latin": "BERT baseline 2024"},
        ])
        glossary = self.write_glossary([
            ("transformer", "变换器"),        # 已应用:译名出现
            ("baseline", "基线"),             # suspect:原词还在(参考文献行),译名没出现
            ("fine-tuning", "微调"),          # unseen:两者都没出现
        ])
        report = quality_check(self.input_path(), str(self.out), "mono", glossary)
        check = report["glossaryCheck"]
        self.assertEqual(check["terms"], 3)
        self.assertEqual(check["applied"], 1)
        self.assertEqual(check["suspect"], 1)
        self.assertEqual(check["unseen"], 1)
        self.assertEqual(check["suspectTerms"], ["baseline"])
        self.assertEqual(check["unseenTerms"], ["fine-tuning"])
        self.assertEqual(check["source"], "custom")

    def test_dual_mode_ignores_left_half(self):
        # 左半是英文原文,右半才是译文;原词只出现在左半,不应被判为 suspect
        make_dual_pdf(str(self.out / "out.dual.pdf"), "transformer model", "变换器模型。")
        report = quality_check(self.input_path(), str(self.out), "dual")
        check = report["glossaryCheck"]
        self.assertIsNone(check)  # 无术语表时返回 None

        glossary = self.write_glossary([("transformer", "变换器模型")])
        report2 = quality_check(self.input_path(), str(self.out), "dual", glossary)
        check2 = report2["glossaryCheck"]
        self.assertEqual(check2["applied"], 1)
        self.assertEqual(check2["suspect"], 0)

    def test_pages_scope_skips_out_of_range_pages(self):
        # 只翻第 1 页时,第 2-4 页保留原文,不应误报疑似未翻译或计入术语统计
        long_english = "Pages kept as original should not raise false positives. " * 8
        make_output_pdf(str(self.out / "out.mono.pdf"), [
            {"cjk": "第一页正常译文。" * 10},
            {"latin": long_english},
            {"latin": long_english},
            {"latin": long_english},
        ])
        glossary = self.write_glossary([("positives", "误报词")])
        report = quality_check(self.input_path(), str(self.out), "mono", glossary, "1")
        entry = report["outputs"][0]
        self.assertEqual(entry["pages"], [], json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["glossaryCheck"]["suspect"], 0)
        self.assertEqual(report["glossaryCheck"]["applied"], 0)

    def test_dual_mode_translated_text_clip(self):
        make_dual_pdf(str(self.out / "out.dual.pdf"), "transformer attention", "注意力机制。")
        with pymupdf.open(str(self.out / "out.dual.pdf")) as doc:
            right_text = translated_page_text(doc, 0, "dual")
            full_text = doc[0].get_text()
        self.assertIn("注意力机制", right_text)
        self.assertNotIn("transformer", right_text)
        self.assertIn("transformer", full_text)

    def test_no_glossary_returns_none(self):
        make_output_pdf(str(self.out / "out.mono.pdf"), [{"cjk": "普通译文。" * 10}])
        report = quality_check(self.input_path(), str(self.out), "mono")
        self.assertIsNone(report["glossaryCheck"])

    def test_nfkc_and_whitespace_normalization(self):
        # 真实 PDF 提取会遇到康熙部首变体（⼒）和断行；统计必须归一化后仍命中
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(72, 72, 540, 400),
            "注意⼒机制。\n深度\n学习模型改变了⾃然语⾔处理。",
            fontsize=12,
            fontname="china-s",
        )
        doc.save(str(self.out / "out.mono.pdf"))
        doc.close()
        glossary = self.write_glossary([
            ("attention mechanism", "注意力机制"),
            ("deep learning", "深度学习"),
            ("natural language processing", "自然语言处理"),
        ])
        report = quality_check(self.input_path(), str(self.out), "mono", glossary)
        check = report["glossaryCheck"]
        self.assertEqual(check["applied"], 3)
        self.assertEqual(check["suspect"], 0)
        self.assertEqual(check["unseen"], 0)


if __name__ == "__main__":
    unittest.main()
