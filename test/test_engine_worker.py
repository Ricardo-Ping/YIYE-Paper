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

    def test_enhance_keeps_rich_text(self):
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
        self.assertIn("--skip-clean", args)
        self.assertIn("--dual-translate-first", args)
        self.assertNotIn("--enhance-compatibility", args)
        self.assertNotIn("--disable-rich-text-translate", args)

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
            # 表格题注即使关闭表格翻译也要进入段落管线(题注要翻译)
            self.assertTrue(lh.is_text_layout(FakeLayout("table_caption")))
            self.assertTrue(lh.is_text_layout(FakeLayout("table_caption_hybrid")))

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
            unicode: str = "ordinary paragraph"

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

            # 图内文字开启、表格关闭时只过滤表格框内正文；即使版式标签误判为普通正文也不能送翻译。
            patch_layout_translation_scope(translate_figures=True, translate_tables=False)
            table_page = FakePage()
            table_page.page_layout = [FakePageLayout(class_name="table", box=Box(x=100.0, y=300.0, x2=400.0, y2=500.0))]
            table_page.pdf_paragraph = [
                FakePara(layout_label="fallback_line", box=Box(x=150.0, y=350.0, x2=300.0, y2=370.0)),
                FakePara(layout_label="paragraph", box=Box(x=150.0, y=100.0, x2=300.0, y2=140.0)),
                FakePara(layout_label="table_caption", box=Box(x=150.0, y=470.0, x2=300.0, y2=490.0)),
            ]
            it_mod.ILTranslator.process_page(object(), table_page, None)
            self.assertEqual(seen["main"][-1], table_page.pdf_paragraph[1:])
            if llm_cls is not None:
                llm_cls.process_page(object(), table_page, None)
                self.assertEqual(seen["llm"][-1], table_page.pdf_paragraph[1:])

            # 参考文献始终保留英文；正文开头仅有引用、但没有书目信号时不能误伤。
            reference_page = FakePage()
            reference_page.page_layout = []
            reference_page.pdf_paragraph = [
                FakePara("plain text", Box(x=10, y=10, x2=100, y2=20), "[24] J. Furst. Proceedings of EDBT, 2025."),
                FakePara("plain text", Box(x=10, y=30, x2=100, y2=40), "[24] shows that the proposed method is faster."),
            ]
            it_mod.ILTranslator.process_page(object(), reference_page, None)
            self.assertEqual(seen["main"][-1], [reference_page.pdf_paragraph[1]])
            if llm_cls is not None:
                llm_cls.process_page(object(), reference_page, None)
                self.assertEqual(seen["llm"][-1], [reference_page.pdf_paragraph[1]])
        finally:
            patch_layout_translation_scope(translate_figures=True, translate_tables=True)
            it_mod.ILTranslator.process_page = saved_main
            if llm_cls is not None and saved_llm is not None:
                llm_cls.process_page = saved_llm

    def test_replace_caption_block_rewrites_english_caption(self):
        import pymupdf

        from engine_worker import _caption_kind, replace_caption_block

        self.assertEqual(_caption_kind("Table"), "table")
        self.assertEqual(_caption_kind("Fig."), "figure")
        self.assertEqual(_caption_kind("Figure"), "figure")

        doc = pymupdf.open()
        try:
            page = doc.new_page(width=595, height=842)
            page.insert_textbox(
                pymupdf.Rect(60, 700, 300, 740),
                "Table 2: Rewrite quality of the system under different models.",
                fontsize=9,
                fontname="helv",
            )
            self.assertTrue(replace_caption_block(page, "table", "2", "表 2：不同模型规模下系统的重写质量。"))
            text = page.get_text()
            self.assertIn("表 2", text)
            self.assertNotIn("Rewrite quality of the system", text)
            # 不存在的编号返回 False,不做任何改动
            self.assertFalse(replace_caption_block(page, "table", "9", "表 9：不存在。"))
        finally:
            doc.close()

    def test_first_page_author_region_excluded_from_pairs(self):
        """第 1 页作者区(含邮箱)不参与逐段配对,避免恢复的英文作者块错配译文。"""
        import os
        import tempfile

        import pymupdf

        from engine_worker import extract_paragraph_pairs

        def block(page, x, y, text):
            font = "china-s" if any("一" <= c <= "鿿" for c in text) else "helv"
            page.insert_text((x, y), text, fontsize=10, fontname=font)

        with tempfile.TemporaryDirectory() as tmp:
            orig = pymupdf.open()
            page = orig.new_page(width=600, height=500)
            block(page, 60, 60, "Dongjie Xu, foo@suda.edu.cn")
            block(page, 60, 200, "EN abstract body text for reading")
            orig.save(os.path.join(tmp, "input.pdf"))
            orig.close()

            out = pymupdf.open()
            page = out.new_page(width=600, height=500)
            block(page, 360, 60, "董杰徐，foo@suda.edu.cn")
            block(page, 360, 200, "摘要正文内容")
            out.save(os.path.join(tmp, "paper.mono.pdf"))
            out.close()

            pages = extract_paragraph_pairs(os.path.join(tmp, "input.pdf"), tmp, "dual")
            joined = "".join(str(p["en"]) for p in pages[0]["pairs"])
            self.assertNotIn("foo@suda.edu.cn", joined)

    def test_translation_cache_map_filters_prompts_and_json(self):
        """批量/术语提示词与其回显不得进入 原文→译文 映射,否则包含匹配配出无关译文。"""
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch

        from engine_worker import _load_translation_cache_map

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "cache.v1.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE _TranslationCache (original_text TEXT, translation TEXT)")
            conn.execute("INSERT INTO _TranslationCache VALUES (?, ?)", ("Normal paragraph text.", "普通段落译文。"))
            conn.execute(
                "INSERT INTO _TranslationCache VALUES (?, ?)",
                ("You are an expert multilingual terminologist. Extract key terms from the text", '[{"src": "LLM", "tgt": "大语言模型"}]'),
            )
            conn.execute(
                "INSERT INTO _TranslationCache VALUES (?, ?)",
                ("你是翻译引擎。Now translate the following text: SELECT * FROM t WHERE x = 1", "SELECT * FROM t WHERE x = 1"),
            )
            conn.commit()
            conn.close()
            with patch("babeldoc.const.CACHE_FOLDER", tmp):
                mapping = _load_translation_cache_map()
            self.assertEqual(list(mapping.keys()), ["Normalparagraphtext."])

    def test_summary_retries_incomplete_response(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from engine_worker import generate_summary
        def response(text):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
        full = "【一句话总结】结论\n【研究问题】问题\n【方法】方法\n【主要结果】结果\n【局限与展望】局限"
        client = Mock()
        client.chat.completions.create.side_effect = [response("【一句话总结】未完成"), response(full)]
        request = {"outputDir": "unused", "config": {"output": "dual", "model": "local"}}
        with patch("engine_worker.translated_full_text", return_value="可执行性 x² " * 50), patch("engine_worker.build_llm_client", return_value=client):
            self.assertEqual(generate_summary(request, "local")["content"], full)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("可执行性 x²", prompt)
        self.assertNotIn("行", prompt)

    def test_cache_pairing_rejects_other_models_translation(self):
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch
        from engine_worker import _load_translation_cache_map

        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(os.path.join(tmp, "cache.db")) as conn:
                conn.execute("CREATE TABLE _TranslationCache (original_text TEXT, translation TEXT)")
                conn.executemany("INSERT INTO _TranslationCache VALUES (?, ?)", [
                    ("SQL query rewriting", "查询重写更高效。"),
                    ("SQL query rewriting", "深度学习模型改变了自然语言处理。"),
                ])
            conn.close()
            with patch("babeldoc.const.CACHE_FOLDER", tmp):
                mapping = _load_translation_cache_map("查询重写更高效。")
            self.assertEqual(mapping, {"SQLqueryrewriting": "查询重写更高效。"})

    def test_short_block_cache_lookup_requires_exact_match(self):
        """短块(如 Abstract)只允许精确缓存命中,包含匹配会把无关译文配给短标题。"""
        from engine_worker import _build_corpus_index, make_cache_lookup

        mapping = {
            "Theabstractpresentsanoverviewofthemethodandthemainresultsofthepaper": "摘要概述了方法与主要结果",
            "SELECT*FROMtitlet,movieinfoidx": "SELECT * FROM title t",
        }
        corpus, entries = _build_corpus_index(mapping)
        lookup = make_cache_lookup(mapping, corpus, entries)
        # 短块即使能在语料里找到包含命中,也不允许(只认精确)
        self.assertIsNone(lookup("Abstract"))
        self.assertIsNone(lookup("SELECT*FROM"))
        # 精确命中始终有效
        self.assertEqual(lookup("SELECT*FROMtitlet,movieinfoidx"), "SELECT * FROM title t")
        # 长块的包含匹配仍然可用:块片段落在某条缓存原文内
        self.assertEqual(
            lookup("Theabstractpresentsanoverviewofthemethod"),
            "摘要概述了方法与主要结果",
        )

    def test_auto_glossary_cleanup_removes_broken_entries(self):
        """自动术语表清洗:断词伪影/截断/过短条目被移除,正常术语保留。"""
        import threading

        from babeldoc.format.pdf import translation_config as tc_mod
        from babeldoc.glossary import GlossaryEntry

        from engine_worker import patch_auto_glossary_cleanup

        patch_auto_glossary_cleanup()
        ctx = tc_mod.SharedContextCrossSplitPart.__new__(tc_mod.SharedContextCrossSplitPart)
        ctx._lock = threading.Lock()
        ctx.unique_name = "auto"
        ctx.user_glossaries = []
        ctx.raw_extracted_terms = [
            ("GRPO", "组相对策略优化"),
            ("curriculum R L strategy", "课程强化学习策略"),  # 断词伪影
            ("TPC", "TPC"),  # 过短
            ("Two-Stage Training Pipeline", "两阶段训练流水线"),
        ]
        ctx.finalize_auto_extracted_glossary()
        cleaned = ctx.auto_extracted_glossary
        self.assertIsNotNone(cleaned)
        names = {entry.source for entry in cleaned.entries}
        self.assertIn("GRPO", names)
        self.assertIn("Two-Stage Training Pipeline", names)
        self.assertNotIn("curriculum R L strategy", names)
        self.assertNotIn("TPC", names)

    def test_strip_watermarks_removes_confirmed_text_only(self):
        import os
        import tempfile

        import pymupdf

        from engine_worker import detect_watermark_suspects, strip_watermarks

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "paper.pdf")
            doc = pymupdf.open()
            for i in range(5):
                page = doc.new_page(width=400, height=600)
                page.insert_text((60, 100 + i * 10), f"Body paragraph number {i} keeps its content.", fontsize=10)
                page.insert_text((120, 300), "CONFIDENTIAL INTERNAL", fontsize=14, rotate=0)
            doc.save(src)
            doc.close()

            # 原始副本不被改动
            before = pymupdf.open(src)
            original_pages = [page.get_text() for page in before]
            before.close()

            suspects = detect_watermark_suspects(src)
            self.assertTrue(any("CONFIDENTIAL" in s["text"] for s in suspects))
            out = os.path.join(tmp, "clean.pdf")
            removed = strip_watermarks(src, suspects, out)
            self.assertGreaterEqual(removed, 5)

            with pymupdf.open(out) as cleaned, pymupdf.open(src) as original:
                for page in cleaned:
                    self.assertNotIn("CONFIDENTIAL", page.get_text())
                    self.assertIn("Body paragraph", page.get_text())
                for i, page in enumerate(original):
                    self.assertIn("CONFIDENTIAL", page.get_text())
            # 空确认列表时原样复制
            out2 = os.path.join(tmp, "clean2.pdf")
            self.assertEqual(strip_watermarks(src, [], out2), 0)
            with pymupdf.open(out2) as copy:
                self.assertIn("CONFIDENTIAL", copy[0].get_text())

    def test_build_prompt_custom_template(self):
        import os

        from engine_worker import build_prompt

        os.environ["YIYE_PROMPT_TEMPLATE"] = "自定义规则 {variant} 收尾"
        try:
            self.assertEqual(build_prompt("zh-CN"), "自定义规则 简体中文 收尾")
        finally:
            os.environ.pop("YIYE_PROMPT_TEMPLATE", None)
        self.assertIn("简体中文", build_prompt("zh-CN"))


