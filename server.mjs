import { createServer } from "node:http";
import { existsSync } from "node:fs";
import { access, copyFile, mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { createReadStream } from "node:fs";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const APP_FILE = path.join(ROOT, "app", "index.html");
// E2E 测试通过 YIYE_DATA_DIR 使用独立数据目录，避免影响真实任务数据
const DATA_DIR = process.env.YIYE_DATA_DIR || path.join(ROOT, "data");
const JOBS_DIR = path.join(DATA_DIR, "jobs");
const GLOSSARIES_DIR = path.join(DATA_DIR, "glossaries");
const JOBS_FILE = path.join(DATA_DIR, "jobs.json");
const WORKER_FILE = path.join(ROOT, "engine_worker.py");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const MAX_FILE_BYTES = 200 * 1024 * 1024;
const HOST = "127.0.0.1";
const PORT = Number(process.env.YIYE_PORT || 4173);

const jobs = new Map();
const secrets = new Map();
// LLM 协议网关注册表：anthropic/gemini 协议的任务在任务期间持有上游配置，
// BabelDOC 只讲 OpenAI 协议，由网关把它的请求翻译成上游协议（内存态，不落盘）
const gatewayRegistry = new Map();
const queue = [];
let active = null;

export function sanitizeFileName(name) {
  let clean = path.basename(String(name || "paper.pdf")).replace(/[<>:"/\\|?*\x00-\x1F]/g, "_").trim();
  // Windows 保留设备名（CON、NUL、COM1…）即使带扩展名也不能直接作为文件名
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)/i.test(clean)) clean = `_${clean}`;
  return clean || "paper.pdf";
}

export function isPdf(buffer) {
  return buffer.length >= 5 && buffer.subarray(0, 5).toString("ascii") === "%PDF-";
}

export function validateConfig(input = {}) {
  const output = ["mono", "dual"].includes(input.output) ? input.output : "dual";
  const provider = ["openai", "ollama"].includes(input.provider) ? input.provider : "ollama";
  const protocol = ["openai", "anthropic", "gemini"].includes(input.protocol) ? input.protocol : "openai";
  const thinking = ["default", "off", "low", "medium", "high"].includes(input.thinking) ? input.thinking : "default";
  // 本地服务一律走 OpenAI 兼容协议;anthropic/gemini 仅对云端入口有意义
  const effectiveProtocol = provider === "ollama" ? "openai" : protocol;
  const target = ["zh-CN", "zh-TW"].includes(input.target) ? input.target : "zh-CN";
  const qps = Math.max(1, Math.min(16, Number(input.qps) || 4));
  const defaults = provider === "ollama"
    ? { baseUrl: "http://127.0.0.1:11434/v1", model: "qwen3:8b" }
    : { baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini" };
  const baseUrl = parseHttpUrl(input.baseUrl || defaults.baseUrl);
  const model = String(input.model || defaults.model).trim();
  if (!model || model.length > 120) throw new Error("模型名称不能为空或过长");
  return {
    output,
    provider,
    protocol: effectiveProtocol,
    thinking,
    target,
    qps,
    baseUrl,
    model,
    pages: validatePages(input.pages),
    enhance: input.enhance === true,
    ocr: input.ocr !== false,
    table: input.table === true,
    glossary: input.glossary !== false,
    figure: false,
    watermark: false,
  };
}

export function parseHttpUrl(value) {
  let parsed;
  try {
    parsed = new URL(String(value || ""));
  } catch {
    throw new Error("接口地址不是有效 URL");
  }
  if (!/^https?:$/.test(parsed.protocol)) throw new Error("接口地址只支持 HTTP 或 HTTPS");
  return parsed.toString().replace(/\/$/, "");
}

export function validatePages(value) {
  const raw = String(value || "").trim().replace(/\s+/g, "");
  if (!raw) return "";
  for (const part of raw.split(",")) {
    // 页码从 1 起(0 不是合法页);开区间 "-3"/"3-" 合法
    const range = part.match(/^(\d+)?-(\d+)?$/);
    if (range) {
      const start = range[1] !== undefined ? Number(range[1]) : 1;
      const end = range[2] !== undefined ? Number(range[2]) : Infinity;
      if (start < 1 || end < 1 || start > end) throw new Error("页码范围格式不正确，示例：1-5,8,11-");
      continue;
    }
    if (!/^[1-9]\d*$/.test(part)) throw new Error("页码范围格式不正确，示例：1-5,8,11-");
  }
  return raw;
}

export function parseGlossaryCsv(text) {
  const clean = String(text || "").replace(/^\uFEFF/, "");
  if (!clean.trim()) throw new Error("术语表内容为空");
  if (Buffer.byteLength(clean, "utf8") > 512 * 1024) throw new Error("术语表超过 512 KB 限制");
  const lines = clean.split(/\r?\n/).filter((line) => line.trim());
  if (lines.length > 2001) throw new Error("术语表最多支持 2000 条词条");
  const header = lines[0].split(",").map((cell) => cell.trim().toLowerCase().replace(/^"|"$/g, ""));
  if (!header.includes("source") || !header.includes("target")) {
    throw new Error('术语表 CSV 需要包含 "source" 和 "target" 列');
  }
  return clean;
}

export function sanitizeGlossaryName(name) {
  const clean = String(name || "")
    .replace(/\.csv$/i, "")
    .replace(/[^\w\u4e00-\u9fff-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 60);
  if (!clean) throw new Error("术语库名称无效");
  return clean;
}

export function countGlossaryEntries(text) {
  const lines = parseGlossaryCsv(String(text || "")).split(/\r?\n/).filter((line) => line.trim());
  return Math.max(0, lines.length - 1);
}

function json(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}

function publicJob(job) {
  const { inputPath, outputDir, requestPath, logBuffer, log, ...safe } = job;
  return { ...safe, log: log.slice(-30) };
}

async function engineReady() {
  try {
    await Promise.all([access(PYTHON), access(WORKER_FILE)]);
    return true;
  } catch {
    return false;
  }
}

async function persistJobs() {
  await mkdir(DATA_DIR, { recursive: true });
  const tmp = `${JOBS_FILE}.${process.pid}.${randomUUID()}.tmp`;
  const data = [...jobs.values()].map(({ inputPath, outputDir, requestPath, logBuffer, ...job }) => job);
  await writeFile(tmp, JSON.stringify(data, null, 2), "utf8");
  await rename(tmp, JOBS_FILE);
}

async function loadJobs() {
  await mkdir(JOBS_DIR, { recursive: true });
  await mkdir(GLOSSARIES_DIR, { recursive: true });
  try {
    const stored = JSON.parse(await readFile(JOBS_FILE, "utf8"));
    for (const job of stored) {
      if (["queued", "running"].includes(job.status)) {
        job.status = "interrupted";
        job.error = "应用重启后任务已中断，请重新提交。API key 未被保存。";
      }
      const dir = path.join(JOBS_DIR, job.id);
      jobs.set(job.id, {
        ...job,
        log: Array.isArray(job.log) ? job.log : [],
        inputPath: path.join(dir, job.inputName || "source.pdf"),
        outputDir: path.join(dir, "output"),
        requestPath: path.join(dir, "request.json"),
      });
    }
    await persistJobs();
  } catch (error) {
    if (error.code !== "ENOENT") console.warn("无法读取历史任务：", error.message);
  }
}

// BabelDOC 各阶段的进度条为分数形式（如 "Translate Paragraphs (1/1) ----- 12/45"）。
// 按阶段映射到整体百分比的区间，让扫描线随真实翻译进度移动。
const STAGE_RANGES = [
  { label: "Parse PDF", start: 1, end: 6 },
  { label: "DetectScannedFile", start: 6, end: 10 },
  { label: "Parse Page Layout", start: 10, end: 14 },
  { label: "Parse Paragraphs", start: 14, end: 18 },
  { label: "Parse Formulas", start: 18, end: 22 },
  { label: "Automatic Term Extraction", start: 22, end: 26 },
  { label: "Translate Paragraphs", start: 26, end: 82 },
  { label: "Typesetting", start: 82, end: 88 },
  { label: "Add Fonts", start: 88, end: 92 },
  { label: "Generate drawing", start: 92, end: 95 },
  { label: "Subset font", start: 95, end: 97 },
  { label: "Save PDF", start: 97, end: 99 },
];

export function parseBabeldocProgress(line) {
  const match = line.match(/^([A-Za-z][A-Za-z0-9 .]*?)\s*\(\d+\/\d+\)\s*-{2,}\s*(\d+)\/(\d+)/);
  if (!match) return null;
  return { stage: match[1].trim(), current: Number(match[2]), total: Number(match[3]) };
}

function stagePercent(progressEvent) {
  const range = STAGE_RANGES.find((entry) => progressEvent.stage.startsWith(entry.label));
  if (!range || progressEvent.total <= 0) return null;
  const fraction = Math.min(1, Math.max(0, progressEvent.current / progressEvent.total));
  return Math.round(range.start + fraction * (range.end - range.start));
}

function appendLog(job, chunk, apiKey = "") {
  // 过短的 key 做逐字替换会把日志里的正常单词一起污染，因此只脱敏足够长的 key
  const secret = apiKey && apiKey.length >= 8 ? apiKey : "";
  job.logBuffer = (job.logBuffer || "") + chunk.toString("utf8");
  const parts = job.logBuffer.split(/\r\n|\r|\n/);
  job.logBuffer = parts.pop();
  for (const raw of parts) {
    const line = raw.replace(/\u001b\[[0-9;]*m/g, "").trim();
    if (!line) continue;
    // 结构化行用原始文本解析，不受脱敏影响；不进入展示日志。
    // BabelDOC 的进度条结尾可能没有换行，标记未必在行首，因此用 indexOf 定位。
    const qualityIndex = line.indexOf("YIYE_QUALITY:");
    if (qualityIndex >= 0) {
      const prefix = line.slice(0, qualityIndex).trim();
      if (prefix) job.log.push(prefix.slice(0, 1200));
      try { job.quality = JSON.parse(line.slice(qualityIndex + "YIYE_QUALITY:".length)); } catch {}
      continue;
    }
    const tokens = line.match(/Total tokens:\s*([\d,]+)/i);
    if (tokens) job.tokensUsed = Number(tokens[1].replaceAll(",", ""));
    const promptTokens = line.match(/Prompt tokens:\s*([\d,]+)/i);
    if (promptTokens) job.promptTokens = Number(promptTokens[1].replaceAll(",", ""));
    const completionTokens = line.match(/Completion tokens:\s*([\d,]+)/i);
    if (completionTokens) {
      job.completionTokens = Number(completionTokens[1].replaceAll(",", ""));
      job.awaitingCompletionValue = false;
    } else if (/Completion tokens:/i.test(line)) {
      // rich 表格把数值折到下一行的形态
      job.awaitingCompletionValue = true;
    } else if (job.awaitingCompletionValue) {
      const value = line.replace(/[^\d,]/g, "").trim();
      if (value) {
        job.completionTokens = Number(value.replaceAll(",", ""));
        job.awaitingCompletionValue = false;
      }
    }
    // BabelDOC 的进度条是分数不是百分数,解析后映射为整体进度,驱动扫描线;
    // 同时记录翻译段落数与扫描检测,供任务统计展示
    const progressEvent = parseBabeldocProgress(line);
    if (progressEvent) {
      const stagePct = stagePercent(progressEvent);
      if (stagePct !== null && stagePct > (job.progress || 0)) job.progress = Math.min(99, stagePct);
      job.stats = job.stats || {};
      if (progressEvent.stage === "Translate Paragraphs") {
        job.stats.paragraphs = { done: progressEvent.current, total: progressEvent.total };
      }
      if (progressEvent.stage === "DetectScannedFile") job.stats.scannedCheck = true;
    }
    const clean = secret ? line.split(secret).join("[REDACTED]") : line;
    job.log.push(clean.slice(0, 1200));
    if (job.log.length > 100) job.log.shift();
    const percent = clean.match(/(?:^|\s)(\d{1,3}(?:\.\d+)?)%/);
    if (percent) job.progress = Math.max(job.progress, Math.min(95, Math.round(Number(percent[1]))));
    if (/layout|parse|版式/i.test(clean)) job.stage = "正在分析版式";
    if (/translat|翻译/i.test(clean)) job.stage = "正在翻译正文";
    if (/render|generate|save|渲染|生成/i.test(clean)) job.stage = "正在重建 PDF";
  }
}

function flushLog(job, apiKey = "") {
  if (job.logBuffer) appendLog(job, "\n", apiKey);
  delete job.logBuffer;
}

async function walkPdfs(dir, prefix = "") {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const relative = path.join(prefix, entry.name);
    if (entry.isDirectory()) result.push(...await walkPdfs(path.join(dir, entry.name), relative));
    else if (entry.name.toLowerCase().endsWith(".pdf")) result.push(relative.replaceAll("\\", "/"));
  }
  return result;
}

async function walkGlossaries(dir) {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (entry.isFile() && entry.name.toLowerCase().endsWith(".csv") && entry.name !== "glossary.csv") {
      result.push(entry.name.replaceAll("\\", "/"));
    }
  }
  return result.sort();
}

function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
  else child.kill("SIGTERM");
}

async function runJob(job) {
  const apiKey = secrets.get(job.id) || "not-needed";
  job.status = "running";
  job.stage = "正在启动翻译引擎";
  job.progress = 2;
  job.startedAt = new Date().toISOString();
  await persistJobs();

  // 取消可能发生在上面 persistJobs 的 await 窗口内（此时 active 尚未赋值，
  // 取消路由无法杀进程），所以 spawn 前必须再检查一次。
  if (job.cancelRequested) {
    job.status = "canceled";
    job.stage = "已取消";
    job.error = null;
    job.finishedAt = new Date().toISOString();
    secrets.delete(job.id);
    if (job.config?.gatewayId) gatewayRegistry.delete(job.config.gatewayId);
    await persistJobs();
    return;
  }

  const child = spawn(PYTHON, [WORKER_FILE], {
    cwd: ROOT,
    env: { ...process.env, YIYE_API_KEY: apiKey, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    windowsHide: true,
  });
  // worker 统一从 stdin 读取任务 JSON(translate 模式),路径不出现在命令行
  child.stdin.end(JSON.stringify({ mode: "translate", requestPath: job.requestPath }), "utf8");
  active = { id: job.id, child };
  child.stdout.on("data", (chunk) => appendLog(job, chunk, apiKey));
  child.stderr.on("data", (chunk) => appendLog(job, chunk, apiKey));

  const exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  }).catch((error) => {
    appendLog(job, error.message, apiKey);
    return -1;
  });

  secrets.delete(job.id);
  if (job.config?.gatewayId) gatewayRegistry.delete(job.config.gatewayId);
  active = null;
  flushLog(job, apiKey);
  secrets.delete(job.id);
  if (job.config?.gatewayId) gatewayRegistry.delete(job.config.gatewayId);
  active = null;
  flushLog(job, apiKey);
  // 从日志提取可读错误:定位最后一条含 error/错误/失败的行,拼接其后续被换行截断的片段
  const errorFromLog = () => {
    let idx = job.log.length - 1;
    while (idx >= 0 && !/error|错误|失败/i.test(job.log[idx])) idx -= 1;
    if (idx < 0) return `翻译引擎退出码：${exitCode}`;
    const joined = job.log.slice(idx, Math.min(idx + 4, job.log.length))
      .join(" ")
      .replace(/\b[\w.]+\.py:\d+/g, "")
      .replace(/\b(?:INFO|ERROR|WARNING|DEBUG)\b[:]?/g, "")
      .replace(/\s+/g, " ")
      .trim();
    return joined.slice(0, 300) || `翻译引擎退出码：${exitCode}`;
  };
  if (job.cancelRequested) {
    job.status = "canceled";
    job.stage = "已取消";
    job.error = null;
  } else if (exitCode === 0) {
    const [pdfs, glossaries] = await Promise.all([
      walkPdfs(job.outputDir).catch(() => []),
      walkGlossaries(job.outputDir),
    ]);
    job.outputs = [...pdfs, ...glossaries];
    const reportName = "quality-report.json";
    if (job.quality && await access(path.join(job.outputDir, reportName)).then(() => true).catch(() => false)) {
      job.outputs.push(reportName);
    }
    // 质检报告本身也是输出,必须以译文 PDF 是否存在作为完成标准
    const hasTranslatedPdf = job.outputs.some((name) => name.toLowerCase().endsWith(".pdf"));
    if (!hasTranslatedPdf) {
      job.status = "failed";
      job.stage = "没有生成译文 PDF";
      job.error = errorFromLog() || "翻译引擎正常退出，但没有生成译文 PDF。请查看任务日志。";
    } else {
      job.status = "completed";
      job.stage = "翻译完成";
      job.progress = 100;
      job.error = null;
      const firstPdf = job.outputs.find((name) => name.toLowerCase().endsWith(".pdf"));
      if (firstPdf) {
        await renderPagePng(path.join(job.outputDir, firstPdf), path.join(path.dirname(job.inputPath), "output-page.png"), 0);
      }
    }
  } else {
    job.status = "failed";
    job.stage = "翻译失败";
    job.error = errorFromLog() + (job.stats?.scannedCheck ? "（已尝试扫描件 OCR 兼容处理，仍未能生成可翻译内容）" : "");
  }
  job.finishedAt = new Date().toISOString();
  await persistJobs();
}

