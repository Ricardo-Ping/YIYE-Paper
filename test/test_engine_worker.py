import unittest

from engine_worker import apply_paragraph_fix, build_args


class EngineArgsTest(unittest.TestCase):
    def test_dual_ollama_job(self):
        request = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "config": {
                "target": "zh-CN",
                "model": "qwen3:8b",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "ocr": True,
                "table": True,
                "glossary": True,
            },
        }
        args = build_args(request, "secret")
        self.assertIn("--no-mono", args)
        self.assertNotIn("--use-alternating-pages-dual", args)  # 默认左右同页
        self.assertIn("--auto-enable-ocr-workaround", args)
        self.assertIn("--translate-table-text", args)
        self.assertNotIn("--no-dual", args)
        self.assertIn("/no_think", args[args.index("--custom-system-prompt") + 1])
        self.assertIn("简体中文", args[args.index("--custom-system-prompt") + 1])
        self.assertIn("--save-auto-extracted-glossary", args)
        self.assertNotIn("--glossary-files", args)
        self.assertNotIn("--enhance-compatibility", args)

    def test_dual_alternating_pages_flag(self):
        request = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "config": {
                "target": "zh-CN",
                "model": "qwen3:8b",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "dualLayout": "alternating",
                "ocr": False,
                "table": False,
                "glossary": False,
            },
        }
        args = build_args(request, "secret")
        self.assertIn("--use-alternating-pages-dual", args)
        self.assertNotIn("--auto-enable-ocr-workaround", args)
        self.assertNotIn("--translate-table-text", args)

    def test_max_pages_per_part_flag(self):
        request = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "config": {
                "target": "zh-CN",
                "model": "qwen3:8b",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "maxPagesPerPart": 50,
                "ocr": False,
                "table": False,
                "glossary": False,
            },
        }
        args = build_args(request, "secret")
        self.assertEqual(args[args.index("--max-pages-per-part") + 1], "50")

        no_part = {**request, "config": {**request["config"]}}
        no_part["config"]["maxPagesPerPart"] = 0
        args = build_args(no_part, "secret")
        self.assertNotIn("--max-pages-per-part", args)

    def test_custom_prompt_appended(self):
        request = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "config": {
                "target": "zh-CN",
                "model": "qwen3:8b",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "ocr": False,
                "table": False,
                "glossary": False,
                "customPrompt": "面向数据库领域读者\t专有名词保留英文",
            },
        }
        args = build_args(request, "secret")
        prompt = args[args.index("--custom-system-prompt") + 1]
        self.assertIn("- 用户额外要求：面向数据库领域读者 专有名词保留英文", prompt)

    def test_prompt_follows_target_language(self):
        base = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "config": {
                "target": "zh-CN",
                "model": "gpt-4o-mini",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "ocr": False,
                "table": False,
                "glossary": False,
            },
        }
        import copy
        tw = copy.deepcopy(base)
        tw["config"]["target"] = "zh-TW"
        args_tw = build_args(tw, "secret")
        prompt = args_tw[args_tw.index("--custom-system-prompt") + 1]
        self.assertIn("繁体中文", prompt)
        self.assertIn("术语表", prompt)
        self.assertIn("不增写、不删减", prompt)
        self.assertNotIn("/no_think", prompt)  # 非 qwen 模型不加
        ja = copy.deepcopy(base)
        ja["config"]["target"] = "ja"  # 已移除的语言应回退简体
        args_ja = build_args(ja, "secret")
        self.assertIn("简体中文", args_ja[args_ja.index("--custom-system-prompt") + 1])

    def test_prompt_version_constant(self):
        from engine_worker import PROMPT_VERSION
        self.assertGreaterEqual(PROMPT_VERSION, 2)

    def test_enhance_flag_passthrough(self):
        request = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "config": {
                "target": "zh-CN",
                "model": "qwen3:8b",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "enhance": True,
                "ocr": False,
                "table": False,
                "glossary": False,
            },
        }
        args = build_args(request, "secret")
        self.assertIn("--enhance-compatibility", args)

    def test_custom_glossary_overrides_auto_extract(self):
        # BabelDOC 在自动抽取与用户术语表并存时只用自动抽取结果，
        # 因此上传自定义术语表时必须关闭自动抽取。
        request = {
            "inputPath": "input.pdf",
            "outputDir": "output",
            "glossaryPath": "glossary.csv",
            "config": {
                "target": "zh-CN",
                "model": "qwen3:8b",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "qps": 4,
                "output": "dual",
                "pages": "1-5,8",
                "ocr": False,
                "table": False,
                "glossary": True,
            },
        }
        args = build_args(request, "secret")
        self.assertEqual(args[args.index("--glossary-files") + 1], "glossary.csv")
        self.assertIn("--no-auto-extract-glossary", args)
        self.assertNotIn("--save-auto-extracted-glossary", args)
        self.assertEqual(args[args.index("--pages") + 1], "1-5,8")


