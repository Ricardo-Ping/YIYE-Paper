import unittest

from engine_worker import build_args


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
        self.assertIn("--auto-enable-ocr-workaround", args)
        self.assertIn("--translate-table-text", args)
        self.assertNotIn("--no-dual", args)
        self.assertNotIn("--use-alternating-pages-dual", args)
        self.assertIn("/no_think", args[args.index("--custom-system-prompt") + 1])
        self.assertIn("简体中文", args[args.index("--custom-system-prompt") + 1])
        self.assertIn("--save-auto-extracted-glossary", args)
        self.assertNotIn("--glossary-files", args)
        self.assertNotIn("--enhance-compatibility", args)

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


if __name__ == "__main__":
    unittest.main()