let pumping = false;
async function pump() {
  // pumping 在任何 await 之前同步置位，保证队列严格串行：
  // 否则快速连续提交时，两个 pump 都会在 active 赋值前看到空而并发启动翻译。
  if (pumping) return;
  pumping = true;
  try {
    while (queue.length) {
      const id = queue.shift();
      const job = jobs.get(id);
      if (!job || job.cancelRequested) continue;
      await runJob(job);
    }
  } finally {
    pumping = false;
  }
}

async function createJob(req) {
  if (!(await engineReady())) {
    const error = new Error("翻译引擎尚未安装，请先在项目目录运行 uv sync");
    error.status = 503;
    throw error;
  }
  const length = Number(req.headers["content-length"] || 0);
  if (length > MAX_FILE_BYTES + 1024 * 1024) {
    const error = new Error("PDF 超过 200 MB 限制");
    error.status = 413;
    throw error;
  }
  const request = new Request(`http://${HOST}:${PORT}/api/jobs`, {
    method: "POST",
    headers: req.headers,
    body: req,
    duplex: "half",
  });
  const form = await request.formData();
  const file = form.get("file");
  if (!file || typeof file.arrayBuffer !== "function") throw new Error("请选择 PDF 文件");
  const originalName = sanitizeFileName(file.name);
  if (!originalName.toLowerCase().endsWith(".pdf")) throw new Error("仅支持 PDF 文件");
  if (file.size > MAX_FILE_BYTES) {
    const error = new Error("PDF 超过 200 MB 限制");
    error.status = 413;
    throw error;
  }
  const buffer = Buffer.from(await file.arrayBuffer());
  if (!isPdf(buffer)) throw new Error("文件不是有效的 PDF");

  let rawConfig = {};
  try { rawConfig = JSON.parse(String(form.get("config") || "{}")); }
  catch { throw new Error("任务配置格式错误"); }
  const config = validateConfig(rawConfig);
  const apiKey = String(form.get("apiKey") || "").trim();
  if (config.provider === "openai" && !apiKey) throw new Error("OpenAI-compatible 接口需要 API key");

  const glossaryFile = form.get("glossary");
  const glossaryName = String(form.get("glossaryName") || "").trim();
  let glossaryText = null;
  if (glossaryFile && typeof glossaryFile.text === "function" && glossaryFile.size > 0) {
    if (glossaryFile.size > 512 * 1024) throw new Error("术语表超过 512 KB 限制");
    glossaryText = parseGlossaryCsv(await glossaryFile.text());
  } else if (glossaryName) {
    const safeName = sanitizeGlossaryName(glossaryName);
    const libraryPath = path.join(GLOSSARIES_DIR, `${safeName}.csv`);
    if (!libraryPath.startsWith(path.resolve(GLOSSARIES_DIR) + path.sep)) throw new Error("术语库名称非法");
    const exists = await access(libraryPath).then(() => true).catch(() => false);
    if (!exists) throw new Error(`术语库中没有「${safeName}」，请先保存或重新选择`);
    glossaryText = parseGlossaryCsv(await readFile(libraryPath, "utf8"));
  }

  const id = randomUUID();
  const dir = path.join(JOBS_DIR, id);
  const outputDir = path.join(dir, "output");
  const inputName = originalName;
  const inputPath = path.join(dir, inputName);
  const requestPath = path.join(dir, "request.json");
  await mkdir(outputDir, { recursive: true });
  let glossaryPath = null;
  if (glossaryText !== null) {
    glossaryPath = path.join(dir, "glossary.csv");
    await writeFile(glossaryPath, glossaryText, "utf8");
  }
  await writeFile(inputPath, buffer);
  await renderPagePng(inputPath, path.join(dir, "input-page.png"), 0);
  // 先注册协议网关(会写入 config.gatewayId),再落盘 request.json,
  // 保证 worker 读到的请求里带有网关配置
  registerGateway(config, apiKey || "ollama");
  await writeFile(requestPath, JSON.stringify({ inputPath, outputDir, glossaryPath, config }, null, 2), "utf8");

  const job = {
    id,
    fileName: originalName,
    inputName,
    fileSize: file.size,
    config,
    status: "queued",
    stage: "等待执行",
    progress: 0,
    createdAt: new Date().toISOString(),
    startedAt: null,
    finishedAt: null,
    outputs: [],
    error: null,
    log: [],
    cancelRequested: false,
    inputPath,
    outputDir,
    requestPath,
  };
  jobs.set(id, job);
  secrets.set(id, apiKey || "ollama");
  // registerGateway 已在写 request.json 之前完成(config.gatewayId 已就位)
  queue.push(id);
  await persistJobs();
  void pump();
  return publicJob(job);
}

