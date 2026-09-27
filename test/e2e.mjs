// 端到端回归：在独立端口和数据目录上跑通完整用户流程，不触碰真实任务数据。
//
//   npm run test:e2e
//
// 覆盖：健康检查、预估与预检、术语库 CRUD、翻译任务全链路（术语库引用 →
// 质检报告 → qualityDetail）、运行中取消、取消后重试（术语表随之复制）、
// provider 测试接口。总耗时约 1–2 分钟（受 BabelDOC 真实处理流程限制）。

import { spawn } from "node:child_process";
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
    }));
    jobForm.append("apiKey", "k");
    const created = await fetch(`${BASE}/api/jobs`, { method: "POST", body: jobForm }).then((r) => r.json());
    // 队列空闲时提交接口返回前任务可能已经开始，两种状态都算正常
    check("任务创建", Boolean(created.id) && ["queued", "running"].includes(created.status), JSON.stringify(created));
    const finishedJob = await waitForJob(created.id);
    check("任务完成", finishedJob.status === "completed", JSON.stringify(finishedJob.error));
    check("输出包含译文 PDF 与质检报告",
      finishedJob.outputs.some((o) => o.endsWith(".pdf")) && finishedJob.outputs.includes("quality-report.json"),
      JSON.stringify(finishedJob.outputs));
    check("质检摘要解析", finishedJob.quality?.ok === true, JSON.stringify(finishedJob.quality));
    const detail = await fetch(`${BASE}/api/jobs/${created.id}`).then((r) => r.json());
    check("qualityDetail 附带页级明细", detail.qualityDetail?.outputs?.length > 0);
    check("术语一致性检查运行", typeof detail.qualityDetail?.glossaryCheck === "object");

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
      const gwJob = await fetch(`${BASE}/api/jobs`, { method: "POST", body: gatewayForm }).then((r) => r.json());
      const gwFinished = await waitForJob(gwJob.id);
      check(`${protocol} 协议经网关翻译完成`, gwFinished.status === "completed", JSON.stringify(gwFinished.error));
    }

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
    const cancelTarget = await fetch(`${BASE}/api/jobs`, { method: "POST", body: cancelForm }).then((r) => r.json());
    await sleep(3000);
    await fetch(`${BASE}/api/jobs/${cancelTarget.id}/cancel`, { method: "POST" });
    // 取消接口返回时进程树可能还在终止中，状态要等 close 事件后才变为 canceled
    const canceled = await waitForJob(cancelTarget.id, 60000);
    check("运行中取消", canceled.status === "canceled", JSON.stringify({ status: canceled.status, error: canceled.error }));

    // 8. 取消后重试（术语表应随之复制：完成后术语检查应运行）
    const retryResponse = await fetch(`${BASE}/api/jobs/${cancelTarget.id}/retry`, {
      method: "POST", headers: { "x-api-key": "k" },
    });
    const retried = await retryResponse.json();
    check("重试创建新任务", retryResponse.status === 202 && retried.id !== cancelTarget.id, JSON.stringify(retried));
    const retriedJob = await waitForJob(retried.id);
    check("重试任务完成", retriedJob.status === "completed", JSON.stringify(retriedJob.error));
    check("重试任务保留术语表", retriedJob.quality?.glossary?.terms === 2, JSON.stringify(retriedJob.quality));

    // 9. 删除单个任务
    const delActive = await fetch(`${BASE}/api/jobs/${created.id}`, { method: "DELETE" });
    // created.id 若已完成则可删；先测拒绝逻辑（无进行中任务时此请求应成功）
    check("删除单个已结束任务", delActive.ok === true, JSON.stringify(await delActive.json().catch(() => ({}))));
    const afterDelete = await fetch(`${BASE}/api/jobs`).then((r) => r.json());
    check("删除后列表不含该任务", !afterDelete.some((job) => job.id === created.id));

    // 10. 清除全部已完成任务
    const cleared = await fetch(`${BASE}/api/jobs`, { method: "DELETE" }).then((r) => r.json());
    check("清除已完成接口返回计数", cleared.ok === true && typeof cleared.removed === "number", JSON.stringify(cleared));
    const afterClear = await fetch(`${BASE}/api/jobs`).then((r) => r.json());
    check("清除后仅剩进行中任务", afterClear.every((job) => ["queued", "running"].includes(job.status)), JSON.stringify(afterClear.map((j) => j.status)));
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