class SummaryMaterialTest(unittest.TestCase):
    def test_translated_full_text_prefers_mono_and_clips_dual(self):
        """速览取材:优先 mono 纯译文;dual 无 mono 时只取右半译文区。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import translated_full_text

        with tempfile.TemporaryDirectory() as tmp:
            dual = pymupdf.open()
            page = dual.new_page(width=400, height=400)
            for y in (60, 80, 100):
                page.insert_text((20, y), "LEFTORIG", fontsize=10)
                page.insert_text((220, y), "RIGHTZH", fontsize=10)
            dual.save(os.path.join(tmp, "paper.dual.pdf"))
            dual.close()

            dual_mode = translated_full_text(tmp, "dual")
            self.assertIn("RIGHTZH", dual_mode)
            self.assertNotIn("LEFTORIG", dual_mode)

            mono = pymupdf.open()
            page = mono.new_page(width=400, height=400)
            page.insert_text((20, 60), "MONOTEXT " * 40, fontsize=10)
            mono.save(os.path.join(tmp, "paper.mono.pdf"))
            mono.close()

            mono_mode = translated_full_text(tmp, "dual")
            self.assertIn("MONOTEXT", mono_mode)
            self.assertNotIn("LEFTORIG", mono_mode)

            empty = translated_full_text(os.path.join(tmp, "nonexist"), "dual")
            self.assertEqual(empty, "")


class CaptionExtractTest(unittest.TestCase):
    def test_extract_captions_pairs_by_number(self):
        """图注配对:Figure N ↔ 图 N：,Table N ↔ 表 N：,按编号对齐。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import extract_captions

        with tempfile.TemporaryDirectory() as tmp:
            orig = pymupdf.open()
            page = orig.new_page(width=500, height=500)
            page.insert_textbox(pymupdf.Rect(40, 40, 460, 120), "Figure 1: pipeline overview of the system", fontsize=10)
            page.insert_textbox(pymupdf.Rect(40, 160, 460, 240), "Table 2: benchmark results summary", fontsize=10)
            orig.save(os.path.join(tmp, "input.pdf"))
            orig.close()

            out = pymupdf.open()
            page = out.new_page(width=500, height=500)
            page.insert_textbox(pymupdf.Rect(40, 40, 460, 120), "图 1：系统流水线总览", fontsize=10, fontname="china-s")
            page.insert_textbox(pymupdf.Rect(40, 160, 460, 240), "表 2：基准测试结果摘要", fontsize=10, fontname="china-s")
            out.save(os.path.join(tmp, "paper.mono.pdf"))
            out.close()

            captions = extract_captions(os.path.join(tmp, "input.pdf"), tmp, "dual")
            by_key = {(c["kind"], c["num"]): c for c in captions}
            self.assertEqual(by_key[("图", 1)]["translated"], "图 1：系统流水线总览")
            self.assertEqual(by_key[("图", 1)]["original"], "Figure 1: pipeline overview of the system")
            self.assertEqual(by_key[("表", 2)]["original"], "Table 2: benchmark results summary")
            self.assertEqual(by_key[("表", 2)]["translated"], "表 2：基准测试结果摘要")
            self.assertEqual(by_key[("图", 1)]["page"], 1)


