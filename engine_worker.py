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
import time
import unicodedata
from datetime import datetime


# 提示词版本：修改提示词内容时递增，便于在文档与问题排查中对应行为变化。
# 注意：BabelDOC 的模板已强制结构规则（tag/占位符/代码不译不改），
# 此处只写内容层面的要求，不与引擎 Rules 重复。
PROMPT_VERSION = 6
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
    "- 标题与列表：论文主标题、摘要标题和章节标题必须翻译，标题中的专有名词和缩写保留；"
    "编号列表必须保留全部编号、顺序和分项结构，每项单独一行。\n"
    "- 占位符完整性：输入中的每个 {{v数字}} 占位符都代表公式、引用或特殊格式，"
    "必须在译文中原样出现且次数一致，绝不能省略。\n"
    "- 图表与参考文献：图表内部文字、表格单元格、参考文献条目按惯例保留英文原样，不翻译。\n"
    "- 长句处理：按{variant}习惯重组语序、断句，但不得改变原句的逻辑关系与限定范围。\n"
    "- 输出纪律：只输出译文本身，不要复述本指令、不要添加注释、标题或解释。"
)

FORCED_LINE_BREAK = "\u3000" * 4


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


def repair_translation_integrity(source: str, translated: str, layout_label: str, retried: bool) -> str | None:
    """校验模型输出中的结构信息；首次失败触发重试，重试仍失败则保留原文。

    retried=True 表示当前已是兜底（简单翻译）路径的输出——它是最后一次机会,
    直接接受译文,避免整段回退英文(富文本跨度可能不完美,但内容已翻译)。
    """
    if retried:
        return translated if translated.strip() else None
    placeholders = set(re.findall(r"\{v\d+\}", source + translated))
    if any(translated.count(token) != source.count(token) for token in placeholders):
        dbg = os.environ.get("YIYE_DEBUG_INTEGRITY")
        if dbg:
            print(f"[integrity] label={layout_label} retried={retried}\n[src] {source[:300]}\n[out] {translated[:300]}", file=sys.stderr, flush=True)
        return None

    source_text = re.sub(r"\s+", " ", source).strip()
    translated_text = re.sub(r"\s+", " ", translated).strip()
    if layout_label == "title" and source_text == translated_text and re.search(r"[a-z]", source_text):
        return None

    leading_style = re.match(r"^(<style\s+id=['\"]\d+['\"]>)(.+?)(</style>)", source, re.DOTALL)
    if leading_style and not re.match(r"^<style\s+id=", translated):
        heading_end = re.search(r"[。！？.!?]", translated)
        if heading_end:
            end = heading_end.end()
            translated = f"{leading_style.group(1)}{translated[:end].rstrip()}{leading_style.group(3)}{translated[end:].lstrip()}"

    # 富文本模式会把编号后的空格变成 {vN}，两种形式都识别。
    number_marker = r"(?<!\d)(\d{1,2})\.(?=\s|\{v\d+\}|<style)"
    numbers = [int(value) for value in re.findall(number_marker, source)]
    if len(numbers) >= 3 and numbers == list(range(numbers[0], numbers[0] + len(numbers))):
        for number in numbers[1:]:
            translated = re.sub(
                rf"(?<!\n)\s*(?={number}\.(?:\s|\{{v\d+\}}|<style))",
                FORCED_LINE_BREAK,
                translated,
                count=1,
            )
    return translated


def repair_batch_translation_bleed(prompt: str, response: str) -> str:
    """检测批量翻译中相邻段落串入，并让受污染项回退为单段重译。"""
    marker = "## Here is the input:"
    if marker not in prompt:
        return response
    try:
        inputs = json.loads(prompt.rsplit(marker, 1)[1].strip())
        cleaned = response.strip()
        if cleaned.startswith("<json>") and cleaned.endswith("</json>"):
            cleaned = cleaned[6:-7]
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        outputs = json.loads(cleaned.strip())
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            return response
        source_by_id = {int(item["id"]): item["input"] for item in inputs}
        output_by_id = {int(item["id"]): item.get("output", item.get("input", "")) for item in outputs}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return response

    def grams(value: str) -> set[str]:
        value = re.sub(r"<[^>]+>|\{v\d+\}", "", value)
        value = "".join(char.casefold() for char in value if char.isalnum())
        return {value[index : index + 3] for index in range(max(0, len(value) - 2))}

    contaminated: set[int] = set()
    ids = sorted(set(source_by_id) & set(output_by_id))
    for pos, left_id in enumerate(ids):
        for right_id in ids[pos + 1 :]:
            left_out, right_out = grams(output_by_id[left_id]), grams(output_by_id[right_id])
            if min(len(left_out), len(right_out)) < 25:
                continue
            output_overlap = len(left_out & right_out) / min(len(left_out), len(right_out))
            left_src, right_src = grams(source_by_id[left_id]), grams(source_by_id[right_id])
            source_overlap = len(left_src & right_src) / max(1, min(len(left_src), len(right_src)))
            if output_overlap < 0.65 or output_overlap - source_overlap < 0.30:
                continue
            longer_id = left_id if len(left_out) > len(right_out) * 1.15 else right_id if len(right_out) > len(left_out) * 1.15 else None
            if longer_id is not None:
                contaminated.add(longer_id)
    if not contaminated:
        return response
    for item in outputs:
        # 模型输出的 id 不可信(null/字符串等):这里不在解析阶段的 try 保护内,
        # 一次 TypeError 会让异常冒泡到 BabelDOC 的批处理层,整批段落被降级重译
        raw_id = item.get("id")
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id in contaminated:
            item["output"] = source_by_id[item_id]
    return json.dumps(outputs, ensure_ascii=False)


def patch_translation_integrity() -> None:
    """在 BabelDOC 写回段落前拦截引用丢失、短标题未译和编号列表合并。"""
    import babeldoc.format.pdf.document_il.midend.il_translator as it
    from babeldoc.translator.translator import BaseTranslator

    original = getattr(it.ILTranslator.post_translate_paragraph, "_yiye_original", it.ILTranslator.post_translate_paragraph)

    def patched(self, paragraph, tracker, translate_input, translated_text):
        trackers = getattr(tracker, "llm_translate_trackers", [])
        retried = any(getattr(item, "fallback_to_translate", False) for item in trackers[:-1])
        repaired = repair_translation_integrity(
            translate_input.unicode,
            translated_text,
            (getattr(paragraph, "layout_label", "") or "").replace("_hybrid", "").strip(),
            retried,
        )
        if repaired is None:
            raise ValueError("translation lost placeholders or left a translatable title unchanged")
        return original(self, paragraph, tracker, translate_input, repaired)

    patched._yiye_original = original
    it.ILTranslator.post_translate_paragraph = patched

    original_llm_translate = getattr(BaseTranslator.llm_translate, "_yiye_original", BaseTranslator.llm_translate)
    if not getattr(BaseTranslator.llm_translate, "_yiye_batch_bleed_patch", False):
        def checked_llm_translate(self, text, *args, **kwargs):
            return repair_batch_translation_bleed(text, original_llm_translate(self, text, *args, **kwargs))

        checked_llm_translate._yiye_original = original_llm_translate
        checked_llm_translate._yiye_batch_bleed_patch = True
        BaseTranslator.llm_translate = checked_llm_translate


def patch_forced_list_line_breaks() -> None:
    """让编号列表分项真正换行；BabelDOC 默认会在排版前丢弃普通换行符。"""
    import babeldoc.format.pdf.document_il.midend.typesetting as ts

    original = getattr(ts.Typesetting.create_typesetting_units, "_yiye_original", ts.Typesetting.create_typesetting_units)

    def patched(self, paragraph, fonts):
        units = original(self, paragraph, fonts)
        result = []
        i = 0
        marker = list(FORCED_LINE_BREAK)
        while i < len(units):
            if [unit.try_get_unicode() for unit in units[i : i + len(marker)]] == marker:
                unit = units[i]
                unit.unicode = " "
                unit.width_cache = 1_000_000
                unit.box_cache = None
                unit.is_space_cache = True
                result.append(unit)
                i += len(marker)
            else:
                result.append(units[i])
                i += 1
        return result

    patched._yiye_original = original
    ts.Typesetting.create_typesetting_units = patched

PLACEHOLDER_PATTERNS = (re.compile(r"\{v\d+\}"), re.compile(r"<style id="))

# 版式质检阈值(只报告"正文压图"类真实缺陷;图内标签属正常版式不报)
TEXT_IMAGE_OVERLAP = 0.30    # 文本框与图片交集面积 / 文本框面积
OVERLAP_MIN_WIDTH = 100      # 参与判定的文本块最小宽度(pt),过滤图内短标签
OVERLAP_MIN_LINES = 2        # 参与判定的文本块最小行数
SHRINK_SIZE_RATIO = 0.88     # 字号低于页面主导字号的该比例视为"被缩小"
SHRINK_SHARE = 0.25          # 被缩小文字占页面中文比例超过该值才报告
SHRINK_MIN_CHARS = 150       # 被缩小中文字符数下限,过滤只有脚注/页眉的页面


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


def translated_page_clip(page, output_mode: str):
    """译文区域:dual 并排布局返回译文所在半页;dual 交替布局的译文页(两半都有
    大量中文)返回整页;mono 返回 None 表示整页。

    translated_half_clip 只认半页,交替页布局的译文页会被错误地砍掉一半。
    """
    import pymupdf

    if output_mode != "dual":
        return None
    rect = page.cropbox
    mid = (rect.x0 + rect.x1) / 2
    left = pymupdf.Rect(rect.x0, rect.y0, mid, rect.y1)
    right = pymupdf.Rect(mid, rect.y0, rect.x1, rect.y1)
    left_cjk = len(re.findall(r"[\u4e00-\u9fff]", page.get_text(clip=left)))
    right_cjk = len(re.findall(r"[\u4e00-\u9fff]", page.get_text(clip=right)))
    if left_cjk > 50 and right_cjk > 50:
        # 与上面切半页的坐标系一致用 cropbox:get_text 是未旋转坐标,
        # page.rect 在旋转页会交换宽高,混用会让整页 clip 错位
        return pymupdf.Rect(page.cropbox)
    return right if right_cjk >= left_cjk else left


