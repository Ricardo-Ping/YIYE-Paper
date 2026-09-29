"""BabelDOC worker. The API key enters through the environment and is never written to disk."""

from __future__ import annotations

import csv
import inspect
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import sys
import unicodedata
from datetime import datetime


# 提示词版本：修改提示词内容时递增，便于在文档与问题排查中对应行为变化。
# 注意：BabelDOC 的模板已强制结构规则（tag/占位符/代码不译不改），
# 此处只写内容层面的要求，不与引擎 Rules 重复。
PROMPT_VERSION = 5
PROMPT_TEMPLATE = (
    "你是资深的学术论文翻译引擎，把英文科研论文翻译成{variant}。逐段翻译以下给定的论文内容，只输出该段的译文。\n"
    "- 忠实原文：不增写、不删减、不解释、不总结；原文没有的信息，译文里不能出现。\n"
    "- 学术文风：使用规范书面语和中文标点，符合中文学术论文的表达习惯；避免口语化和翻译腔。\n"
    "- 术语纪律：术语表中给出的词条必须按给定译名翻译，并全文保持一致；"
    "术语表没有的专业术语按学界通用译法处理，没有把握时保留英文原名（首次出现可括注原文）；"
    "GPU、LSTM 这类常见缩写保留英文。\n"
    "- 原样保留：数值、单位、化学式、变量名、数学表达式；"
    "作者姓名、单位、邮箱（邮箱若被断行拆开，须修复为完整形式）；"
    "网址、代码、数据集与代码仓库名；引用编号如 [12] 的位置与内容。\n"
    "- 图表与参考文献：图表内部文字、表格单元格、参考文献条目按惯例保留英文原样，不翻译。\n"
    "- 长句处理：按{variant}习惯重组语序、断句，但不得改变原句的逻辑关系与限定范围。\n"
    "- 输出纪律：只输出译文本身，不要复述本指令、不要添加注释、标题或解释。"
)


def build_prompt(target: str) -> str:
    """组装生效的翻译提示词:用户自定义模板(环境变量传入)优先,否则内置默认。

    模板中的 {variant} 占位符按目标语言替换;用户模板未写占位符时自动追加语言要求。
    """
    variant = "繁体中文" if target == "zh-TW" else "简体中文"
    custom = os.environ.get("YIYE_PROMPT_TEMPLATE", "").strip()
    if custom:
        if "{variant}" in custom:
            return custom.replace("{variant}", variant)
        return f"{custom}\n输出语言：{variant}。"
    return PROMPT_TEMPLATE.format(variant=variant)

PLACEHOLDER_PATTERNS = (re.compile(r"\{v\d+\}"), re.compile(r"<style id="))

# 版式质检阈值(只报告"正文压图"类真实缺陷;图内标签属正常版式不报)
TEXT_IMAGE_OVERLAP = 0.30    # 文本框与图片交集面积 / 文本框面积
OVERLAP_MIN_WIDTH = 100      # 参与判定的文本块最小宽度(pt),过滤图内短标签
OVERLAP_MIN_LINES = 2        # 参与判定的文本块最小行数


def load_glossary_entries(glossary_path: str | None, output_dir: str) -> tuple[list[tuple[str, str]], str | None]:
    """优先加载自定义术语表，否则使用引擎自动导出的 *.glossary.csv。"""
    path = None
    source = None
    if glossary_path and Path(glossary_path).exists():
        path, source = Path(glossary_path), "custom"
    else:
        candidates = sorted(Path(output_dir).glob("*.glossary.csv"))
        if candidates:
            path, source = candidates[0], "auto-extracted"
    if not path:
        return [], None
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return [], None
    entries: list[tuple[str, str]] = []
    for row in csv.DictReader(io.StringIO(text)):
        src = (row.get("source") or "").strip()
        tgt = (row.get("target") or "").strip()
        if src and tgt:
            entries.append((src, tgt))
    return entries, source


def pages_in_scope(page_count: int, pages_spec: str | None) -> set[int]:
    """解析 --pages 语法（如 1-5,8,11-），返回 0-based 的参与翻译页索引。

    BabelDOC 对范围外页面保留原文原页，质检必须把它们排除，
    否则会对"本来就没翻"的页面误报疑似未翻译。
    """
    spec = (pages_spec or "").strip()
    if not spec:
        return set(range(page_count))
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else page_count
            result.update(i - 1 for i in range(max(start, 1), min(end, page_count) + 1))
        elif part.isdigit():
            page = int(part)
            if 1 <= page <= page_count:
                result.add(page - 1)
    return result


def translated_half_clip(page, output_mode: str):
    """dual 对照页自动定位译文所在半页(兼容模式会把译文页排在左侧)。

    按左右半页的中文字符数判断(2 倍差异才算明确);分不清时按引擎默认返回右半。
    非 dual 输出返回 None。
    """
    import pymupdf

    if output_mode != "dual":
        return None
    # get_text() 使用未旋转的 PDF 坐标；page.rect 在 90°/270° 页面会交换宽高，
    # 必须用 cropbox 才能正确切分左右栏。
    rect = page.cropbox
    mid = (rect.x0 + rect.x1) / 2
    left = pymupdf.Rect(rect.x0, rect.y0, mid, rect.y1)
    right = pymupdf.Rect(mid, rect.y0, rect.x1, rect.y1)
    left_cjk = len(re.findall(r"[\u4e00-\u9fff]", page.get_text(clip=left)))
    right_cjk = len(re.findall(r"[\u4e00-\u9fff]", page.get_text(clip=right)))
    if right_cjk >= left_cjk:
        return right
    return left


def translated_page_text(doc, page_index: int, output_mode: str) -> str:
    """提取译文文本。左右对照模式自动识别译文在哪一半，避免把另一半原文算进统计。"""
    import pymupdf

    page = doc[page_index]
    clip = translated_half_clip(page, output_mode)
    if clip is not None:
        return page.get_text(clip=clip)
    return page.get_text()


def build_args(request: dict, api_key: str) -> list[str]:
    config = request["config"]
    gateway_port = os.environ.get("YIYE_PORT", "4173")
    # anthropic/gemini 协议经本地网关转换为 OpenAI 协议，再交给 BabelDOC
    if request["config"].get("gatewayId"):
        gateway_id = request["config"]["gatewayId"]
        upstream = (
            f"http://127.0.0.1:{gateway_port}/api/llm-gateway/{gateway_id}/v1",
            "yiye-gateway",
        )
    else:
        upstream = (config["baseUrl"], api_key)

    thinking = config.get("thinking", "default")
    is_qwen = "qwen" in config["model"].lower()
    is_qwen3 = "qwen3" in config["model"].lower()
    prompt = build_prompt(config["target"])
    # 用户自定义翻译要求追加在内置规则后,空白折叠为单行防注入结构
    custom = re.sub(r"\s+", " ", (config.get("customPrompt") or "")).strip()
    if custom:
        prompt += "\n- 用户额外要求：" + custom
    # 仅 Qwen3 的思考开关走提示词(/no_think,旧版 Ollama 模板只认软开关);
    # qwen2.5 与云端 qwen 没有该开关,注入只会污染提示词。非 qwen 走引擎的 thinking/reasoning 参数
    if is_qwen3 and thinking in ("default", "off"):
        prompt = f"/no_think {prompt}"

    args = [
        "babeldoc",
        "--files", request["inputPath"],
        "--output", request["outputDir"],
        "--lang-in", "en",
        "--lang-out", config["target"],
        "--openai",
        "--openai-model", config["model"],
        "--openai-base-url", upstream[0],
        "--openai-api-key", upstream[1],
        "--qps", str(config["qps"]),
        "--pool-max-workers", str(config["qps"]),
        "--report-interval", "0.5",
        "--watermark-output-mode", "no_watermark",
        "--custom-system-prompt", prompt,
    ]
    if not is_qwen:
        if thinking == "off":
            args += ["--openai-thinking", "disabled"]
        elif thinking in ("low", "medium", "high"):
            args += ["--openai-reasoning", thinking]

    if config["output"] == "mono":
        args.append("--no-dual")
    else:
        args.append("--no-mono")
        if config.get("dualLayout") == "alternating":
            # 交替页对照:奇数页原文、偶数页译文,适合打印后逐页对照
            args.append("--use-alternating-pages-dual")
    if config.get("pages"):
        args += ["--pages", config["pages"]]
    if config.get("maxPagesPerPart"):
        # 长文档分批:引擎按页数拆分逐段翻译后自动合并回单个 PDF,
        # 降低超长论文一次性排版失败/超时的风险
        args += ["--max-pages-per-part", str(int(config["maxPagesPerPart"]))]
    if config.get("enhance"):
        # 等价于 --skip-clean --dual-translate-first --disable-rich-text-translate，
        # 用画质换稳健性，供排版异常的疑难 PDF 使用
        args.append("--enhance-compatibility")
    if config.get("ignoreCache"):
        # 忽略翻译缓存：更换术语表/提示词后强制全部段落重新翻译
        args.append("--ignore-cache")
    if config.get("fontFamily"):
        # 衬线/无衬线/手写体，影响译文 CJK 字体风格
        args += ["--primary-font-family", config["fontFamily"]]
    # BabelDOC 在自动术语抽取和用户术语表同时存在时只使用自动抽取结果，
    # 因此上传了自定义术语表时必须关闭自动抽取，保证用户术语生效。
    if request.get("glossaryPath"):
        args += ["--glossary-files", request["glossaryPath"], "--no-auto-extract-glossary"]
    elif config["glossary"]:
        args.append("--save-auto-extracted-glossary")
    else:
        args.append("--no-auto-extract-glossary")
    if config["ocr"]:
        args.append("--auto-enable-ocr-workaround")
    if config["table"]:
        args.append("--translate-table-text")
    return args