class TranslationIntegrityTest(unittest.TestCase):
    def test_reference_entry_detection_requires_bibliography_evidence(self):
        from types import SimpleNamespace

        from engine_worker import is_protected_reference_paragraph

        self.assertTrue(is_protected_reference_paragraph(SimpleNamespace(layout_label="reference", unicode="No year")))
        self.assertTrue(is_protected_reference_paragraph(SimpleNamespace(layout_label="plain text", unicode="[23] A. Author. Journal paper, 2024.")))
        self.assertFalse(is_protected_reference_paragraph(SimpleNamespace(layout_label="plain text", unicode="[23] shows the method is faster.")))

    def test_author_year_reference_entries_are_protected(self):
        from types import SimpleNamespace

        from engine_worker import is_protected_reference_paragraph

        # AAAI/ACL 无编号格式:"Surname, I.; and Surname, I. 2023. Title. Venue, pages."
        entry = SimpleNamespace(
            layout_label="plain text",
            unicode="Bai, Q.; Alsudais, S.; and Li, C. 2023. QueryBooster: Improving SQL Performance Using Middleware. Proc. VLDB Endow., 16(11): 2911–2924.",
        )
        self.assertTrue(is_protected_reference_paragraph(entry))
        self.assertTrue(
            is_protected_reference_paragraph(
                SimpleNamespace(layout_label="paragraph", unicode="Sun, Z.; Zhou, X.; and Li, G. 2024. R-Bot: An LLM-based Query Rewrite System. CoRR, abs/2412.01661.")
            )
        )
        # 机构条目:"PostgreSQL. 2025. PostgreSQL: The world's most advanced open source database."
        self.assertTrue(
            is_protected_reference_paragraph(
                SimpleNamespace(layout_label="plain text", unicode="PostgreSQL. 2025. PostgreSQL: The World's Most Advanced Open Source Database. Retrieved July 1, 2025 from https://www.postgresql.org.")
            )
        )
        # 富文本标记不应影响判定
        self.assertTrue(
            is_protected_reference_paragraph(
                SimpleNamespace(layout_label="plain text", unicode="<style id='7'>Huo, N.; et al.</style> 2023. Can LLM Be a Database Interface? In Proc. NeurIPS."),
            )
        )
        # 跨栏合并的多条目段落整段保护
        merged = SimpleNamespace(
            layout_label="paragraph",
            unicode="Begoli, E.; Camacho, J. 2021. A middleware for SQL. In Proc. VLDB. Potts, C. 2011. On word vectors. In Lin, D.; eds.",
        )
        self.assertTrue(is_protected_reference_paragraph(merged))
        # 正文段:开头是人名但缺少书目信号时不得误伤
        body = SimpleNamespace(layout_label="plain text", unicode="Kemper, A. and Neumann, T. have argued that hypergraphs remain underexplored in 2019 surveys of the field.")
        self.assertFalse(is_protected_reference_paragraph(body))
        # 普通句式不以作者列表开头
        self.assertFalse(
            is_protected_reference_paragraph(
                SimpleNamespace(layout_label="plain text", unicode="For each query, we conduct five executions in 2023 and compute the average after excluding outliers.")
            )
        )

    def test_nature_and_initial_first_reference_entries_are_protected(self):
        from types import SimpleNamespace

        from engine_worker import is_protected_reference_paragraph

        def para(text):
            return SimpleNamespace(layout_label="plain text", unicode=text)

        # Nature/Science 式:"1. Surname, I. et al. Venue vol, pages (year)."
        self.assertTrue(
            is_protected_reference_paragraph(
                para("1. Bai, Q. et al. Proc. VLDB Endow. 16, 2911–2924 (2023). QueryBooster: Improving SQL Performance.")
            )
        )
        # 名前姓后式:"A. Surname and B. Surname. 2020. Title. Journal, vol(issue): pages."
        self.assertTrue(
            is_protected_reference_paragraph(
                para("A. Bai and C. Li. 2020. A study of query rewriting. Journal of Database Systems, 12(3): 45–67.")
            )
        )
        # 正文编号列表:编号后没有作者特征,不得误伤
        self.assertFalse(
            is_protected_reference_paragraph(
                para("1. In 2023 the team proposed a faster method for query rewriting and validated it on benchmarks.")
            )
        )
        # 普通句式不以作者缩写开头
        self.assertFalse(
            is_protected_reference_paragraph(
                para("A short note about the design was published in 2020 on the project blog.")
            )
        )

    def test_caption_paragraphs_survive_table_exclusion(self):
        from types import SimpleNamespace

        from engine_worker import is_caption_paragraph

        self.assertTrue(is_caption_paragraph(SimpleNamespace(layout_label="table_caption", unicode="Table 1: Latency.")))
        self.assertTrue(is_caption_paragraph(SimpleNamespace(layout_label="fallback_line", unicode="Table 2: Rewrite quality of the system under different models.")))
        self.assertTrue(is_caption_paragraph(SimpleNamespace(layout_label="paragraph", unicode="图 3：等价比与改进次数")))
        self.assertFalse(is_caption_paragraph(SimpleNamespace(layout_label="table_cell", unicode="Table 1 shows the latency results.")))

    def test_reference_page_detector_covers_author_year_style(self):
        from engine_worker import is_reference_page_text

        text = "\n".join(
            [
                "Bai, Q.; Alsudais, S.; and Li, C. 2023. QueryBooster. Proc. VLDB Endow., 16(11): 2911–2924.",
                "Bing, L. 2024. LLM-R2: A Great Rewrite System. In Proceedings of AAAI 2024.",
                "Huo, N.; et al. 2023. Can LLM Be a Database Interface? In NeurIPS.",
            ]
        )
        self.assertTrue(is_reference_page_text(text))
        self.assertFalse(is_reference_page_text("We follow Bai, Q. 2023. and build the system.\nSecond line of body text.\nThird line of body text."))

    def test_typesetting_fidelity_prefers_tight_leading_over_font_shrink(self):
        from babeldoc.format.pdf.document_il.midend import typesetting as ts_mod

        from engine_worker import patch_typesetting_fidelity

        applied: list[float] = []

        class FakeUnit:
            def render(self):
                return [], [], []

        rendered_units = [FakeUnit(), FakeUnit()]

        class FakeBox:
            pass

        class FakeParagraph:
            box = FakeBox()
            scale = None
            pdf_paragraph_composition = None

        class FakePage:
            pdf_curve = []
            pdf_form = []

        class FakeOwner:
            def _layout_typesetting_units(self, units, box, scale, line_skip, paragraph, use_english_line_break=True):
                # 模拟引擎行为:1.25 倍以上行距放不下,更紧凑的行距可以放下
                if line_skip > 1.18:
                    return [], False
                return rendered_units, True

            def expanded_box_down(self, paragraph, page):
                return None  # 无向下空隙

            def _render_side_effect(self):
                applied.append(1)

        fake_original_calls: list[bool] = []

        def fake_original_find(self, paragraph, page, typesetting_units, initial_scale=1.0, use_english_line_break=True, apply_layout=False):
            fake_original_calls.append(apply_layout)
            return 0.9, None

        saved_find = ts_mod.Typesetting._find_optimal_scale_and_layout
        ts_mod.Typesetting._find_optimal_scale_and_layout = fake_original_find
        try:
            patch_typesetting_fidelity()
            patched = ts_mod.Typesetting._find_optimal_scale_and_layout
            self.assertTrue(getattr(patched, "_yiye_fidelity_patch", False))
            owner = FakeOwner()
            paragraph = FakeParagraph()
            page = FakePage()
            # 预计算阶段:原本会缩到 0.9,现返回 1.0
            scale, units = patched(owner, paragraph, page, ["u"], 1.0, True, False)
            self.assertEqual(scale, 1.0)
            self.assertEqual(units, rendered_units)
            # 应用阶段:不落回引擎缩放,且不重复走引擎 apply
            scale, units = patched(owner, paragraph, page, ["u"], 1.0, True, True)
            self.assertEqual(scale, 1.0)
            self.assertEqual(units, rendered_units)
            self.assertEqual(paragraph.scale, 1.0)
            self.assertEqual(paragraph.pdf_paragraph_composition, [])
            # 行距完全放不下时回退引擎行为
            def never_fit(self2, units2, box2, scale2, line_skip2, paragraph2, use_english_line_break=True):
                return [], False

            owner._layout_typesetting_units = never_fit
            scale, units = patched(owner, paragraph, page, ["u"], 1.0, True, True)
            self.assertEqual(scale, 0.9)
            self.assertIsNone(units)
        finally:
            ts_mod.Typesetting._find_optimal_scale_and_layout = saved_find

    def test_missing_reference_placeholders_retry_accepts_translation(self):
        from engine_worker import repair_translation_integrity

        source = "Inefficiencies {v1} include Entity Framework {v2} and Hibernate {v3}."
        translated = "低效问题包括 Entity Framework 和 Hibernate。"
        self.assertIsNone(repair_translation_integrity(source, translated, "plain text", retried=False))
        # 兜底路径:接受译文(富文本跨度可能丢失,但整段回退英文更差)
        self.assertEqual(repair_translation_integrity(source, translated, "plain text", retried=True), translated)
        self.assertIsNone(repair_translation_integrity("Text {v1}", "译文 {v1} {v1}", "plain text", retried=False))
        self.assertIsNone(repair_translation_integrity("Text {v1}", "译文 {v1} {v2}", "plain text", retried=False))

    def test_batch_content_bleed_falls_back_only_the_contaminated_item(self):
        import json

        from engine_worker import repair_batch_translation_bleed

        inputs = [
            {"id": 0, "input": "<style id='1'>System Environment.</style> All queries run on PostgreSQL v14."},
            {"id": 1, "input": "The server has 515 GB RAM and 8 NVIDIA A100 GPUs."},
        ]
        response = [
            {"id": 0, "output": "<style id='1'>系统环境。</style>所有查询均在PostgreSQL v14上执行。服务器配备515 GB RAM和8块NVIDIA A100 GPU。"},
            {"id": 1, "output": "服务器配备515 GB RAM和8块NVIDIA A100 GPU。"},
        ]
        repaired = json.loads(
            repair_batch_translation_bleed(
                "prompt\n## Here is the input:\n" + json.dumps(inputs, ensure_ascii=False),
                json.dumps(response, ensure_ascii=False),
            )
        )
        self.assertEqual(repaired[0]["output"], inputs[0]["input"])
        self.assertEqual(repaired[1]["output"], response[1]["output"])

    def test_short_english_title_retries_and_numbered_list_keeps_line_breaks(self):
        from engine_worker import FORCED_LINE_BREAK, repair_translation_integrity

        title = "Query Rewriting via LLMs"
        self.assertIsNone(repair_translation_integrity(title, title, "title", retried=False))
        numbered = "1.{v1}First item. 2.{v2}Second item. 3.{v3}Third item."
        translated = "1.{v1}第一项。2.{v2}第二项。3.{v3}第三项。"
        repaired = repair_translation_integrity(numbered, translated, "plain text", retried=False)
        self.assertEqual(repaired, f"1.{{v1}}第一项。{FORCED_LINE_BREAK}2.{{v2}}第二项。{FORCED_LINE_BREAK}3.{{v3}}第三项。")

    def test_leading_rich_heading_is_restored_when_model_drops_style_tag(self):
        from engine_worker import repair_translation_integrity

        source = "<style id='1'>System Environment.</style>All queries run on PostgreSQL v14."
        translated = "系统环境。所有查询均在 PostgreSQL v14 中执行。"
        self.assertEqual(
            repair_translation_integrity(source, translated, "plain text", retried=False),
            "<style id='1'>系统环境。</style>所有查询均在 PostgreSQL v14 中执行。",
        )

    def test_author_region_is_copied_without_retypesetting(self):
        import os
        import tempfile

        import pymupdf

        from engine_worker import restore_author_blocks

        with tempfile.TemporaryDirectory() as tmp:
            original = os.path.join(tmp, "original.pdf")
            translated = os.path.join(tmp, "translated.pdf")

            src = pymupdf.open()
            page = src.new_page(width=300, height=400)
            page.insert_text((70, 45), "English Paper Title", fontsize=18, fontname="tiro")
            page.insert_text((80, 85), "Alice Example and Bob Example", fontsize=12, fontname="tiro")
            page.insert_text((75, 110), "University alice@example.com", fontsize=10, fontname="tiit")
            src.save(original)
            src.close()

            out = pymupdf.open()
            page = out.new_page(width=600, height=400)
            page.insert_text((70, 45), "中文论文标题", fontsize=18, fontname="china-s")
            page.insert_text((80, 85), "爱丽丝和鲍勃", fontsize=12, fontname="china-s")
            page.insert_text((75, 110), "某大学 alice@example.com", fontsize=10, fontname="china-s")
            page.show_pdf_page(pymupdf.Rect(300, 0, 600, 400), pymupdf.open(original), 0)
            out.save(translated)
            out.close()

            self.assertEqual(restore_author_blocks(translated, original), 0)
            with pymupdf.open(translated) as doc:
                left = doc[0].get_text("dict", clip=pymupdf.Rect(0, 0, 300, 160))
                spans = [span for block in left["blocks"] if block.get("type") == 0 for line in block.get("lines", []) for span in line.get("spans", [])]
                text = " ".join(span["text"] for span in spans)
                self.assertIn("中文论文标题", text)
                self.assertIn("Alice Example", text)
                self.assertNotIn("爱丽丝", text)
                email = next(span for span in spans if "alice@example.com" in span["text"])
                self.assertIn("Italic", email["font"])

    def test_author_region_without_email_keeps_translated_title(self):
        import os
        import tempfile

        import pymupdf

        from engine_worker import restore_author_blocks

        with tempfile.TemporaryDirectory() as tmp:
            original = os.path.join(tmp, "original.pdf")
            translated = os.path.join(tmp, "translated.pdf")

            src = pymupdf.open()
            page = src.new_page(width=300, height=400)
            page.insert_text((55, 45), "English Paper Title", fontsize=18, fontname="tiro")
            page.insert_text((70, 82), "Dongjie Xu, Yue Cui", fontsize=12, fontname="tiro")
            page.insert_text((64, 102), "Weijie Shi, Qingzhi Ma", fontsize=12, fontname="tiro")
            page.insert_text((58, 126), "1 Soochow University  2 ByteDance Inc.", fontsize=9, fontname="tiro")
            page.insert_text((120, 160), "Abstract", fontsize=10, fontname="tiro")
            src.save(original)
            src.close()

            out = pymupdf.open()
            page = out.new_page(width=600, height=400)
            page.insert_text((55, 45), "中文论文标题", fontsize=18, fontname="china-s")
            page.insert_text((70, 82), "徐东杰、崔越", fontsize=12, fontname="china-s")
            page.insert_text((64, 102), "施卫杰、马清智", fontsize=12, fontname="china-s")
            page.insert_text((58, 126), "1 苏州大学  2 字节跳动", fontsize=9, fontname="china-s")
            page.insert_text((120, 160), "摘要", fontsize=10, fontname="china-s")
            with pymupdf.open(original) as source:
                page.show_pdf_page(pymupdf.Rect(300, 0, 600, 400), source, 0)
            out.save(translated)
            out.close()

            self.assertEqual(restore_author_blocks(translated, original), 0)
            with pymupdf.open(translated) as doc:
                text = doc[0].get_text("text", clip=pymupdf.Rect(0, 0, 300, 180))
                self.assertIn("中文论文标题", text)
                self.assertIn("Dongjie Xu", text)
                self.assertIn("Soochow University", text)
                self.assertNotIn("徐东杰", text)
                self.assertNotIn("苏州大学", text)