class ParagraphPairTest(unittest.TestCase):
    def _block(self, page, x, y, text, fontsize=10):
        font = "china-s" if any("一" <= c <= "鿿" for c in text) else "helv"
        page.insert_text((x, y), text, fontsize=fontsize, fontname=font)

    def test_pairs_sorted_by_column_then_y(self):
        """双栏页面:左栏先于右栏,栏内按纵坐标排序;原文译文按序配对。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import extract_paragraph_pairs

        with tempfile.TemporaryDirectory() as tmp:
            # 译文页的中文块集中在右半(dual 对照的译文区),左半留给原文
            orig = pymupdf.open()
            page = orig.new_page(width=600, height=500)
            self._block(page, 60, 60, "EN left top")
            self._block(page, 60, 140, "EN left bottom")
            self._block(page, 360, 60, "EN right top")
            orig.save(os.path.join(tmp, "input.pdf"))
            orig.close()

            out = pymupdf.open()
            page = out.new_page(width=600, height=500)
            self._block(page, 360, 60, "中文右上")
            self._block(page, 360, 140, "中文右中")
            self._block(page, 360, 220, "中文右下")
            out.save(os.path.join(tmp, "paper.mono.pdf"))
            out.close()

            pages = extract_paragraph_pairs(os.path.join(tmp, "input.pdf"), tmp, "dual")
            self.assertEqual(len(pages), 1)
            pairs = pages[0]["pairs"]
            self.assertEqual([p["en"] for p in pairs], ["EN left top", "EN left bottom", "EN right top"])
            self.assertEqual([p["zh"] for p in pairs], ["中文右上", "中文右中", "中文右下"])

    def test_unequal_counts_padded(self):
        """单侧多出的段落保留并以空对照占位,顺序不丢。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import extract_paragraph_pairs

        with tempfile.TemporaryDirectory() as tmp:
            orig = pymupdf.open()
            page = orig.new_page(width=600, height=400)
            self._block(page, 60, 60, "EN one")
            self._block(page, 60, 160, "EN two")
            self._block(page, 60, 260, "EN three")
            orig.save(os.path.join(tmp, "input.pdf"))
            orig.close()

            out = pymupdf.open()
            page = out.new_page(width=600, height=400)
            self._block(page, 60, 60, "中文一")
            out.save(os.path.join(tmp, "paper.mono.pdf"))
            out.close()

            pages = extract_paragraph_pairs(os.path.join(tmp, "input.pdf"), tmp, "dual")
            pairs = pages[0]["pairs"]
            self.assertEqual(len(pairs), 3)
            self.assertEqual(pairs[0], {"en": "EN one", "zh": "中文一"})
            self.assertEqual(pairs[1]["zh"], "")
            self.assertEqual(pairs[2]["en"], "EN three")

    def test_unequal_counts_do_not_shift_cached_pairs(self):
        """插入标题导致块数不等时,不把标题错配给正文,缓存命中的译文也不重复。"""
        import os
        import tempfile
        from unittest.mock import patch

        import pymupdf

        from engine_worker import extract_paragraph_pairs

        with tempfile.TemporaryDirectory() as tmp:
            orig = pymupdf.open()
            page = orig.new_page(width=600, height=400)
            self._block(page, 60, 80, "Original paragraph one")
            self._block(page, 60, 180, "Original paragraph two")
            orig.save(os.path.join(tmp, "input.pdf"))
            orig.close()

            out = pymupdf.open()
            page = out.new_page(width=600, height=400)
            self._block(page, 60, 40, "插入标题")
            self._block(page, 60, 100, "译文一")
            self._block(page, 60, 200, "译文二")
            out.save(os.path.join(tmp, "paper.mono.pdf"))
            out.close()

            with patch("engine_worker._load_translation_cache_map", return_value={"Originalparagraphtwo": "译文二"}):
                pairs = extract_paragraph_pairs(os.path.join(tmp, "input.pdf"), tmp, "mono")[0]["pairs"]

            self.assertEqual(pairs[0], {"en": "Original paragraph one", "zh": ""})
            self.assertEqual(pairs[1], {"en": "Original paragraph two", "zh": "译文二"})
            self.assertEqual(sum(pair["zh"] == "译文二" for pair in pairs), 1)
            self.assertIn({"en": "", "zh": "插入标题"}, pairs)
            self.assertIn({"en": "", "zh": "译文一"}, pairs)


