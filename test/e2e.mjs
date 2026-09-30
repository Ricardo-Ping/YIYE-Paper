// 端到端回归：在独立端口和数据目录上跑通完整用户流程，不触碰真实任务数据。
//
//   npm run test:e2e
//
// 覆盖：健康检查、预估与预检、术语库 CRUD、翻译任务全链路（术语库引用 →
// 质检报告 → qualityDetail）、运行中取消、取消后重试（术语表随之复制）、
// provider 测试接口。总耗时约 1–2 分钟（受 BabelDOC 真实处理流程限制）。

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const ROOT = path.resolve(import.meta.dirname, "..");
const SERVER_PORT = Number(process.env.E2E_SERVER_PORT || 4280);
const MOCK_PORT = Number(process.env.E2E_MOCK_PORT || 5280);
const BASE = `http://127.0.0.1:${SERVER_PORT}`;
const MOCK_BASE = `http://127.0.0.1:${MOCK_PORT}/v1`;
const DATA_DIR = path.join(ROOT, "data-e2e");
const SAMPLE_PDF = path.join(ROOT, "test", "sample-paper.pdf");
const GLOSSARY_CSV = "source,target\ndeep learning,深度学习\nattention mechanism,注意力机制\n";

let passed = 0;
let failed = 0;
const children = [];

function check(name, condition, detail = "") {
  if (condition) {
    passed += 1;
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.error(`  ✗ ${name}${detail ? ` —— ${detail}` : ""}`);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function startChild(command, args, env) {
  const child = spawn(command, args, { cwd: ROOT, env: { ...process.env, ...env }, windowsHide: true });
  children.push(child);
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  return child;
}

async function waitFor(url, label, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {}
    await sleep(300);
  }
  throw new Error(`${label} 在 ${timeoutMs}ms 内未就绪`);
}

async function waitForJob(id, timeoutMs = 180000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    await sleep(2000);
    const job = await fetch(`${BASE}/api/jobs/${id}`).then((r) => r.json());
    if (["completed", "failed", "canceled", "interrupted"].includes(job.status)) return job;
  }
  throw new Error(`任务 ${id} 超时未完成`);
}

