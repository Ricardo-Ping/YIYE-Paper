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
  // 交替页布局已开放:提交门禁移除,设置区提供对照版式选择
  assert.ok(html.includes('<option value="alternating">'));
  assert.ok(html.includes('id="dual-layout-field"'));
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
  const state = { mindmapBusy: new Set(), mindmapOpen: new Set(["one"]), mindmaps: new Map(), mindmapCollapsed: new Set() };
  const ctx = loadFunctions(["escapeHtml", "mindmapTree", "countMindmapDescendants", "mindmapNodeHtml", "mindmapNodesHtml", "mindmapPanelHtml", "fetchMindmap"], {
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
  assert.match(rendered, /下载译文 PDF（paper\.pdf）/);
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

test("sentence alignment uses anchor DP for unequal counts and falls back when no anchors", () => {
  const ctx = loadFunctions(["alignSentences", "alignByAnchors", "sentenceAnchors"], { state: {} });
  // 中英句数不等:两段中文合并了三句英文中的前两句 —— 锚点 DP 应按 E3-Rewrite/GRPO/TPC-H/25.6% 对齐
  const en = ["Query rewriting is hard.", "We propose E3-Rewrite with GRPO on TPC-H benchmarks.", "Results show 25.6% speedup."];
  const zh = ["查询重写很难。我们提出E3-Rewrite，在TPC-H基准上使用GRPO。", "结果显示25.6%的加速。"];
  const aligned = ctx.alignSentences(en, zh);
  assert.equal(aligned.length, 2);
  assert.match(aligned[0].en, /E3-Rewrite/);
  assert.match(aligned[0].zh, /E3-Rewrite/);
  assert.match(aligned[0].zh, /GRPO/);
  assert.match(aligned[1].en, /25\.6%|speedup/);
  assert.match(aligned[1].zh, /25\.6%|加速/);
  // 无锚点(中文无任何拉丁词/数字)时退回比例法,内容不丢
  const noAnchor = ctx.alignSentences(
    ["One two three.", "Four five six.", "Seven eight nine."],
    ["甲乙丙丁。", "戊己庚辛。"],
  );
  const covered = noAnchor.reduce((sum, g) => sum + g.en.length + g.zh.length, 0);
  assert.ok(covered > 0);
  // 等长保持 1:1
  const equal = ctx.alignSentences(["Hello world.", "Second sentence."], ["你好世界。", "第二句。"]);
  assert.equal(equal.length, 2);
  assert.equal(equal[0].zh, "你好世界。");
});

test("report markdown renderer builds tables, headings and lists", () => {
  const ctx = loadFunctions(["renderReportMarkdown", "inlineMarkdown", "escapeHtml"], { state: {} });
  const html = ctx.renderReportMarkdown([
    "# 多论文对比报告",
    "## 一、总览",
    "| 论文 | 主题 |",
    "| --- | --- |",
    "| E3-Rewrite | **SQL查询重写** |",
    "",
    "- 第一条要点",
    "- 第二条要点",
  ].join("\n"));
  assert.match(html, /class="report-h"[^>]*>多论文对比报告/);
  assert.match(html, /<table class="report-table">/);
  assert.match(html, /<td>E3-Rewrite<\/td>/);
  assert.match(html, /<strong>SQL查询重写<\/strong>/);
  assert.match(html, /<li>第一条要点<\/li>/);
  // 表头分隔行不应生成单元格
  assert.ok(!html.includes("<td>---</td>"));
});

test("search highlight only wraps text segments, never tag/attribute names", () => {
  const ctx = loadFunctions(["highlightTerm"], {});
  const out = ctx.highlightTerm('<span class="rs-sent">process class diagram</span>', "class");
  assert.equal(out, '<span class="rs-sent">process <mark>class</mark> diagram</span>');
  // 无命中时原样返回
  assert.equal(ctx.highlightTerm("<b>alpha</b>", "omega"), "<b>alpha</b>");
  assert.equal(ctx.highlightTerm("<b>alpha</b>", ""), "<b>alpha</b>");
});

test("pair edit helpers standardize data-job and make the cancel button optional", () => {
  const ctx = loadFunctions(["pairEditButtonsHtml", "pairEditState", "pairEditAreaHtml", "escapeHtml"], { state: { paraApplyBusy: new Set() } });
  const btns = ctx.pairEditButtonsHtml("j1", "1:0", 2, "原文", { cancel: false });
  // 保存/写回按钮必须带 data-job(修复历史上 /api/jobs/undefined 的请求),ekey/page/orig 齐全
  assert.match(btns, /data-edit-save="j1"/);
  assert.match(btns, /data-apply-paragraph|data-edit-apply="j1"/);
  assert.match(btns, /data-job="j1"/);
  assert.match(btns, /data-ekey="1:0"/);
  assert.match(btns, /data-page="2"/);
  assert.match(btns, /data-orig="原文"/);
  assert.ok(!btns.includes("data-edit-cancel"));
  assert.match(ctx.pairEditButtonsHtml("j1", "1:0", 2, "原文"), /data-edit-cancel/);
  assert.match(ctx.pairEditAreaHtml("j1", "1:0", "草稿"), /data-edit-input="1:0"/);
  const edited = ctx.pairEditState({ "1:0": { text: "改后" } }, "1:0", { zh: "原译" });
  assert.equal(edited.editedText, "改后");
  assert.equal(edited.isEdited, true);
  const untouched = ctx.pairEditState({}, "1:0", { zh: "原译" });
  assert.equal(untouched.editedText, null);
  assert.equal(untouched.isEdited, false);
});

test("pairSentGroupsHtml aligns sentences and only emits clickable spans with a jobId", () => {
  const ctx = loadFunctions(
    ["pairSentGroupsHtml", "alignSentences", "splitSentences", "splitZhSentences", "escapeHtml"],
    { state: {} },
  );
  const en = "We propose E3-Rewrite with GRPO. Results show 25.6% speedup.";
  const zh = "我们提出E3-Rewrite与GRPO。结果显示25.6%加速。";
  const plain = ctx.pairSentGroupsHtml(en, zh);
  assert.ok(plain.includes('class="rp-sent-group"'));
  assert.ok(!plain.includes("data-sent"));
  const clickable = ctx.pairSentGroupsHtml(en, zh, { jobId: "j1", paragraph: en });
  assert.match(clickable, /data-sent=/);
  assert.match(clickable, /data-job="j1"/);
  assert.match(clickable, /data-paragraph=/);
});

test("startParagraphApply prefers current input, falls back to saved edit, rejects empty", async () => {
  const calls = [];
  const errors = [];
  const state = {
    qualityDetails: new Map([["j1", { edits: { "1:0": { text: "已保存的译文" } } }]]),
    pdfReader: null,
    readerEditing: new Set(),
    paraApplyBusy: new Set(),
  };
  const ctx = loadFunctions(["startParagraphApply", "applyParagraphToPdf"], {
    state,
    CSS: { escape: (s) => s },
    showError: (m) => errors.push(m),
    renderTasks: () => {},
    document: { querySelector: () => null },
    fetch: async (url, opts) => {
      calls.push({ url, body: JSON.parse(opts.body) });
      return { ok: true, json: async () => ({}) };
    },
  });
  // 输入框不存在:回退已保存的校对文本
  ctx.startParagraphApply({ dataset: { job: "j1", ekey: "1:0", page: "2", orig: "原文" } });
  await new Promise((resolve) => setTimeout(resolve, 0));
  // 写回成功后同步保存校对记录:apply-paragraph + save-edit 两次调用;
  // 匹配基准用 lastApplied(无记录时回退引擎原译"原文")
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].body, { page: 2, oldText: "原文", newText: "已保存的译文" });
  assert.deepEqual(calls[1].body, { key: "1:0", text: "已保存的译文", origText: "原文", lastApplied: "已保存的译文" });
  // 输入框为空且无已存文本:报错且不发请求
  errors.length = 0;
  ctx.startParagraphApply({ dataset: { job: "j1", ekey: "9:9", page: "1", orig: "原文" } });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls.length, 2); // 空输入不发新请求,仍是此前的 2 次
  assert.match(errors.join("\n"), /没有可写入的译文/);
});