class FigureCropTest(unittest.TestCase):
    def test_caption_region_and_crop_rendered(self):
        """图片在图注上方时:区域取图片框;裁剪 PNG 落盘。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import extract_captions, render_figure_crops

        with tempfile.TemporaryDirectory() as tmp:
            orig = pymupdf.open()
            page = orig.new_page(width=500, height=500)
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80))
            pix.clear_with(180)
            page.insert_image(pymupdf.Rect(60, 40, 300, 160), pixmap=pix)
            page.insert_textbox(pymupdf.Rect(40, 170, 460, 240), "Figure 1: sample chart overview", fontsize=10)
            orig.save(os.path.join(tmp, "input.pdf"))
            orig.close()

            out = pymupdf.open()
            page = out.new_page(width=500, height=500)
            page.insert_textbox(pymupdf.Rect(40, 170, 460, 240), "图 1：样例图表概览", fontsize=10, fontname="china-s")
            out.save(os.path.join(tmp, "paper.mono.pdf"))
            out.close()

            captions = extract_captions(os.path.join(tmp, "input.pdf"), tmp, "dual")
            self.assertEqual(len(captions), 1)
            self.assertIsNotNone(captions[0].get("rect"))
            generated = render_figure_crops(os.path.join(tmp, "input.pdf"), tmp, captions)
            self.assertEqual(generated, 1)
            self.assertTrue(os.path.exists(os.path.join(tmp, "figures", "fig-1.png")))


class ApplyParagraphTest(unittest.TestCase):
    def test_apply_paragraph_replaces_text(self):
        """写回闭环:旧译文被擦除,新译文以 CJK 字体回填同区域。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import apply_paragraph_fix

        with tempfile.TemporaryDirectory() as tmp:
            src = pymupdf.open()
            page = src.new_page(width=500, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 460, 200),
                "旧译文：这是原来的翻译内容，包含数字 123 和术语 LLM。",
                fontsize=10,
                fontname="china-s",
            )
            src.save(os.path.join(tmp, "translated.pdf"))
            src.close()

            out_path = os.path.join(tmp, "adjusted.pdf")
            code = apply_paragraph_fix(
                os.path.join(tmp, "translated.pdf"),
                out_path,
                1,
                "旧译文：这是原来的翻译内容",
                "新译文：这是修正后的内容，更准确。",
            )
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(out_path))

            doc = pymupdf.open(out_path)
            text = doc[0].get_text()
            doc.close()
            self.assertIn("新译文", text)
            self.assertNotIn("这是原来的翻译内容", text)

    def test_apply_missing_paragraph_fails(self):
        import os
        import tempfile

        import pymupdf

        from engine_worker import apply_paragraph_fix

        with tempfile.TemporaryDirectory() as tmp:
            src = pymupdf.open()
            page = src.new_page(width=500, height=300)
            page.insert_textbox(pymupdf.Rect(40, 40, 460, 120), "旧译文内容", fontsize=10, fontname="china-s")
            src.save(os.path.join(tmp, "translated.pdf"))
            src.close()

            code = apply_paragraph_fix(
                os.path.join(tmp, "translated.pdf"),
                os.path.join(tmp, "adjusted.pdf"),
                1,
                "不存在的旧译文",
                "新译文",
            )
            self.assertEqual(code, 3)


class ApplyFragmentTest(unittest.TestCase):
    def test_apply_fragment_inside_block_preserves_context(self):
        """旧译文是块内片段(带句号边界差异)时:只替换片段,保留块内其他句子。"""
        import os
        import tempfile

        import pymupdf

        with tempfile.TemporaryDirectory() as tmp:
            src = pymupdf.open()
            page = src.new_page(width=500, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 460, 200),
                "前一句背景说明。注意力机制使得变换器能够高效地捕捉长距离依赖关系。后一句补充说明。",
                fontsize=10,
                fontname="china-s",
            )
            src.save(os.path.join(tmp, "translated.pdf"))
            src.close()

            out_path = os.path.join(tmp, "adjusted.pdf")
            code = apply_paragraph_fix(
                os.path.join(tmp, "translated.pdf"),
                out_path,
                1,
                "注意力机制使得变换器能够高效地捕捉长距离依赖关系",
                "新的注释译文",
            )
            self.assertEqual(code, 0)

            doc = pymupdf.open(out_path)
            text = doc[0].get_text()
            doc.close()
            self.assertIn("新的注释译文", text)
            self.assertIn("前一句背景说明", text)
            self.assertIn("后一句补充说明", text)
            self.assertNotIn("注意力机制使得", text)


