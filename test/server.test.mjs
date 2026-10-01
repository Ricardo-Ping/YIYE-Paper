import test from "node:test";
import assert from "node:assert/strict";
import { appendLog, applyThinkingToUpstream, auditCitations, countGlossaryEntries, isPdf, isRuntimeStale, parseBabeldocProgress, parseGlossaryCsv, parseHttpUrl, publicJob, sanitizeFileName, sanitizeGlossaryName, validateConfig, validatePages } from "../server.mjs";

test("PDF trust-boundary validation", () => {
  assert.equal(isPdf(Buffer.from("%PDF-1.7\n")), true);
  assert.equal(isPdf(Buffer.from("not a pdf")), false);
  assert.equal(sanitizeFileName("../bad:name.pdf"), "bad_name.pdf");
});

test("runtime staleness detects server source changes", () => {
  assert.equal(isRuntimeStale(), false);
  assert.equal(isRuntimeStale(-1), true);
});

test("provider defaults and limits", () => {
  const ollama = validateConfig({ provider: "ollama", qps: 999 });
  assert.equal(ollama.baseUrl, "http://127.0.0.1:11434/v1");
  assert.equal(ollama.model, "qwen2.5:7b");
  assert.equal(ollama.qps, 16);
  assert.equal(ollama.output, "dual");
  assert.throws(() => validateConfig({ baseUrl: "file:///secret" }), /HTTP/);
  assert.equal(validateConfig({ target: "ja" }).target, "zh-CN");
  // 自定义翻译要求:空白折叠(含换行) + 500 字截断
  assert.equal(validateConfig({ customPrompt: "  面向  行内读者。\n保留公式  " }).customPrompt, "面向 行内读者。 保留公式");
  assert.equal(validateConfig({ customPrompt: "x".repeat(600) }).customPrompt.length, 500);
  assert.equal(validateConfig({}).customPrompt, "");
  // 长文档分批:范围钳制 20–500,非法/未填一律 0(不分批)
  assert.equal(validateConfig({ maxPagesPerPart: 50 }).maxPagesPerPart, 50);
  assert.equal(validateConfig({ maxPagesPerPart: 5 }).maxPagesPerPart, 0);
  assert.equal(validateConfig({ maxPagesPerPart: 9999 }).maxPagesPerPart, 0);
  assert.equal(validateConfig({ maxPagesPerPart: "abc" }).maxPagesPerPart, 0);
  assert.equal(validateConfig({ target: "zh-TW" }).target, "zh-TW");
  assert.equal(validateConfig({ output: "alternate" }).output, "dual");
  // 交替页布局已完成质检与页码映射适配,允许提交
  assert.equal(validateConfig({ dualLayout: "alternating" }).dualLayout, "alternating");
  // 本地服务一律走 OpenAI 兼容协议
  assert.equal(validateConfig({ provider: "ollama", protocol: "anthropic" }).protocol, "openai");
  // 水印移除:只保留"用户勾选确认"的文本,非字符串/过短/超长一律丢弃
  const wm = validateConfig({
    watermarkSuspects: [
      { text: "  CONFIDENTIAL INTERNAL  " },
      { text: "ab" },
      { text: 42 },
      null,
      { text: "x".repeat(300) },
    ],
  });
  assert.deepEqual(wm.watermarkSuspects, [{ text: "CONFIDENTIAL INTERNAL" }]);
  assert.equal(validateConfig({ watermarkSuspects: "hack" }).watermarkSuspects.length, 0);
  assert.equal(validateConfig({}).watermarkSuspects.length, 0);
});