async function serveResult(res, job, relativeName, inline = false) {
  if (!job.outputs.includes(relativeName)) return json(res, 404, { error: "结果文件不存在" });
  const filePath = path.resolve(job.outputDir, relativeName);
  if (!filePath.startsWith(path.resolve(job.outputDir) + path.sep)) return json(res, 400, { error: "非法文件路径" });
  const info = await stat(filePath);
  const extension = filePath.toLowerCase().split(".").pop();
  const contentType = { csv: "text/csv; charset=utf-8", json: "application/json; charset=utf-8" }[extension] || "application/pdf";
  res.writeHead(200, {
    "content-type": contentType,
    "content-length": info.size,
    "content-disposition": `${inline ? "inline" : "attachment"}; filename*=UTF-8''${encodeURIComponent(path.basename(filePath))}`,
  });
  createReadStream(filePath).pipe(res);
}

function readJsonBody(req, limit = 64 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("请求体过大"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(new Error("请求体不是有效 JSON"));
      }
    });
    req.on("error", reject);
  });
}

async function providerModels(baseUrl, apiKey, protocol = "openai") {
  let response;
  try {
    if (protocol === "anthropic") {
      response = await fetch(`${parseHttpUrl(baseUrl)}/v1/models`, {
        headers: { "x-api-key": apiKey || "", "anthropic-version": "2023-06-01" },
        signal: AbortSignal.timeout(10_000),
      });
    } else if (protocol === "gemini") {
      response = await fetch(`${parseHttpUrl(baseUrl)}/models?key=${encodeURIComponent(apiKey || "")}`, {
        signal: AbortSignal.timeout(10_000),
      });
    } else {
      response = await fetch(`${parseHttpUrl(baseUrl)}/models`, {
        headers: { authorization: `Bearer ${apiKey || "ollama"}` },
        signal: AbortSignal.timeout(10_000),
      });
    }
  } catch (error) {
    if (error.name === "TimeoutError") throw new Error("获取模型列表超时，请确认服务是否在运行");
    throw new Error(`无法连接到接口：${error.message}`);
  }
  if (!response.ok) throw new Error(`模型列表请求失败（HTTP ${response.status}）`);
  const data = await response.json();
  let models;
  if (protocol === "gemini") {
    models = (data.models || []).map((item) => String(item.name || "").replace(/^models\//, ""));
  } else {
    models = (data.data || data.models || []).map((item) => item?.id || item?.name);
  }
  return { models: [...new Set(models.filter((id) => typeof id === "string" && id))].sort() };
}

async function providerTest({ baseUrl, model, apiKey, protocol = "openai" }) {
  const target = parseHttpUrl(baseUrl);
  const name = String(model || "").trim();
  if (!name) throw new Error("请先填写模型名称");
  const safeProtocol = ["openai", "anthropic", "gemini"].includes(protocol) ? protocol : "openai";
  const started = Date.now();
  let response;
  try {
    if (safeProtocol === "openai") {
      response = await fetch(`${target}/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${apiKey || "ollama"}` },
        body: JSON.stringify({
          model: name,
          messages: [{ role: "user", content: "Translate to Chinese, output the translation only: paper" }],
          max_tokens: 1000,
          temperature: 0,
          stream: false,
        }),
        signal: AbortSignal.timeout(90_000),
      });
    } else {
      // anthropic/gemini 协议：注册临时网关配置，发 OpenAI 格式请求由网关转换，测完即清
      const gatewayId = randomUUID();
      gatewayRegistry.set(gatewayId, { protocol: safeProtocol, baseUrl: target, apiKey: apiKey || "" });
      try {
        response = await fetch(`http://127.0.0.1:${PORT}/api/llm-gateway/${gatewayId}/v1/chat/completions`, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: "Bearer gateway-test" },
          body: JSON.stringify({
            model: name,
            messages: [{ role: "user", content: "Translate to Chinese, output the translation only: paper" }],
            max_tokens: 1000,
            temperature: 0,
            stream: false,
          }),
          signal: AbortSignal.timeout(90_000),
        });
      } finally {
        gatewayRegistry.delete(gatewayId);
      }
    }
  } catch (error) {
    if (error.name === "TimeoutError") throw new Error("连接超时（90 秒），请检查地址、模型是否已下载或服务是否在运行");
    throw new Error(`无法连接到接口：${error.message}`);
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.error?.message || `HTTP ${response.status}`;
    throw new Error(`接口返回错误：${detail}`);
  }
  const reply = String(data?.choices?.[0]?.message?.content || "").trim();
  if (!reply) throw new Error("接口连通，但没有返回译文内容，请确认该模型可用");
  return {
    ok: true,
    latencyMs: Date.now() - started,
    reply: reply.slice(0, 80),
    model: data?.model || name,
  };
}

