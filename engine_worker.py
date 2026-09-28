"""BabelDOC worker. The API key enters through the environment and is never written to disk."""

from __future__ import annotations

import csv
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
PROMPT_VERSION = 2
PROMPT_TEMPLATE = (
    "你是一位资深的学术翻译引擎，专门把英文科研论文翻译成{variant}。\n"
    "- 忠实原文：不增写、不删减、不解释、不总结；原文没有的信息，译文里不能出现。\n"
    "- 学术文风：使用规范书面语，符合中文学术论文的表达习惯；避免口语化和翻译腔。\n"
    "- 术语纪律：术语表中给出的词条必须按给定译名翻译，并全文保持一致；"
    "术语表没有的专业术语按学界通用译法处理，没有把握时保留英文原名。\n"
    "- 数字与单位：数值、单位、化学式、变量名保持原样，不换算、不改写。\n"
    "- 长句处理：按{variant}习惯重组语序、断句，但不得改变原句的逻辑关系与限定范围。"
)


def build_prompt(target: str) -> str:
    variant = "繁体中文" if target == "zh-TW" else "简体中文"
    return PROMPT_TEMPLATE.format(variant=variant)

PLACEHOLDER_PATTERNS = (re.compile(r"\{v\d+\}"), re.compile(r"<style id="))

# 版式质检阈值(调研文档 P2:只报告明显问题,避免对上标/脚注误报)
MIN_FONT_SIZE = 6.0          # 正文最小可读字号(pt)
TEXT_IMAGE_OVERLAP = 0.30    # 文本框与图片交集面积 / 文本框面积
MIN_TEXT_LEN = 20            # 参与重叠判定的最小文本长度


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


def translated_page_text(doc, page_index: int, output_mode: str) -> str:
    """提取译文文本。左右对照模式只取右半页，避免把左半原文算进术语统计。"""
    import pymupdf

    page = doc[page_index]
    if output_mode == "dual":
        rect = page.rect
        return page.get_text(clip=pymupdf.Rect(rect.width / 2, 0, rect.width, rect.height))
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
    prompt = build_prompt(config["target"])
    # Qwen3 的思考开关走提示词(/no_think);非 qwen 走引擎的 thinking/reasoning 参数
    if is_qwen and thinking in ("default", "off"):
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
    if config.get("pages"):
        args += ["--pages", config["pages"]]
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
    """版式排版检查(与文本提取不同,基于 span/图片几何,可可靠检出):

    - font_too_small:译文 span 字号低于 MIN_FONT_SIZE
    - text_image_overlap:文本框与图片区域大面积重叠(正文覆盖配图)
    均为 warning 级提示,只报告明显问题。
    """
    import pymupdf

    issues: list[str] = []
    clip_rect = clip if clip is not None else page.rect
    small_font = False
    overlaps_image = False

    d = page.get_text("dict", clip=clip_rect)
    text_rects: list[tuple[float, float, float, float]] = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_text = ""
        block_rect = None
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                block_text += span.get("text", "")
                if block_rect is None:
                    block_rect = pymupdf.Rect(span["bbox"])
                else:
                    block_rect |= pymupdf.Rect(span["bbox"])
                if span.get("size", 99) < MIN_FONT_SIZE and span.get("text", "").strip():
                    small_font = True
        if block_text.strip() and block_rect is not None:
            text_rects.append((block_rect.x0, block_rect.y0, block_rect.x1, block_rect.y1))

    if small_font:
        issues.append("font_too_small")

    image_rects = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    for tx0, ty0, tx1, ty1 in text_rects:
        t_rect = pymupdf.Rect(tx0, ty0, tx1, ty1)
        t_area = t_rect.get_area()
        if t_area <= 0:
            continue
        for img_rect in image_rects:
            inter = t_rect & img_rect
            if not inter.is_empty and inter.get_area() > 0.30 * t_area:
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
            rows.append(f"<tr><td>{esc(entry['file'])} 第 {p['page']} 页</td><td>—</td><td>{esc('、'.join(p['issues']))}</td></tr>")

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
{''.join(f"<tr><td>{esc(r[0])}</td><td>{esc(r[1])}</td><td>{esc(r[2])}</td></tr>" for r in rows)}
</table>
{glossary_rows}
</body></html>"""


def quality_check(input_path: str, output_dir: str, output_mode: str, glossary_path: str | None = None, pages_spec: str | None = None) -> dict:
    """翻译完成后的逐页渲染检查，生成 quality-report.json。

    覆盖调研清单 P0「每页渲染检查」：空白页、译文缺失（疑似未翻译）、
    占位符残留、页数不符，以及术语表应用情况的一致性检查。
    结果只报告不阻断 —— 是否可接受由用户判断。
    """
    import pymupdf

    with pymupdf.open(input_path) as src:
        input_pages = src.page_count
    expected = {"mono": input_pages, "dual": input_pages}[output_mode]
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
                clip = None
                if output_mode == "dual":
                    # 左右对照只检查右半译文区
                    clip = pymupdf.Rect(page.rect.width / 2, 0, page.rect.width, page.rect.height)
                text = page.get_text(clip=clip)
                if len(text.strip()) < 5 and not page.get_images() and not page.get_drawings():
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
    report["glossaryCheck"] = check_glossary_consistency(report, output_dir, output_mode, glossary_path, in_scope, input_pages)
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
    只报告不删除 —— 水印是否需要处理由用户判断。
    """
    import pymupdf
    from collections import Counter

    try:
        doc = pymupdf.open(input_path)
    except Exception:
        return []
    total = doc.page_count
    if total < 3:
        doc.close()
        return []
    # 收集每页的 (归一化文本, 归一化位置) 签名，排除页眉页脚区
    span_pages: dict[tuple, set[int]] = {}
    for i in range(total):
        page = doc[i]
        h = page.rect.height
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
                        continue  # 页眉页脚区
                    key = (squash_text(text)[:50], round(bbox[0] / page.rect.width, 1), round(ry, 1))
                    span_pages.setdefault(key, set()).add(i)
    doc.close()
    if not span_pages:
        return []
    threshold = max(3, int(total * 0.5))
    suspects = []
    for (text, rx, ry), pages in span_pages.items():
        if len(pages) >= threshold:
            suspects.append({"text": text[:80], "pages": sorted(pages)})
    if not suspects:
        return []
    return [{
        "type": "repeated_text",
        "text": s["text"],
        "pages": s["pages"],
    } for s in suspects]


def squash_text(text: str) -> str:
    """PDF 提取文本常带康熙部首变体（⼒ vs 力）和断行空格，统计前必须归一化。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def check_glossary_consistency(report: dict, output_dir: str, output_mode: str, glossary_path: str | None, in_scope: set[int], original_pages: int) -> dict | None:
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


def revise_pdf(translated_pdf: str, original_pdf: str, out_path: str, keep_original: list[int]) -> int:
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
        revised = pymupdf.open()
        for i in range(translated.page_count):
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
        )
        if code != 0:
            return code
        print("YIYE_STAGE: revised pdf written", flush=True)
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
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        mp.set_start_method("spawn")
    raise SystemExit(main())