class LayoutScopeTest(unittest.TestCase):
    def test_patch_excludes_figure_and_table(self):
        """按开关收紧翻译范围:图内文字/表格单元格默认不译,开关开启恢复。"""
        import babeldoc.format.pdf.document_il.midend.paragraph_finder as pf
        from babeldoc.format.pdf.document_il.utils import layout_helper as lh

        from engine_worker import patch_layout_translation_scope

        class FakeLayout:
            def __init__(self, name):
                self.name = name

        original = lh.is_text_layout
        try:
            patch_layout_translation_scope(translate_figures=False, translate_tables=False)
            self.assertTrue(lh.is_text_layout(FakeLayout("plain text")))
            self.assertFalse(lh.is_text_layout(FakeLayout("figure_text")))
            self.assertFalse(lh.is_text_layout(FakeLayout("figure_text_hybrid")))
            self.assertFalse(lh.is_text_layout(FakeLayout("table_cell")))
            self.assertTrue(lh.is_text_layout(FakeLayout("paragraph")))

            patch_layout_translation_scope(translate_figures=True, translate_tables=False)
            self.assertTrue(lh.is_text_layout(FakeLayout("figure_text")))
            self.assertFalse(lh.is_text_layout(FakeLayout("table_cell")))

            patch_layout_translation_scope(translate_figures=True, translate_tables=True)
            self.assertTrue(lh.is_text_layout(FakeLayout("table_cell")))
        finally:
            lh.is_text_layout = original
            pf.is_text_layout = original

    def test_process_page_wrappers_geometry_skip(self):
        """段落层包装:图内段落按几何判定摘除、图注保留,调用后放回。

        用真实 IL Box(字段 x/y/x2/y2)构造段落,防止包装代码误用 x0/y0 属性名。
        """
        import dataclasses
        from dataclasses import dataclass

        import babeldoc.format.pdf.document_il.midend.il_translator as it_mod
        from babeldoc.format.pdf.document_il.il_version_1 import Box

        from engine_worker import patch_layout_translation_scope

        @dataclass
        class FakePara:
            layout_label: str
            box: object

        @dataclass
        class FakePageLayout:
            class_name: str
            box: object

        class FakePage:
            page_layout = []
            pdf_paragraph = []

        def make_page():
            page = FakePage()
            page.page_layout = [FakePageLayout(class_name="figure", box=Box(x=100.0, y=600.0, x2=400.0, y2=750.0))]
            page.pdf_paragraph = [
                FakePara(layout_label="paragraph", box=Box(x=150.0, y=650.0, x2=300.0, y2=670.0)),
                FakePara(layout_label="paragraph", box=Box(x=150.0, y=100.0, x2=300.0, y2=140.0)),
                FakePara(layout_label="figure_caption", box=Box(x=150.0, y=655.0, x2=300.0, y2=665.0)),
            ]
            return page

        seen: dict[str, list] = {}

        def make_sentinel(key):
            def sentinel(self, page, executor, *a, **k):
                seen.setdefault(key, []).append(list(page.pdf_paragraph))
                return "done"

            return sentinel

        saved_main = it_mod.ILTranslator.process_page
        it_mod.ILTranslator.process_page = make_sentinel("main")
        llm_cls = None
        saved_llm = None
        try:
            import babeldoc.format.pdf.document_il.midend.il_translator_llm_only as llm_mod

            llm_cls = llm_mod.ILTranslatorLLMOnly
            saved_llm = llm_cls.process_page
            llm_cls.process_page = make_sentinel("llm")
        except Exception:
            llm_cls = None
        try:
            patch_layout_translation_scope(translate_figures=False, translate_tables=True)
            page = make_page()
            result = it_mod.ILTranslator.process_page(object(), page, None)
            self.assertEqual(result, "done")
            # 引擎看到的段落:图内段落被摘除,图注保留
            self.assertEqual(seen["main"][0], page.pdf_paragraph[1:])
            # 调用后放回,数量与顺序不变
            self.assertEqual(len(page.pdf_paragraph), 3)
            self.assertEqual([p.layout_label for p in page.pdf_paragraph], ["paragraph", "paragraph", "figure_caption"])

            if llm_cls is not None:
                self.assertTrue(getattr(llm_cls.process_page, "_yiye_scoped", False))
                page2 = make_page()
                llm_cls.process_page(object(), page2, None)
                self.assertEqual(seen["llm"][0], page2.pdf_paragraph[1:])
                self.assertEqual(len(page2.pdf_paragraph), 3)

            # 图内文字开启、表格关闭时不得继续套用旧几何过滤。
            patch_layout_translation_scope(translate_figures=True, translate_tables=False)
            enabled_page = make_page()
            it_mod.ILTranslator.process_page(object(), enabled_page, None)
            self.assertEqual(seen["main"][-1], enabled_page.pdf_paragraph)
            if llm_cls is not None:
                llm_cls.process_page(object(), enabled_page, None)
                self.assertEqual(seen["llm"][-1], enabled_page.pdf_paragraph)
            patch_layout_translation_scope(translate_figures=True, translate_tables=True)
            self.assertFalse(hasattr(it_mod.ILTranslator.process_page, "_yiye_original"))
            if llm_cls is not None:
                self.assertFalse(hasattr(llm_cls.process_page, "_yiye_original"))
        finally:
            patch_layout_translation_scope(translate_figures=True, translate_tables=True)
            it_mod.ILTranslator.process_page = saved_main
            if llm_cls is not None and saved_llm is not None:
                llm_cls.process_page = saved_llm

    def test_build_prompt_custom_template(self):
        import os

        from engine_worker import build_prompt

        os.environ["YIYE_PROMPT_TEMPLATE"] = "自定义规则 {variant} 收尾"
        try:
            self.assertEqual(build_prompt("zh-CN"), "自定义规则 简体中文 收尾")
        finally:
            os.environ.pop("YIYE_PROMPT_TEMPLATE", None)
        self.assertIn("简体中文", build_prompt("zh-CN"))


if __name__ == "__main__":
    unittest.main()