function renderPagePng(pdfPath, outPath, pageIndex = 0) {
  // 预览用页面图,失败不影响任务本身;参数经 stdin 传入,不落命令行
  return new Promise((resolve) => {
    const child = spawn(PYTHON, [WORKER_FILE], {
      cwd: ROOT,
      windowsHide: true,
      // 统一 UTF-8:中文安装路径下,默认本地编码会导致 worker 崩溃
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    });
    let stderrTail = "";
    const timer = setTimeout(() => {
      stopChild(child);
      resolve(false);
    }, 20000);
    child.stderr.on("data", (chunk) => { stderrTail = (stderrTail + chunk).slice(-300); });
    child.once("error", () => { clearTimeout(timer); resolve(false); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && existsSync(outPath)) return resolve(true);
      console.warn(`[页面预览] 渲染失败(code ${code})：${stderrTail.trim() || "无 stderr"}`);
      resolve(false);
    });
    child.stdin.end(JSON.stringify({ mode: "render", pdfPath, outPath, page: pageIndex }), "utf8");
  });
}

async function servePageImage(res, job, name) {
  const filePath = path.join(path.dirname(job.inputPath), name);
  if (!filePath.startsWith(path.resolve(JOBS_DIR))) return json(res, 400, { error: "非法路径" });
  if (!existsSync(filePath)) return json(res, 404, { error: "页面图尚未生成" });
  const body = await readFile(filePath);
  res.writeHead(200, {
    "content-type": "image/png",
    "content-length": body.length,
    "cache-control": "no-store",
  });
  res.end(body);
}

