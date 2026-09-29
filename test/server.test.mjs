import test from "node:test";
import assert from "node:assert/strict";
import { applyThinkingToUpstream, countGlossaryEntries, isPdf, parseBabeldocProgress, parseGlossaryCsv, parseHttpUrl, sanitizeFileName, sanitizeGlossaryName, validateConfig, validatePages } from "../server.mjs";

test("PDF trust-boundary validation", () => {
  assert.equal(isPdf(Buffer.from("%PDF-1.7\n")), true);
  assert.equal(isPdf(Buffer.from("not a pdf")), false);
  assert.equal(sanitizeFileName("../bad:name.pdf"), "bad_name.pdf");
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
  assert.throws(() => validateConfig({ dualLayout: "alternating" }), /页码映射/);
  // 本地服务一律走 OpenAI 兼容协议
  assert.equal(validateConfig({ provider: "ollama", protocol: "anthropic" }).protocol, "openai");
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
  // 真实日志样例:分数式进度条
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
