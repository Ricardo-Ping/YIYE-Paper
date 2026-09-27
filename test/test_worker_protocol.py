"""worker 通信协议集成测试:以子进程方式调用 engine_worker.py,
验证 stdin JSON 协议(estimate/render/translate 模式)与错误路径,
覆盖服务端与 worker 的真实通信约定。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf

from test_common import PROJECT_ROOT


def run_worker(payload: dict, cwd: Path | None = None):
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "engine_worker.py")],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=str(cwd or PROJECT_ROOT),
        timeout=60,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def estimate_result(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("YIYE_ESTIMATE: "):
            return json.loads(line[len("YIYE_ESTIMATE: "):])
    raise AssertionError(f"stdout 中没有 YIYE_ESTIMATE 行:\n{stdout}")


class WorkerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        src = pymupdf.open()
        page = src.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 540, 400), "Worker protocol integration test. " * 6, fontsize=12)
        src.save(str(self.dir / "sample.pdf"))
        src.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_estimate_mode(self):
        code, out, _ = run_worker({"mode": "estimate", "pdfPath": str(self.dir / "sample.pdf"), "ocr": True})
        self.assertEqual(code, 0)
        result = estimate_result(out)
        self.assertEqual(result["pages"], 1)
        self.assertGreater(result["estimatedTokens"], 0)

    def test_render_mode_writes_png(self):
        out_png = self.dir / "page.png"
        code, _, _ = run_worker({
            "mode": "render",
            "pdfPath": str(self.dir / "sample.pdf"),
            "outPath": str(out_png),
            "page": 0,
        })
        self.assertEqual(code, 0)
        self.assertTrue(out_png.exists() and out_png.stat().st_size > 500)

    def test_translate_mode_without_key_fails(self):
        request_file = self.dir / "request.json"
        request_file.write_text(json.dumps({
            "inputPath": str(self.dir / "sample.pdf"),
            "outputDir": str(self.dir / "out"),
            "config": {"target": "zh-CN", "model": "m", "baseUrl": "http://127.0.0.1:9/v1", "qps": 1, "output": "mono", "ocr": False, "table": False, "glossary": False},
        }), encoding="utf-8")
        code, out, err = run_worker({"mode": "translate", "requestPath": str(request_file)})
        self.assertEqual(code, 2)
        self.assertIn("missing YIYE_API_KEY", err)

    def test_unknown_mode_fails(self):
        code, _, err = run_worker({"mode": "nonsense"})
        self.assertEqual(code, 2)

    def test_estimate_rejects_non_pdf(self):
        target = self.dir / "note.txt"
        target.write_text("plain text", encoding="utf-8")
        code, out, _ = run_worker({"mode": "estimate", "pdfPath": str(target)})
        result = estimate_result(out)
        self.assertTrue(any("pdf" in e.lower() for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