function needsGateway(config) {
  return ["anthropic", "gemini"].includes(config?.protocol);
}

function registerGateway(config, apiKey) {
  if (!needsGateway(config)) return;
  const gatewayId = randomUUID();
  gatewayRegistry.set(gatewayId, { protocol: config.protocol, baseUrl: config.baseUrl, apiKey });
  config.gatewayId = gatewayId;
}

function runRevise({ translatedPdf, originalPdf, outPath, keepOriginal }) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [WORKER_FILE], { cwd: ROOT, windowsHide: true });
    let stderrTail = "";
    const timer = setTimeout(() => {
      stopChild(child);
      reject(new Error("修订版生成超时"));
    }, 60_000);
    child.stderr.on("data", (chunk) => { stderrTail = (stderrTail + chunk).slice(-300); });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && existsSync(outPath)) return resolve(true);
      reject(new Error(`修订版生成失败：${stderrTail.trim() || `退出码 ${code}`}`));
    });
    child.stdin.end(JSON.stringify({ mode: "revise", translatedPdf, originalPdf, outPath, keepOriginal }), "utf8");
  });
}

async function buildRevisedPdf(res, job, body) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法生成修订版" });
  const pages = [...new Set((Array.isArray(body?.pages) ? body.pages : [])
    .map((p) => Math.round(Number(p)))
    .filter((p) => Number.isInteger(p) && p >= 1))].sort((a, b) => a - b);
  if (!pages.length) return json(res, 400, { error: "请先选择要保留原文的页码" });
  const translatedPdf = (job.outputs || []).find((name) => name.toLowerCase().endsWith(".pdf"));
  if (!translatedPdf) return json(res, 409, { error: "任务没有可用的译文 PDF" });
  const translatedPath = path.join(job.outputDir, translatedPdf);
  const outPath = path.join(job.outputDir, "revised-output.pdf");
  try {
    await runRevise({ translatedPdf: translatedPath, originalPdf: job.inputPath, outPath, keepOriginal: pages });
  } catch (error) {
    return json(res, 500, { error: error.message });
  }
  if (!job.outputs.includes("revised-output.pdf")) job.outputs.push("revised-output.pdf");
  await persistJobs();
  return json(res, 200, { ok: true, file: "revised-output.pdf", keptOriginalPages: pages });
}

function runEstimate(filePath) {  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [WORKER_FILE], {
      cwd: ROOT,
      windowsHide: true,
      // 统一 UTF-8:中文安装路径下,默认本地编码会导致 worker 崩溃
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    });
    let out = "";
    let stderrTail = "";
    const timer = setTimeout(() => {
      stopChild(child);
      reject(new Error("预估超时，请重试"));
    }, 30_000);
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.stderr.on("data", (chunk) => { stderrTail = (stderrTail + chunk).slice(-300); });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      const match = out.match(/YIYE_ESTIMATE: (.*)/) || [];
      if (code !== 0 || !match[1]) {
        return reject(new Error(`预估失败：${stderrTail.trim() || "请确认翻译引擎已安装（uv sync）"}`));
      }
      try { resolve(JSON.parse(match[1])); } catch { reject(new Error("预估结果解析失败")); }
    });
    child.stdin.end(JSON.stringify({ mode: "estimate", pdfPath: filePath, ocr: true }), "utf8");
  });
}

