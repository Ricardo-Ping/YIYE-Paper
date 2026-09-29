import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../app/index.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function loadFunctions(names, globals) {
  const context = vm.createContext(globals);
  for (const name of names) {
    const start = script.search(new RegExp(`    (?:async )?function ${name}\\(`));
    assert.ok(start >= 0, name);
    const end = script.indexOf("\n    }", start) + 6;
    vm.runInContext(script.slice(start, end), context);
  }
  return context;
}

test("inline application script parses and only supported layout is selectable", () => {
  new vm.Script(script);
  assert.ok(!html.includes('<option value="alternating">'));
  assert.match(html, /prefers-reduced-motion/);
  assert.match(html, /<dialog[^>]*id="aichat-panel"/);
});

test("multi-chat submits current model, history and citations without a server-side jobs Map", async () => {
  const elements = { "#aichat-input": { value: "总结贡献" }, "#api-key": { value: "" }, "#assistant-error": {} };
  const job = { id: "one", status: "completed" };
  const m = { docs: new Map([["one", true]]), thread: [{ role: "user", content: "前一个问题" }], busy: false };
  let request;
  const ctx = loadFunctions(["sendMultiChat"], {
    state: { jobs: [job], multiChat: m }, $: (id) => elements[id],
    config: () => ({ provider: "ollama", model: "local-model" }), renderAiChatThread() {}, AbortSignal,
    fetch: async (_url, options) => { request = JSON.parse(options.body); return { ok: true, json: async () => ({ answer: "答案", docs: [{ id: "one" }] }) }; },
  });
  await ctx.sendMultiChat();
  assert.equal(request.question, "总结贡献");
  assert.equal(request.model.provider, "ollama");
  assert.equal(request.history.length, 1);
  assert.equal(m.thread.at(-1).content, "答案");
  assert.equal(m.order[0], job);
  assert.equal(m.busy, false);
  assert.equal(elements["#aichat-input"].value, "");
  elements["#aichat-input"].value = "重试问题";
  ctx.fetch = async () => { throw new Error("模型离线"); };
  await ctx.sendMultiChat();
  assert.match(m.thread.at(-1).content, /模型离线/);
  assert.equal(elements["#aichat-input"].value, "重试问题");
  m.docs.clear();
  await ctx.sendMultiChat();
  assert.equal(elements["#assistant-error"].hidden, false);
});

test("mindmap errors stay visible and a retry replaces them with escaped content", async () => {
  const state = { mindmapBusy: new Set(), mindmapOpen: new Set(["one"]), mindmaps: new Map() };
  const ctx = loadFunctions(["escapeHtml", "mindmapNodesHtml", "mindmapPanelHtml", "fetchMindmap"], {
    state, $: () => ({ value: "" }), config: () => ({}), renderTasks() {},
    fetch: async () => ({ ok: false, json: async () => ({ error: "模型连接失败" }) }),
  });
  await ctx.fetchMindmap("one");
  assert.match(ctx.mindmapPanelHtml({ id: "one" }), /模型连接失败/);
  assert.match(ctx.mindmapPanelHtml({ id: "one" }), /data-mindmap-refresh/);
  ctx.fetch = async () => ({ ok: true, json: async () => ({ markdown: "- 主题\n  - <script>alert(1)</script>" }) });
  await ctx.fetchMindmap("one", true);
  const result = ctx.mindmapPanelHtml({ id: "one", fileName: "test.pdf" });
  assert.match(result, /&lt;script&gt;/);
  assert.ok(!result.includes("模型连接失败"));
  assert.equal(state.mindmapBusy.size, 0);
});

test("task download list filters out backend-only artifacts", () => {
  const ctx = loadFunctions(["escapeHtml", "taskHtml"], {
    state: { chatOpen: new Set(), mindmapOpen: new Set(), figuresOpen: new Set(), readerOpen: new Set(), qualityDetails: new Map() },
    statusNames: {}, runningTimeText: () => "", aiSummaryHtml: () => "", readerPanelHtml: () => "",
    mindmapPanelHtml: () => "", figuresPanelHtml: () => "", qualityIssuesHtml: () => "", watermarkSuspectsHtml: () => "", chatPanelHtml: () => "",
  });
  const rendered = ctx.taskHtml({ id: "one", status: "completed", fileName: "paper.pdf", outputs: ["paper.pdf", "report.json", "terms.csv", "report.html"] });
  assert.match(rendered, /下载 paper.pdf/);
  for (const name of ["report.json", "terms.csv", "report.html"]) assert.ok(!rendered.includes(name));
});

test("watermark suspects are escaped, linked to original pages and never offer deletion", () => {
  const detail = { watermarkSuspects: [{ text: "<script>bad()</script>", pages: [1, 3] }] };
  const ctx = loadFunctions(["escapeHtml", "watermarkSuspectsHtml"], {
    state: { qualityDetails: new Map([["one", detail]]) },
  });
  const rendered = ctx.watermarkSuspectsHtml({ id: "one" });
  assert.match(rendered, /疑似水印 · 1 组/);
  assert.match(rendered, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.match(rendered, /\/api\/jobs\/one\/original#page=3/);
  assert.match(rendered, /不会自动删除或改动原 PDF/);
  assert.ok(!rendered.includes("data-watermark-delete"));
  assert.equal(ctx.watermarkSuspectsHtml({ id: "missing" }), "");
});

test("citation status distinguishes checked page ranges from warnings", () => {
  const ctx = loadFunctions(["escapeHtml", "citationNoticeHtml"], {});
  assert.match(ctx.citationNoticeHtml({ validCount: 2, warning: "" }), /2 个页码引用已通过范围检查/);
  assert.match(ctx.citationNoticeHtml({ validCount: 0, warning: "回答没有提供页码引用" }), /只校验页码范围/);
  assert.equal(ctx.citationNoticeHtml(null), "");
});