def preflight(pdf_path: str, ocr_enabled: bool) -> tuple[list[str], list[str]]:
    """提交后、引擎启动前的快速检查。返回 (错误, 警告);错误会终止任务。

    覆盖调研清单 P0「预检并明确报错」:损坏、密码加密、无文字层、页面旋转。
    页面旋转只警告不阻断 —— BabelDOC 可以处理旋转页,整篇拦截会误伤横版图表页。
    """
    import pymupdf

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        return [f"预检失败：PDF 损坏或无法解析（{exc}）"], []

    errors: list[str] = []
    warnings: list[str] = []
    with doc:
        if doc.needs_pass:
            return ["预检失败：PDF 已加密，请先解除密码保护再上传"], []
        if doc.page_count == 0:
            return ["预检失败：PDF 没有任何页面"], []

        sampled = min(10, doc.page_count)
        text_chars = sum(len(doc[i].get_text().strip()) for i in range(sampled))
        if text_chars < 100:
            if ocr_enabled:
                warnings.append("预检警告：未检测到有效文字层，将按扫描件 OCR 兼容模式处理，效果可能受限")
            else:
                errors.append("预检失败：未检测到文字层（可能是扫描件）。可在界面开启「扫描页 OCR」后重试")

        rotated = [str(i + 1) for i in range(doc.page_count) if doc[i].rotation % 360 in (90, 270)]
        if rotated:
            warnings.append("预检警告：第 " + "、".join(rotated[:10]) + " 页为横向/旋转页面，请核对译文中该页方向")
    return errors, warnings


def typography_issues(page, clip=None) -> list[str]:
    """版式排版检查:正文大面积覆盖图片时报告 text_image_overlap。

    精度规则(避免把图内标签误报为缺陷):
    - 只检查宽度 ≥ OVERLAP_MIN_WIDTH 且行数 ≥ OVERLAP_MIN_LINES 的正文型块;
      图内短标签(单行、窄块,如流程图节点文字)不参与判定;
    - 交集超过文本块面积 TEXT_IMAGE_OVERLAP 比例才算重叠。
    """
    import pymupdf

    issues: list[str] = []
    clip_rect = clip if clip is not None else page.rect
    overlaps_image = False

    d = page.get_text("dict", clip=clip_rect)
    text_rects: list[tuple[float, float, float, float]] = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        if len(lines) < OVERLAP_MIN_LINES:
            continue
        spans = [s for l in lines for s in l.get("spans", [])]
        if not spans:
            continue
        block_width = max(s["bbox"][2] for s in spans) - min(s["bbox"][0] for s in spans)
        if block_width < OVERLAP_MIN_WIDTH:
            continue
        block_text = "".join(s.get("text", "") for s in spans)
        if len(block_text.strip()) < 20:
            continue
        text_rects.append((block["bbox"][0], block["bbox"][1], block["bbox"][2], block["bbox"][3]))

    image_rects = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    for tx0, ty0, tx1, ty1 in text_rects:
        t_rect = pymupdf.Rect(tx0, ty0, tx1, ty1)
        t_area = t_rect.get_area()
        if t_area <= 0:
            continue
        for img_rect in image_rects:
            inter = t_rect & img_rect
            if not inter.is_empty and inter.get_area() > TEXT_IMAGE_OVERLAP * t_area:
                overlaps_image = True
                break
        if overlaps_image:
            break

    if overlaps_image:
        issues.append("text_image_overlap")
    return issues


def _render_report_html(report: dict) -> str:
    """把质检结果渲染为可直接在浏览器查看的 HTML 报告。"""
    import html as _html

    esc = _html.escape
    ok_badge = ("✓ 全部通过", "color:#18473d") if report["ok"] else (f"✗ {report['issueCount']} 项问题", "color:#c0392b")
    rows = []
    for entry in report.get("outputs", []):
        for issue in entry.get("issues", []):
            rows.append(f"<tr><td>{esc(entry['file'])}</td><td>—</td><td>{esc(issue)}</td></tr>")
        for p in entry.get("pages", []):
            rows.append(f"<tr><td>{esc(entry['file'])}</td><td>{p['page']}</td><td>{esc('、'.join(p['issues']))}</td></tr>")

    glossary = report.get("glossaryCheck")
    glossary_rows = ""
    if glossary:
        g = glossary
        glossary_rows = f"""
        <h2>术语一致性</h2>
        <p>术语库：{esc(g.get('source', ''))} · 共 {g['terms']} 条 ｜ 已应用 {g['applied']} ｜ 疑似未按术语表 {g['suspect']} ｜ 未出现 {g['unseen']}</p>"""
        if g.get("suspectTerms"):
            glossary_rows += f"<p style='color:#c0392b'>疑似未按术语表：{esc('、'.join(g['suspectTerms']))}</p>"
        if g.get("unseenTerms"):
            glossary_rows += f"<p style='color:#888'>未出现：{esc('、'.join(g['unseenTerms']))}</p>"

    watermarks = report.get("watermarkSuspects")
    watermark_rows = ""
    if watermarks:
        watermark_rows = "<h2>疑似水印</h2><p>以下文本在多页相同位置重复出现，可能为水印：</p><ul>"
        for w in watermarks:
            wm_pages = '、'.join(str(p) for p in w['pages'])
            watermark_rows += f"<li>「{esc(w['text'])}」— 第 {wm_pages} 页</li>"
        watermark_rows += "</ul><p style='color:#888'>水印是否需要处理由你判断，本工具不会自动删除。</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>质检报告</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; color: #222; }}