async function estimateRequest(req) {
  if (!(await engineReady())) {
    const error = new Error("翻译引擎尚未安装，请先在项目目录运行 uv sync");
    error.status = 503;
    throw error;
  }
  const request = new Request(`http://${HOST}:${PORT}/api/estimate`, {
    method: "POST",
    headers: req.headers,
    body: req,
    duplex: "half",
  });
  const form = await request.formData();
  const file = form.get("file");
  if (!file || typeof file.arrayBuffer !== "function") throw new Error("请选择 PDF 文件");
  const originalName = sanitizeFileName(file.name);
  if (!originalName.toLowerCase().endsWith(".pdf")) throw new Error("仅支持 PDF 文件");
  if (file.size > MAX_FILE_BYTES) {
    const error = new Error("PDF 超过 200 MB 限制");
    error.status = 413;
    throw error;
  }
  const buffer = Buffer.from(await file.arrayBuffer());
  if (!isPdf(buffer)) throw new Error("文件不是有效的 PDF");
  const tmpPath = path.join(DATA_DIR, `estimate-${randomUUID()}.pdf`);
  try {
    await writeFile(tmpPath, buffer);
    return await runEstimate(tmpPath);
  } finally {
    await rm(tmpPath, { force: true }).catch(() => {});
  }
}

async function listGlossaries() {
  const entries = [];
  for (const file of await readdir(GLOSSARIES_DIR, { withFileTypes: true }).catch(() => [])) {
    if (!file.isFile() || !file.name.toLowerCase().endsWith(".csv")) continue;
    const fullPath = path.join(GLOSSARIES_DIR, file.name);
    try {
      const text = await readFile(fullPath, "utf8");
      entries.push({
        name: file.name.replace(/\.csv$/i, ""),
        entries: countGlossaryEntries(text),
        bytes: (await stat(fullPath)).size,
      });
    } catch {}
  }
  return entries.sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
}

async function glossaryLibraryRequest(req, res, name) {
  if (req.method === "GET" && !name) {
    return json(res, 200, await listGlossaries());
  }
  if (req.method === "GET" && name) {
    const safeName = sanitizeGlossaryName(decodeURIComponent(name));
    const filePath = path.join(GLOSSARIES_DIR, `${safeName}.csv`);
    if (!filePath.startsWith(path.resolve(GLOSSARIES_DIR) + path.sep)) return json(res, 400, { error: "非法路径" });
    try {
      const body = await readFile(filePath);
      res.writeHead(200, {
        "content-type": "text/csv; charset=utf-8",
        "content-length": body.length,
        "content-disposition": `attachment; filename*=UTF-8''${encodeURIComponent(safeName + ".csv")}`,
      });
      return res.end(body);
    } catch {
      return json(res, 404, { error: "术语表不存在" });
    }
  }
  if (req.method === "POST" && !name) {
    const request = new Request(`http://${HOST}:${PORT}/api/glossaries`, {
      method: "POST",
      headers: req.headers,
      body: req,
      duplex: "half",
    });
    const form = await request.formData();
    const file = form.get("file");
    if (!file || typeof file.text !== "function" || !file.size) throw new Error("请选择术语表 CSV 文件");
    if (file.size > 512 * 1024) throw new Error("术语表超过 512 KB 限制");
    const text = parseGlossaryCsv(await file.text());
    const safeName = sanitizeGlossaryName(form.get("name") || file.name);
    await mkdir(GLOSSARIES_DIR, { recursive: true });
    await writeFile(path.join(GLOSSARIES_DIR, `${safeName}.csv`), text, "utf8");
    return json(res, 201, { name: safeName, entries: countGlossaryEntries(text) });
  }
  if (req.method === "DELETE" && name) {
    const safeName = sanitizeGlossaryName(decodeURIComponent(name));
    const target = path.join(GLOSSARIES_DIR, `${safeName}.csv`);
    if (!target.startsWith(path.resolve(GLOSSARIES_DIR) + path.sep)) return json(res, 400, { error: "非法路径" });
    await rm(target, { force: false }).catch(() => {
      const error = new Error("术语表不存在");
      error.status = 404;
      throw error;
    });
    return json(res, 200, { ok: true, name: safeName });
  }
  return json(res, 405, { error: "不支持的请求方式" });
}

function isFinishedJob(job) {
  return !["queued", "running"].includes(job.status);
}

async function deleteJob(res, jobId) {
  const job = jobs.get(jobId);
  if (!job) return json(res, 404, { error: "任务不存在" });
  if (!isFinishedJob(job)) return json(res, 409, { error: "任务进行中，请先取消再删除" });
  jobs.delete(jobId);
  await rm(path.join(JOBS_DIR, jobId), { recursive: true, force: true }).catch(() => {});
  await persistJobs();
  return json(res, 200, { ok: true, id: jobId });
}

async function clearFinishedJobs(res) {
  const finished = [...jobs.values()].filter(isFinishedJob);
  for (const job of finished) {
    jobs.delete(job.id);
    await rm(path.join(JOBS_DIR, job.id), { recursive: true, force: true }).catch(() => {});
  }
  await persistJobs();
  return json(res, 200, { ok: true, removed: finished.length });
}

