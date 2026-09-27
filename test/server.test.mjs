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
  assert.equal(ollama.model, "qwen3:8b");
  assert.equal(ollama.qps, 16);
  assert.equal(ollama.output, "dual");
  assert.throws(() => validateConfig({ baseUrl: "file:///secret" }), /HTTP/);
  assert.equal(validateConfig({ target: "ja" }).target, "zh-CN");
  assert.equal(validateConfig({ target: "zh-TW" }).target, "zh-TW");
  assert.equal(validateConfig({ output: "alternate" }).output, "dual");
});

test("page range validation", () => {
  assert.equal(validatePages("1-5,8,11-"), "1-5,8,11-");
  assert.equal(validatePages(" 3 "), "3");
  assert.equal(validatePages("-3"), "-3");
  assert.equal(validatePages(""), "");
  assert.equal(validatePages(undefined), "");
  assert.throws(() => validatePages("5-1,abc"), /页码范围/);
  assert.throws(() => validatePages("1--2"), /页码范围/);
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