h1 {{ border-bottom: 2px solid #2c3e50; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
th {{ background: #f0f0f0; }}
.ok {{ color: #27ae60; font-weight: bold; }}
.bad {{ color: #c0392b; font-weight: bold; }}
</style></head><body>
<h1>译页 · 质检报告</h1>
<p>生成时间：{esc(report['generatedAt'])} ｜ 原文 {report['inputPages']} 页 ｜ 预期 {report['expectedPages']} 页 ｜ 问题 {report['issueCount']} 项</p>
<h2>文件</h2>
<table><tr><th>文件</th><th>页码</th><th>问题</th></tr>
{''.join(rows) if rows else '<tr><td colspan="3">未发现问题</td></tr>'}
</table>
{glossary_rows}
{watermark_rows}
</body></html>"""


def _has_visual_content(page, clip=None) -> bool:
    """clip 区域内是否存在图片或矢量绘图。clip 为 None 时检查整页。

    空白判定必须限定在检查区域内:整页级的 get_images()/get_drawings()
    会被另一侧(如左半原文)的插图误判为"非空白",漏报译文缺失。
    """
    import pymupdf

    for block in page.get_text("dict", clip=clip).get("blocks", []):
        if block.get("type") == 1:  # 图片块
            return True
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"])
        if clip is None or not (rect & clip).is_empty:
            return True
    return False


def quality_check(input_path: str, output_dir: str, output_mode: str, glossary_path: str | None = None, pages_spec: str | None = None) -> dict:
    """翻译完成后的逐页渲染检查，生成 quality-report.json。

    覆盖调研清单 P0「每页渲染检查」：空白页、译文缺失（疑似未翻译）、
    占位符残留、页数不符，以及术语表应用情况的一致性检查。
    结果只报告不阻断 —— 是否可接受由用户判断。
    """
    import pymupdf

    with pymupdf.open(input_path) as src:
        input_pages = src.page_count
    expected = input_pages
    in_scope = pages_in_scope(input_pages, pages_spec)

    report = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "inputPages": input_pages,
        "expectedPages": expected,
        "outputs": [],
        "issueCount": 0,
    }

    for pdf in sorted(Path(output_dir).glob("*.pdf")):
        entry: dict = {"file": pdf.name, "issues": [], "pages": []}
        with pymupdf.open(pdf) as doc:
            entry["actualPages"] = doc.page_count
            if doc.page_count != expected:
                entry["issues"].append(f"页数 {doc.page_count} 与预期 {expected} 不符")
            for i in range(doc.page_count):
                # 页码范围外的页面保留原文，不做译文检查
                if i not in in_scope:
                    continue
                page_issues: list[str] = []
                page = doc[i]
                # dual 对照自动识别译文在哪一半(兼容模式会把译文页排在左侧)
                clip = translated_half_clip(page, output_mode)
                text = page.get_text(clip=clip)
                if len(text.strip()) < 5 and not _has_visual_content(page, clip):
                    # dual 模式下左半有原文、右半为空，说明这一页没有翻出来
                    page_issues.append("译文缺失" if output_mode == "dual" else "空白页")
                cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
                latin = len(re.findall(r"[A-Za-z]", text))
                if latin >= 300 and cjk == 0:
                    page_issues.append("疑似未翻译")
                if any(pat.search(text) for pat in PLACEHOLDER_PATTERNS):
                    page_issues.append("占位符残留")
                page_issues.extend(typography_issues(page, clip))
                if page_issues:
                    entry["pages"].append({"page": i + 1, "issues": page_issues})
        entry["issueCount"] = len(entry["issues"]) + len(entry["pages"])
        report["outputs"].append(entry)

    report["issueCount"] = sum(item["issueCount"] for item in report["outputs"])
    report["ok"] = report["issueCount"] == 0
    report["glossaryCheck"] = check_glossary_consistency(report, output_dir, output_mode, glossary_path, in_scope)
    report["watermarkSuspects"] = detect_watermark_suspects(input_path)
    report_path = Path(output_dir) / "quality-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = Path(output_dir) / "quality-report.html"
    html_path.write_text(_render_report_html(report), encoding="utf-8")
    summary = {"ok": report["ok"], "issueCount": report["issueCount"], "pages": input_pages, "outputs": len(report["outputs"])}
    if report["glossaryCheck"]:
        glossary = report["glossaryCheck"]
        summary["glossary"] = {"terms": glossary["terms"], "applied": glossary["applied"], "suspect": glossary["suspect"]}
    if report["watermarkSuspects"]:
        summary["watermark"] = True
    print("\nYIYE_QUALITY: " + json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
    return report


def detect_watermark_suspects(input_path: str) -> list[dict]:
    """检测原文 PDF 中的疑似水印：多页同位置出现的重复文本。

    排除页眉页脚区域（页面前 8% 和后 8%）以及纯数字/短标识符。
    页码为 1-based。只报告不删除 —— 水印是否需要处理由用户判断。
    """
    import pymupdf
    from collections import Counter

    try:
        doc = pymupdf.open(input_path)
    except Exception:
        return []
    with doc:
        total = doc.page_count
        if total < 3:
            return []
        span_pages: dict[tuple, set[int]] = {}
        span_info: dict[tuple, dict] = {}
        for i in range(total):
            page = doc[i]
            h = page.rect.height
            w = page.rect.width
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if len(text) < 4:
                            continue
                        bbox = span["bbox"]
                        ry = bbox[1] / h
                        if ry < 0.08 or ry > 0.92:
                            continue
                        key = (squash_text(text)[:50], round(bbox[0] / w, 1), round(ry, 1))
                        span_pages.setdefault(key, set()).add(i)
                        if key not in span_info:
                            span_info[key] = {"text": text[:80], "bbox": [round(v, 1) for v in bbox]}
    if not span_pages:
        return []
    threshold = max(3, int(total * 0.5))
    suspects = []
    for key, pages in span_pages.items():
        if len(pages) >= threshold:
            info = span_info[key]
            suspects.append({
                "text": info["text"],
                "pages": sorted(p + 1 for p in pages),  # 1-based
                "bbox": info["bbox"],
            })
    return suspects


def squash_text(text: str) -> str:
    """PDF 提取文本常带康熙部首变体（⼒ vs 力）和断行空格，统计前必须归一化。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


SUMMARY_PROMPT = (
    "你是学术论文阅读助手。请基于给定的论文中文译文内容，输出中文速览。"
    "严格按照以下格式输出，共 5 行，每行以【】标签开头，每部分 1-3 句，不要输出任何其他内容：\n"
    "【一句话总结】\n【研究问题】\n【方法】\n【主要结果】\n【局限与展望】"
)


def translated_full_text(output_dir: str, output_mode: str, max_chars: int = 20000, page_markers: bool = False) -> str:
    """取译文文本作为素材：优先纯译文 mono PDF；dual 只取右半译文区。

    page_markers 时每页前插入【第 N 页】标记,供任务问答的引用跳转定位。
    """
    import pymupdf

    pdfs = sorted(Path(output_dir).glob("*.pdf"))
    if not pdfs:
        return ""
    mono = next((p for p in pdfs if p.name.lower().endswith(".mono.pdf")), None)
    parts: list[str] = []
    with pymupdf.open(mono or pdfs[0]) as doc:
        for i, page in enumerate(doc):
            if mono is None and output_mode == "dual":
                clip = translated_half_clip(page, output_mode)
                text = page.get_text(clip=clip)
            else:
                text = page.get_text()
            parts.append(f"【第 {i + 1} 页】\n{text}" if page_markers else text)
    text = re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()
    return text[:max_chars]


def build_llm_client(request: dict, api_key: str):
    from openai import OpenAI

    config = request["config"]
    base_url = config["baseUrl"]
    if config.get("gatewayId"):
        # anthropic/gemini 协议经本地网关转换,与翻译请求同一条通路;网关不校验鉴权字段
        base_url = f"http://127.0.0.1:{os.environ.get('YIYE_PORT', '4173')}/api/llm-gateway/{config['gatewayId']}/v1"
    return OpenAI(base_url=base_url, api_key=api_key, timeout=240, max_retries=0)


def generate_summary(request: dict, api_key: str) -> dict | None:
    """翻译完成后用同一模型生成中文速览（对标竞品的「AI 速读」）。

    失败只告警不阻断 —— 速览是增值能力,翻译成果本身不受影响。
    """
    text = translated_full_text(request["outputDir"], request["config"]["output"])
    if len(text) < 200:
        return None
    config = request["config"]
    client = build_llm_client(request, api_key)
    response = client.chat.completions.create(
        model=config["model"],
        temperature=0.2,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"论文译文如下：\n{text}"},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        return None
    return {
        "model": config["model"],
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "content": content[:4000],
    }


def check_glossary_consistency(report: dict, output_dir: str, output_mode: str, glossary_path: str | None, in_scope: set[int]) -> dict | None:
    """统计术语表中每个词条在译文里的应用情况。

    - applied：目标译名出现，视为已应用；
    - suspect：译名未出现、但原词仍出现在译文区域，疑似未按术语表翻译；
      （提示词允许专名词保留英文，因此 suspect 只是提示，不是缺陷判定）
    - unseen：译文区域里原词和译名都没出现，多半是该术语恰好没被翻到。
    """
    import pymupdf

    entries, source = load_glossary_entries(glossary_path, output_dir)
    if not entries or not report["outputs"]:
        return None
    first_pdf = Path(output_dir) / report["outputs"][0]["file"]
    parts: list[str] = []
    with pymupdf.open(first_pdf) as doc:
        for i in range(doc.page_count):
            if i not in in_scope:
                continue
            parts.append(translated_page_text(doc, i, output_mode))
    full_text = squash_text("\n".join(parts))
    applied = 0
    suspect: list[str] = []
    unseen: list[str] = []
    for src, tgt in entries:
        if full_text.count(squash_text(tgt)):
            applied += 1
        elif full_text.count(squash_text(src)):
            suspect.append(src)
        else:
            unseen.append(src)
    return {
        "source": source,
        "terms": len(entries),
        "applied": applied,
        "suspect": len(suspect),
        "unseen": len(unseen),
        "suspectTerms": suspect[:20],
        "unseenTerms": unseen[:20],
    }


CAPTION_ORIGINAL = re.compile(r"^(figure|fig\.?|table)\s*(\d+)\s*[.:：]", re.I)
CAPTION_TRANSLATED = re.compile(r"^(图|表)\s*(\d+)[：:]")


def _page_blocks_text(page, clip=None) -> list[str]:
    blocks = []
    for block in page.get_text("dict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        text = " ".join(span.get("text", "") for line in block.get("lines", []) for span in line.get("spans", [])).strip()
        if text:
            blocks.append(re.sub(r"\s+", " ", text))
    return blocks


def patch_layout_translation_scope(translate_figures: bool, translate_tables: bool) -> None:
    """按开关收紧 BabelDOC 的翻译范围(默认:图内文字与表格单元格保留英文原文)。

    两个拦截层:
    1. is_text_layout 白名单收紧(figure_text/table_cell 等版式);
    2. il_translator.process_page 段落循环按 layout_label 跳过被排除类别的段落
       (图内文字的版式类名不总是 figure_text,几何/标签两种判定互补)。
    开关开启时恢复引擎默认行为。引擎版本升级导致签名变化时静默跳过。
    """
    from babeldoc.format.pdf.document_il.utils import layout_helper as lh
    import babeldoc.format.pdf.document_il.midend.paragraph_finder as paragraph_finder

    excluded: set[str] = set()
    if not translate_figures:
        excluded |= {"figure_text", "figure_text_hybrid", "figure_title", "chart_title"}
    if not translate_tables:
        excluded |= {"table_cell", "table_cell_hybrid", "wired_table_cell", "wireless_table_cell", "table_text", "table_caption", "table_footnote"}
    if not excluded:
        # 全开时恢复引擎原版(此前可能被本函数包装过)
        if getattr(lh.is_text_layout, "_yiye_scoped", False) and getattr(lh, "is_text_layout_original", None):
            lh.is_text_layout = lh.is_text_layout_original
        if getattr(paragraph_finder.is_text_layout, "_yiye_scoped", False) and getattr(paragraph_finder, "is_text_layout_original", None):
            paragraph_finder.is_text_layout = paragraph_finder.is_text_layout_original
        for module_name, class_name in (
            ("babeldoc.format.pdf.document_il.midend.il_translator", "ILTranslator"),
            ("babeldoc.format.pdf.document_il.midend.il_translator_llm_only", "ILTranslatorLLMOnly"),
        ):
            translator = getattr(sys.modules.get(module_name), class_name, None)
            original_page = getattr(getattr(translator, "process_page", None), "_yiye_original", None)
            if original_page:
                translator.process_page = original_page
        return

    original = getattr(lh, "is_text_layout_original", None) or lh.is_text_layout

    def is_text_layout_scoped(layout):
        if not original(layout):
            return False
        name = (getattr(layout, "name", "") or "").strip()
        return name not in excluded and name.replace("_hybrid", "") not in excluded

    is_text_layout_scoped._yiye_scoped = True
    lh.is_text_layout = is_text_layout_scoped
    lh.is_text_layout_original = original
    paragraph_finder.is_text_layout = is_text_layout_scoped
    paragraph_finder.is_text_layout_original = original

    # 第二层:il_translator 的段落循环按 layout_label 跳过被排除类别的段落
    import inspect

    import babeldoc.format.pdf.document_il.midend.il_translator as it

    translator_cls = getattr(it, "ILTranslator", None) or getattr(it, "IlTranslator", None)
    try:
        original_process = getattr(translator_cls.process_page, "_yiye_original", translator_cls.process_page)
        params = list(inspect.signature(original_process).parameters)
        if params[:3] != ["self", "page", "executor"]:
            print("翻译范围补丁(段落层)跳过：引擎签名已变化", file=sys.stderr)
            return

        def process_page_scoped(self, page, executor, *args, **kwargs):
            # 摘除被排除的段落(调用后放回,不影响后续阶段):原版对每个段落无条件调度
            skipped = 0
            fig_boxes = [
                layout.box
                for layout in getattr(page, "page_layout", [])
                if (getattr(layout, "class_name", "") or "") in ("figure", "chart")
                and getattr(layout, "box", None) is not None
            ] if not translate_figures else []
            removed: list[tuple[int, object]] = []
            kept = []
            for idx, paragraph in enumerate(page.pdf_paragraph):
                label = (getattr(paragraph, "layout_label", "") or "").strip()
                skip = label.replace("_hybrid", "") in excluded and label not in ("figure_caption", "table_caption")
                if not skip and label not in ("figure_caption", "table_caption") and fig_boxes and getattr(paragraph, "box", None) is not None:
                    pbox = paragraph.box
                    cx = (pbox.x + pbox.x2) / 2
                    cy = (pbox.y + pbox.y2) / 2
                    for fbox in fig_boxes:
                        if fbox.x <= cx <= fbox.x2 and fbox.y <= cy <= fbox.y2:
                            skip = True
                            break
                if skip:
                    removed.append((idx, paragraph))
                    skipped += 1
                else:
                    kept.append(paragraph)
            if removed:
                page.pdf_paragraph = kept
                print(f"翻译范围：跳过 {len(removed)} 个图内/表格段落", flush=True)
            try:
                return original_process(self, page, executor, *args, **kwargs)
            finally:
                if removed:
                    for idx, paragraph in removed:
                        page.pdf_paragraph.insert(min(idx, len(page.pdf_paragraph)), paragraph)

        process_page_scoped._yiye_original = original_process
        translator_cls.process_page = process_page_scoped
    except Exception as exc:
        print(f"翻译范围补丁(段落层)未生效（{exc}）", file=sys.stderr)

    # 图/图表版式框提取(两个包装共用)
    def fig_boxes_fn(page):
        return [
            layout.box
            for layout in getattr(page, "page_layout", [])
            if (getattr(layout, "class_name", "") or "") in ("figure", "chart")
            and getattr(layout, "box", None) is not None
        ]

    # LLM 引擎实际走 ILTranslatorLLMOnly(独立实现),同样包装
    try:
        llm_cls = getattr(it, "ILTranslatorLLMOnly", None)
        if llm_cls is None:
            import babeldoc.format.pdf.document_il.midend.il_translator_llm_only as itl

            llm_cls = itl.ILTranslatorLLMOnly
        original_llm_process = getattr(llm_cls.process_page, "_yiye_original", llm_cls.process_page)
        llm_params = list(inspect.signature(original_llm_process).parameters)
        if llm_params[:3] != ["self", "page", "executor"]:
            print("翻译范围补丁(LLM 段落层)跳过：引擎签名已变化", file=sys.stderr)
            return

        def llm_process_page_scoped(self, page, executor, *args, **kwargs):
            removed: list[tuple[int, object]] = []
            kept = []
            fboxes = fig_boxes_fn(page) if not translate_figures else []
            for idx, paragraph in enumerate(page.pdf_paragraph):
                label = (getattr(paragraph, "layout_label", "") or "").strip()
                skip = label.replace("_hybrid", "") in excluded and label not in ("figure_caption", "table_caption")
                if not skip and label not in ("figure_caption", "table_caption") and fboxes and getattr(paragraph, "box", None) is not None:
                    cx = (paragraph.box.x + paragraph.box.x2) / 2
                    cy = (paragraph.box.y + paragraph.box.y2) / 2
                    for fbox in fboxes:
                        if fbox.x <= cx <= fbox.x2 and fbox.y <= cy <= fbox.y2:
                            skip = True
                            break
                if skip:
                    removed.append((idx, paragraph))
                else:
                    kept.append(paragraph)
            if removed:
                page.pdf_paragraph = kept
                print(f"翻译范围：跳过 {len(removed)} 个图内/表格段落", flush=True)
            try:
                return original_llm_process(self, page, executor, *args, **kwargs)
            finally:
                if removed:
                    for idx, paragraph in removed:
                        page.pdf_paragraph.insert(min(idx, len(page.pdf_paragraph)), paragraph)

        llm_process_page_scoped._yiye_scoped = True
        llm_process_page_scoped._yiye_original = original_llm_process
        llm_cls.process_page = llm_process_page_scoped
    except Exception as exc:
        print(f"翻译范围补丁(LLM 段落层)未生效（{exc}）", file=sys.stderr)


def restore_author_blocks(translated_pdf: str, original_pdf: str) -> int:
    """把第 1 页作者区恢复为英文原文(姓名/单位/邮箱),对标中译本保留原作者信息的惯例。

    背景:作者区块是"整块都是人名+邮箱"的短文本,小模型经常无视提示词规则
    执意音译姓名并把邮箱断行,提示词管不住;此处用确定性代码擦除音译结果、
    原样回填英文作者信息(按作者条目压缩为"名 · 单位 · 邮箱"单行,统一不同论文格式)。
    定位:原文与译文第 1 页顶部 45% 内含邮箱(@)的文本块簇,区域取并集。
    未找到时返回 1(如纯扫描页),不影响任务。
    """
    import pymupdf

    def author_clusters(page):
        top_limit = page.rect.height * 0.45
        found = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            text = " ".join(s.get("text", "") for l in block.get("lines", []) for s in l.get("spans", [])).strip()
            rect = pymupdf.Rect(block["bbox"])
            if "@" in text and rect.y1 <= top_limit:
                found.append((rect, text))
        found.sort(key=lambda item: (round(item[0].x0), item[0].y0))
        return found

    with pymupdf.open(original_pdf) as odoc:
        if odoc.page_count == 0:
            return 1
        opage = odoc[0]
        o_blocks = author_clusters(opage)
        if not o_blocks:
            return 1
        region = o_blocks[0][0]
        for rect, _ in o_blocks[1:]:
            region |= rect
        region = pymupdf.Rect(max(region.x0 - 6, 0), max(region.y0 - 6, 0), region.x1 + 6, region.y1 + 6)
        # 原文作者区各行(按栏位+纵坐标排序)与字号,原样回填
        lines: list[tuple[tuple, str]] = []
        sizes: list[float] = []
        for block in opage.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            rect_b = pymupdf.Rect(block["bbox"])
            if not rect_b.intersects(region):
                continue
            for l in block.get("lines", []):
                line_text = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
                if line_text:
                    lines.append(((round(l["bbox"][0]), round(l["bbox"][1])), line_text))
                    sizes.extend(s.get("size", 9.0) for s in l.get("spans", []))
    lines.sort(key=lambda item: (item[0][1], item[0][0]))
    block_text = "\n".join(t for _, t in lines)

    doc = pymupdf.open(translated_pdf)
    tpage = doc[0]
    # dual 对照页两侧都有内容:只处理译文所在半侧,原文侧的英文作者区保持原样
    half = translated_half_clip(tpage, "dual")
    t_blocks = []
    for block in tpage.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        text = " ".join(s.get("text", "") for l in block.get("lines", []) for s in l.get("spans", [])).strip()
        rect = pymupdf.Rect(block["bbox"])
        if "@" in text and rect.y0 <= tpage.rect.height * 0.45 and (half is None or rect.intersects(half)):
            t_blocks.append((rect, text))
    if not t_blocks:
        doc.close()
        return 1
    # dual 页两侧分别是原文与译文:作者区只处理译文侧(按 @ 块的中文字符数判断)
    mid = tpage.rect.width / 2
    by_side = {0: [], 1: []}
    for rect, text in t_blocks:
        by_side[0 if (rect.x0 + rect.x1) / 2 < mid else 1].append((rect, text))
    t_blocks = max(by_side.values(), key=lambda items: sum(sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff") for _, text in items))
    t_region = t_blocks[0][0]
    for rect, _ in t_blocks[1:]:
        t_region |= rect
    t_region = pymupdf.Rect(max(t_region.x0 - 6, 0), max(t_region.y0 - 6, 0), t_region.x1 + 6, min(t_region.y1 + 6, tpage.rect.height))

    tpage.add_redact_annot(t_region)
    tpage.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
    font_size = max(6.0, min(sizes)) if sizes else 9.0
    inserted = False
    while font_size >= 5.0:
        rc = tpage.insert_textbox(t_region, block_text, fontname="helv", fontsize=font_size, align=0)
        if rc >= 0:
            inserted = True
            break
        font_size -= 0.5
    if not inserted:
        doc.close()
        print("YIYE_AUTHOR: 回填放不下，放弃本次作者区恢复", file=sys.stderr)
        return 1
    tmp_path = str(translated_pdf) + ".author-restore.tmp"
    doc.save(tmp_path)
    doc.close()
    os.replace(tmp_path, translated_pdf)
    print(f"YIYE_AUTHOR: 已恢复第 1 页作者区英文原文({len(lines)} 行)", flush=True)
    return 0


def extract_captions(input_path: str, output_dir: str, output_mode: str) -> list[dict]:
    """提取图表图注并按编号配对(原文图注 ↔ 译文图注),供图表速读。

    图注以 Figure N/Fig. N/Table N(原文)与 图 N：/表 N：(译文)开头,编号即配对键;
    配不上的编号单侧保留。纯文本对照,不改动原图。
    同时为每条图注定位其图表区域(rect),供裁剪缩略图使用:
    图片类取正上方水平重叠的图片框合并;表格类取图注上方固定高度区域。
    """
    import pymupdf

    originals: dict[tuple[bool, int], dict] = {}
    with pymupdf.open(input_path) as doc:
        for p, page in enumerate(doc):
            image_rects = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                text = " ".join(span.get("text", "") for line in block.get("lines", []) for span in line.get("spans", [])).strip()
                text = re.sub(r"\s+", " ", text)
                if not text:
                    continue
                m = CAPTION_ORIGINAL.match(text)
                if not m:
                    continue
                key = (m.group(1).lower().startswith("fig"), int(m.group(2)))
                bbox = pymupdf.Rect(block["bbox"])
                entry = originals.setdefault(key, {"page": p + 1, "original": text[:300], "rect": None})
                region = _figure_region(page, image_rects, bbox, key[0])
                if region and entry.get("rect") is None:
                    entry["rect"] = region

    translated: dict[tuple[bool, int], dict] = {}
    pdfs = sorted(Path(output_dir).glob("*.pdf"))
    mono = next((p for p in pdfs if p.name.lower().endswith(".mono.pdf")), None)
    if pdfs:
        with pymupdf.open(mono or pdfs[0]) as doc:
            for p, page in enumerate(doc):
                clip = translated_half_clip(page, output_mode)
                for text in _page_blocks_text(page, clip):
                    m = CAPTION_TRANSLATED.match(text)
                    if m:
                        key = (m.group(1) == "图", int(m.group(2)))
                        translated.setdefault(key, {"page": p + 1, "translated": text[:300]})

    results = []
    for key in sorted(set(originals) | set(translated), key=lambda k: (not k[0], k[1])):
        is_figure, num = key
        o = originals.get(key) or {}
        t = translated.get(key) or {}
        if not o and not t:
            continue
        results.append({
            "kind": "图" if is_figure else "表",
            "num": num,
            "page": o.get("page") or t.get("page"),
            "original": o.get("original", ""),
            "translated": t.get("translated", ""),
            "rect": o.get("rect"),
        })
    return results


def _looks_like_json(text: str) -> bool:
    """形似 JSON 的判定:以 [ 或 { 开头且紧随其后是引号/花括号/换行。
    参考文献条目(如 "[1] Param Aggarwal. 2019.")虽以 [ 开头但不是 JSON。"""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "[{":
        return False
    body = stripped[1:].lstrip()
    if not body:
        return False
    if stripped[0] == "[":
        return body[0] in "{\"["  # [1] 这种引用编号不是 JSON
    return body[0] in "\"}" or body.startswith("\n")


def _load_translation_cache_map() -> dict[str, str]:
    """从 BabelDOC 全局翻译缓存(SQLite)读取 原文→译文 映射。

    键为去除全部空白后的原文,用于把位置配对校正为语义配对。
    读取失败或库不存在时返回空表,不影响逐段对照。
    """
    import glob
    import sqlite3

    from babeldoc.const import CACHE_FOLDER

    mapping: dict[str, str] = {}
    for db_file in glob.glob(str(Path(CACHE_FOLDER) / "*.db")):
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            try:
                for original, translation in conn.execute("SELECT original_text, translation FROM _TranslationCache"):
                    if not original or not translation:
                        continue
                    # 过滤非段落行:术语抽取 JSON、mock/JSON 模式的结构化响应都会进缓存,
                    # 混进语料库会让包含匹配吐出 JSON
                    if _looks_like_json(original) or _looks_like_json(translation):
                        continue
                    mapping[re.sub(r"\s+", "", original)] = re.sub(r"[ \t]+", " ", translation).strip()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return mapping


def _build_corpus_index(mapping: dict[str, str]) -> tuple[str, list[tuple[int, str, str]]]:
    """把全部缓存原文拼成语料库并记录每条的起始偏移,支持子串包含查找。"""
    parts: list[str] = []
    entries: list[tuple[int, str, str]] = []
    pos = 0
    for orig, trans in mapping.items():
        entries.append((pos, orig, trans))
        parts.append(orig)
        pos += len(orig) + 1
    return "\n".join(parts), entries


def _cache_match(corpus: str, entries: list[tuple[int, str, str]], key: str) -> str | None:
    """块文本 ↔ 缓存匹配:精确 → 包含(块片段落在某条缓存原文内,支持跨条拼接)。"""
    import bisect

    if len(key) < 8:
        return None
    starts = [e[0] for e in entries]
    remaining = key
    translations: list[str] = []
    pos = 0  # remaining 里的消费位置
    for _ in range(3):
        found = corpus.find(remaining[pos:])
        if found < 0:
            return "\n".join(translations) if translations else None
        idx = bisect.bisect_right(starts, found) - 1
        if idx < 0:
            return None
        _, _, trans = entries[idx]
        translations.append(trans)
        consumed = len(remaining) - pos
        orig_len = len(entries[idx][1]) - (found - entries[idx][0])
        if consumed <= orig_len:
            break
        pos += orig_len + 1  # 跳过条目与其后的分隔符
    return "\n".join(translations) if translations else None


def extract_paragraph_pairs(input_path: str, output_dir: str, output_mode: str, max_pages: int = 200) -> list[dict]:
    """逐页配对原文段落与译文段落,生成逐段对照阅读数据(对标 PDF Pro 的段落对照)。

    配对策略:同页内两侧块各按"栏位+纵坐标"排序。块数相等时按位置配对并用
    BabelDOC 缓存校正;块数不等但存在缓存命中时只保留确定配对,其余以空对照
    占位,避免图注/跨栏标题插入后把整页后续段落错配。
    """
    import pymupdf

    def blocks(page, clip=None, single_column=False):
        rect = page.rect
        mid = rect.width / 2
        out = []
        for b in page.get_text("dict", clip=clip).get("blocks", []):
            if b.get("type") != 0:
                continue
            text = " ".join(s.get("text", "") for l in b.get("lines", []) for s in l.get("spans", [])).strip()
            if len(text) < 2:
                continue
            x0, y0 = b["bbox"][0], b["bbox"][1]
            column = 1 if (single_column or (x0 + b["bbox"][2]) / 2 >= mid) else 0
            out.append((column, round(y0, 1), round(x0, 1), re.sub(r"\s+", " ", text)))
        out.sort()
        return [t for _, _, _, t in out]

    pages = []
    pdfs = sorted(Path(output_dir).glob("*.pdf"))
    mono = next((p for p in pdfs if p.name.lower().endswith(".mono.pdf")), None)
    if not pdfs:
        return pages
    # 翻译缓存是"原文段落 → 译文段落"的精确对照,用它校正位置配对的错位
    cache_map = _load_translation_cache_map()
    corpus, entries = _build_corpus_index(cache_map)
    with pymupdf.open(input_path) as src, pymupdf.open(mono or pdfs[0]) as dst:
        count = min(src.page_count, dst.page_count, max_pages)
        for i in range(count):
            en = blocks(src[i])
            clip = translated_half_clip(dst[i], output_mode)
            single_column = clip is not None
            zh = blocks(dst[i], clip, single_column)
            pairs = []
            cached_translations = [
                _cache_match(corpus, entries, re.sub(r"\s+", "", en_text))
                for en_text in en
            ]
            conservative = len(en) != len(zh) and any(cached_translations)
            used_zh: set[int] = set()
            pair_count = len(en) if conservative else max(len(en), len(zh))
            for j in range(pair_count):
                en_text = en[j] if j < len(en) else ""
                cached = cached_translations[j] if j < len(cached_translations) else None
                zh_text = "" if conservative else (zh[j] if j < len(zh) else "")
                if not conservative and j < len(zh):
                    used_zh.add(j)
                # 语义校正:该原文块在缓存里有对应译文时,以缓存为准
                if cached:
                    zh_text = cached
                    cached_key = re.sub(r"\s+", "", cached)
                    match = next((
                        k for k, text in enumerate(zh)
                        if k not in used_zh and re.sub(r"\s+", "", text) == cached_key
                    ), None)
                    if match is not None:
                        used_zh.add(match)
                if zh_text and _looks_like_json(zh_text):
                    zh_text = ""  # 历史污染的 JSON 译文不作为对照内容展示
                pairs.append({"en": en_text, "zh": zh_text})
            if conservative:
                pairs.extend(
                    {"en": "", "zh": text}
                    for k, text in enumerate(zh)
                    if k not in used_zh and not _looks_like_json(text)
                )
            # 相邻重复(同一段落被拆成多块,缓存译文相同)合并为一条
            merged: list[dict] = []
            for pair in pairs:
                if merged and merged[-1] == pair:
                    continue
                merged.append(pair)
            if merged:
                # 该页是否真的被翻译过:页码范围外的页面保留原文,两侧内容一致
                translated = any(
                    pair["en"] and pair["zh"] and re.sub(r"\s+", "", pair["en"]) != re.sub(r"\s+", "", pair["zh"])
                    for pair in merged
                )
                pages.append({"page": i + 1, "pairs": merged, "translated": translated})
    return pages


def _figure_region(page, image_rects: list, caption_bbox, is_figure: bool):
    """按图注块反推图表区域:自底向上贪心聚类图注上方的图片框与矢量绘图。

    停止条件:垂直间隙 ≥ 14pt(不与图表相连的页眉线/上方段落不会吸入)。
    表格由文字构成,贪心聚类命中率低,不生成裁剪(宁缺毋滥)。
    """
    import pymupdf

    cap = pymupdf.Rect(caption_bbox)
    candidates = list(image_rects)
    for d in page.get_drawings():
        rect = pymupdf.Rect(d["rect"])
        if rect.width > 3 and rect.height > 3 and rect.y1 <= cap.y0 + 8:
            candidates.append(rect)
    candidates.sort(key=lambda r: -r.y1)
    candidates = [r for r in candidates if min(r.x1, cap.x1) - max(r.x0, cap.x0) > 0.25 * max(cap.width, 1)]
    region = None
    for rect in candidates:
        if region is None:
            region = pymupdf.Rect(rect)
            continue
        gap = region.y0 - rect.y1
        h_overlap = min(region.x1, rect.x1) - max(region.x0, rect.x0)
        if gap < 14 and h_overlap > 0.2 * max(rect.width, 1):
            region |= rect
    if region is None:
        return None
    region = pymupdf.Rect(max(region.x0 - 4, 0), max(region.y0 - 4, 0), min(region.x1 + 4, page.rect.width), min(region.y1 + 4, cap.y0))
    if region.get_area() <= 0 or region.height < 40 or region.width < 60:
        return None
    return [round(v, 1) for v in (region.x0, region.y0, region.x1, region.y1)]


def render_figure_crops(input_path: str, output_dir: str, captions: list[dict], dpi: int = 100) -> int:
    """按 figures.json 中的区域字段渲染图表缩略图到 output/figures/。返回生成数量。"""
    import pymupdf

    needed = [c for c in captions if c.get("rect") and c.get("page")]
    if not needed:
        return 0
    crop_dir = Path(output_dir) / "figures"
    crop_dir.mkdir(parents=True, exist_ok=True)
    by_page: dict[int, list[dict]] = {}
    for c in needed:
        by_page.setdefault(c["page"], []).append(c)
    generated = 0
    with pymupdf.open(input_path) as doc:
        for page_num, items in by_page.items():
            if page_num < 1 or page_num > doc.page_count:
                continue
            page = doc[page_num - 1]
            for c in items:
                x0, y0, x1, y1 = c["rect"]
                pix = page.get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1), dpi=dpi)
                name = f"fig-{c['num']}.png"
                pix.save(str(crop_dir / name))
                c["crop"] = f"figures/{name}"
                generated += 1
    return generated


def count_tokens(text: str) -> tuple[int, str]:
    """用 tiktoken 精确统计 token 数。o200k_base 是 GPT-4o/5 系现行编码，
    对其他模型（Qwen/DeepSeek/Claude 等）也是密度最接近的通用估算基准。"""
    try:
        import tiktoken

        try:
            encoding = tiktoken.get_encoding("o200k_base")
            name = "o200k_base"
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")
            name = "cl100k_base"
        return len(encoding.encode(text, disallowed_special=())), name
    except Exception:
        # tiktoken 不可用时退回字符数粗估（英文约 4 字符 = 1 token）
        return -(-len(text) // 4), "chars/4"


def estimate_pdf(pdf_path: str, ocr_enabled: bool = True) -> dict:
    """提交前的预估：页数、字符数、粗略 token 数，附带走一遍预检。

    token 数用 tiktoken 对提取文本精确分词（o200k_base）。
    该值为「文档内容输入」的准确量级；实际全程用量还会加上每段的
    提示词与术语块开销，以及中文译文的输出部分（约为输入的 1.2–1.6 倍）。
    不同厂商 tokenizer 有差异，误差约 ±10%。
    """
    import pymupdf

    if Path(pdf_path).resolve().suffix.lower() != ".pdf":
        return {"pages": 0, "chars": 0, "estimatedTokens": 0, "errors": ["仅支持 .pdf 文件"], "warnings": []}
    if not _is_under(Path(pdf_path).resolve(), _allowed_roots()):
        return {"pages": 0, "chars": 0, "estimatedTokens": 0, "errors": ["路径超出允许范围"], "warnings": []}
    result = {"pages": 0, "chars": 0, "estimatedTokens": 0, "errors": [], "warnings": []}
    errors, warnings = preflight(pdf_path, ocr_enabled)
    result["errors"] = errors
    result["warnings"] = warnings
    if errors:
        return result
    try:
        with pymupdf.open(pdf_path) as doc:
            result["pages"] = doc.page_count
            full_text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
            result["chars"] = len(full_text)
    except Exception as exc:
        result["errors"] = [f"PDF 损坏或无法解析（{exc}）"]
        return result
    result["estimatedTokens"], result["tokenizer"] = count_tokens(full_text)
    return result


def _allowed_roots() -> list[Path]:
    """worker 只允许访问这些根目录下的文件:工作目录、数据目录(YIYE_DATA_DIR)、系统临时目录。"""
    roots = [Path.cwd().resolve()]
    env_dir = os.environ.get("YIYE_DATA_DIR")
    if env_dir:
        roots.append(Path(env_dir).resolve())
        roots.append((Path.cwd() / env_dir).resolve())
    import tempfile

    roots.append(Path(tempfile.gettempdir()).resolve())
    return roots


def _is_under(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def render_page(pdf_path: str, out_path: str, page_index: int) -> int:
    """把 PDF 指定页渲染为 PNG,供预览面板显示真实页面。"""
    import pymupdf

    src = Path(pdf_path).resolve()
    dst = Path(out_path).resolve()
    allowed = _allowed_roots()
    if not (_is_under(src, allowed) and _is_under(dst, allowed)):
        print("render-page: 路径超出允许范围", file=sys.stderr)
        return 2
    if src.suffix.lower() != ".pdf" or dst.suffix.lower() != ".png":
        print("render-page: 仅支持 .pdf 输入与 .png 输出", file=sys.stderr)
        return 2
    with pymupdf.open(src) as doc:
        if doc.needs_pass or doc.page_count <= page_index:
            return 3
        pix = doc[page_index].get_pixmap(dpi=110)
        pix.save(str(dst))
    print("YIYE_RENDER: ok", flush=True)
    return 0


def apply_paragraph_fix(translated_pdf: str, out_path: str, page_num: int, old_text: str, new_text: str) -> int:
    """把重译后的段落写回译文 PDF:定位旧译文块 → 红action 擦除 → 原字号自适应回填。

    定位用两阶段匹配:先严格空白归一化,再退化为"仅字母数字汉字"的宽松匹配
    (逐段对照的译文碎片常带句号边界差异,严格匹配会漏)。
    旧译文只是块内片段时,用空白弹性正则只替换该片段,保留块内其他内容。
    找不到目标段落或新译文放不下时返回非零。
    """
    import pymupdf

    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    def loose(s: str) -> str:
        # NFKC 先归一化:PDF 字体 ToUnicode 常把汉字映射成兼容表意文字(如 了 U+F973),
        # 不归一化则与界面/JSON 里的标准汉字永远匹配不上
        s = unicodedata.normalize("NFKC", s or "")
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", s)

    with pymupdf.open(translated_pdf) as doc:
        if page_num < 1 or page_num > doc.page_count:
            print("apply: 页码超出范围", file=sys.stderr)
            return 3
        page = doc[page_num - 1]
        target = None
        target_raw = ""
        loose_old = loose(old_text)
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            raw_text = "".join(s.get("text", "") for l in block.get("lines", []) for s in l.get("spans", []))
            n_text = norm(raw_text)
            if not n_text:
                continue
            if old_text and (norm(old_text) in n_text or n_text in norm(old_text)):
                target = block
                target_raw = raw_text
                break
            if not target and loose_old and (loose_old in loose(raw_text) or loose(raw_text) in loose_old):
                target = block
                target_raw = raw_text
        if target is None:
            print("apply: 未找到目标段落", file=sys.stderr)
            return 3
        rect = pymupdf.Rect(target["bbox"])
        sizes = [s.get("size", 9.0) for l in target.get("lines", []) for s in l.get("spans", [])]
        font_size = max(5.0, min(sizes)) if sizes else 9.0
        # 擦除旧段落文字(保留区域内的图片与矢量图形)
        page.add_redact_annot(rect)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        # 组装回填文本:旧译文只是块内片段时,用空白弹性正则只替换该片段,保留块内其他句子
        flex_old = r"\s*".join(re.escape(ch) for ch in old_text if not ch.isspace())
        new_block, count = re.subn(flex_old, lambda _m: new_text, target_raw, count=1)
        if count == 0 or not new_block.strip():
            new_block = new_text
        inserted = False
        while font_size >= 5.0:
            rc = page.insert_textbox(rect, new_block, fontname="china-s", fontsize=font_size, align=0)
            if rc >= 0:
                inserted = True
                break
            font_size -= 0.5
        if not inserted:
            print("apply: 新译文过长放不下", file=sys.stderr)
            return 4
        doc.save(out_path)
    print("YIYE_APPLY: ok", flush=True)
    return 0


def patch_progress_output() -> None:
    """把 BabelDOC 的 rich 进度显示切换为 tqdm 分支。

    rich Live 在非终端(子进程管道)下不输出中间帧,进度只在任务结束时可见;
    tqdm 分支每次更新都会输出一行含整体百分比(引擎按阶段权重计算)的日志,
    服务端解析后驱动预览扫描线。"""
    import babeldoc.main as babeldoc_main

    original = babeldoc_main.create_progress_handler

    def tqdm_progress_handler(translation_config, show_log=False):
        translation_config.use_rich_pbar = False
        return original(translation_config, show_log)

    babeldoc_main.create_progress_handler = tqdm_progress_handler


def patch_cjk_line_spacing() -> None:
    """把 BabelDOC 的 CJK 排版行距从 1.5 倍收紧到与英文一致的 1.3 倍。

    引擎给中文段落 1.5 倍行距,译文塞回原段落盒时被迫整体缩小字号
    (实测正文 10pt 缩到 7.5pt 左右),是译文与原版格式差异的最大来源。
    1.3 倍行距对中文字体同样安全,可让绝大多数段落保持原字号。
    引擎签名变化时静默跳过 —— 只影响字号,不影响翻译流程。
    """
    try:
        from babeldoc.format.pdf.document_il.midend import typesetting as ts_mod

        original = ts_mod.Typesetting._layout_typesetting_units
        if getattr(original, "_yiye_line_skip_patch", False):
            return
        params = list(inspect.signature(original).parameters)
        if params[:6] != ["self", "typesetting_units", "box", "scale", "line_skip", "paragraph"]:
            print("排版行距补丁跳过：引擎函数签名已变化", flush=True)
            return

        def patched(self, typesetting_units, box, scale, line_skip, paragraph, use_english_line_break=True, *args, **kwargs):
            return original(self, typesetting_units, box, scale, min(line_skip, 1.3), paragraph, use_english_line_break, *args, **kwargs)

        patched._yiye_line_skip_patch = True
        ts_mod.Typesetting._layout_typesetting_units = patched
    except Exception as exc:
        print(f"排版行距补丁未生效（{exc}），按引擎默认行距排版", flush=True)


def revise_pdf(translated_pdf: str, original_pdf: str, out_path: str, keep_original: list[int], output_mode: str = "mono") -> int:
    """生成修订版 PDF:keep_original 中的页(1-based)取原文,其余页取译文。

    用于质检发现问题页后的"本页保留原文"回退 —— 人工核对后的最终交付物。
    """
    import pymupdf

    src_t = Path(translated_pdf).resolve()
    src_o = Path(original_pdf).resolve()
    dst = Path(out_path).resolve()
    allowed = _allowed_roots()
    if not all(_is_under(p, allowed) for p in (src_t, src_o, dst)):
        print("revise: 路径超出允许范围", file=sys.stderr)
        return 2
    if src_t.suffix.lower() != ".pdf" or src_o.suffix.lower() != ".pdf" or dst.suffix.lower() != ".pdf":
        print("revise: 仅支持 .pdf 文件", file=sys.stderr)
        return 2

    keep = {int(p) for p in keep_original}
    with pymupdf.open(src_t) as translated, pymupdf.open(src_o) as original:
        if translated.page_count != original.page_count:
            print("revise: 译文与原文页数不一致，无法生成修订版", file=sys.stderr)
            return 2
        if not keep or any(p < 1 or p > original.page_count for p in keep):
            print("revise: 页码超出原文范围", file=sys.stderr)
            return 2
        revised = pymupdf.open()
        for i in range(translated.page_count):
            # “保留原文页”应保持原页尺寸、方向和内容，不把整页缩放或复制进双栏。
            # PDF 允许混合页尺寸；其余页仍保持翻译输出原样。
            source = original if (i + 1) in keep else translated
            revised.insert_pdf(source, from_page=i, to_page=i)
        revised.save(str(dst))
        revised.close()
    print("YIYE_REVISE: ok", flush=True)
    return 0


def main() -> int:
    # 任务参数经 stdin JSON 传入(路径不出现在命令行),并由 _allowed_roots 白名单约束
    raw = sys.stdin.read()
    if not raw.strip():
        print("usage: engine_worker.py < request.json", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except Exception:
        print("invalid request json", file=sys.stderr)
        return 2

    mode = payload.get("mode")
    if mode == "estimate":
        result = estimate_pdf(payload["pdfPath"], bool(payload.get("ocr", True)))
        print("YIYE_ESTIMATE: " + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    if mode == "render":
        code = render_page(payload["pdfPath"], payload["outPath"], int(payload.get("page", 0)))
        if code != 0:
            return code
        print("YIYE_STAGE: rendered preview page", flush=True)
        return 0
    if mode == "revise":
        code = revise_pdf(
            payload["translatedPdf"],
            payload["originalPdf"],
            payload["outPath"],
            payload.get("keepOriginal", []),
            payload.get("outputMode", "mono"),
        )
        if code != 0:
            return code
        print("YIYE_STAGE: revised pdf written", flush=True)
        return 0
    if mode == "apply_paragraph":
        code = apply_paragraph_fix(
            payload["translatedPdf"],
            payload["outPath"],
            int(payload["page"]),
            payload["oldText"],
            payload["newText"],
        )
        if code != 0:
            return code
        print("YIYE_STAGE: paragraph applied", flush=True)
        return 0
    if mode != "translate":
        print("unknown mode", file=sys.stderr)
        return 2

    request_path = Path(payload["requestPath"]).resolve()
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("request file not found", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"invalid request json: {exc}", file=sys.stderr)
        return 2
    api_key = os.environ.pop("YIYE_API_KEY", "")
    if not api_key:
        print("missing YIYE_API_KEY", file=sys.stderr)
        return 2

    errors, warnings = preflight(request["inputPath"], bool(request["config"].get("ocr")))
    for line in warnings:
        print(line, flush=True)
    if errors:
        for line in errors:
            print(line, file=sys.stderr, flush=True)
        return 3

    from babeldoc.main import cli

    patch_progress_output()
    patch_layout_translation_scope(translate_figures=bool(request["config"].get("figure")), translate_tables=bool(request["config"].get("table")))
    patch_cjk_line_spacing()
    sys.argv = build_args(request, api_key)
    print("YIYE_STAGE: starting BabelDOC", flush=True)
    cli()
    try:
        quality_check(
            request["inputPath"],
            request["outputDir"],
            request["config"]["output"],
            request.get("glossaryPath"),
            request["config"].get("pages"),
        )
    except Exception as exc:  # 质检自身故障不应让已完成的翻译标记为失败
        print(f"质检警告：质检步骤异常（{exc}），已跳过", flush=True)

    # 译文全文落盘,供任务问答(chat)构建上下文;带页码标记,回答可引用跳转
    try:
        full_text = translated_full_text(request["outputDir"], request["config"]["output"], max_chars=150000, page_markers=True)
        if full_text:
            (Path(request["outputDir"]) / "translated-text.txt").write_text(full_text, encoding="utf-8")
    except Exception as exc:
        print(f"译文全文导出失败（{exc}），任务问答将不可用", flush=True)

    # 图注对照提取(图表速读);失败不影响任务
    try:
        captions = extract_captions(request["inputPath"], request["outputDir"], request["config"]["output"])
        if captions:
            generated = render_figure_crops(request["inputPath"], request["outputDir"], captions)
            (Path(request["outputDir"]) / "figures.json").write_text(json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"图表速读：{len(captions)} 条图注，{generated} 张缩略图", flush=True)
    except Exception as exc:
        print(f"图注提取失败（{exc}），已跳过", flush=True)

    # 逐段对照数据(阅读模式);失败不影响任务
    try:
        pairs_pages = extract_paragraph_pairs(request["inputPath"], request["outputDir"], request["config"]["output"])
        if pairs_pages:
            (Path(request["outputDir"]) / "paragraphs.json").write_text(json.dumps({"pages": pairs_pages}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"逐段对照提取失败（{exc}），已跳过", flush=True)

    # 作者区恢复:第 1 页作者姓名/单位/邮箱原样保留英文(擦除音译与重排碎片);失败不影响任务
    try:
        out_pdfs = sorted(Path(request["outputDir"]).glob("*.pdf"))
        if out_pdfs:
            restore_author_blocks(str(out_pdfs[0]), request["inputPath"])
    except Exception as exc:
        print(f"作者区恢复失败（{exc}），已跳过", flush=True)

    # AI 速览:用同一模型对译文做中文速读总结;失败仅告警,不影响任务结果
    if request["config"].get("aiSummary", True):
        try:
            summary = generate_summary(request, api_key)
            if summary:
                summary_path = Path(request["outputDir"]) / "summary.json"
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                print("YIYE_STAGE: ai summary written", flush=True)
        except Exception as exc:
            print(f"AI 速览生成失败（{exc}），已跳过", flush=True)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        mp.set_start_method("spawn")
    raise SystemExit(main())