class BoxExpansionFidelityTest(unittest.TestCase):
    def test_typesetting_fidelity_expands_box_into_free_space(self):
        """下方有空隙时:扩展段落盒保持原字号,而不是缩小字号。"""
        from babeldoc.format.pdf.document_il.midend import typesetting as ts_mod

        from engine_worker import patch_typesetting_fidelity

        class FakeUnit:
            def render(self):
                return [], [], []

        rendered_units = [FakeUnit()]

        class FakeBox:
            x, y, x2 = 50.0, 700.0, 300.0

            def __init__(self, y2):
                self.y2 = y2

        class FakeParagraph:
            box = FakeBox(720.0)
            scale = None
            pdf_paragraph_composition = None

        class FakePage:
            pdf_curve = []
            pdf_form = []

        class FakeOwner:
            boxes_seen = []

            def _layout_typesetting_units(self, units, box, scale, line_skip, paragraph, use_english_line_break=True):
                self.boxes_seen.append((box.y, box.y2, scale))
                # 原盒(高 20)放不下;向下扩展后(高 40)放得下
                if box.y2 - box.y >= 40:
                    return rendered_units, True
                return [], False

            def get_max_bottom_space(self, box, page):
                # 盒下方还有约 60pt 空隙;扩盒限幅一倍盒高,实际只下移 20pt
                return 640.0

        def fake_original_find(self, paragraph, page, typesetting_units, initial_scale=1.0, use_english_line_break=True, apply_layout=False):
            return 0.8, None

        saved = ts_mod.Typesetting._find_optimal_scale_and_layout
        ts_mod.Typesetting._find_optimal_scale_and_layout = fake_original_find
        try:
            patch_typesetting_fidelity()
            patched = ts_mod.Typesetting._find_optimal_scale_and_layout
            owner = FakeOwner()
            paragraph = FakeParagraph()
            page = FakePage()
            scale, units = patched(owner, paragraph, page, ["u"], 1.0, True, True)
            self.assertEqual(scale, 1.0)
            self.assertEqual(units, rendered_units)
            self.assertEqual(paragraph.scale, 1.0)
            # 盒被向下扩展(PDF y 轴向上,下移即 y 减小,限幅一倍盒高)并回写
            self.assertEqual(paragraph.box.y, 680.0)
            self.assertEqual(paragraph.box.y2, 720.0)
            self.assertEqual(owner.boxes_seen[0], (680.0, 720.0, 1.0))
        finally:
            ts_mod.Typesetting._find_optimal_scale_and_layout = saved