function readRawBody(req, limit = 8 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("请求体过大"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function openaiChunk(id, model, content) {
  return `data: ${JSON.stringify({
    id,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, delta: { role: "assistant", content }, finish_reason: null }],
  })}\n\ndata: ${JSON.stringify({ id, object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\ndata: [DONE]\n\n`;
}

function respondOpenaiText(res, body, id, model, text, usage) {
  if (body.stream) {
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-store", connection: "keep-alive" });
    res.write(openaiChunk(id, model, text));
    return res.end();
  }
  return json(res, 200, {
    id,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
    usage: usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  });
}

const THINKING_BUDGETS = { anthropic: { low: 4096, medium: 8192, high: 16384 }, gemini: { low: 1024, medium: 8192, high: 24576 } };

// BabelDOC 传来两种思考信号:reasoning("low"/"medium"/"high")与 thinking:{type:"disabled"}。
// 按上游协议转换为各自的思考参数。
export function applyThinkingToUpstream(upstreamBody, openaiBody, protocol) {
  if (protocol === "anthropic") {
    const level = typeof openaiBody.reasoning === "string" ? openaiBody.reasoning.toLowerCase() : "";
    if (!openaiBody.thinking && ["low", "medium", "high"].includes(level)) {
      upstreamBody.thinking = { type: "enabled", budget_tokens: THINKING_BUDGETS.anthropic[level] || 8192 };
      delete upstreamBody.temperature; // Claude 思考模式不支持 temperature
    }
    return;
  }
  if (protocol === "gemini") {
    if (openaiBody.thinking?.type === "disabled") {
      upstreamBody.generationConfig.thinkingConfig = { thinkingBudget: 0 };
      return;
    }
    const level = typeof openaiBody.reasoning === "string" ? openaiBody.reasoning.toLowerCase() : "";
    const budgets = { low: 1024, medium: 8192, high: 24576 };
    if (budgets[level]) upstreamBody.generationConfig.thinkingConfig = { thinkingBudget: budgets[level] };
  }
}

async function gatewayChat(req, res, gatewayId) {
  const provider = gatewayRegistry.get(gatewayId);
  if (!provider) {
    return json(res, 404, { error: { message: "网关配置不存在（任务可能已结束），请重新提交或重试任务" } });
  }
  let body;
  try {
    body = JSON.parse((await readRawBody(req)).toString("utf8") || "{}");
  } catch {
    return json(res, 400, { error: { message: "请求体不是有效 JSON" } });
  }
  const messages = Array.isArray(body.messages) ? body.messages : [];
  const systemText = messages.filter((m) => m.role === "system").map((m) => String(m.content ?? "")).join("\n").trim();
  const chat = messages.filter((m) => m.role !== "system");
  const id = `gw-${randomUUID()}`;
  const maxTokens = Number(body.max_tokens) > 0 ? Number(body.max_tokens) : 8192;

  try {
    if (provider.protocol === "anthropic") {
      const upstreamBody = {
        model: body.model,
        max_tokens: maxTokens,
        ...(body.temperature !== undefined ? { temperature: body.temperature } : {}),
        ...(systemText ? { system: systemText } : {}),
        messages: chat.map((m) => ({ role: m.role, content: String(m.content ?? "") })),
      };
      applyThinkingToUpstream(upstreamBody, body, "anthropic");
      const upstream = await fetch(`${provider.baseUrl}/v1/messages`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": provider.apiKey, "anthropic-version": "2023-06-01" },
        body: JSON.stringify(upstreamBody),
        signal: AbortSignal.timeout(300000),
      });
      const data = await upstream.json().catch(() => null);
      if (!upstream.ok) {
        return json(res, upstream.status, { error: { message: data?.error?.message || `上游返回 HTTP ${upstream.status}` } });
      }
      const text = (Array.isArray(data.content) ? data.content : []).filter((part) => part?.type === "text").map((part) => part.text || "").join("");
      return respondOpenaiText(res, body, id, body.model, text, {
        prompt_tokens: data.usage?.input_tokens ?? 0,
        completion_tokens: data.usage?.output_tokens ?? 0,
        total_tokens: (data.usage?.input_tokens ?? 0) + (data.usage?.output_tokens ?? 0),
      });
    }
    if (provider.protocol === "gemini") {
      const url = `${provider.baseUrl}/models/${encodeURIComponent(body.model)}:generateContent?key=${encodeURIComponent(provider.apiKey)}`;
      const upstreamBody = {
        ...(systemText ? { systemInstruction: { parts: [{ text: systemText }] } } : {}),
        contents: chat.map((m) => ({ role: m.role === "assistant" ? "model" : "user", parts: [{ text: String(m.content ?? "") }] })),
        generationConfig: {
          ...(body.temperature !== undefined ? { temperature: body.temperature } : {}),
          maxOutputTokens: maxTokens,
        },
      };
      applyThinkingToUpstream(upstreamBody, body, "gemini");
      const upstream = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(upstreamBody),
        signal: AbortSignal.timeout(300000),
      });
      const data = await upstream.json().catch(() => null);
      if (!upstream.ok) {
        return json(res, upstream.status, { error: { message: data?.error?.message || `上游返回 HTTP ${upstream.status}` } });
      }
      const text = (data.candidates?.[0]?.content?.parts || []).map((part) => part.text || "").join("");
      return respondOpenaiText(res, body, id, body.model, text, {
        prompt_tokens: data.usageMetadata?.promptTokenCount ?? 0,
        completion_tokens: data.usageMetadata?.candidatesTokenCount ?? 0,
        total_tokens: data.usageMetadata?.totalTokenCount ?? 0,
      });
    }
    return json(res, 400, { error: { message: `网关不支持协议 ${provider.protocol}` } });
  } catch (error) {
    if (error.name === "TimeoutError") {
      return json(res, 504, { error: { message: "网关上游请求超时" } });
    }
    return json(res, 502, { error: { message: `网关上游请求失败：${error.message}` } });
  }
}