test("markdown export builder covers zh/en/both and keeps untranslated pages", async () => {
  const { buildMarkdownExport } = await import("../server.mjs");
  const pages = [
    { page: 1, translated: true, pairs: [{ en: "SQL query rewriting aims high.", zh: "SQL 查询重写目标远大。" }, { en: "Only English here.", zh: "" }] },
    { page: 2, translated: false, pairs: [{ en: "Kept as original.", zh: "Kept as original." }] },
  ];
  const zh = buildMarkdownExport(pages, "zh", "论文A");
  assert.match(zh, /# 论文A/);
  assert.match(zh, /## 第 1 页/);
  assert.match(zh, /SQL 查询重写目标远大。/);
  assert.match(zh, /> Only English here\./); // 无译文段落回退原文
  assert.match(zh, /## 第 2 页（未翻译 · 保留原文）/)
  const both = buildMarkdownExport(pages, "both", "论文A");
  assert.match(both, /（双语对照）/);
  assert.match(both, /> SQL query rewriting aims high\./);
  const en = buildMarkdownExport(pages, "en", "论文A");
  assert.doesNotMatch(en, /查询重写目标远大/);
});

test("edits merge into export pairs and edits CSV lists only proofread rows", async () => {
  const { applyEditsToPairs, buildEditsCsv } = await import("../server.mjs");
  const pages = [
    { page: 1, translated: true, pairs: [{ en: "First paragraph.", zh: "原译文一。" }, { en: "Second paragraph.", zh: "原译文二。" }] },
  ];
  const edits = { "1:0": { text: "校对后的一。" }, "1:1": { text: "   " } };
  applyEditsToPairs(pages, edits);
  assert.equal(pages[0].pairs[0].zh, "校对后的一。");
  assert.equal(pages[0].pairs[1].zh, "原译文二。"); // 空白校对不生效
  const csv = buildEditsCsv(pages, edits);
  const rows = csv.split("\n");
  assert.equal(rows.length, 2); // 表头 + 仅一条有效校对
  assert.match(csv, /"First paragraph\."/, "原文列");
  assert.match(csv, /"校对后的一。"/, "校对后译文列");
  // 空校对 → 只有表头
  assert.equal(buildEditsCsv(pages, {}).split("\n").length, 1);
});

test("term pair extraction tolerates table/list formats and dedupes", async () => {
  const { extractTermPairsFromText, mergeGlossaryEntries, glossaryEntriesFromText, removeGlossaryEntry } = await import("../server.mjs");
  // 术语库条目解析与单条删除
  const library = "source,target\nGRPO,组相对策略优化\nTPC-H,TPC-H\n\"A,B\"行,\"含,逗号\"";
  const entries = glossaryEntriesFromText(library);
  assert.equal(entries.length, 3);
  const removed = removeGlossaryEntry(library, "GRPO");
  assert.equal(removed.removed, true);
  assert.ok(!removed.text.includes("组相对策略优化"));
  assert.match(removed.text, /TPC-H,TPC-H/);
  assert.equal(removeGlossaryEntry(library, "不存在").removed, false);
  const text = [
    "英文术语,中文译名",
    "| E3-Rewrite | E3重写框架 |",
    "- QueryBooster, 查询加速器",
    "1. GRPO\t组相对策略优化",
    "| --- | --- |",
    "| E3-Rewrite | E3重写框架 |", // 重复跳过
    "| only |", // 缺译文跳过
    "| TPC-H | TPC-H |", // 恒等对保留(钉住术语保持英文)
  ].join("\n");
  const pairs = extractTermPairsFromText(text);
  assert.deepEqual(pairs, [
    { source: "E3-Rewrite", target: "E3重写框架" },
    { source: "QueryBooster", target: "查询加速器" },
    { source: "GRPO", target: "组相对策略优化" },
    { source: "TPC-H", target: "TPC-H" },
  ]);
  // 合并:库内已有同名词跳过,新增计数正确;含逗号的值加引号;恒等对正常入库
  const { text: merged, added } = mergeGlossaryEntries("source,target\nGRPO,已有译法", pairs);
  assert.match(merged, /^source,target\n/);
  assert.match(merged, /GRPO,已有译法/);           // 已有 → 不覆盖
  assert.match(merged, /E3-Rewrite,E3重写框架/);
  assert.match(merged, /TPC-H,TPC-H/);
  assert.equal(added, 3);
});

test("glossary term filtering for retranslation", async () => {
  const { parseGlossaryEntries, filterGlossaryTerms } = await import("../server.mjs");
  const csv = "source,target\nLLM,大语言模型\nquery plan,查询计划\nKathDB-FAO,凯特数据库";
  const entries = parseGlossaryEntries(csv);
  assert.equal(entries.length, 3);
  assert.deepEqual(entries[0], ["LLM", "大语言模型"]);
  const hit = filterGlossaryTerms(entries, "The LLM generates a query plan for KathDB-FAO.");
  assert.equal(hit.length, 3);
  const none = filterGlossaryTerms(entries, "Totally unrelated text about coffee.");
  assert.equal(none.length, 0);
});

test("page range validation", () => {
  assert.equal(validatePages("1-5,8,11-"), "1-5,8,11-");
  assert.equal(validatePages(" 3 "), "3");
  assert.equal(validatePages("-3"), "-3");
  assert.equal(validatePages(""), "");
  assert.equal(validatePages(undefined), "");
  assert.throws(() => validatePages("5-1,abc"), /页码范围/);
  assert.throws(() => validatePages("1--2"), /页码范围/);
  // 页码从 1 起:0 不是合法页
  assert.throws(() => validatePages("0"), /页码范围/);
  assert.throws(() => validatePages("0-3"), /页码范围/);
  assert.throws(() => validatePages("5-1"), /页码范围/);
});

test("glossary CSV validation", () => {
  const csv = parseGlossaryCsv("source,target\nattention,注意力\n");
  assert.match(csv, /attention,注意力/);
  assert.equal(parseGlossaryCsv("\uFEFFsource,target\na,b\n"), "source,target\na,b\n");
  assert.throws(() => parseGlossaryCsv("term,译名\n"), /source/);
  assert.throws(() => parseGlossaryCsv("   \n"), /为空/);
  assert.throws(() => parseGlossaryCsv(`source,target\n${"x\n".repeat(3000)}`), /2000/);
});

test("provider URL parsing for test endpoints", () => {
  assert.equal(parseHttpUrl("http://127.0.0.1:11434/v1/"), "http://127.0.0.1:11434/v1");
  assert.throws(() => parseHttpUrl("ftp://example.com"), /HTTP/);
  assert.throws(() => parseHttpUrl("not a url"), /URL/);
});

test("glossary library name sanitization", () => {
  assert.equal(sanitizeGlossaryName("my terms.csv"), "my_terms");
  assert.equal(sanitizeGlossaryName("../evil"), "evil");
  assert.equal(sanitizeGlossaryName("物理术语"), "物理术语");
  assert.throws(() => sanitizeGlossaryName("///"), /无效/);
  assert.throws(() => sanitizeGlossaryName(""), /无效/);
  assert.throws(() => sanitizeGlossaryName("..."), /无效/);
});

test("glossary entry counting", () => {
  assert.equal(countGlossaryEntries("source,target\nattention,注意力\ntransformer,变换器\n"), 2);
  assert.equal(countGlossaryEntries("source,target\n"), 0);
});

test("babeldoc progress bar parsing", () => {
  // 真实日志样例:分数式进度条(rich ASCII 回退,如今是 tqdm 补丁未生效时的兜底)
  const translate = parseBabeldocProgress("Translate Paragraphs (1/1)                                     ----- 12/45   0:03… 0:00:12");
  assert.equal(translate.stage, "Translate Paragraphs");
  assert.equal(translate.current, 12);
  assert.equal(translate.total, 45);
  const save = parseBabeldocProgress("Save PDF (1/1)                                         ----- 2/2   0:00… 0:00:…");
  assert.equal(save.stage, "Save PDF");
  assert.equal(save.current, 2);
  assert.equal(save.total, 2);
  assert.equal(parseBabeldocProgress("INFO     INFO:babeldoc.main:Total tokens: 60"), null);
  assert.equal(parseBabeldocProgress("YIYE_QUALITY: {\"ok\":true}"), null);
});

test("babeldoc tqdm progress frames carry engine overall percentage", () => {
  // 真实日志样例:tqdm 分支帧(引擎按阶段权重算好的整体百分比在 bar 段)
  const running = parseBabeldocProgress("Translate Paragraphs (12/45):  45%|████▌     | 45.271481/100 [00:52<00:31,  1.01s/it]");
  assert.equal(running.stage, "Translate Paragraphs");
  assert.equal(running.current, 12);
  assert.equal(running.total, 45);
  assert.ok(Math.abs(running.overall - 45.271481) < 1e-6);
  const complete = parseBabeldocProgress("Translate Paragraphs (Complete):  69%|██████▊   | 68.67115148114051/100 [00:52<00:31,  1.01s/it]");
  assert.equal(complete.stage, "Translate Paragraphs");
  assert.equal(complete.current, null);
  assert.equal(complete.total, null);
  assert.ok(Math.abs(complete.overall - 68.67115148114051) < 1e-9);
  const fonts = parseBabeldocProgress("Add Fonts (1/218):  92%|█████████▏| 92.44177677675557/100 [00:57<00:02,  3.48it/s]");
  assert.equal(fonts.stage, "Add Fonts");
  assert.equal(fonts.current, 1);
  assert.equal(fonts.total, 218);
  const initial = parseBabeldocProgress("translate:   0%|          | 0/100 [00:00<?, ?it/s]");
  assert.equal(initial.stage, "translate");
  assert.equal(initial.overall, 0);
});

test("model download tqdm bars are not mistaken for translation progress", () => {
  // 下载条的 desc 含文件名(带点号)、分母不是 100,都不能入账为翻译进度
  assert.equal(parseBabeldocProgress("model.safetensors:  33%|███▌     | 0.87G/2.6G [00:10<00:20, 100MB/s]"), null);
  assert.equal(parseBabeldocProgress("config.json: 100%|██████| 631/631 [00:00<00:00, 1.20MB/s]"), null);
  assert.equal(parseBabeldocProgress(" 45%|████▌     | 45/100 [00:52<00:31,  1.01s/it]"), null);
});

test("thinking config maps to upstream protocols", () => {
  // anthropic:reasoning 级别映射为 thinking budget,并移除 temperature
  const anthropicBody = { model: "claude-opus-5", temperature: 0.3 };
  applyThinkingToUpstream(anthropicBody, { reasoning: "high" }, "anthropic");
  assert.deepEqual(anthropicBody.thinking, { type: "enabled", budget_tokens: 16384 });
  assert.ok(anthropicBody.max_tokens > anthropicBody.thinking.budget_tokens);
  assert.equal(anthropicBody.temperature, undefined);
  // thinking disabled 不注入(anthropic 默认无思考)
  const offUpstream = { model: "claude-opus-5", temperature: 0.3 };
  applyThinkingToUpstream(offUpstream, { thinking: { type: "disabled" } }, "anthropic");
  assert.equal(offUpstream.thinking, undefined);
  // gemini:disabled → thinkingBudget 0;级别 → 对应预算
  const geminiOff = { generationConfig: {} };
  applyThinkingToUpstream(geminiOff, { thinking: { type: "disabled" } }, "gemini");
  assert.equal(geminiOff.generationConfig.thinkingConfig.thinkingBudget, 0);
  const geminiLow = { generationConfig: {} };
  applyThinkingToUpstream(geminiLow, { reasoning: "low" }, "gemini");
  assert.equal(geminiLow.generationConfig.thinkingConfig.thinkingBudget, 1024);
  // 无思考信号 → 不注入
  const none = { generationConfig: {} };
  applyThinkingToUpstream(none, {}, "gemini");
  assert.equal(none.generationConfig.thinkingConfig, undefined);
});

test("reading citations are checked against pages actually sent to the model", () => {
  assert.deepEqual(auditCitations("结论【第 2 页】", ["1", "2"]), {
    count: 1, validCount: 1, invalid: [], warning: "",
  });
  const missing = auditCitations("没有引用", ["1"]);
  assert.match(missing.warning, /没有提供页码引用/);
  const invalid = auditCitations("错误【第 9 页】", ["1", "2"]);
  assert.equal(invalid.validCount, 0);
  assert.deepEqual(invalid.invalid, ["【第 9 页】"]);
  const multi = auditCitations("对比【文档 1 第 1-2 页】【文档 2 第 3 页】", ["1:1", "1:2", "2:3"], true);
  assert.equal(multi.validCount, 2);
  assert.equal(multi.warning, "");
});

test("reading assistant accepts current model and rejects invalid override", async () => {
  const { readingModelConfig } = await import("../server.mjs");
  const previous = validateConfig({ provider: "ollama" });
  assert.equal(readingModelConfig(undefined, previous), previous);
  const current = readingModelConfig({ provider: "openai", protocol: "anthropic", baseUrl: "https://example.com", model: "reader-model" }, previous);
  assert.equal(current.protocol, "anthropic");
  assert.equal(current.model, "reader-model");
  assert.throws(() => readingModelConfig({ model: "reader-model" }, previous), /配置不完整/);
  assert.throws(() => readingModelConfig({ model: "reader-model", baseUrl: "file:///secret" }, previous), /HTTP/);
});



test("model speed measurement", async () => {
  const { measureModelSpeed } = await import("../server.mjs");
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return { ok: true, status: 200, json: async () => ({ usage: { completion_tokens: 120 } }) };
  };
  try {
    const first = await measureModelSpeed({ baseUrl: "http://speed-test.local/v1", model: "m1" });
    assert.equal(first.completionTokens, 120);
    assert.ok(first.tokensPerSec > 0);
    assert.equal(first.cached, false);
    const second = await measureModelSpeed({ baseUrl: "http://speed-test.local/v1", model: "m1" });
    assert.equal(second.cached, true);
    assert.equal(calls, 1, "15 分钟内同 地址|模型 应命中缓存");
    globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
    await assert.rejects(measureModelSpeed({ baseUrl: "http://speed-test.local/v1", model: "m2" }), /HTTP 404/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("translation stall watchdog", async () => {
  const { isStalled } = await import("../server.mjs");
  const now = 1_000_000_000_000;
  const stallMs = 12 * 60_000;
  const running = (overrides = {}) => ({ status: "running", startedAt: now - 60_000, lastProgressAt: now - 30_000, ...overrides });
  // 刚有日志输出 → 未停滞
  assert.equal(isStalled(running(), now, stallMs), false);
  // 长时间无日志 → 停滞
  assert.equal(isStalled(running({ lastProgressAt: now - 13 * 60_000 }), now, stallMs), true);
  // 没有 lastProgressAt 时退回 startedAt
  assert.equal(isStalled(running({ lastProgressAt: undefined }), now, stallMs), false);
  assert.equal(isStalled(running({ lastProgressAt: undefined, startedAt: now - 13 * 60_000 }), now, stallMs), true);
  // 非运行状态/已请求取消 → 不判停滞
  assert.equal(isStalled(running({ status: "queued" }), now, stallMs), false);
  assert.equal(isStalled(running({ status: "completed" }), now, stallMs), false);
  assert.equal(isStalled(running({ cancelRequested: true, lastProgressAt: now - 13 * 60_000 }), now, stallMs), false);
});

test("completed paragraph frames and cleanup logs retain accurate task state", () => {
  const job = { progress: 0, log: [] };
  appendLog(job, "Translate Paragraphs (69/475):  58%|x| 58/100 [00:32]\n");
  assert.equal(publicJob(job).stats.paragraphs.done, 69);
  assert.equal(publicJob({ ...job, status: "completed" }).stats.paragraphs.done, 475);
  assert.equal(job.stats.paragraphs.done, 69);
  appendLog(job, "Translate Paragraphs (Complete):  89%|x| 89/100 [00:34]\n");
  assert.deepEqual(job.stats.paragraphs, { done: 475, total: 475 });
  appendLog(job, "Save PDF (Complete):  99%|x| 99/100 [00:40]\n");
  assert.equal(job.stage, "正在重建 PDF");
  appendLog(job, "INFO:babeldoc.format.pdf.translation_config:cleanup temp files\n");
  assert.equal(job.stage, "正在重建 PDF");
  appendLog(job, "YIYE_STAGE: postprocessing\n");
  assert.equal(job.stage, "正在校验与整理译文");
  appendLog(job, "YIYE_STAGE: ai summary starting\n");
  assert.equal(job.stage, "正在生成 AI 速览");
});

test("reading context normalizes PDF CJK glyphs without changing formulas", async () => {
  const { normalizeReadingText } = await import("../server.mjs");
  assert.equal(normalizeReadingText("可执行性、等价性和效率；x² + ①"), "可执行性、等价性和效率；x² + ①");
});