def translated_page_text(doc, page_index: int, output_mode: str) -> str:
    """提取译文文本。左右对照模式自动识别译文在哪一半，避免把另一半原文算进统计。"""
    import pymupdf

    page = doc[page_index]
    clip = translated_page_clip(page, output_mode)
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
    # thinking/reasoning 会作为未文档化字段进入 OpenAI 请求体,严格端点
    # (OpenAI 官方 API 等)会因未知字段对每个请求返回 400,拖垮整个任务。
    # 仅在网关路径(anthropic/gemini 协议)发送 —— 网关会把它转换成上游
    # 协议参数;直连 OpenAI-compatible 端点不发,保持端点默认思考行为。
    # Qwen3 的思考开关始终走提示词 /no_think,不依赖请求体字段。
    if not is_qwen and request["config"].get("gatewayId"):
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
        # 保留富文本翻译，避免标题、编号列表、上下标等特殊格式被整体降级。
        # 疑难 PDF 只启用较安全的两项兼容措施。
        args.append("--skip-clean")
        # --dual-translate-first 把译文页排到输出偶数位(1-based),与交替页布局
        # "奇数页原文、偶数页译文"的页序约定冲突,两者互斥
        if config.get("dualLayout") != "alternating":
            args.append("--dual-translate-first")
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

        # 采样前 10 页 + 中部页面:长文档的前置页(封面/版权/纯图目录)常没有
        # 文字层,只看开头会把正文正常的 PDF 误判成扫描件
        sample_indexes = set(range(min(10, doc.page_count)))
        if doc.page_count > 20:
            mid = doc.page_count // 2
            sample_indexes.update({mid - 1, mid, min(mid + 4, doc.page_count - 1)})
        text_chars = sum(len(doc[i].get_text().strip()) for i in sorted(sample_indexes))
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
    """版式排版检查:正文大面积覆盖图片、译文段落字号被整体压缩。

    精度规则(避免把图内标签误报为缺陷):
    - 只检查宽度 ≥ OVERLAP_MIN_WIDTH 且行数 ≥ OVERLAP_MIN_LINES 的正文型块;
      图内短标签(单行、窄块,如流程图节点文字)不参与判定;
    - 交集超过文本框面积 TEXT_IMAGE_OVERLAP 比例才算重叠。

    字号检查(font_size_shrink):译文侧中文按字号统计,大量文字明显小于
    页面主导字号(段落盒放不下被引擎整体缩小)时报告;脚注/页眉等天然
    小字因占比与数量门槛(SHRINK_*)不会触发。
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

    sizes: dict[float, int] = {}
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
                if cjk:
                    key = round(span.get("size", 0.0), 1)
                    sizes[key] = sizes.get(key, 0) + cjk
    if sum(sizes.values()) >= SHRINK_MIN_CHARS:
        dominant_size = max(sizes, key=lambda s: sizes[s])
        dominant = sizes[dominant_size]
        shrunk = sum(count for size, count in sizes.items() if size < dominant_size * SHRINK_SIZE_RATIO and size >= 5.0)
        if shrunk >= SHRINK_MIN_CHARS and shrunk / dominant >= SHRINK_SHARE:
            issues.append("font_size_shrink")
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
            page_label = p["page"] if p.get("outputPage") in (None, p["page"]) else f"{p['page']}（输出页 {p['outputPage']}）"
            rows.append(f"<tr><td>{esc(entry['file'])}</td><td>{page_label}</td><td>{esc('、'.join(p['issues']))}</td></tr>")

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


def is_reference_page_text(text: str) -> bool:
    """参考文献占主导的页面故意保留英文，不应被质检误报为未翻译。

    同时识别编号式([1] .../1. ...)与作者-年份式(Author, A. 2020. ...)条目。
    """
    if len(re.findall(r"(?m)^\s*\[\d{1,3}\]\s+\S", text)) >= 3:
        return True
    author_year = re.findall(r"(?m)^\s*[A-Z][A-Za-z'’\-]+,\s*[A-Z]\.[^\n]{0,240}?\b(?:19|20)\d{2}\b", text)
    if len(author_year) >= 3:
        return True
    numbered = re.findall(r"(?m)^\s*\d{1,3}\.\s+[A-Z][^\n]{0,240}?\b(?:19|20)\d{2}\b", text)
    return len(numbered) >= 3


def quality_check(
    input_path: str,
    output_dir: str,
    output_mode: str,
    glossary_path: str | None = None,
    pages_spec: str | None = None,
    dual_layout: str = "side",
    captions: list[dict] | None = None,
) -> dict:
    """翻译完成后的逐页渲染检查，生成 quality-report.json。

    覆盖调研清单 P0「每页渲染检查」：空白页、译文缺失（疑似未翻译）、
    占位符残留、页数不符，以及术语表应用情况的一致性检查。
    结果只报告不阻断 —— 是否可接受由用户判断。
    交替页对照(alternating)下奇数页是原样原文页、偶数页是整页译文,
    原文页跳过译文检查,译文页按整页检查,避免"疑似未翻译"误报。
    """
    import pymupdf

    with pymupdf.open(input_path) as src:
        input_pages = src.page_count
    alternating = output_mode == "dual" and dual_layout == "alternating"
    # 交替页对照的输出是"原文页 + 译文页"成对出现,页数翻倍
    expected = input_pages * 2 if alternating else input_pages
    in_scope = pages_in_scope(input_pages, pages_spec)

    report = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "inputPages": input_pages,
        "expectedPages": expected,
        "outputs": [],
        "issueCount": 0,
    }

    # 只检查主翻译产物:用 main_translated_pdfs 而非裸 glob("*.pdf"),
    # 排除校对写回/修订版/作者区恢复/临时文件等衍生文件,
    # 避免衍生文件页数与整篇预期不符造成永久误报
    for pdf in main_translated_pdfs(output_dir):
        entry: dict = {"file": pdf.name, "issues": [], "pages": []}
        with pymupdf.open(pdf) as doc:
            entry["actualPages"] = doc.page_count
            if doc.page_count != expected:
                entry["issues"].append(f"页数 {doc.page_count} 与预期 {expected} 不符")
            for i in range(doc.page_count):
                if alternating and (i + 1) % 2 == 1:
                    continue  # 交替页:奇数位是原样原文页,不做译文检查
                # 页码范围外的页面保留原文,不做译文检查;
                # 交替页输出翻倍,输出页 i 对应原文页 i//2(0-based)
                if (i // 2 if alternating else i) not in in_scope:
                    continue
                page_issues: list[str] = []
                page = doc[i]
                # dual 对照自动识别译文在哪一半(兼容模式会把译文页排在左侧),
                # 译文溢出到左半的页与交替页的整页译文都按识别结果检查
                clip = translated_page_clip(page, output_mode)
                text = page.get_text(clip=clip)
                if len(text.strip()) < 5 and not _has_visual_content(page, clip):
                    # dual 模式下左半有原文、右半为空，说明这一页没有翻出来
                    page_issues.append("译文缺失" if output_mode == "dual" else "空白页")
                cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
                latin = len(re.findall(r"[A-Za-z]", text))
                if latin >= 300 and cjk == 0 and not is_reference_page_text(text):
                    page_issues.append("疑似未翻译")
                if any(pat.search(text) for pat in PLACEHOLDER_PATTERNS):
                    page_issues.append("占位符残留")
                page_issues.extend(typography_issues(page, clip))
                if page_issues:
                    # page 统一为原文页号(与"保留原文/重译此页"的入参一致);
                    # outputPage 是交付 PDF 里的实际页位,供前端 #page 锚点跳转
                    entry["pages"].append({
                        "page": (i // 2 + 1) if alternating else i + 1,
                        "outputPage": i + 1,
                        "issues": page_issues,
                    })
        entry["issueCount"] = len(entry["issues"]) + len(entry["pages"])
        report["outputs"].append(entry)

    report["issueCount"] = sum(item["issueCount"] for item in report["outputs"])
    report["ok"] = report["issueCount"] == 0
    report["glossaryCheck"] = check_glossary_consistency(report, output_dir, output_mode, glossary_path, in_scope, captions, input_path, dual_layout)
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
            # get_text 是未旋转坐标,cropbox 才与之一致(90°/270° 页 rect 交换宽高)
            h = page.cropbox.height or page.rect.height
            w = page.cropbox.width or page.rect.width
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if len(text) < 4:
                            continue
                        bbox = span["bbox"]
                        # 以 span 纵向中心落带判定:仅看顶边会把基线恰在带下缘的
                        # running header(y0≈0.09h)漏进候选造成误报
                        ry = (bbox[1] + bbox[3]) / 2 / h
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


def strip_watermarks(input_path: str, suspects: list, out_path: str) -> int:
    """按用户确认的文本移除水印：整份文档精确匹配该文本并 redaction 擦除。

    只删除与确认文本一致的文字对象（图片与矢量图形保留，表格线不受影响），
    原文件不做任何改动，结果写入 out_path。返回擦除的文本对象数；
    没有可用的确认文本时原样复制并返回 0。
    """
    import pymupdf
    import shutil

    texts = []
    for item in suspects or []:
        text = str(item.get("text", "") if isinstance(item, dict) else item).strip()
        if len(text) >= 4 and text not in texts:
            texts.append(text)
    if not texts:
        shutil.copyfile(input_path, out_path)
        return 0
    with pymupdf.open(input_path) as doc:
        removed = 0
        for page in doc:
            rects = []
            for text in texts:
                rects.extend(page.search_for(text))
            if not rects:
                continue
            for rect in rects:
                page.add_redact_annot(rect)
            # 保留区域内的图片与矢量图形，只擦文字
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
            removed += len(rects)
        doc.save(out_path, garbage=3, deflate=True)
    return removed


SUMMARY_PROMPT = (
    "你是学术论文阅读助手。请基于给定的论文中文译文内容，输出中文速览。"
    "严格按照以下格式输出，共 5 行，每行以【】标签开头，每部分 1-3 句，不要输出任何其他内容：\n"
    "【一句话总结】\n【研究问题】\n【方法】\n【主要结果】\n【局限与展望】"
)


def translated_full_text(output_dir: str, output_mode: str, max_chars: int = 20000, page_markers: bool = False, dual_layout: str | None = None) -> str:
    """取译文文本作为素材：优先纯译文 mono PDF；dual 只取右半译文区。

    page_markers 时每页前插入【第 N 页】标记,供任务问答的引用跳转定位。
    """
    import pymupdf

    # 与质检同口径:只取主翻译产物,避免抓到校对写回/修订版等衍生文件
    pdfs = main_translated_pdfs(output_dir)
    if not pdfs:
        return ""
    mono = next((p for p in pdfs if p.name.lower().endswith(".mono.pdf")), None)
    parts: list[str] = []
    with pymupdf.open(mono or pdfs[0]) as doc:
        # 交替页布局判定:显式 dualLayout 优先;
        # 未提供时按文档级检测——存在"两半都有大量中文"的整页译文页即为交替页
        alternating = False
        if mono is None and output_mode == "dual" and dual_layout is not None:
            alternating = dual_layout == "alternating"
        if mono is None and output_mode == "dual" and dual_layout is None:
            for page in doc:
                rect = page.cropbox
                mid = (rect.x0 + rect.x1) / 2
                left_cjk = len(re.findall(r"[\u4e00-\u9fff]", page.get_text(clip=pymupdf.Rect(rect.x0, rect.y0, mid, rect.y1))))
                right_cjk = len(re.findall(r"[\u4e00-\u9fff]", page.get_text(clip=pymupdf.Rect(mid, rect.y0, rect.x1, rect.y1))))
                if left_cjk > 50 and right_cjk > 50:
                    alternating = True
                    break
        for i, page in enumerate(doc):
            if mono is None and output_mode == "dual":
                # 交替页布局的原文页整页无中文,跳过以免污染问答上下文与引用定位
                if alternating and len(re.findall(r"[\u4e00-\u9fff]", page.get_text())) < 20:
                    continue
                clip = translated_page_clip(page, output_mode)
                text = page.get_text(clip=clip)
            else:
                text = page.get_text()
            parts.append(f"【第 {i + 1} 页】\n{text}" if page_markers else text)
    text = re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()
    return text[:max_chars]


def patch_auto_glossary_cleanup() -> None:
    """清洗 BabelDOC 自动抽取的术语表:过滤断词碎片等坏条目,降低术语噪声。

    自动抽取会把 PDF 断词伪影('curriculum R L strategy')、截断条目和过短碎片
    一并收进术语表;这类条目注入提示词只会误导模型,并让质检的"疑似未按术语表
    翻译"虚高。真实术语不受影响。引擎行为变化时静默跳过。
    """
    from babeldoc.format.pdf import translation_config as tc_mod

    original = tc_mod.SharedContextCrossSplitPart.finalize_auto_extracted_glossary
    if getattr(original, "_yiye_glossary_cleanup", False):
        return

    SINGLE_LETTER_PAIR = re.compile(r"\b[A-Za-z]\s+[A-Za-z]\b")

    def bad_entry(source: str) -> bool:
        src = (source or "").strip()
        if len(src) < 4:
            return True
        if src.endswith(("-", " -")) or src.startswith("-"):
            return True
        # 断词伪影:孤立单字母对('R L'、'A N'),来自 PDF 换行连字符的补全失败
        if SINGLE_LETTER_PAIR.search(src):
            return True
        return False

    def patched(self):
        original(self)
        glossary = self.auto_extracted_glossary
        if glossary is None:
            return
        kept = [entry for entry in glossary.entries if not bad_entry(entry.source)]
        if len(kept) == len(glossary.entries):
            return
        cleaned = type(glossary)(name=glossary.name, entries=kept)
        self.auto_extracted_glossary = cleaned
        print(
            f"术语清洗：自动术语表 {len(glossary.entries)} 条中移除 {len(glossary.entries) - len(kept)} 条坏条目",
            flush=True,
        )

    patched._yiye_glossary_cleanup = True
    patched._yiye_original = original
    tc_mod.SharedContextCrossSplitPart.finalize_auto_extracted_glossary = patched


def build_llm_client(request: dict, api_key: str):
    from openai import OpenAI

    config = request["config"]
    base_url = config["baseUrl"]
    if config.get("gatewayId"):
        # anthropic/gemini 协议经本地网关转换,与翻译请求同一条通路;网关不校验鉴权字段
        base_url = f"http://127.0.0.1:{os.environ.get('YIYE_PORT', '4173')}/api/llm-gateway/{config['gatewayId']}/v1"
    return OpenAI(base_url=base_url, api_key=api_key, timeout=240, max_retries=0)


def _caption_kind(word: str) -> str:
    word = word.lower().rstrip(".")
    return "figure" if word.startswith("fig") else "table"


def _caption_free_space_below(page, rect, clip=None) -> float:
    """题注块正下方到下一个文本块之间的空隙高度(限幅,避免覆盖正文)。"""
    limit = rect.height * 1.6
    next_top = None
    for block in page.get_text("dict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        bbox = block["bbox"]
        if bbox[1] >= rect.y1 - 1 and not (bbox[2] <= rect.x0 or bbox[0] >= rect.x1):
            next_top = bbox[1] if next_top is None else min(next_top, bbox[1])
    if next_top is None:
        return limit
    return max(0.0, min(next_top - rect.y1 - 3.0, limit))


def replace_caption_block(page, kind: str, num: str, new_text: str, clip=None) -> bool:
    """定位页内(可限 clip)指定编号的英文题注块,擦除后原位回填中文译文。

    与段落编辑写回(apply_paragraph_fix)同一套策略:块定位 → redaction 擦字 →
    原位回填。回填优先用 insert_htmlbox(中文字距正常,可向下借用空隙两行内自适应),
    失败再退回 insert_textbox 逐级缩字号。找不到块或都放不下时返回 False。
    """
    import pymupdf

    target = None
    for block in page.get_text("dict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        raw = "".join(span.get("text", "") for line in block.get("lines", []) for span in line.get("spans", []))
        match = CAPTION_ORIGINAL.match(raw.strip())
        if match and _caption_kind(match.group(1)) == kind and match.group(2) == str(num):
            target = block
            break
    if target is None:
        return False
    rect = pymupdf.Rect(target["bbox"])
    original_text = raw.strip()
    # 擦除题注文字(保留区域内的图片与矢量图形,不伤表格线)
    page.add_redact_annot(rect)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
    expanded = pymupdf.Rect(rect)
    expanded.y1 += _caption_free_space_below(page, rect, clip)
    try:
        spare, scale = page.insert_htmlbox(expanded, new_text, scale_low=0.55)
        if scale > 0:
            return True
    except Exception:
        pass
    sizes = [span.get("size", 9.0) for line in target.get("lines", []) for span in line.get("spans", [])]
    font_size = max(5.0, min(sizes)) if sizes else 9.0
    while font_size >= 5.0:
        if page.insert_textbox(expanded, new_text, fontname="china-s", fontsize=font_size, align=0) >= 0:
            return True
        font_size -= 0.5
    # 译文实在放不下时回写英文原文,宁可保留原文也不丢题注。
    # 循环退出时 font_size 已减到 4.5(低于自身 5.0 下限),回写前拉回下限
    rc = page.insert_textbox(expanded, original_text, fontname="helv", fontsize=max(5.0, font_size), align=0)
    if rc < 0:
        print("题注：原文本回填也放不下，该题注区域可能残留空白", file=sys.stderr)
    return False


def main_translated_pdfs(output_dir: str) -> list[Path]:
    """输出目录中的主翻译产物(排除校对写回/修订版/作者区恢复/题注补译等衍生文件),排序稳定。"""
    derived = {"adjusted-output.pdf", "revised-output.pdf"}
    return [
        p for p in sorted(Path(output_dir).glob("*.pdf"))
        if p.name not in derived
        and not p.name.endswith("-author-restored.pdf")
        and not p.name.endswith("-caption-rescued.pdf")
        and not p.name.endswith("-apply-new.pdf")
        and not p.name.endswith("-outline-new.pdf")
        and ".tmp.pdf" not in p.name
    ]


def rescue_untranslated_captions(request: dict, api_key: str) -> list[tuple[str, int, str]]:
    """补译仍为英文的图表题注并写回成品 PDF(失败只告警,不阻断)。

    返回实际写回的 (kind("图"/"表"), num, 补译文本) 列表,供 figures.json 同步刷新。

    根因:BabelDOC 的表格检测框有时把题注一并圈进表格,题注字符在段落组成阶段
    被并入表格结构,根本不会进入翻译管线(实测同一篇论文 Table 1/3 正常翻译、
    Table 2/4 被吞)。这里在成品 PDF 的译文侧扫描仍是 Figure/Table N. 形态、
    且同页没有对应"图 N/表 N"译文的题注,用同一模型批量补译后原位回填。
    """
    import pymupdf

    output_mode = request["config"]["output"]
    dual_layout = request["config"].get("dualLayout", "side")
    alternating = output_mode == "dual" and dual_layout == "alternating"
    out_pdfs = main_translated_pdfs(request["outputDir"])
    if not out_pdfs:
        return []
    target_path = out_pdfs[0]
    with pymupdf.open(request["inputPath"]) as src:
        page_count = src.page_count
    in_scope = pages_in_scope(page_count, request["config"].get("pages"))

    doc = pymupdf.open(target_path)
    try:
        pending: list[tuple[int, str, str, str]] = []
        for i in range(doc.page_count):
            if alternating:
                if i % 2 == 0:
                    continue  # 原样原文页,避免把英文补译写上原文页
                if (i // 2) not in in_scope:
                    continue
            elif i not in in_scope:
                continue
            page = doc[i]
            clip = translated_page_clip(page, output_mode)
            scope_text = page.get_text(clip=clip) if clip is not None else page.get_text()
            # 该页译文侧没有中文(保留原文页/交替页的原文页)则跳过,避免把译文写上原文页
            if len(re.findall(r"[\u4e00-\u9fff]", scope_text)) < 50:
                continue
            translated: set[tuple[str, str]] = set()
            english: dict[tuple[str, str], tuple[int, str]] = {}
            for block in page.get_text("dict", clip=clip).get("blocks", []):
                if block.get("type") != 0:
                    continue
                raw = "".join(span.get("text", "") for line in block.get("lines", []) for span in line.get("spans", [])).strip()
                zh = CAPTION_TRANSLATED.match(raw)
                if zh:
                    translated.add(("figure" if zh.group(1) in ("图", "圖") else "table", zh.group(2)))
                    continue
                en = CAPTION_ORIGINAL.match(raw)
                if en:
                    key = (_caption_kind(en.group(1)), en.group(2))
                    # 同一题注被拆成多个块时取最长块(完整题注),避免只译出"Figure 1."编号
                    if key not in english or len(raw) > len(english[key][1]):
                        english[key] = (i, raw)
            for key, (page_index, raw) in english.items():
                if key not in translated:
                    pending.append((page_index, key[0], key[1], raw))
        if not pending:
            return []

        listing = "\n".join(f"{idx + 1}. {item[3]}" for idx, item in enumerate(pending))
        client = build_llm_client(request, api_key)
        # 繁体任务(zh-TW)补译也必须是繁体,且"Figure N"的译名用「圖」,与正文翻译一致
        target = request["config"].get("target", "zh-CN")
        if target == "zh-TW":
            target_rule = "把下列学术论文的图表题注逐条翻译成繁体中文。编号格式固定：Table N 译作「表 N」、Figure N 译作「圖 N」"
        else:
            target_rule = "把下列学术论文的图表题注逐条翻译成简体中文。编号格式固定：Table N 译作“表 N”、Figure N 译作“图 N”"
        response = client.chat.completions.create(
            model=request["config"]["model"],
            temperature=0.2,
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{target_rule}；"
                        "数据集名、方法名、模型名保留英文。"
                        "每条输出一行，格式为“序号. 译文”，除译文外不要输出任何其他文字。\n\n" + listing
                    ),
                }
            ],
        )
        content = (response.choices[0].message.content or "").strip()
        translations: dict[int, str] = {}
        for line in content.splitlines():
            line = line.strip().lstrip("-* ")
            match = re.match(r"^(\d+)\s*[.、:：]\s*(.+)$", line)
            if match:
                translations[int(match.group(1)) - 1] = match.group(2).strip()
        applied: list[tuple[str, int, str]] = []
        for idx, (page_index, kind, num, raw) in enumerate(pending):
            new_text = translations.get(idx)
            if not new_text:
                continue
            if replace_caption_block(doc[page_index], kind, num, new_text, clip=translated_page_clip(doc[page_index], output_mode)):
                applied.append(("图" if kind == "figure" else "表", int(num), new_text))
        if applied:
            tmp_path = target_path.with_suffix(".rescue.tmp.pdf")
            doc.save(str(tmp_path))
            doc.close()
            # 目标可能被浏览器预览占用(Windows 下 os.replace 抛 PermissionError):
            # 重试后仍失败则写备选名;两种结局都要清理临时文件,否则残留的
            # .rescue.tmp.pdf 会被当作交付 PDF 列出
            replaced = False
            for delay in (0.2, 0.5, 1.0, 2.0, 4.0):
                try:
                    os.replace(tmp_path, target_path)
                    replaced = True
                    break
                except PermissionError:
                    time.sleep(delay)
            if not replaced:
                # 备选名不能用 restore_author_blocks 的 "-author-restored":
                # 两个写回同时被锁定时会互相覆盖,且语义误导
                fallback = target_path.with_name(target_path.stem + "-caption-rescued.pdf")
                try:
                    os.replace(tmp_path, fallback)
                    print(f"题注补译：目标被占用，已写入备选文件 {fallback.name}", flush=True)
                    return applied
                except Exception as exc:
                    print(f"题注补译：写回失败（{exc}），补译内容未保存", file=sys.stderr)
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    return []
            print(f"题注补译：{len(applied)}/{len(pending)} 条写回（{target_path.name}）", flush=True)
            return applied
        print("题注补译：无题注写回", flush=True)
        return []
    finally:
        if not doc.is_closed:
            doc.close()


def generate_summary(request: dict, api_key: str) -> dict | None:
    """翻译完成后用同一模型生成中文速览（对标竞品的「AI 速读」）。

    失败只告警不阻断 —— 速览是增值能力,翻译成果本身不受影响。
    """
    text = translated_full_text(request["outputDir"], request["config"]["output"], dual_layout=request["config"].get("dualLayout", "side"))
    if len(text) < 200:
        return None
    config = request["config"]
    client = build_llm_client(request, api_key)
    # zh-TW 任务的速览也输出繁体,与正文翻译语言一致
    target_note = "请用繁体中文输出。" if config.get("target") == "zh-TW" else ""
    response = client.chat.completions.create(
        model=config["model"],
        temperature=0.2,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT + target_note},
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


def _figure_rects_on_output(captions: list[dict], input_path: str, output_pdf: str, output_mode: str, in_scope: set[int], dual_layout: str = "side") -> dict[int, list]:
    """把图注反推的图表区域(原文页坐标)映射到译文输出的页坐标。

    返回 {输出页 0-based: [Rect]};mono 1:1、dual 并排按半页缩放平移、
    alternating 的译文页在输出第 2N 页且整页坐标 1:1。
    """
    import pymupdf

    if not captions:
        return {}
    alternating = output_mode == "dual" and dual_layout == "alternating"
    mapping: dict[int, list] = {}
    with pymupdf.open(input_path) as src, pymupdf.open(output_pdf) as out:
        for caption in captions:
            page_no = caption.get("page")
            rect = caption.get("rect")
            if not page_no or not rect or (page_no - 1) not in in_scope:
                continue
            src_page = src[page_no - 1]
            if output_mode == "dual":
                out_index = 2 * page_no - 1 if alternating else page_no - 1
                if out_index >= out.page_count:
                    continue
                if alternating:
                    mapped = pymupdf.Rect(rect)  # 整页译文,坐标与原文 1:1
                else:
                    out_page = out[out_index]
                    clip = translated_page_clip(out_page, "dual")
                    if clip is None:
                        continue
                    sx, sy = clip.width / src_page.rect.width, clip.height / src_page.rect.height
                    mapped = pymupdf.Rect(
                        clip.x0 + rect[0] * sx,
                        clip.y0 + rect[1] * sy,
                        clip.x0 + rect[2] * sx,
                        clip.y0 + rect[3] * sy,
                    )
            else:
                out_index = page_no - 1
                mapped = pymupdf.Rect(rect)
            mapping.setdefault(out_index, []).append(mapped)
    return mapping


def _term_only_inside_figures(pdf_path, term: str, rects_by_page: dict[int, list], output_mode: str, in_scope: set[int], dual_layout: str = "side") -> bool:
    """术语在译文区域里的每一次出现是否都落在图表区域内(是则视为正确保留英文)。

    dual 页面还包含原文半区,原词必然也在那里出现——搜索必须限定在译文区。
    rects_by_page 的键是输出页 0-based 索引;交替页下输出页 i 对应原文页 i//2。
    """
    import pymupdf

    alternating = output_mode == "dual" and dual_layout == "alternating"
    hits = 0
    with pymupdf.open(pdf_path) as doc:
        for page_index, rects in rects_by_page.items():
            if ((page_index // 2) if alternating else page_index) not in in_scope or not rects:
                continue
            page = doc[page_index]
            clip = translated_page_clip(page, output_mode)
            for rect in page.search_for(term, clip=clip):
                center = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                if not any(r.contains(center) for r in rects):
                    return False
                hits += 1
    return hits > 0


def check_glossary_consistency(report: dict, output_dir: str, output_mode: str, glossary_path: str | None, in_scope: set[int], captions: list[dict] | None = None, input_path: str | None = None, dual_layout: str = "side") -> dict | None:
    """统计术语表中每个词条在译文里的应用情况。

    - applied：目标译名出现，视为已应用；
    - suspect：译名未出现、但原词仍出现在译文区域，疑似未按术语表翻译；
      图表区域内的原词不计入——图表内容按惯例保留英文，属正确行为；
      （提示词允许专名词保留英文，因此 suspect 只是提示，不是缺陷判定）
    - unseen：译文区域里原词和译名都没出现，多半是该术语恰好没被翻到。
    """
    import pymupdf

    entries, source = load_glossary_entries(glossary_path, output_dir)
    if not entries or not report["outputs"]:
        return None
    first_pdf = Path(output_dir) / report["outputs"][0]["file"]
    alternating = output_mode == "dual" and dual_layout == "alternating"
    parts: list[str] = []
    with pymupdf.open(first_pdf) as doc:
        for i in range(doc.page_count):
            if alternating:
                if i % 2 == 0:
                    continue  # 交替页:原样原文页不参与术语统计,避免原词虚报 suspect
                if (i // 2) not in in_scope:
                    continue
            elif i not in in_scope:
                continue
            parts.append(translated_page_text(doc, i, output_mode))
    full_text = squash_text("\n".join(parts))
    fig_rects = _figure_rects_on_output(captions or [], input_path or "", str(first_pdf), output_mode, in_scope, dual_layout) if (captions and input_path) else {}
    applied = 0
    suspect: list[str] = []
    unseen: list[str] = []
    figure_kept = 0
    for src, tgt in entries:
        if full_text.count(squash_text(tgt)):
            applied += 1
        elif full_text.count(squash_text(src)):
            if fig_rects and _term_only_inside_figures(first_pdf, src, fig_rects, output_mode, in_scope, dual_layout):
                figure_kept += 1
            else:
                suspect.append(src)
        else:
            unseen.append(src)
    return {
        "source": source,
        "terms": len(entries),
        "applied": applied,
        "suspect": len(suspect),
        "unseen": len(unseen),
        "figureKept": figure_kept,
        "suspectTerms": suspect[:20],
        "unseenTerms": unseen[:20],
    }


CAPTION_ORIGINAL = re.compile(r"^(figure|fig\.?|table)\s*(\d+)\s*[.:：]", re.I)
CAPTION_TRANSLATED = re.compile(r"^(图|圖|表)\s*(\d+)[：:]")


def _page_blocks_text(page, clip=None) -> list[str]:
    blocks = []
    for block in page.get_text("dict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        text = " ".join(span.get("text", "") for line in block.get("lines", []) for span in line.get("spans", [])).strip()
        if text:
            blocks.append(re.sub(r"\s+", " ", text))
    return blocks


REFERENCE_ENTRY_START = re.compile(r"^\s*\[\d{1,3}\]\s+\S")
REFERENCE_ENTRY_EVIDENCE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b|\b(?:doi|arxiv|corr|proceedings|journal|vol\.?|pages?|www\.)\b)",
    re.I,
)
# 作者-年份式条目(AAAI/ACL 等无编号格式):"Kemper, A.; and Neumann, T. 2015. ..."
REFERENCE_AUTHOR_YEAR_START = re.compile(r"^\s*[A-Z][A-Za-z'’\-]+,\s*[A-Z]\.")
# 名前姓后式:"A. Bai and C. Li. 2020. A study of ..."
REFERENCE_INITIAL_FIRST_START = re.compile(r"^\s*[A-Z]\.\s*[A-Z][A-Za-z'’\-]+")
# Nature/Science 式编号:"1. Bai, Q. et al. Proc. VLDB Endow. 16, 2911–2924 (2023)."
REFERENCE_NUMBER_DOT_START = re.compile(r"^\s*\d{1,3}\.\s+\S")
REFERENCE_ORG_YEAR_START = re.compile(r"^\s*[A-Z][\w\-]{2,39}\.\s*(?:19|20)\d{2}\b")
REFERENCE_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
REFERENCE_ET_AL = re.compile(r"\bet\s+al\.?", re.I)
REFERENCE_VENUE_EVIDENCE = re.compile(
    r"\b(?:proceedings|proc\b|journal|corr|arxiv|doi|press|retrieved|vol\.|no\.\s?\d|eds?\."
    r"|association|conference|symposium|transactions|technical report|thesis)\b"
    r"|\d+\s*\(\s*\d+\s*\)\s*[:.]?\s*\d|\d{2,5}\s*[–—-]\s*\d{2,5}",
    re.I,
)
# 跨栏/跨页合并会把多条文献并进一段;正文段不会出现两个以上"姓, 名."式作者列表
REFERENCE_MERGED_AUTHOR = re.compile(r"[.;]\s+[A-Z][A-Za-z'’\-]+,\s*[A-Z]\.")
STYLE_MARKUP = re.compile(r"<style\s+id=['\"]\d+['\"]>|</style>")


def paragraph_plain_text(paragraph) -> str:
    """去掉富文本标记后的段落纯文本,供参考文献/题注判定使用。"""
    return STYLE_MARKUP.sub("", (getattr(paragraph, "unicode", "") or "")).strip()


def is_protected_reference_paragraph(paragraph) -> bool:
    """识别应保留英文原文的参考文献条目，避免误伤普通正文开头的引用。

    覆盖五种版式:编号式([1] Author...)、Nature 式编号(1. Author...)、
    作者-年份式(Author, A. 2023. ...)、名前姓后式(A. Author. 2020. ...)、
    机构条目(PostgreSQL. 2025. ...),以及被跨栏合并成一段的多条目文本。
    """
    label = (getattr(paragraph, "layout_label", "") or "").strip().replace("_hybrid", "")
    if label == "reference":
        return True
    text = paragraph_plain_text(paragraph)
    if not text:
        return False
    if REFERENCE_ENTRY_START.match(text) and REFERENCE_ENTRY_EVIDENCE.search(text):
        return True
    if REFERENCE_YEAR.search(text):
        if REFERENCE_AUTHOR_YEAR_START.match(text) and REFERENCE_VENUE_EVIDENCE.search(text):
            return True
        if REFERENCE_ORG_YEAR_START.match(text):
            return True
        # 名前姓后式:"A. Bai and C. Li. 2020. ..."
        if REFERENCE_INITIAL_FIRST_START.match(text) and REFERENCE_VENUE_EVIDENCE.search(text):
            return True
        # Nature/Science 式编号:"1. Bai, Q. et al. Proc. VLDB ... (2023)."
        # 编号后紧跟作者特征(姓,名. / 名.姓 / et al.)才算条目,正文编号列表不误伤
        if REFERENCE_NUMBER_DOT_START.match(text):
            head = text[:56]
            if (
                REFERENCE_AUTHOR_YEAR_START.search(head)
                or REFERENCE_INITIAL_FIRST_START.search(head[3:])
                or REFERENCE_ET_AL.search(head)
            ) and REFERENCE_VENUE_EVIDENCE.search(text):
                return True
    if re.match(r"^\s*References\b", text) and REFERENCE_AUTHOR_YEAR_START.search(text):
        return True
    return bool(
        REFERENCE_AUTHOR_YEAR_START.match(text)
        and REFERENCE_YEAR.search(text)
        and len(REFERENCE_MERGED_AUTHOR.findall(text)) >= 2
    )


# 图表题注(Table 1: ... / 图 2 ...)始终翻译;表格单元格里的同名文本除外
CAPTION_TEXT_START = re.compile(r"^\s*(?:(?:Table|Figure|Listing|Algorithm|Chart)\s*\d|[图表]\s*\d)", re.I)
CAPTION_LAYOUTS = {"figure_caption", "table_caption", "caption"}
CELL_LIKE_LABELS = {"table_cell", "wired_table_cell", "wireless_table_cell", "figure_text", "table_text"}


def is_caption_paragraph(paragraph) -> bool:
    label = (getattr(paragraph, "layout_label", "") or "").strip().replace("_hybrid", "")
    if label in CAPTION_LAYOUTS:
        return True
    if label in CELL_LIKE_LABELS:
        return False
    return bool(CAPTION_TEXT_START.match(paragraph_plain_text(paragraph)))


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

    excluded: set[str] = {"reference", "reference_hybrid"}
    if not translate_figures:
        excluded |= {"figure_text", "figure_text_hybrid", "figure_title", "chart_title"}
    if not translate_tables:
        excluded |= {"table_cell", "table_cell_hybrid", "wired_table_cell", "wireless_table_cell", "table_text", "table_caption", "table_footnote"}
    original = getattr(lh, "is_text_layout_original", None) or lh.is_text_layout

    def is_text_layout_scoped(layout):
        name = (getattr(layout, "name", "") or "").strip()
        normalized = name.replace("_hybrid", "")
        if not original(layout):
            # 引擎白名单之外的题注混合版式(table_caption_hybrid 等)也放行,
            # 否则 Table 1 题注在版式层就被丢弃,段落实质永远轮不到豁免。
            return normalized in CAPTION_LAYOUTS
        # 题注版式(Table 1: .../Figure 2 ...)即使关闭图表翻译也要译出
        if normalized in CAPTION_LAYOUTS:
            return True
        return name not in excluded and normalized not in excluded

    is_text_layout_scoped._yiye_scoped = True
    lh.is_text_layout = is_text_layout_scoped
    lh.is_text_layout_original = original
    paragraph_finder.is_text_layout = is_text_layout_scoped
    paragraph_finder.is_text_layout_original = original

    # 第二层:il_translator 的段落循环按 layout_label 跳过被排除类别的段落
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
            scope_boxes = excluded_boxes_fn(page)
            removed: list[tuple[int, object]] = []
            kept = []
            for idx, paragraph in enumerate(page.pdf_paragraph):
                label = (getattr(paragraph, "layout_label", "") or "").strip()
                caption = is_caption_paragraph(paragraph)
                skip = is_protected_reference_paragraph(paragraph) or (
                    label.replace("_hybrid", "") in excluded and label not in ("figure_caption", "table_caption") and not caption
                )
                if not skip and not caption and label not in ("figure_caption", "table_caption") and scope_boxes and getattr(paragraph, "box", None) is not None:
                    pbox = paragraph.box
                    cx = (pbox.x + pbox.x2) / 2
                    cy = (pbox.y + pbox.y2) / 2
                    for scope_box in scope_boxes:
                        if scope_box.x <= cx <= scope_box.x2 and scope_box.y <= cy <= scope_box.y2:
                            skip = True
                            break
                if skip:
                    removed.append((idx, paragraph))
                else:
                    kept.append(paragraph)
            if removed:
                page.pdf_paragraph = kept
                print(f"翻译范围：跳过 {len(removed)} 个图内/表格/参考文献段落", flush=True)
            try:
                return original_process(self, page, executor, *args, **kwargs)
            finally:
                if removed:
                    for idx, paragraph in removed:
                        page.pdf_paragraph.insert(min(idx, len(page.pdf_paragraph)), paragraph)

        process_page_scoped._yiye_original = original_process
        process_page_scoped._yiye_scoped = True
        translator_cls.process_page = process_page_scoped
    except Exception as exc:
        print(f"翻译范围补丁(段落层)未生效（{exc}）", file=sys.stderr)

    # 图表版式框提取(两个包装共用)。表格检测到的文字有时仍被标成 fallback_line，
    # 因此关闭表格翻译时必须同时按 table 几何范围过滤。
    def excluded_boxes_fn(page):
        classes = set()
        if not translate_figures:
            classes |= {"figure", "chart"}
        if not translate_tables:
            classes.add("table")
        return [
            layout.box
            for layout in getattr(page, "page_layout", [])
            if (getattr(layout, "class_name", "") or "") in classes
            and getattr(layout, "box", None) is not None
        ]

    # LLM 引擎实际走 ILTranslatorLLMOnly(独立实现),同样包装
    try:
        llm_cls = getattr(it, "ILTranslatorLLMOnly", None)
        if llm_cls is None:
            import babeldoc.format.pdf.document_il.midend.il_translator_llm_only as itl

            llm_cls = itl.ILTranslatorLLMOnly

        # 跨页/跨栏翻译在 process_page 之前运行，也必须在共享筛选入口排除参考文献。
        original_should_translate = getattr(
            llm_cls._should_translate_paragraph,
            "_yiye_original",
            llm_cls._should_translate_paragraph,
        )

        def should_translate_scoped(self, paragraph, *args, **kwargs):
            if is_protected_reference_paragraph(paragraph):
                return False
            return original_should_translate(self, paragraph, *args, **kwargs)

        should_translate_scoped._yiye_original = original_should_translate
        llm_cls._should_translate_paragraph = should_translate_scoped

        original_llm_process = getattr(llm_cls.process_page, "_yiye_original", llm_cls.process_page)
        llm_params = list(inspect.signature(original_llm_process).parameters)
        if llm_params[:3] != ["self", "page", "executor"]:
            print("翻译范围补丁(LLM 段落层)跳过：引擎签名已变化", file=sys.stderr)
            return

        def llm_process_page_scoped(self, page, executor, *args, **kwargs):
            removed: list[tuple[int, object]] = []
            kept = []
            scope_boxes = excluded_boxes_fn(page)
            for idx, paragraph in enumerate(page.pdf_paragraph):
                label = (getattr(paragraph, "layout_label", "") or "").strip()
                caption = is_caption_paragraph(paragraph)
                skip = is_protected_reference_paragraph(paragraph) or (
                    label.replace("_hybrid", "") in excluded and label not in ("figure_caption", "table_caption") and not caption
                )
                if not skip and not caption and label not in ("figure_caption", "table_caption") and scope_boxes and getattr(paragraph, "box", None) is not None:
                    cx = (paragraph.box.x + paragraph.box.x2) / 2
                    cy = (paragraph.box.y + paragraph.box.y2) / 2
                    for scope_box in scope_boxes:
                        if scope_box.x <= cx <= scope_box.x2 and scope_box.y <= cy <= scope_box.y2:
                            skip = True
                            break
                if skip:
                    removed.append((idx, paragraph))
                else:
                    kept.append(paragraph)
            if removed:
                page.pdf_paragraph = kept
                print(f"翻译范围：跳过 {len(removed)} 个图内/表格/参考文献段落", flush=True)
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


def find_author_region(opage) -> "object | None":
    """在原文第 1 页定位作者区(姓名/单位/邮箱)矩形;找不到返回 None。

    与 restore_author_blocks / extract_paragraph_pairs 共用,保证"原样恢复"与
    "逐段对照跳过"两者对作者区的认定一致。
    """
    import pymupdf

    # get_text() 返回未旋转坐标,而 page.rect 在 90°/270° 旋转页交换宽高:
    # 旋转页上 top_limit 用错轴,作者区会整体错位,restore 会把原文覆盖到
    # 译文页的错误位置。横版首页直接跳过作者区恢复,宁可保留译文也不覆盖错
    if opage.rotation % 360 in (90, 270):
        return None

    top_limit = opage.rect.height * 0.45
    blocks = []
    for block in opage.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        spans = [s for line in block.get("lines", []) for s in line.get("spans", [])]
        text = " ".join(span.get("text", "") for span in spans).strip()
        rect = pymupdf.Rect(block["bbox"])
        if text and rect.y1 <= top_limit:
            blocks.append((rect, text, max((span.get("size", 0) for span in spans), default=0)))
    blocks.sort(key=lambda item: (item[0].y0, item[0].x0))

    region = None
    email_blocks = [item for item in blocks if "@" in item[1]]
    if email_blocks:
        region = pymupdf.Rect(email_blocks[0][0])
        for rect, _, _ in email_blocks[1:]:
            region |= rect
        # 邮箱块正上方最近的一行通常是作者姓名；只吸收这一行，避免把论文标题一起覆盖。
        # 标题是顶部区域最大字号的块:字号达到顶部最大字号 90% 的行不吸收
        # (紧凑版式下标题与邮箱间没有作者行,最近的"preceding"就是标题本身;
        #  作者名行字号介于 email 与标题之间,不受影响)
        email_size = max((item[2] for item in email_blocks), default=0.0)
        top_max_size = max((item[2] for item in blocks), default=0.0)
        title_size_floor = max(email_size, top_max_size * 0.9)
        preceding = [
            item for item in blocks
            if item[0].y1 <= region.y0 and region.y0 - item[0].y1 <= 40 and item[2] < title_size_floor
        ]
        if preceding:
            nearest_gap = min(region.y0 - item[0].y1 for item in preceding)
            for rect, _, _ in preceding:
                if region.y0 - rect.y1 <= nearest_gap + 3:
                    region |= rect
    else:
        abstract_blocks = [item for item in blocks if re.match(r"^abstract\b", item[1], re.IGNORECASE)]
        if not abstract_blocks:
            return None
        abstract_top = min(item[0].y0 for item in abstract_blocks)
        before_abstract = [
            item
            for item in blocks
            if item[0].y1 <= abstract_top and item[0].width >= item[0].height * 1.5
        ]
        if not before_abstract:
            return None
        title = max(before_abstract, key=lambda item: (item[2], item[0].width))
        title_size = title[2]
        # 与标题同字号的行是标题续行(如 "and Efficiency"),属于标题而非作者信息;
        # 只有明显小于标题字号的行(作者/单位)才纳入恢复区域,
        # 否则标题续行会被原文覆盖,出现"半中半英"的标题
        metadata = [
            item
            for item in before_abstract
            if item is not title
            and item[0].y0 >= title[0].y1 - 2
            and item[2] < title_size * 0.9
        ]
        if not metadata:
            # 少见版式:标题与作者行同字号,无法区分时回退为不过滤,保证作者区仍被恢复
            metadata = [item for item in before_abstract if item is not title and item[0].y0 >= title[0].y1 - 2]
        if not metadata:
            return None
        region = pymupdf.Rect(metadata[0][0])
        for rect, _, _ in metadata[1:]:
            region |= rect
    return pymupdf.Rect(
        max(region.x0 - 2, 0),
        max(region.y0 - 2, 0),
        min(region.x1 + 2, opage.rect.width),
        min(region.y1 + 2, opage.rect.height),
    )


def restore_author_blocks(translated_pdf: str, original_pdf: str, output_mode: str = "dual", dual_layout: str = "side") -> int:
    """把第 1 页作者区从原 PDF 原样覆盖到译文侧，保留姓名、单位、邮箱及字体版式。"""
    import pymupdf

    with pymupdf.open(original_pdf) as odoc, pymupdf.open(translated_pdf) as doc:
        if odoc.page_count == 0:
            return 1
        opage = odoc[0]
        region = find_author_region(opage)
        if region is None:
            return 1

        # 交替页:输出第 1 页是原样原文页,机器音译的作者区在输出第 2 页(整页译文)
        page_index = 1 if (output_mode == "dual" and dual_layout == "alternating") else 0
        if page_index >= doc.page_count:
            return 1
        tpage = doc[page_index]
        is_dual = tpage.rect.width >= opage.rect.width * 1.8
        target_page = translated_half_clip(tpage, "dual") if is_dual else tpage.rect
        sx, sy = target_page.width / opage.rect.width, target_page.height / opage.rect.height
        target = pymupdf.Rect(
            target_page.x0 + region.x0 * sx,
            target_page.y0 + region.y0 * sy,
            target_page.x0 + region.x1 * sx,
            target_page.y0 + region.y1 * sy,
        )
        tpage.add_redact_annot(target)
        tpage.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        tpage.show_pdf_page(target, odoc, 0, clip=region, keep_proportion=False, overlay=True)
        tmp_path = str(translated_pdf) + ".author-restore.tmp"
        doc.save(tmp_path)
    replaced = False
    for delay in (0.2, 0.5, 1.0, 2.0, 4.0):
        try:
            os.replace(tmp_path, translated_pdf)
            replaced = True
            break
        except PermissionError:
            time.sleep(delay)
    if not replaced:
        # 目标被占用(如浏览器正在预览该 PDF):写入备选交付名,不丢恢复结果
        fallback = str(Path(translated_pdf).with_name(Path(translated_pdf).stem + "-author-restored.pdf"))
        try:
            os.replace(tmp_path, fallback)
            print(f"YIYE_AUTHOR: 目标被占用,已写入备选文件 {Path(fallback).name}", flush=True)
            return 0
        except Exception as exc:
            print(f"YIYE_AUTHOR: 恢复结果写入失败（{exc}）", file=sys.stderr)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return 1
    print("YIYE_AUTHOR: 已原样恢复第 1 页作者区", flush=True)  # 交替页落在输出第 2 页
    return 0


def extract_captions(input_path: str, output_dir: str, output_mode: str, dual_layout: str = "side") -> list[dict]:
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
    alternating = output_mode == "dual" and dual_layout == "alternating"
    # 与质检同口径:只扫描主翻译产物,避免扫到校对/恢复/临时衍生文件
    pdfs = main_translated_pdfs(output_dir)
    mono = next((p for p in pdfs if p.name.lower().endswith(".mono.pdf")), None)
    if pdfs:
        with pymupdf.open(mono or pdfs[0]) as doc:
            for p, page in enumerate(doc):
                # translated_page_clip 兼容交替页的整页译文与译文溢出到左半的页
                clip = translated_page_clip(page, output_mode)
                for text in _page_blocks_text(page, clip):
                    m = CAPTION_TRANSLATED.match(text)
                    if m:
                        key = (m.group(1) in ("图", "圖"), int(m.group(2)))
                        # 交替页:译文在输出第 2N 页,换算回原文页号与原文侧命中同一坐标系
                        page_no = (p + 1) // 2 if (alternating and (p + 1) % 2 == 0) else p + 1
                        translated.setdefault(key, {"page": page_no, "translated": text[:300]})

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
    # 矢量图表的区域内有真实文字层:按版面行序提取"图中文字"(供图表速读按需翻译)。
    # 提取规则:行按 y 排序、同行按 x 拼接;过滤纯符号/单字符碎片;按行去重。
    # 位图无文字层,inner_text 为空,不在此处做 OCR。
    if input_path:
        with pymupdf.open(input_path) as doc:
            for entry in results:
                rect = entry.get("rect")
                page_no = entry.get("page")
                if not rect or not page_no or page_no > doc.page_count:
                    entry["innerText"] = ""
                    continue
                try:
                    raw = doc[page_no - 1].get_text("dict", clip=pymupdf.Rect(rect))
                except Exception:
                    entry["innerText"] = ""
                    continue
                line_entries = []
                for block in raw.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                        if not text:
                            continue
                        y0 = round(line["bbox"][1])
                        x0 = round(line["bbox"][0])
                        line_entries.append((y0, x0, text))
                line_entries.sort()
                merged = []
                for y0, _, text in line_entries:
                    if merged and abs(y0 - merged[-1][0]) <= 2:
                        merged[-1][1] += " " + text
                    else:
                        merged.append([y0, text])
                clean = []
                seen = set()
                for _, text in merged:
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(re.sub(r"[\W_]+", "", text, flags=re.UNICODE)) < 2:
                        continue  # 纯符号/单字符碎片(如 ❌ ✅ 单独成行)
                    if text in seen:
                        continue
                    seen.add(text)
                    clean.append(text)
                entry["innerText"] = "\n".join(clean)[:4000]
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
                    # 批量/术语提示词整段入缓存时(响应未解析成结构化文本),几千字符的
                    # 提示词会连同其回显混进语料,包含匹配会配出完全无关的译文
                    flat_original = re.sub(r"\s+", "", original)
                    if (
                        "heretheinput" in flat_original
                        or "translatethefollowingtext" in flat_original
                        or "multilingualterminologist" in flat_original
                    ):
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
    """块文本 ↔ 缓存匹配:精确 → 包含(块片段落在某条缓存原文内则返回该条译文)。

    语料按条目用 "\\n" 分隔且键已去除全部空白,跨条目的键必然在分隔符处
    find 失败,因此"跨条拼接"实际不可达 —— 行为上等价于单条包含匹配,
    多轮循环只是防御性写法。
    """
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


def make_cache_lookup(cache_map: dict[str, str], corpus: str, entries: list[tuple[int, str, str]]):
    """构造 块文本 → 缓存译文 的查找函数。

    短块(标题/页眉/侧栏文字,如 "Abstract")只允许精确命中;包含匹配要求键足够长,
    否则短词会命中任意含该词的缓存条目,把无关译文配给短标题。
    """

    def lookup(norm_key: str) -> str | None:
        if len(norm_key) < 8:
            return None
        exact = cache_map.get(norm_key)
        if exact:
            return exact
        if len(norm_key) < 24:
            return None
        return _cache_match(corpus, entries, norm_key)

    return lookup


def _looks_two_column(center_xs, page_width: float) -> bool:
    """启发式判断版面是否双栏:块中心同时出现在页宽 35% 线两侧之外。

    单栏版面的通栏段落/标题中心贴近页中线,不会两侧同时命中;
    双栏版面的正文块中心分别聚集在两栏中线附近。
    """
    left = any(cx < page_width * 0.35 for cx in center_xs)
    right = any(cx > page_width * 0.65 for cx in center_xs)
    return left and right


def extract_paragraph_pairs(input_path: str, output_dir: str, output_mode: str, max_pages: int = 200, dual_layout: str = "side") -> list[dict]:
    """逐页配对原文段落与译文段落,生成逐段对照阅读数据(对标 PDF Pro 的段落对照)。

    配对策略:同页内两侧块各按"栏位+纵坐标"排序。块数相等时按位置配对并用
    BabelDOC 缓存校正;块数不等但存在缓存命中时只保留确定配对,其余以空对照
    占位,避免图注/跨栏标题插入后把整页后续段落错配。
    第 1 页作者区不参与配对:作者区被 restore_author_blocks 原样恢复为英文,
    译文侧读到的仍是音译/重排碎片,位置配对会把机构名错配到标题或摘要上。
    交替页布局:原文页 i 与输出第 2i+1 页(0-based)配对,译文整页保留原版
    分栏,两侧都按双栏排序。
    """
    import pymupdf

    def in_region(x0, y0, x1, y1, rects) -> bool:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return any(rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1 for rect in rects)

    def block_rows(page, clip=None, single_column=False, mid_override=None, skip_rects=()):
        rect = page.rect
        mid = mid_override if mid_override is not None else rect.width / 2
        out = []
        center_xs: list[float] = []
        for b in page.get_text("dict", clip=clip).get("blocks", []):
            if b.get("type") != 0:
                continue
            text = " ".join(s.get("text", "") for l in b.get("lines", []) for s in l.get("spans", [])).strip()
            if len(text) < 2:
                continue
            x0, y0, x1 = b["bbox"][0], b["bbox"][1], b["bbox"][2]
            if in_region(x0, y0, x1, b["bbox"][3], skip_rects):
                continue
            center_xs.append((x0 + x1) / 2)
            column = 1 if (single_column or (x0 + x1) / 2 >= mid) else 0
            out.append((column, round(y0, 1), round(x0, 1), re.sub(r"\s+", " ", text)))
        return out, center_xs

    def blocks(page, clip=None, single_column=False, mid_override=None, skip_rects=()):
        out, _ = block_rows(page, clip, single_column, mid_override, skip_rects)
        out.sort()
        return [t for _, _, _, t in out]

    pages = []
    # 与质检同口径:只取主翻译产物,避免抓到校对写回/修订版等衍生文件
    pdfs = main_translated_pdfs(output_dir)
    mono = next((p for p in pdfs if p.name.lower().endswith(".mono.pdf")), None)
    if not pdfs:
        return pages
    # 翻译缓存是"原文段落 → 译文段落"的精确对照,用它校正位置配对的错位
    cache_map = _load_translation_cache_map()
    corpus, entries = _build_corpus_index(cache_map)
    cache_lookup = make_cache_lookup(cache_map, corpus, entries)
    alternating = output_mode == "dual" and dual_layout == "alternating"
    with pymupdf.open(input_path) as src, pymupdf.open(mono or pdfs[0]) as dst:
        # 交替页译文在输出第 2i+1 页,原文页数以 dst.page_count//2 为上界,
        # 防止产物页数异常(分批合并失败/被截断)时索引越界
        count = (min(src.page_count, dst.page_count // 2, max_pages) if alternating
                 else min(src.page_count, dst.page_count, max_pages))
        for i in range(count):
            # 第 1 页:作者区在原文页与译文半页各算一份(坐标按半页缩放),两侧都跳过
            # 交替页:译文在输出第 2i+1 页(0-based),整页保留原版分栏
            tpage = dst[2 * i + 1] if alternating else dst[i]
            en_skip, zh_skip = (), ()
            if i == 0:
                region = find_author_region(src[0])
                if region is not None:
                    en_skip = (region,)
                    tclip = translated_half_clip(tpage, output_mode)
                    is_dual = tpage.rect.width >= src[0].rect.width * 1.8
                    if is_dual and tclip is not None:
                        target_page = tclip
                    else:
                        target_page = tpage.rect
                    sx = target_page.width / src[0].rect.width
                    sy = target_page.height / src[0].rect.height
                    zh_skip = (
                        pymupdf.Rect(
                            target_page.x0 + region.x0 * sx,
                            target_page.y0 + region.y0 * sy,
                            target_page.x0 + region.x1 * sx,
                            target_page.y0 + region.y1 * sy,
                        ),
                    )
            en_rows, en_centers = block_rows(src[i], skip_rects=en_skip)
            en = [t for _, _, _, t in sorted(en_rows)]
            if alternating:
                zh = blocks(tpage, None, False, skip_rects=zh_skip)
            else:
                clip = translated_half_clip(tpage, output_mode)
                if clip is None:
                    zh = blocks(tpage, None, False, skip_rects=zh_skip)
                else:
                    # 译文半页整体位于页面一侧,若按整页中线分栏,半页内所有块
                    # 都会落进同一栏,排序退化成纯纵向,与原文侧"栏位优先"的
                    # 顺序不一致 —— 双栏论文会出现 L1,R1,L2,R2 对 L1,L2,R1,R2
                    # 的系统性错位配对。原文侧为双栏时,分栏中线取译文半页自身
                    # 的中点,恢复与原文侧一致的 (栏, y) 排序;单栏原文的半页
                    # 译文通栏排布,仍按纯纵向排序。
                    two_column = _looks_two_column(en_centers, src[i].rect.width)
                    zh = blocks(tpage, clip, not two_column, mid_override=(clip.x0 + clip.x1) / 2, skip_rects=zh_skip)
            pairs = []
            cached_translations = [cache_lookup(re.sub(r"\s+", "", en_text)) for en_text in en]
            # 第 1 页存在作者区时块组成两侧差异大(标题通栏/侧栏文字/恢复的英文作者区),
            # 位置配对必然错位,强制保守模式:只保留缓存确认的配对,其余单侧占位
            conservative = (len(en) != len(zh) and any(cached_translations)) or (i == 0 and bool(zh_skip))
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
    is_figure 只决定调用方的展示分组:表格题注走同一聚类(表格框线属
    get_drawings,实测同样能生成 fig-t{N}.png 裁剪),并非"表格不生成裁剪"。
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
                # 图 N 与 表 N 的 num 各自独立计数,同名会互相覆盖缩略图
                name = f"fig-{c['num']}.png" if c["kind"] == "图" else f"fig-t{c['num']}.png"
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
        n_old = norm(old_text) if old_text else ""
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            raw_text = "".join(s.get("text", "") for l in block.get("lines", []) for s in l.get("spans", []))
            n_text = norm(raw_text)
            if not n_text:
                continue
            l_text = loose(raw_text)
            # 反向包含(块 ⊂ 目标文本)只接受足够长的块,
            # 否则页码"2"、编号"1."这类碎块会误中并被整段新译文覆盖
            if n_old and (n_old in n_text or (len(n_text) >= 8 and n_text in n_old)):
                target = block
                target_raw = raw_text
                break
            if not target and loose_old and (loose_old in l_text or (len(l_text) >= 8 and l_text in loose_old)):
                target = block
                target_raw = raw_text
        if target is None:
            print("apply: 未找到目标段落（该段落可能已被之前的写回修改过，请改用「重译此页」生成整页新译文）", file=sys.stderr)
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
        # 累积写回时源就是目标(以 adjusted-output.pdf 为源再写同名文件):
        # pymupdf 拒绝非增量的"保存到原路径",必须另存临时文件再原子替换;
        # 目标被阅读器占用时重试,仍失败写备选名,不丢本次修改
        same_target = os.path.abspath(out_path) == os.path.abspath(translated_pdf)
        if same_target:
            doc.save(str(out_path) + ".apply.tmp", garbage=3, deflate=True)
        else:
            doc.save(out_path, garbage=3, deflate=True)
    if os.path.abspath(out_path) == os.path.abspath(translated_pdf):
        tmp_path = str(out_path) + ".apply.tmp"
        replaced = False
        for delay in (0.2, 0.5, 1.0, 2.0, 4.0):
            try:
                os.replace(tmp_path, out_path)
                replaced = True
                break
            except PermissionError:
                time.sleep(delay)
        if not replaced:
            fallback = str(Path(out_path).with_name(Path(out_path).stem + "-apply-new.pdf"))
            try:
                os.replace(tmp_path, fallback)
                print(f"apply: 目标被占用,已写入备选文件 {Path(fallback).name}", file=sys.stderr)
            except Exception as exc:
                print(f"apply: 写回失败（{exc}）", file=sys.stderr)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return 1
    print("YIYE_APPLY: ok", flush=True)
    return 0


def patch_placeholder_batch_isolation() -> None:
    """含 {vN} 占位符的段落用小批次翻译,从源头减少占位符丢失。

    BabelDOC 按"200 token 或 5 段"打包批次,多段一起翻译时注意力稀释,
    模型偶发丢弃 {vN} 占位符(公式/引用随之丢失)。给占位符段落的 token 计数
    加权后,批次在遇到占位符段落时立即收口,其后新开小批次;
    顺带提高这类段落的翻译优先级。不影响无占位符段落。
    """
    import babeldoc.format.pdf.document_il.midend.il_translator_llm_only as itl

    original = itl.ILTranslatorLLMOnly.calc_token_count
    if getattr(original, "_yiye_placeholder_isolation", False):
        return

    def patched(self, text):
        count = original(self, text)
        if isinstance(text, str) and "{v" in text:
            count = max(count, 400)
        return count

    patched._yiye_placeholder_isolation = True
    patched._yiye_original = original
    itl.ILTranslatorLLMOnly.calc_token_count = patched


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


def patch_typesetting_fidelity() -> None:
    """字号保真:引擎要缩字号时,先尝试用更紧凑的行距在原字号下重排。

    BabelDOC 的兜底策略是逐级缩小字号(scale 1.0 → 0.9 → …)把译文塞回原段落盒,
    行距 1.3 倍仍高于原文的紧凑行距(约 1.15 倍),导致"系统环境"这类段落在原文
    本就放得下的盒子里被缩到 0.9,与相邻正文形成明显的字号差。
    这里在引擎缩字号之前,用 1.25/1.18/1.1 倍行距逐档试排;仍放不下才回退引擎缩放。
    引擎签名变化时静默跳过。
    """
    try:
        from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraphComposition
        from babeldoc.format.pdf.document_il.midend import typesetting as ts_mod

        original_find = ts_mod.Typesetting._find_optimal_scale_and_layout
        if getattr(original_find, "_yiye_fidelity_patch", False):
            return
        params = list(inspect.signature(original_find).parameters)
        if params[:7] != ["self", "paragraph", "page", "typesetting_units", "initial_scale", "use_english_line_break", "apply_layout"]:
            print("排版保真补丁跳过：引擎函数签名已变化", flush=True)
            return

        tight_line_skips = (1.25, 1.18, 1.1)

        def try_tight_layout(self, paragraph, page, typesetting_units, scale, use_english_line_break, box=None):
            box = box or paragraph.box
            if box is None or not typesetting_units:
                return None
            for line_skip in tight_line_skips:
                try:
                    typeset_units, all_fit = self._layout_typesetting_units(
                        typesetting_units,
                        box,
                        scale,
                        line_skip,
                        paragraph,
                        use_english_line_break,
                    )
                except Exception:
                    continue
                if all_fit and typeset_units:
                    return typeset_units
            return None

        def expanded_box_down(self, paragraph, page):
            """段落盒向下扩展后的新 Box(延伸到下一个内容块之前,限幅一倍盒高)。

            引擎 get_max_bottom_space 返回盒下方可达的最低 y(绝对坐标),+2 留边距;
            腾不出一行以上空间时返回 None。
            """
            from babeldoc.format.pdf.document_il.il_version_1 import Box

            box = paragraph.box
            if box is None:
                return None
            try:
                min_y = self.get_max_bottom_space(box, page) + 2
            except Exception:
                return None
            if min_y >= box.y:
                return None
            extra = box.y - min_y
            box_height = box.y2 - box.y
            if extra > box_height * 1.0:
                min_y = box.y - box_height * 1.0
            if box.y - min_y < 6.0:
                return None
            return Box(x=box.x, y=min_y, x2=box.x2, y2=box.y2)

        def apply_typeset_units(self, paragraph, page, typeset_units, scale, box=None):
            # 与引擎 _find_optimal_scale_and_layout 的 apply_layout 分支保持一致;
            # 盒向下扩展时同步回写 paragraph.box(与引擎自身的扩盒逻辑一致)
            paragraph.scale = scale
            if box is not None:
                paragraph.box = box
            paragraph.pdf_paragraph_composition = []
            for unit in typeset_units:
                chars, curves, forms = unit.render()
                for char in chars:
                    paragraph.pdf_paragraph_composition.append(PdfParagraphComposition(pdf_character=char))
                for curve in curves:
                    page.pdf_curve.append(curve)
                for form in forms:
                    page.pdf_form.append(form)

        def patched_find(self, paragraph, page, typesetting_units, initial_scale=1.0, use_english_line_break=True, apply_layout=False):
            try:
                probe_scale, _ = original_find(self, paragraph, page, typesetting_units, initial_scale, use_english_line_break, False)
            except Exception:
                return original_find(self, paragraph, page, typesetting_units, initial_scale, use_english_line_break, apply_layout)
            if probe_scale >= 0.995:
                return original_find(self, paragraph, page, typesetting_units, initial_scale, use_english_line_break, apply_layout)
            # 引擎会缩小字号:先试"原字号 + 紧凑行距",再试"原字号 + 向下扩盒"
            # (下方有空隙时保持原字号,正文字号不再忽大忽小),都放不下才回退缩字号
            candidates = []
            try:
                # 注意 expanded_box_down 是本补丁内的局部函数,不是引擎方法;
                # 走 self. 属性查找会 AttributeError 被吞,策略静默失效
                expanded = expanded_box_down(self, paragraph, page)
            except Exception:
                expanded = None
            if expanded is not None:
                candidates.append(expanded)
            if paragraph.box is not None:
                candidates.append(paragraph.box)
            for box in candidates:
                for scale_try in dict.fromkeys([1.0 if initial_scale < 0.995 else initial_scale, initial_scale]):
                    tight = try_tight_layout(self, paragraph, page, typesetting_units, scale_try, use_english_line_break, box)
                    if tight is not None:
                        if apply_layout:
                            apply_typeset_units(self, paragraph, page, tight, scale_try, box)
                        return scale_try, tight
            return original_find(self, paragraph, page, typesetting_units, initial_scale, use_english_line_break, apply_layout)

        patched_find._yiye_fidelity_patch = True
        patched_find._yiye_original = original_find
        ts_mod.Typesetting._find_optimal_scale_and_layout = patched_find
    except Exception as exc:
        print(f"排版保真补丁未生效（{exc}），按引擎默认缩放排版", flush=True)


def build_pdf_outline(translated_pdf: str, output_mode: str = "dual", dual_layout: str = "side") -> int:
    """从译文版式收集章节标题,写入 PDF 书签(阅读器目录导航)。

    标题启发式:字号 ≥ 页面主导字号 ×1.12、单行 ≤60 字、位于上下 8% 页眉页脚带之外;
    层级按相对字号分三档。同页相邻的同级碎行合并为一条。
    交替页布局只扫译文页(偶数输出页)。
    """
    import pymupdf
    from collections import Counter

    src = Path(translated_pdf).resolve()
    if not _is_under(src, _allowed_roots()):
        print("outline: 路径超出允许范围", file=sys.stderr)
        return 2
    alternating = output_mode == "dual" and dual_layout == "alternating"

    per_page = []
    dominant_sizes: Counter = Counter()
    with pymupdf.open(src) as doc:
        for pno, page in enumerate(doc):
            # 交替页布局:偶数输出位(0-based)是原样原文页,只扫译文页,
            # 否则英文原文标题会混入候选形成中英重复书签
            if alternating and pno % 2 == 0:
                per_page.append([])
                continue
            clip = translated_page_clip(page, output_mode)
            entries = []
            sizes: Counter = Counter()
            # get_text 是未旋转坐标,cropbox 才与之一致(90°/270° 页 rect 会交换宽高)
            height = page.cropbox.height or page.rect.height
            for block in page.get_text("dict", clip=clip).get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if not text:
                        continue
                    # PDF 内部空格字形常被 ToUnicode 映射成 € 或 NBSP 伪影,
                    # 书签/合并显示都会带错字,统一还原为普通空格
                    text = text.replace("\u20ac", " ").replace("\u00a0", " ").strip()
                    if not text:
                        continue
                    spans = line.get("spans", [])
                    size = max((span.get("size", 0.0) for span in spans), default=0.0)
                    bold = any(span.get("flags", 0) & 16 for span in spans)
                    # 等宽占比按字符数计:代码行常是"中文字体+等宽变量"混排,
                    # all() 判定会被中文 span 破坏
                    mono_chars = sum(len(s.get("text", "")) for s in spans
                                     if "mon" in s.get("font", "").lower() or "courier" in s.get("font", "").lower() or "consol" in s.get("font", "").lower() or "menlo" in s.get("font", "").lower())
                    mono = mono_chars * 2 >= len(text)
                    y_ratio = line["bbox"][1] / height
                    sizes[round(size, 1)] += len(text)
                    entries.append((line["bbox"][1], size, y_ratio, text, bold, mono))
            per_page.append(entries)
            dominant_sizes.update(sizes)
        body_size = dominant_sizes.most_common(1)[0][0] if dominant_sizes else 0.0
        if body_size <= 0:
            print("书签：未找到正文基准字号，跳过", flush=True)
            return 1

        # 收集候选标题并做"同页相邻碎行"合并。
        # 双信号判定:纯"字号≥正文×1.12"对 IEEE/ICDE/ACL 系双栏模板结构性失效
        # (章节标题字号≤正文,实测整篇 0 候选),补充加粗+编号/章节词信号:
        # - 字号信号:大字号行(≥正文 1.12 倍)
        # - 加粗信号:pymupdf flags bit4(粗体) + 短行 + 编号模式或章节词/全大写英文
        # 页眉带(顶部 8%)只滤 running header;第 1 页顶部恰是论文标题,放宽到 2%。
        SECTION_ZH = ("摘要", "引言", "概述", "背景", "相关工作", "方法", "模型", "实验", "评估", "结果", "分析", "讨论", "结论", "局限", "展望", "致谢", "参考文献")
        SECTION_EN = ("abstract", "introduction", "background", "related work", "method", "approach", "experiments", "evaluation", "results", "analysis", "discussion", "conclusion", "limitations", "acknowledg", "references")

        def is_section_numbering(text: str) -> bool:
            return bool(re.match(r"^(\d+(\.\d+)*|[IVX]+)\s*[.、)）]", text) or re.match(r"^第[一二三四五六七八九十]+\s*[章节部分]", text))

        def bold_section_hit(text: str, bold: bool, mono: bool) -> bool:
            # 等宽字体是代码块特征(NimbusMono/Courier/Consolas),整行大写的
            # SQL/代码会被当成"全大写标题",直接排除
            if not bold or mono or not text or len(text) > 50:
                return False
            if is_section_numbering(text):
                return True
            # 代码关键字开头(译文页的 SQL/代码行常通篇大写粗体)不作为标题
            if re.match(r"^(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|WHERE|FROM|GROUP|ORDER|SET|WITH|VALUES|BEGIN|END|IF|ELSE|FOR|WHILE|RETURN|IMPORT|DEF|CLASS|FUNCTION|VAR|LET|CONST)\b", text):
                return False
            # 全大写英文标题(IEEE 系常见);排除 SQL/代码行(含代码标点或词数过多)
            if re.match(r"^[A-Z0-9][A-Z0-9 .\-/&:]{2,40}$", text) and sum(c.isalpha() for c in text) >= 3:
                return len(text.split()) <= 8 and not any(ch in text for ch in ";=()[]{}")
            stripped = re.sub(r"[。.:：\s]+$", "", text).lower()
            # 章节词命中要求标题形态:短、无句内标点 —— 否则"相关工作：xxx"开头的
            # 正文段落会被整段误判为章节标题
            if any(stripped.startswith(w) for w in SECTION_ZH):
                return len(stripped) <= 30 and "，" not in text and "、" not in text
            if any(stripped.startswith(w) for w in SECTION_EN):
                return len(stripped) <= 40 and "," not in text
            return False

        headlines = []
        for pno, entries in enumerate(per_page):
            page_headlines = []
            floor_ratio = 0.02 if pno == 0 else 0.08  # 第 1 页顶部是论文标题,不是页眉
            for y0, size, y_ratio, text, bold, mono in entries:
                if len(text) > 60 or y_ratio < floor_ratio or y_ratio > 0.95:
                    continue
                if re.match(r"^\d+[\s.]*$", text):
                    continue
                size_hit = size >= body_size * 1.12
                bold_hit = bold_section_hit(text, bold, mono)
                if not size_hit and not bold_hit:
                    continue
                ratio = size / body_size if body_size else 0
                if is_section_numbering(text):
                    level = 3 if re.match(r"^\d+\.\d+", text) else 2
                elif size_hit:
                    level = 1 if ratio >= 1.45 else 2 if ratio >= 1.2 else 3
                else:
                    level = 2
                if page_headlines and page_headlines[-1]["level"] == level and y0 - page_headlines[-1]["y2"] <= size * 0.8 and len(page_headlines[-1]["title"]) + len(text) <= 80:
                    prev = page_headlines[-1]["title"]
                    # 拼接边界补空格:PDF 提取的空格常呈 € 等伪影,两侧都是 ASCII 时不补会粘连
                    joiner = " " if prev and text and prev[-1].isascii() and prev[-1].isalnum() and text[0].isascii() and text[0].isalnum() else ""
                    page_headlines[-1]["title"] = prev + joiner + text
                    page_headlines[-1]["y2"] = y0 + size
                    continue
                page_headlines.append({"level": level, "title": text, "page": pno + 1, "y2": y0 + size, "y0": y0})
            headlines.extend(page_headlines)

        # 页眉/侧栏类重复标题:相同文字出现在 3 页以上时丢弃
        repeated: Counter = Counter(h["title"] for h in headlines)
        headlines = [h for h in headlines if repeated[h["title"]] < 3]
        # 学术版面噪声过滤:
        # - arXiv/IDs 类侧栏(含 5 位以上连续数字)
        # - 断裂碎片(无中文且以小写字母开头)
        # - 作者单位列表(多处"单词+上标数字")
        def is_noise(title: str) -> bool:
            if re.search(r"\d{5,}", title):
                return True
            if not re.search(r"[\u4e00-\u9fff]", title) and re.match(r"^[a-z]", title):
                return True
            if len(re.findall(r"[A-Za-z]{2,}\d", title)) >= 2:
                return True
            if title.startswith(("•", "·", "‣", "▪", "- ")):
                return True
            return False

        headlines = [h for h in headlines if not is_noise(h["title"])]
        if not headlines:
            print("书签：未识别到章节标题，跳过", flush=True)
            return 1
        # 单候选也写入(整篇书签为 0 比只有论文标题一条更不可用)
        # pymupdf 约束:首条必须 1 级,相邻层级落差 ≤1
        toc = []
        prev_level = 0
        for h in headlines:
            level = h["level"] if toc else 1
            level = min(level, prev_level + 1) if toc else 1
            toc.append([level, h["title"], h["page"]])
            prev_level = level
        doc.set_toc(toc)
        tmp_path = str(src) + ".outline.tmp"
        doc.save(tmp_path, garbage=3, deflate=True)
    # 交付 PDF 可能被阅读器占用:重试后仍失败写备选名,不丢书签结果
    replaced = False
    for delay in (0.2, 0.5, 1.0, 2.0, 4.0):
        try:
            os.replace(tmp_path, src)
            replaced = True
            break
        except PermissionError:
            time.sleep(delay)
    if not replaced:
        fallback = src.with_name(src.stem + "-outline-new.pdf")
        try:
            os.replace(tmp_path, fallback)
            print(f"书签：目标被占用，已写入备选文件 {fallback.name}", flush=True)
            return 0
        except Exception as exc:
            print(f"书签：写入失败（{exc}）", file=sys.stderr)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return 1
    print(f"书签：已写入 {len(headlines)} 条章节书签", flush=True)
    return 0


def revise_pdf(translated_pdf: str, original_pdf: str, out_path: str, keep_original: list[int], output_mode: str = "mono", dual_layout: str = "side") -> int:
    """生成修订版 PDF:keep_original 中的页(1-based)取原文,其余页取译文。

    用于质检发现问题页后的"本页保留原文"回退 —— 人工核对后的最终交付物。
    交替页对照(alternating)下输出是"原文页+译文页"成对出现:保留原文时,
    该原文页对应的译文页(第 2N 页)也替换为原文页,页数与配对结构保持不变。
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
    alternating = output_mode == "dual" and dual_layout == "alternating"
    with pymupdf.open(src_t) as translated, pymupdf.open(src_o) as original:
        expected = original.page_count * 2 if alternating else original.page_count
        if translated.page_count != expected:
            print(f"revise: 译文页数 {translated.page_count} 与预期 {expected} 不一致，无法生成修订版", file=sys.stderr)
            return 2
        if not keep or any(p < 1 or p > original.page_count for p in keep):
            print("revise: 页码超出原文范围", file=sys.stderr)
            return 2
        revised = pymupdf.open()
        if alternating:
            for out_i in range(translated.page_count):
                orig_n = out_i // 2 + 1
                if (out_i + 1) % 2 == 1:
                    # 奇数位输出本来就是原文页
                    revised.insert_pdf(translated, from_page=out_i, to_page=out_i)
                elif orig_n in keep:
                    # 保留原文:该页的译文页也替换为原文页
                    revised.insert_pdf(original, from_page=orig_n - 1, to_page=orig_n - 1)
                else:
                    revised.insert_pdf(translated, from_page=out_i, to_page=out_i)
        else:
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

    def require_key(name: str):
        # 缺键时给出干净的中文报错并以退出码 2 结束,
        # 而不是裸 KeyError traceback(server 端 errorFromLog 提取质量差)
        value = payload.get(name)
        if value is None:
            print(f"{mode}: 请求缺少必填字段 {name}", file=sys.stderr)
            raise SystemExit(2)
        return value

    mode = payload.get("mode")
    if mode == "estimate":
        result = estimate_pdf(require_key("pdfPath"), bool(payload.get("ocr", True)))
        print("YIYE_ESTIMATE: " + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    if mode == "scan-watermark":
        pdf_path = str(Path(require_key("pdfPath")).resolve())
        if not _is_under(Path(pdf_path), _allowed_roots()):
            print("scan-watermark: 路径超出允许范围", file=sys.stderr)
            return 2
        suspects = detect_watermark_suspects(pdf_path)
        print("YIYE_WATERMARK: " + json.dumps({"suspects": suspects}, ensure_ascii=False), flush=True)
        return 0
    if mode == "strip-watermark":
        pdf_path = str(Path(require_key("pdfPath")).resolve())
        out_path = str(Path(require_key("outPath")).resolve())
        if not all(_is_under(Path(p), _allowed_roots()) for p in (pdf_path, out_path)):
            print("strip-watermark: 路径超出允许范围", file=sys.stderr)
            return 2
        removed = strip_watermarks(pdf_path, payload.get("suspects") or [], out_path)
        print("YIYE_WATERMARK: " + json.dumps({"removed": removed, "out": out_path}, ensure_ascii=False), flush=True)
        print("YIYE_STAGE: watermark stripped", flush=True)
        return 0
    if mode == "replace-page":
        source_pdf = str(Path(require_key("sourcePdf")).resolve())
        temp_pdf = str(Path(require_key("tempPdf")).resolve())
        out_path = str(Path(require_key("outPath")).resolve())
        page_index = int(require_key("pageIndex"))
        if not all(_is_under(Path(p), _allowed_roots()) for p in (source_pdf, temp_pdf, out_path)):
            print("replace-page: 路径超出允许范围", file=sys.stderr)
            return 2
        import pymupdf

        with pymupdf.open(source_pdf) as doc, pymupdf.open(temp_pdf) as temp:
            if page_index < 0 or page_index >= doc.page_count:
                print("replace-page: 页码超出译文范围", file=sys.stderr)
                return 3
            if page_index >= temp.page_count:
                print("replace-page: 重译输出缺少对应页", file=sys.stderr)
                return 3
            # 删除旧页后在同一位置插入重译页,其余页保持原样
            doc.delete_page(page_index)
            doc.insert_pdf(temp, from_page=page_index, to_page=page_index, start_at=page_index)
            doc.save(out_path, garbage=3, deflate=True)
        print("YIYE_REPLACE_PAGE: ok", flush=True)
        return 0
    if mode == "render":
        code = render_page(require_key("pdfPath"), require_key("outPath"), int(payload.get("page", 0)))
        if code != 0:
            return code
        print("YIYE_STAGE: rendered preview page", flush=True)
        return 0
    if mode == "revise":
        code = revise_pdf(
            require_key("translatedPdf"),
            require_key("originalPdf"),
            require_key("outPath"),
            payload.get("keepOriginal", []),
            payload.get("outputMode", "mono"),
            payload.get("dualLayout", "side"),
        )
        if code != 0:
            return code
        print("YIYE_STAGE: revised pdf written", flush=True)
        return 0
    if mode == "apply_paragraph":
        code = apply_paragraph_fix(
            require_key("translatedPdf"),
            require_key("outPath"),
            int(require_key("page")),
            require_key("oldText"),
            require_key("newText"),
        )
        if code != 0:
            return code
        print("YIYE_STAGE: paragraph applied", flush=True)
        return 0
    if mode != "translate":
        print("unknown mode", file=sys.stderr)
        return 2

    request_path = Path(require_key("requestPath")).resolve()
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

    # 水印移除:用户在提交前已检测并勾选确认的重复文本,翻译前在派生副本上擦除。
    # 原始上传文件保持原样;后续质检/逐段对照/阅读器原文视图都指向无水印副本,保持一致。
    watermark_suspects = request["config"].get("watermarkSuspects") or []
    if watermark_suspects:
        src = Path(request["inputPath"])
        work_dir = src.parent / "watermark-work"
        work_dir.mkdir(exist_ok=True)
        cleaned = work_dir / src.name
        try:
            removed = strip_watermarks(request["inputPath"], watermark_suspects, str(cleaned))
            if removed:
                request["inputPath"] = str(cleaned)
                print(f"水印移除：已擦除 {removed} 处确认文本（原文件未改动）", flush=True)
            else:
                print("水印移除：未匹配到确认文本，按原文件翻译", flush=True)
        except Exception as exc:
            print(f"水印移除失败（{exc}），按原文件翻译", flush=True)

    from babeldoc.main import cli

    # 补丁目标直取 BabelDOC 内部属性,引擎升级后任一属性改名/移位都会让
    # AttributeError 在 cli() 之前崩掉整个任务。统一兜底:单个补丁失败只
    # 降级(失去对应增强),不阻断翻译,与排版补丁内部的分层 try/except 一致
    for apply in (
        lambda: patch_progress_output(),
        lambda: patch_layout_translation_scope(translate_figures=bool(request["config"].get("figure")), translate_tables=bool(request["config"].get("table"))),
        lambda: patch_translation_integrity(),
        lambda: patch_forced_list_line_breaks(),
        lambda: patch_cjk_line_spacing(),
        lambda: patch_typesetting_fidelity(),
        lambda: patch_auto_glossary_cleanup(),
        lambda: patch_placeholder_batch_isolation(),
    ):
        try:
            apply()
        except Exception as exc:
            print(f"YIYE_WARN: 引擎补丁加载失败（{exc.__class__.__name__}: {exc}），对应增强已跳过", file=sys.stderr, flush=True)
    sys.argv = build_args(request, api_key)
    print("YIYE_STAGE: starting BabelDOC", flush=True)
    cli()
    # 章节书签:从译文版式收集标题写入 PDF 书签(阅读器目录导航);失败不影响任务
    try:
        out_pdfs = main_translated_pdfs(request["outputDir"])
        if out_pdfs:
            build_pdf_outline(str(out_pdfs[0]), request["config"]["output"], request["config"].get("dualLayout", "side"))
    except Exception as exc:
        print(f"书签生成失败（{exc}），已跳过", flush=True)
    # 图注对照提取(图表速读)提前:术语质检需要图表区域来排除"图内正确保留英文"的词
    captions: list[dict] = []
    try:
        captions = extract_captions(request["inputPath"], request["outputDir"], request["config"]["output"], dual_layout=request["config"].get("dualLayout", "side"))
        if captions:
            generated = render_figure_crops(request["inputPath"], request["outputDir"], captions)
            (Path(request["outputDir"]) / "figures.json").write_text(json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"图表速读：{len(captions)} 条图注，{generated} 张缩略图", flush=True)
    except Exception as exc:
        captions = []
        print(f"图注提取失败（{exc}），已跳过", flush=True)
    # 题注补译:表格检测框覆盖题注时,题注段不会进入翻译管线(实测 Table 2/4 被吞),
    # 这里在成品 PDF 上补译仍为英文的图表题注;失败不影响任务
    try:
        rescued = rescue_untranslated_captions(request, api_key)
        # 补译写回 PDF 后同步刷新 figures.json 的题注译文,图表速读不再显示旧英文
        if rescued and captions:
            by_key = {(kind, num): text for kind, num, text in rescued}
            changed = False
            for entry in captions:
                text = by_key.get((entry.get("kind"), entry.get("num")))
                if text and entry.get("translated") != text:
                    entry["translated"] = text
                    changed = True
            if changed:
                (Path(request["outputDir"]) / "figures.json").write_text(json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"题注补译失败（{exc}），已跳过", flush=True)
    try:
        quality_check(
            request["inputPath"],
            request["outputDir"],
            request["config"]["output"],
            request.get("glossaryPath"),
            request["config"].get("pages"),
            request["config"].get("dualLayout", "side"),
            captions,
        )
    except Exception as exc:  # 质检自身故障不应让已完成的翻译标记为失败
        print(f"质检警告：质检步骤异常（{exc}），已跳过", flush=True)

    # 译文全文落盘,供任务问答(chat)构建上下文;带页码标记,回答可引用跳转
    try:
        full_text = translated_full_text(request["outputDir"], request["config"]["output"], max_chars=150000, page_markers=True, dual_layout=request["config"].get("dualLayout", "side"))
        if full_text:
            (Path(request["outputDir"]) / "translated-text.txt").write_text(full_text, encoding="utf-8")
    except Exception as exc:
        print(f"译文全文导出失败（{exc}），任务问答将不可用", flush=True)

    # 逐段对照数据(阅读模式);失败不影响任务
    try:
        pairs_pages = extract_paragraph_pairs(request["inputPath"], request["outputDir"], request["config"]["output"], dual_layout=request["config"].get("dualLayout", "side"))
        if pairs_pages:
            (Path(request["outputDir"]) / "paragraphs.json").write_text(json.dumps({"pages": pairs_pages}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"逐段对照提取失败（{exc}），已跳过", flush=True)

    # 作者区恢复:第 1 页作者姓名/单位/邮箱原样保留英文(擦除音译与重排碎片);失败不影响任务
    try:
        # 恢复对象是全部"翻译产物"(排除校对写回/修订版/上次恢复衍生等文件),
        # 否则存在 adjusted-output.pdf 时会把恢复写进衍生文件而漏掉主译文
        for pdf_path in main_translated_pdfs(request["outputDir"]):
            restore_author_blocks(
                str(pdf_path),
                request["inputPath"],
                request["config"]["output"],
                request["config"].get("dualLayout", "side"),
            )
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