async function retryJob(req, res, oldJobId) {
  const oldJob = jobs.get(oldJobId);
  if (!oldJob) return json(res, 404, { error: "任务不存在" });
  if (!["failed", "canceled", "interrupted"].includes(oldJob.status)) {
    return json(res, 409, { error: "只有失败、取消或中断的任务可以重试" });
  }
  if (!(await engineReady())) {
    const error = new Error("翻译引擎尚未安装，请先在项目目录运行 uv sync");
    error.status = 503;
    throw error;
  }
  const apiKey = String(req.headers["x-api-key"] || "").trim();
  if (oldJob.config.provider === "openai" && !apiKey) {
    return json(res, 400, { error: "请在上方 API key 输入框填写后重试（key 不落盘，无法复用上次的）" });
  }
  const id = randomUUID();
  const dir = path.join(JOBS_DIR, id);
  const outputDir = path.join(dir, "output");
  const inputName = oldJob.inputName || "source.pdf";
  const inputPath = path.join(dir, inputName);
  const requestPath = path.join(dir, "request.json");
  try {
    await mkdir(outputDir, { recursive: true });
    await copyFile(oldJob.inputPath, inputPath);
  } catch {
    return json(res, 410, { error: "原任务的 PDF 文件已不存在，无法重试，请重新上传" });
  }
  // 原任务用过自定义术语表时必须一并复制，否则重试后的翻译和术语质检都会偏离原配置
  let glossaryPath = null;
  const oldGlossary = path.join(path.dirname(oldJob.inputPath), "glossary.csv");
  if (await access(oldGlossary).then(() => true).catch(() => false)) {
    glossaryPath = path.join(dir, "glossary.csv");
    await copyFile(oldGlossary, glossaryPath);
  }
  // 重试用独立的 config 副本,先注册网关(写入 gatewayId)再落盘 request.json
  const retryConfig = JSON.parse(JSON.stringify(oldJob.config));
  delete retryConfig.gatewayId; // 旧 gatewayId 属于上次任务,重试重新注册
  registerGateway(retryConfig, apiKey || "ollama");
  await writeFile(requestPath, JSON.stringify({ inputPath, outputDir, glossaryPath, config: retryConfig }, null, 2), "utf8");

  const job = {
    id,
    fileName: oldJob.fileName,
    inputName,
    fileSize: oldJob.fileSize,
    config: retryConfig,
    status: "queued",
    stage: "等待执行（重试）",
    progress: 0,
    createdAt: new Date().toISOString(),
    startedAt: null,
    finishedAt: null,
    outputs: [],
    error: null,
    log: [],
    cancelRequested: false,
    retriedFrom: oldJobId,
    inputPath,
    outputDir,
    requestPath,
  };
  jobs.set(id, job);
  secrets.set(id, apiKey || "ollama");
  oldJob.retriedAs = id;
  // 网关已在写 request.json 前注册(retryConfig.gatewayId 已就位)
  queue.push(id);
  await persistJobs();
  void pump();
  return json(res, 202, publicJob(job));
}

async function loadQualityDetail(job) {
  if (!job.quality || job.status !== "completed") return null;
  try {
    return JSON.parse(await readFile(path.join(job.outputDir, "quality-report.json"), "utf8"));
  } catch {
    return null;
  }
}

export async function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || `${HOST}:${PORT}`}`);
  if (req.method === "GET" && url.pathname === "/") {
    const body = await readFile(APP_FILE);
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "content-length": body.length, "cache-control": "no-store" });
    return res.end(body);
  }
  if (req.method === "GET" && url.pathname === "/api/health") {
    return json(res, 200, { ok: true, engineReady: await engineReady(), activeJobId: active?.id || null });
  }
  if (req.method === "GET" && url.pathname === "/api/providers/models") {
    const baseUrl = url.searchParams.get("baseUrl") || "";
    const apiKey = req.headers["x-api-key"] || "";
    const protocol = ["openai", "anthropic", "gemini"].includes(url.searchParams.get("protocol")) ? url.searchParams.get("protocol") : "openai";
    return json(res, 200, await providerModels(baseUrl, String(apiKey), protocol));
  }
  if (req.method === "POST" && url.pathname === "/api/providers/test") {
    const body = await readJsonBody(req);
    return json(res, 200, await providerTest(body));
  }
  if (req.method === "POST" && url.pathname === "/api/estimate") {
    return json(res, 200, await estimateRequest(req));
  }
  if (req.method === "GET" && url.pathname === "/api/jobs") {
    const items = [...jobs.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)).map(publicJob);
    return json(res, 200, items);
  }
  if (req.method === "POST" && url.pathname === "/api/jobs") {
    return json(res, 202, await createJob(req));
  }
  const match = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)$/i);
  if (req.method === "GET" && match) {
    const job = jobs.get(match[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    const detail = await loadQualityDetail(job);
    return json(res, 200, { ...publicJob(job), qualityDetail: detail });
  }
  if (req.method === "DELETE" && match) {
    return deleteJob(res, match[1]);
  }
  if (req.method === "DELETE" && url.pathname === "/api/jobs") {
    return clearFinishedJobs(res);
  }
  const cancel = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/cancel$/i);
  if (req.method === "POST" && cancel) {
    const job = jobs.get(cancel[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    if (!["queued", "running"].includes(job.status)) return json(res, 409, { error: "任务已结束" });
    job.cancelRequested = true;
    if (active?.id === job.id) stopChild(active.child);
    else {
      job.status = "canceled";
      job.stage = "已取消";
      secrets.delete(job.id);
    }
    await persistJobs();
    return json(res, 200, publicJob(job));
  }
  const retry = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/retry$/i);
  if (req.method === "POST" && retry) {
    return retryJob(req, res, retry[1]);
  }
  const result = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/result\/(.+)$/i);
  if (req.method === "GET" && result) {
    const job = jobs.get(result[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return serveResult(res, job, decodeURIComponent(result[2]), url.searchParams.get("inline") === "1");
  }
  const pageImg = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/page-image\/(input-page\.png|output-page\.png)$/i);
  if (req.method === "GET" && pageImg) {
    const job = jobs.get(pageImg[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return servePageImage(res, job, pageImg[2]);
  }
  const gatewayMatch = url.pathname.match(/^\/api\/llm-gateway\/([0-9a-f-]+)\/v1\/chat\/completions$/i);
  if (req.method === "POST" && gatewayMatch) {
    return gatewayChat(req, res, gatewayMatch[1]);
  }
  const revisedMatch = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/revised-pdf$/i);
  if (req.method === "POST" && revisedMatch) {
    const job = jobs.get(revisedMatch[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    const body = await readJsonBody(req).catch(() => ({}));
    return buildRevisedPdf(res, job, body);
  }
  const glossaryMatch = url.pathname.match(/^\/api\/glossaries(?:\/([^/]+))?$/i);
  if (glossaryMatch && ["GET", "POST", "DELETE"].includes(req.method)) {
    return glossaryLibraryRequest(req, res, glossaryMatch[1]);
  }
  return json(res, 404, { error: "页面不存在" });
}

export async function startServer() {
  await loadJobs();
  const server = createServer((req, res) => {
    handle(req, res).catch((error) => {
      console.error(error);
      if (!res.headersSent) json(res, error.status || 400, { error: error.message || "请求失败" });
      else res.destroy();
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", (error) => {
      if (error.code === "EADDRINUSE") {
        console.error(`端口 ${PORT} 已被其他程序占用。请关闭占用程序，或用其他端口启动：YIYE_PORT=4174 npm start`);
        reject(error);
      } else reject(error);
    });
    server.listen(PORT, HOST, () => {
      console.log(`译页已启动：http://${HOST}:${PORT}`);
      if (!jobs.size) console.log("首次使用请先运行：uv sync");
      resolve(server);
    });
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await startServer();
}