async function main() {
  console.log("E2E 回归开始\n");
  await rm(DATA_DIR, { recursive: true, force: true });

  startChild(process.execPath, [path.join(ROOT, "test", "mock_llm.mjs")], { MOCK_PORT: String(MOCK_PORT) });
  startChild(process.execPath, [path.join(ROOT, "server.mjs")], {
    YIYE_PORT: String(SERVER_PORT),
    YIYE_DATA_DIR: DATA_DIR,
    // 测试期间开发者保存 server.mjs 不应触发自动重载打断用例
    YIYE_AUTO_RELOAD: "off",
  });
  await waitFor(`${MOCK_BASE}/models`, "mock LLM");
  await waitFor(`${BASE}/api/health`, "应用服务");

  try {
    // 1. 健康检查
    const health = await fetch(`${BASE}/api/health`).then((r) => r.json());
    check("健康检查 engineReady", health.ok === true && health.engineReady === true);

    // 2. provider 测试接口
    const test = await fetch(`${BASE}/api/providers/test`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ baseUrl: MOCK_BASE, model: "mock-large", apiKey: "k" }),
    }).then((r) => r.json());
    check("provider 连通性测试", test.ok === true && typeof test.latencyMs === "number", JSON.stringify(test));
    // 2.5 anthropic 协议的测试连接应经本地网关转换成功
    const anthropicTest = await fetch(`${BASE}/api/providers/test`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ baseUrl: `${MOCK_BASE}/anthropic`.replace("/v1", ""), model: "mock-large", apiKey: "k", protocol: "anthropic" }),
    }).then((r) => r.json());
    check("anthropic 协议测试连接(经网关)", anthropicTest.ok === true, JSON.stringify(anthropicTest));

    // 3. 预估：正常 PDF
    const form = new FormData();
    form.append("file", new Blob([await readFile(SAMPLE_PDF)]), "sample-paper.pdf");
    const estimate = await fetch(`${BASE}/api/estimate`, { method: "POST", body: form }).then((r) => r.json());
    check("预估返回页数与 token", estimate.pages === 1 && estimate.estimatedTokens > 0, JSON.stringify(estimate));

    // 4. 预估：加密 PDF 走预检报错
    const encryptedForm = new FormData();
    encryptedForm.append("file", new Blob([await readFile(path.join(ROOT, "test", "encrypted-sample.pdf"))]), "locked.pdf");
    const encryptedEstimate = await fetch(`${BASE}/api/estimate`, { method: "POST", body: encryptedForm }).then((r) => r.json());
    check("预估对加密 PDF 给出明确报错", (encryptedEstimate.errors || []).some((e) => e.includes("加密")));

    // 4.5 提示词模板：默认可读、可自定义；自定义版本保持到主任务结束,验证传递链路后恢复
    const promptDefault = await fetch(`${BASE}/api/prompt-template`).then((r) => r.json());
    check("提示词默认模板可读", promptDefault.customized === false && promptDefault.template.includes("忠实原文") && promptDefault.template.includes("简体中文"), JSON.stringify(promptDefault).slice(0, 120));
    const promptSave = await fetch(`${BASE}/api/prompt-template`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ template: "自定义规则 {variant} 收尾" }),
    }).then((r) => r.json());
    check("提示词自定义保存", promptSave.ok === true && promptSave.customized === true, JSON.stringify(promptSave));
    const promptCustom = await fetch(`${BASE}/api/prompt-template`).then((r) => r.json());
    check("提示词读取返回自定义版本", promptCustom.customized === true && promptCustom.template.startsWith("自定义规则"), JSON.stringify(promptCustom).slice(0, 120));

    // 5. 术语库 CRUD
    const glossaryForm = new FormData();
    glossaryForm.append("file", new Blob([GLOSSARY_CSV], { type: "text/csv" }), "e2e-terms.csv");
    const saved = await fetch(`${BASE}/api/glossaries`, { method: "POST", body: glossaryForm }).then((r) => r.json());
    check("术语库保存", saved.name === "e2e-terms" && saved.entries === 2, JSON.stringify(saved));
    const list = await fetch(`${BASE}/api/glossaries`).then((r) => r.json());
    check("术语库列表", Array.isArray(list) && list.some((item) => item.name === "e2e-terms"));
    const download = await fetch(`${BASE}/api/glossaries/e2e-terms`);
    check("术语库下载", download.ok && (await download.text()).includes("deep learning,深度学习"));

    // 6. 翻译任务全链路（引用术语库）
    const jobForm = new FormData();
    jobForm.append("file", new Blob([await readFile(SAMPLE_PDF)]), "sample-paper.pdf");
    jobForm.append("glossaryName", "e2e-terms");
    jobForm.append("config", JSON.stringify({
      provider: "openai", baseUrl: MOCK_BASE, model: "mock-large",
      output: "dual", target: "zh-CN", qps: 2, ocr: true, table: true, glossary: true,
      // 主任务跳过全局翻译缓存,保证提示词/请求链路检查每次真实走到 mock
      ignoreCache: true,
    }));
    jobForm.append("apiKey", "k");
    const created = await fetch(`${BASE}/api/jobs`, { method: "POST", body: jobForm }).then((r) => r.json());
    // 队列空闲时提交接口返回前任务可能已经开始，两种状态都算正常
    check("任务创建", Boolean(created.id) && ["queued", "running"].includes(created.status), JSON.stringify(created));
    const finishedJob = await waitForJob(created.id);
    check("任务完成", finishedJob.status === "completed", JSON.stringify(finishedJob.error));
    // 自定义提示词模板应随任务到达模型(mock 记录最近一次 system 内容),验证后恢复默认
    const lastSystem = await fetch(`${MOCK_BASE}/last-system`).then((r) => r.json());
    check("自定义提示词到达模型", lastSystem.system.includes("自定义规则") && lastSystem.system.includes("简体中文"), JSON.stringify(lastSystem).slice(0, 140));
    const promptRestore = await fetch(`${BASE}/api/prompt-template`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ template: "" }),
    }).then((r) => r.json());
    const promptAfterRestore = await fetch(`${BASE}/api/prompt-template`).then((r) => r.json());
    check("提示词清空恢复默认", promptRestore.customized === false && promptAfterRestore.customized === false && promptAfterRestore.template.includes("忠实原文"), JSON.stringify(promptAfterRestore).slice(0, 120));
    check("输出只含 PDF，内部 JSON/CSV/HTML 不进入下载列表",
      finishedJob.outputs.length > 0 && finishedJob.outputs.every((o) => o.toLowerCase().endsWith(".pdf")),
      JSON.stringify(finishedJob.outputs));
    check("质检摘要解析", finishedJob.quality?.ok === true, JSON.stringify(finishedJob.quality));
    const detail = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    check("qualityDetail 附带页级明细", detail.qualityDetail?.outputs?.length > 0);
    check("术语一致性检查运行", typeof detail.qualityDetail?.glossaryCheck === "object");

    // 4.1 重复上传:同文件同配置的已完成任务 → 409 + duplicateOf 引导复用
    const dupForm = new FormData();
    dupForm.append("file", new Blob([await readFile(SAMPLE_PDF)]), "sample-paper.pdf");
    dupForm.append("config", JSON.stringify({ provider: "openai", baseUrl: MOCK_BASE, model: "mock-large", output: "dual", target: "zh-CN" }));
    dupForm.append("apiKey", "k");
    const dupResponse = await fetch(`${BASE}/api/jobs`, { method: "POST", body: dupForm });
    const dupData = await dupResponse.json().catch(() => ({}));
    check("重复上传返回409与原任务引用", dupResponse.status === 409 && dupData.duplicateOf === created.id, JSON.stringify(dupData).slice(0, 140));

    // 5.5 任务问答（基于译文全文）
    const chatResponse = await fetch(`${BASE}/api/jobs/${created.id}/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: "这篇论文的核心内容是什么？", model: { provider: "openai", baseUrl: MOCK_BASE, model: "mock-small", protocol: "openai" } }),
    });
    const chatData = await chatResponse.json();
    check("任务问答返回内容", chatResponse.status === 200 && typeof chatData.answer === "string" && chatData.answer.length > 0,
      JSON.stringify(chatData).slice(0, 140));
    check("任务问答页码引用通过范围检查", chatData.citationCheck?.validCount === 1 && !chatData.citationCheck?.warning,
      JSON.stringify(chatData.citationCheck));
    check("任务问答采用当前选定模型", chatData.model === "mock-small");
    const retransEmpty = await fetch(`${BASE}/api/jobs/${created.id}/retranslate`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ en: "  " }),
    });
    check("段落重译拒绝空原文", retransEmpty.status === 400);

    // 5.46 译文编辑校对：保存 → 详情可见 → 写回 PDF → 改回原文即撤销
    const detail0 = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    const firstPair = detail0.qualityDetail?.paragraphs?.[0]?.pairs?.[0];
    check("逐段对照数据存在", Boolean(firstPair), JSON.stringify(detail0.qualityDetail?.paragraphs?.[0] || {}).slice(0, 100));
    const editSave = await fetch(`${BASE}/api/jobs/${created.id}/save-edit`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ key: "1:0", text: "校对后的人工译文。", origText: firstPair.zh }),
    });
    check("校对保存", editSave.status === 200);
    const editDetail = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    check("详情附带校对内容", editDetail.qualityDetail?.edits?.["1:0"]?.text === "校对后的人工译文。", JSON.stringify(editDetail.qualityDetail?.edits).slice(0, 120));
    const editRevert = await fetch(`${BASE}/api/jobs/${created.id}/save-edit`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ key: "1:0", text: firstPair.zh, origText: firstPair.zh }),
    });
    check("校对改回原文即撤销", editRevert.status === 200);
    const revertDetail = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    check("撤销后编辑删除", !revertDetail.qualityDetail?.edits?.["1:0"]);

    // 5.47 校对写回 PDF
    const applyResponse = await fetch(`${BASE}/api/jobs/${created.id}/apply-paragraph`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ page: detail0.qualityDetail.paragraphs[0].page, oldText: firstPair.zh, newText: "人工校对并写回的译文。" }),
    });
    check("校对写回 PDF", applyResponse.status === 200,
      JSON.stringify({ firstPair, page: detail0.qualityDetail.paragraphs[0].page, apply: await applyResponse.text() }).slice(0, 300));
    const appliedJob = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    check("写回产物进入输出", appliedJob.outputs.includes("adjusted-output.pdf"), JSON.stringify(appliedJob.outputs));

    const chatBadRequest = await fetch(`${BASE}/api/jobs/${created.id}/chat`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question: "  " }),
    });
    check("空问题被拒绝", chatBadRequest.status === 400);

    // 5.45 段落重译（备选译文对照）
    const retransResponse = await fetch(`${BASE}/api/jobs/${created.id}/retranslate`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ en: "We design, implement and evaluate a new query evaluation subsystem for multimodal DBMS." }),
    });
    const retransData = await retransResponse.json();
    check("段落重译返回备选译文", retransResponse.status === 200 && typeof retransData.translation === "string" && retransData.translation.length > 0,
      JSON.stringify(retransData).slice(0, 140));
    const retransEmpty2 = await fetch(`${BASE}/api/jobs/${created.id}/retranslate`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ en: "  " }),
    });
    check("段落重译拒绝空原文", retransEmpty2.status === 400);
    const mindmapResponse = await fetch(`${BASE}/api/jobs/${created.id}/mindmap`, {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}",
    });
    const mindmapData = await mindmapResponse.json();
    check("思维导图生成", mindmapResponse.status === 200 && typeof mindmapData.markdown === "string" && mindmapData.markdown.length > 0,
      JSON.stringify(mindmapData).slice(0, 140));
    const mindmapCached = await fetch(`${BASE}/api/jobs/${created.id}/mindmap`, {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}",
    });
    const mindmapCachedData = await mindmapCached.json();
    check("思维导图缓存命中", mindmapCachedData.generatedAt === mindmapData.generatedAt);

    // 5.7 多文档问答（跨任务）
    const multiResponse = await fetch(`${BASE}/api/chat-multi`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ job_ids: [created.id], question: "这几篇论文分别讲了什么？", history: [{ role: "user", content: "上一轮问题标记-测试历史" }, { role: "assistant", content: "上一轮回答标记-保留上下文" }] }),
    });
    const multiData = await multiResponse.json();
    check("多文档问答返回内容", multiResponse.status === 200 && typeof multiData.answer === "string" && multiData.answer.length > 0 && multiData.docs?.length === 1,
      JSON.stringify(multiData).slice(0, 140));
    check("多文档问答页码引用通过范围检查", multiData.citationCheck?.validCount === 1 && !multiData.citationCheck?.warning,
      JSON.stringify(multiData.citationCheck));
    const multiMessages = await fetch(`${MOCK_BASE}/last-system`).then((r) => r.json());
    check("多文档问答保留对话历史", multiMessages.system.includes("上一轮问题标记-测试历史") && multiMessages.system.includes("上一轮回答标记-保留上下文"));
    const multiEmpty = await fetch(`${BASE}/api/chat-multi`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ job_ids: [], question: "x" }),
    });
    check("多文档问答拒绝空选择", multiEmpty.status === 400);

    // 6.5 预览页面图（上传时的原文页 + 完成后的译文页）
    const inputPage = await fetch(`${BASE}/api/jobs/${created.id}/page-image/input-page.png`);
    check("原文页渲染图可用", inputPage.ok && (await inputPage.arrayBuffer()).byteLength > 1000);
    const outputPage = await fetch(`${BASE}/api/jobs/${created.id}/page-image/output-page.png`);
    check("译文页渲染图可用", outputPage.ok && (await outputPage.arrayBuffer()).byteLength > 1000);
    const badPage = await fetch(`${BASE}/api/jobs/${created.id}/page-image/evil.png`);
    check("页面图路由拒绝非法文件名", badPage.status === 404 || badPage.status === 400);

    // 6.6 协议网关：anthropic / gemini 协议任务经本地网关转换
    for (const protocol of ["anthropic", "gemini"]) {
      const protoBase = protocol === "anthropic" ? `${MOCK_BASE}/anthropic`.replace("/v1", "") : `${MOCK_BASE}/gemini/v1beta`.replace("/v1", "");
      const gatewayForm = new FormData();
      gatewayForm.append("file", new Blob([await readFile(SAMPLE_PDF)]), "sample-paper.pdf");
      gatewayForm.append("config", JSON.stringify({
        provider: "openai", protocol, baseUrl: protoBase, model: "mock-large",
        output: "mono", target: "zh-CN", qps: 2, ocr: true, table: false, glossary: true,
      }));
      gatewayForm.append("apiKey", "k");
      // 与已完成任务同文件同配置,force 跳过重复检测才能各建各的任务
      gatewayForm.append("force", "true");
      const gwJob = await fetch(`${BASE}/api/jobs`, { method: "POST", body: gatewayForm }).then((r) => r.json());
      const gwFinished = await waitForJob(gwJob.id);
      check(`${protocol} 协议经网关翻译完成`, gwFinished.status === "completed", JSON.stringify(gwFinished.error));
    }

    // 6.6b 交替页布局全链路:提交门禁已移除,输出翻倍、质检/逐段对照/页码映射按原文页号
    const altForm = new FormData();
    altForm.append("file", new Blob([await readFile(SAMPLE_PDF)]), "sample-paper.pdf");
    altForm.append("config", JSON.stringify({
      provider: "openai", baseUrl: MOCK_BASE, model: "mock-large",
      // enhance 与交替页组合是页序回归的关键场景:enhance 的 --dual-translate-first
      // 会翻转译文页位置,必须被交替页布局排斥
      output: "dual", dualLayout: "alternating", target: "zh-CN", qps: 2, ocr: true, table: false, glossary: true, enhance: true,
    }));
    altForm.append("apiKey", "k");
    // 与主任务同文件同配置(dual),force 跳过重复检测
    altForm.append("force", "true");
    const altJob = await fetch(`${BASE}/api/jobs`, { method: "POST", body: altForm }).then((r) => r.json());
    const altFinished = await waitForJob(altJob.id);
    check("交替页任务完成", altFinished.status === "completed", JSON.stringify(altFinished.error));
    const altDetail = await fetch(`${BASE}/api/jobs/${altJob.id}`).then((r) => r.json());
    const altQuality = altDetail.qualityDetail || {};
    const altExpected = altQuality.expectedPages || 0;
    check("交替页输出页数翻倍且与预期一致",
      altExpected > 0 && altExpected % 2 === 0
      && (altQuality.outputs || []).every((o) => o.actualPages === altExpected && !o.issues.some((issue) => issue.includes("页数"))),
      JSON.stringify({ expectedPages: altExpected, outputs: altQuality.outputs?.map((o) => ({ actual: o.actualPages, issues: o.issues })) }));
    check("交替页逐段对照按原文页号配对",
      (altQuality.paragraphs || []).length === altExpected / 2
      && (altQuality.paragraphs || []).every((p) => p.pairs?.length > 0),
      JSON.stringify({ pages: (altQuality.paragraphs || []).map((p) => p.page) }));
    // 页序断言:译文页必须真的在偶数位(整页中文),否则说明 --dual-translate-first 泄漏进交替页
    check("交替页译文页在偶数位(页序未被 enhance 翻转)",
      (altQuality.paragraphs || []).some((p) => p.pairs.some((pair) => pair.zh && /[\u4e00-\u9fff]/.test(pair.zh)))
      && !(altQuality.outputs || []).some((o) => (o.pages || []).some((item) => item.issues.includes("疑似未翻译"))),
      JSON.stringify(altQuality.outputs?.map((o) => o.pages)));
    check("交替页术语一致性运行", typeof altQuality.glossaryCheck === "object", JSON.stringify(altQuality.glossaryCheck));

    // 6.7 修订版:把第 1 页替换为原文页
    const revised = await fetch(`${BASE}/api/jobs/${created.id}/revised-pdf`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pages: [1] }),
    }).then((r) => r.json());
    check("修订版生成", revised.ok === true && revised.file === "revised-output.pdf", JSON.stringify(revised));
    const revisedList = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    check("修订版进入任务输出", revisedList.outputs.includes("revised-output.pdf"));

    // 6.8 无页码选择的修订请求应被拒绝
    const badRevise = await fetch(`${BASE}/api/jobs/${created.id}/revised-pdf`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pages: [] }),
    }).then((r) => r.json());
    check("空页码修订被拒绝", Boolean(badRevise.error));

    // 7. 运行中取消
    const cancelForm = new FormData();
    cancelForm.append("file", new Blob([await readFile(SAMPLE_PDF)]), "sample-paper.pdf");
    cancelForm.append("glossaryName", "e2e-terms");
    cancelForm.append("config", JSON.stringify({
      provider: "openai", baseUrl: MOCK_BASE, model: "mock-large",
      output: "mono", target: "zh-CN", qps: 2, ocr: true, table: false, glossary: true,
    }));
    cancelForm.append("apiKey", "k");
    // 同文件同配置的任务已存在,force 跳过重复检测
    cancelForm.append("force", "true");
    const cancelTarget = await fetch(`${BASE}/api/jobs`, { method: "POST", body: cancelForm }).then((r) => r.json());
    await sleep(3000);
    await fetch(`${BASE}/api/jobs/${cancelTarget.id}/cancel`, { method: "POST" });
    // 取消接口返回时进程树可能还在终止中，状态要等 close 事件后才变为 canceled
    const canceled = await waitForJob(cancelTarget.id, 60000);
    check("运行中取消", canceled.status === "canceled", JSON.stringify({ status: canceled.status, error: canceled.error }));

    // 8. 取消后原地重试（同一任务复活，术语表沿用原目录：完成后术语检查应运行）
    const retryResponse = await fetch(`${BASE}/api/jobs/${cancelTarget.id}/retry`, {
      method: "POST", headers: { "x-api-key": "k" },
    });
    const retried = await retryResponse.json();
    check("重试复用原任务", retryResponse.status === 202 && retried.id === cancelTarget.id && ["queued", "running"].includes(retried.status), JSON.stringify(retried));
    const retriedJob = await waitForJob(retried.id);
    check("重试任务完成", retriedJob.status === "completed", JSON.stringify(retriedJob.error));
    check("重试任务保留术语表", retriedJob.quality?.glossary?.terms === 2, JSON.stringify(retriedJob.quality));

    // 9. 删除单个任务
    const taskDir = path.join(DATA_DIR, "jobs", created.id);
    for (const name of ["internal.json", "internal.csv", "internal.html"]) {
      await writeFile(path.join(taskDir, "output", name), "cleanup-regression", "utf8");
    }
    const delActive = await fetch(`${BASE}/api/jobs/${created.id}`, { method: "DELETE" });
    // created.id 若已完成则可删；先测拒绝逻辑（无进行中任务时此请求应成功）
    check("删除单个已结束任务", delActive.ok === true, JSON.stringify(await delActive.json().catch(() => ({}))));
    const afterDelete = await fetch(`${BASE}/api/jobs`).then((r) => r.json());
    check("删除后列表不含该任务", !afterDelete.some((job) => job.id === created.id));
    check("删除任务同步清理 PDF 和内部 JSON/CSV/HTML", !existsSync(taskDir));

    // 10. 清除全部已完成任务
    const cleared = await fetch(`${BASE}/api/jobs`, { method: "DELETE" }).then((r) => r.json());
    check("清除已完成接口返回计数", cleared.ok === true && typeof cleared.removed === "number", JSON.stringify(cleared));
    const afterClear = await fetch(`${BASE}/api/jobs`).then((r) => r.json());
    check("清除后仅剩进行中任务", afterClear.every((job) => ["queued", "running"].includes(job.status)), JSON.stringify(afterClear.map((j) => j.status)));
    check("批量清理同步删除任务目录", !existsSync(path.join(DATA_DIR, "jobs", retried.id)));

    // 11. MCP server：initialize → tools/list → translate_paper(等待完成)
    const mcpInit = await fetch(`${BASE}/mcp`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }),
    }).then((r) => r.json());
    check("MCP initialize", mcpInit.result?.serverInfo?.name === "yiye-paper", JSON.stringify(mcpInit).slice(0, 120));
    const mcpTools = await fetch(`${BASE}/mcp`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }),
    }).then((r) => r.json());
    check("MCP 工具列表", (mcpTools.result?.tools || []).map((t) => t.name).join(",") === "translate_paper,get_job,list_jobs",
      JSON.stringify(mcpTools.result?.tools?.map((t) => t.name)));
    const mcpCall = await fetch(`${BASE}/mcp`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0", id: 3, method: "tools/call",
        params: { name: "translate_paper", arguments: { pdf_path: SAMPLE_PDF, wait: true, timeout_sec: 120 } },
      }),
    }).then((r) => r.json());
    const mcpText = mcpCall.result?.content?.[0]?.text || "";
    check("MCP 翻译完成", mcpText.includes("状态：completed"), mcpText.slice(0, 160));
  } finally {
    for (const child of children) {
      if (child.exitCode === null) {
        if (process.platform === "win32") spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
        else child.kill("SIGTERM");
      }
    }
    await rm(DATA_DIR, { recursive: true, force: true }).catch(() => {});
  }

  console.log(`\nE2E 结果：${passed} 通过，${failed} 失败`);
  process.exit(failed ? 1 : 0);
}

main().catch((error) => {
  console.error(`E2E 异常终止：${error.message}`);
  for (const child of children) {
    if (child.exitCode === null) {
      if (process.platform === "win32") spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
      else child.kill("SIGTERM");
    }
  }
  process.exit(1);
});