class PlaceholderBatchIsolationTest(unittest.TestCase):
    def test_placeholder_paragraphs_inflate_token_count_for_smaller_batches(self):
        from babeldoc.format.pdf.document_il.midend import il_translator_llm_only as itl

        from engine_worker import patch_placeholder_batch_isolation

        class FakeTokenizer:
            def encode(self, text, disallowed_special=()):
                return [0] * (len(text) // 4)

        patch_placeholder_batch_isolation()
        patched = itl.ILTranslatorLLMOnly.calc_token_count

        class FakeSelf:
            tokenizer = FakeTokenizer()

        fake = FakeSelf()
        plain = patched(fake, "a plain paragraph without placeholders")
        weighted = patched(fake, "text with {v1} and {v2} placeholders")
        # 含占位符的段落计数被放大(触发更小的批次),普通段落不变
        self.assertEqual(plain, len("a plain paragraph without placeholders") // 4)
        self.assertGreaterEqual(weighted, 400)
        self.assertEqual(patched(fake, ""), 0)


class PdfOutlineTest(unittest.TestCase):
    def test_build_pdf_outline_writes_bookmarks_from_headings(self):
        import os
        import tempfile

        import pymupdf

        from engine_worker import build_pdf_outline

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "paper.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=400, height=600)
            page.insert_text((50, 80), "深度翻译研究", fontsize=16, fontname="china-s")
            for j in range(6):
                page.insert_text((50, 150 + j * 16), f"这是第{j+1}段正文内容,字号保持十磅。", fontsize=10, fontname="china-s")
            page2 = doc.new_page(width=400, height=600)
            page2.insert_text((50, 80), "第二章 实验与分析", fontsize=13, fontname="china-s")
            for j in range(6):
                page2.insert_text((50, 150 + j * 16), f"第二章第{j+1}段正文,讨论实验设置与结果。", fontsize=10, fontname="china-s")
            doc.save(path)
            doc.close()

            self.assertEqual(build_pdf_outline(path, "mono"), 0)
            out_doc = pymupdf.open(path)
            toc = out_doc.get_toc()
            out_doc.close()
            titles = [entry[1] for entry in toc]
            self.assertIn("深度翻译研究", titles)
            self.assertIn("第二章 实验与分析", titles)
            levels = {entry[1]: entry[0] for entry in toc}
            self.assertEqual(levels["深度翻译研究"], 1)
            self.assertEqual(levels["第二章 实验与分析"], 2)
            # 正文行不应进入书签
            self.assertFalse(any("正文内容" in t for t in titles))


if __name__ == "__main__":
    unittest.main()
