import { createServer } from "node:http";
import { existsSync, statSync } from "node:fs";
import { access, mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { createReadStream } from "node:fs";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const APP_FILE = path.join(ROOT, "app", "index.html");

// 默认翻译提示词模板(与 engine_worker.py 的 PROMPT_TEMPLATE 保持同步):
// 从 engine_worker 读取,保证单一事实来源
export async function getPromptTemplate() {
  try {
    const worker = await readFile(path.join(ROOT, "engine_worker.py"), "utf8");
    const match = worker.match(/PROMPT_TEMPLATE = \(\n([\s\S]*?)\n\)/);
    if (!match) throw new Error("template not found");
    const lines = match[1].split("\n")
      .map((line) => line.trim().replace(/^"|"$/g, "").replace(/\\n$/, "\n"))
      .join("")
      .replace(/\\n/g, "\n")
      .replace(/\{variant\}/g, "简体中文");
    return { template: lines.trim() };
  } catch (error) {
    return { template: "", error: error.message };
  }
}
// E2E 测试通过 YIYE_DATA_DIR 使用独立数据目录，避免影响真实任务数据
const DATA_DIR = process.env.YIYE_DATA_DIR || path.join(ROOT, "data");
const JOBS_DIR = path.join(DATA_DIR, "jobs");
const GLOSSARIES_DIR = path.join(DATA_DIR, "glossaries");
const JOBS_FILE = path.join(DATA_DIR, "jobs.json");
const PROMPT_TEMPLATE_FILE = path.join(DATA_DIR, "prompt-template.txt");
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
  if (input.dualLayout === "alternating") throw new Error("交替页面尚未完成质检与页码映射，请使用左右对照");
  const output = ["mono", "dual"].includes(input.output) ? input.output : "dual";
  const dualLayout = input.dualLayout === "alternating" ? "alternating" : "side";
  const provider = ["openai", "ollama"].includes(input.provider) ? input.provider : "ollama";
  const protocol = ["openai", "anthropic", "gemini"].includes(input.protocol) ? input.protocol : "openai";
  const thinking = ["default", "off", "low", "medium", "high"].includes(input.thinking) ? input.thinking : "default";
  // 本地服务一律走 OpenAI 兼容协议;anthropic/gemini 仅对云端入口有意义
  const effectiveProtocol = provider === "ollama" ? "openai" : protocol;
  const target = ["zh-CN", "zh-TW"].includes(input.target) ? input.target : "zh-CN";
  const qps = Math.max(1, Math.min(16, Number(input.qps) || 4));
  const defaults = provider === "ollama"
    ? { baseUrl: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" }
    : { baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini" };
  const baseUrl = parseHttpUrl(input.baseUrl || defaults.baseUrl);
  const model = String(input.model || defaults.model).trim();
  if (!model || model.length > 120) throw new Error("模型名称不能为空或过长");
  return {
    output,
    dualLayout,
    provider,
    protocol: effectiveProtocol,
    thinking,
    target,
    qps,
    baseUrl,
    model,
    pages: validatePages(input.pages),
    maxPagesPerPart: (() => {
      const raw = Number(input.maxPagesPerPart);
      return Number.isFinite(raw) && raw >= 20 && raw <= 500 ? Math.round(raw) : 0;
    })(),
    enhance: input.enhance === true,
    ignoreCache: input.ignoreCache === true,
    fontFamily: ["serif", "sans-serif", "script"].includes(input.fontFamily) ? input.fontFamily : "",
    ocr: input.ocr !== false,
    table: input.table === true,
    glossary: input.glossary !== false,
    aiSummary: input.aiSummary !== false,
    customPrompt: String(input.customPrompt ?? "").replace(/\s+/g, " ").trim().slice(0, 500),
    figure: input.figure === true,
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
  return { ...safe, outputs: (job.outputs || []).filter((name) => name.toLowerCase().endsWith(".pdf")), log: log.slice(-30) };
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

function appendLog(job, chunk, apiKey = "", stream = { logBuffer: "" }) {
  // 过短的 key 做逐字替换会把日志里的正常单词一起污染，因此只脱敏足够长的 key
  const secret = apiKey && apiKey.length >= 8 ? apiKey : "";
  // stdout 与 stderr 各自持有行缓冲：两路管道的数据块边界互不相干，
  // 混用同一个缓冲会把半行拼成假行，破坏进度/token/结构化行的解析
  stream.logBuffer = (stream.logBuffer || "") + chunk.toString("utf8");
  const parts = stream.logBuffer.split(/\r\n|\r|\n/);
  stream.logBuffer = parts.pop();
  for (const raw of parts) {
    const line = raw.replace(/\u001b\[[0-9;]*m/g, "").trim();
    if (!line) continue;
    // 任何日志输出都算活动迹象,停滞看门狗据此判断任务是否还活着
    job.lastProgressAt = Date.now();
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
    // 百分比形态的日志(模型下载等)与真实翻译进度无关,不做进度映射:
    // 阶段进度完全由上面的分数式进度条驱动,避免早到的"100%"把进度条钉死
    if (/layout|parse|版式/i.test(clean)) job.stage = "正在分析版式";
    if (/translat|翻译/i.test(clean)) job.stage = "正在翻译正文";
    if (/render|generate|save|渲染|生成/i.test(clean)) job.stage = "正在重建 PDF";
  }
}

function flushLog(job, apiKey = "", streams = []) {
  for (const stream of streams) {
    if (stream?.logBuffer) appendLog(job, "\n", apiKey, stream);
    delete stream?.logBuffer;
  }
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

function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true });
    killer.on("error", () => {});
  } else child.kill("SIGTERM");
}

// 任务不再需要凭据时统一释放：API key 与协议网关配置都只驻留内存
function releaseJobSecrets(job) {
  secrets.delete(job.id);
  if (job.config?.gatewayId) gatewayRegistry.delete(job.config.gatewayId);
}

// 所有 Python 子进程的统一入口:UTF-8 环境 + stdin JSON 任务。
// translate 模式需要流式日志与取消,由 runJob 在此基础上自行处理。
function spawnWorker(extraEnv = {}) {
  const child = spawn(PYTHON, [WORKER_FILE], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1", YIYE_API_KEY: "not-needed", OLLAMA_KEEP_ALIVE: "30m", ...extraEnv },
    windowsHide: true,
  });
  // worker 启动失败立即退出时 stdin 可能触发 EPIPE;后果由 close 事件统一收尾,这里吞掉流错误
  child.stdin?.on("error", () => {});
  return child;
}

// 停滞判定:运行中的任务超过该时长没有任何日志输出(进度/Token/阶段行都算),
// 视为引擎挂死。阈值需大于 BabelDOC 的单请求超时(600 秒),避免误杀慢请求
export function isStalled(job, now = Date.now(), stallMs = Number(process.env.YIYE_STALL_MINUTES || 12) * 60_000) {
  if (job.status !== "running" || job.cancelRequested) return false;
  const raw = job.lastProgressAt ?? job.startedAt;
  const last = typeof raw === "number" ? raw : Number.isFinite(Number(raw)) ? Date.parse(raw) : NaN;
  if (!Number.isFinite(last)) return false;
  return now - last > stallMs;
}

async function runJob(job) {
  const apiKey = secrets.get(job.id) || "not-needed";
  job.status = "running";
  job.stage = "正在启动翻译引擎";
  job.progress = 2;
  job.startedAt = new Date().toISOString();
  await persistJobs();
  // 用户自定义提示词模板(数据目录)通过环境变量传给 worker;无自定义时用 worker 内置默认
  let promptEnv = {};
  try {
    const custom = (await readFile(PROMPT_TEMPLATE_FILE, "utf8")).trim();
    if (custom) promptEnv = { YIYE_PROMPT_TEMPLATE: custom };
  } catch {}

  // 取消可能发生在上面 persistJobs 的 await 窗口内（此时 active 尚未赋值，
  // 取消路由无法杀进程），所以 spawn 前必须再检查一次。
  if (job.cancelRequested) {
    job.status = "canceled";
    job.stage = "已取消";
    job.error = null;
    job.finishedAt = new Date().toISOString();
    releaseJobSecrets(job);
    await persistJobs();
    return;
  }

  const child = spawnWorker({ YIYE_API_KEY: apiKey, ...promptEnv });
  // worker 统一从 stdin 读取任务 JSON(translate 模式),路径不出现在命令行
  child.stdin.end(JSON.stringify({ mode: "translate", requestPath: job.requestPath }), "utf8");
  active = { id: job.id, child };
  job.lastProgressAt = Date.now();
  const stdout = { logBuffer: "" };
  const stderr = { logBuffer: "" };
  child.stdout.on("data", (chunk) => appendLog(job, chunk, apiKey, stdout));
  child.stderr.on("data", (chunk) => appendLog(job, chunk, apiKey, stderr));

  // 停滞看门狗:引擎挂死时自动终止,把"永远卡住"变成可重试的失败
  const stallMinutes = Number(process.env.YIYE_STALL_MINUTES || 12);
  const watchdog = setInterval(() => {
    if (isStalled(job)) {
      job.stalled = true;
      appendLog(job, `翻译停滞：超过 ${stallMinutes} 分钟无任何输出，自动终止任务`, apiKey);
      stopChild(child);
    }
  }, 15_000);

  const exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  }).catch((error) => {
    appendLog(job, error.message, apiKey);
    return -1;
  });
  clearInterval(watchdog);

  releaseJobSecrets(job);
  active = null;
  flushLog(job, apiKey, [stdout, stderr]);
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
  } else if (job.stalled) {
    job.status = "failed";
    job.stage = "翻译停滞已终止";
    job.error = `翻译引擎超过 ${stallMinutes} 分钟无任何输出，已自动终止（通常是模型服务挂起或网络中断）。可点击重试：已翻译段落经引擎缓存跳过，无需从头再来。`;
  } else if (exitCode === 0) {
    // JSON/CSV/HTML 是任务内部资料，和 PDF 一起存放、清理，仅展示 PDF 交付物。
    job.outputs = await walkPdfs(job.outputDir).catch(() => []);
    // 完成标准以译文 PDF 是否存在为准
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
      if (!job) continue;
      if (job.cancelRequested || job.status === "canceled") {
        releaseJobSecrets(job);
        continue;
      }
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

  const job = await createJobRecord({ buffer, originalName, fileSize: file.size, config, apiKey: apiKey || "ollama", glossaryText });
  queue.push(job.id);
  await persistJobs();
  void pump();
  return publicJob(job);
}

// 任务记录创建的统一入口:落盘 PDF、渲染预览图、注册网关、写 request.json 并入队。
// 上传接口与 MCP 的本地文件翻译共用。
async function createJobRecord({ buffer, originalName, fileSize, config, apiKey, glossaryText = null }) {
  const id = randomUUID();
  const dir = path.join(JOBS_DIR, id);
  const outputDir = path.join(dir, "output");
  const inputPath = path.join(dir, originalName);
  const requestPath = path.join(dir, "request.json");
  await mkdir(outputDir, { recursive: true });
  let glossaryPath = null;
  if (glossaryText) {
    glossaryPath = path.join(dir, "glossary.csv");
    await writeFile(glossaryPath, glossaryText, "utf8");
  }
  await writeFile(inputPath, buffer);
  await renderPagePng(inputPath, path.join(dir, "input-page.png"), 0);
  // 先注册协议网关(会写入 config.gatewayId),再落盘 request.json,
  // 保证 worker 读到的请求里带有网关配置
  registerGateway(config, apiKey);
  await writeFile(requestPath, JSON.stringify({ inputPath, outputDir, glossaryPath, config }, null, 2), "utf8");

  const job = {
    id,
    fileName: originalName,
    inputName: originalName,
    fileSize,
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
  secrets.set(id, apiKey);
  return job;
}

// ── MCP(Model Context Protocol)server:让 Claude/Cursor 等智能体驱动本机翻译器 ──
// 精简实现:streamable HTTP 上的 JSON-RPC,无状态,支持 initialize/ping/tools/list/tools/call。
const MCP_TOOLS = [
  {
    name: "translate_paper",
    description: "翻译一篇本地英文论文 PDF(默认本地 Ollama 模型,输出中文)。返回任务状态与结果文件路径。",
    inputSchema: {
      type: "object",
      properties: {
        pdf_path: { type: "string", description: "PDF 文件的绝对路径(本机)" },
        pages: { type: "string", description: "可选,页码范围,如 1-5,8" },
        output: { type: "string", enum: ["mono", "dual"], description: "mono=纯中文译文,dual=左右对照,默认 dual" },
        model: { type: "string", description: "可选,覆盖模型名(默认 qwen2.5:7b)" },
        wait: { type: "boolean", description: "是否等待翻译完成,默认 true" },
        timeout_sec: { type: "number", description: "等待超时秒数,默认 600,上限 1800" },
      },
      required: ["pdf_path"],
    },
  },
  {
    name: "get_job",
    description: "查询翻译任务的状态与输出文件。",
    inputSchema: {
      type: "object",
      properties: { job_id: { type: "string", description: "任务 ID" } },
      required: ["job_id"],
    },
  },
  {
    name: "list_jobs",
    description: "列出最近的翻译任务。",
    inputSchema: { type: "object", properties: {} },
  },
];

function mcpToolResult(text, isError = false) {
  return { content: [{ type: "text", text }], isError };
}

async function mcpDispatch(method, params = {}) {
  if (method === "initialize") {
    return { protocolVersion: "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "yiye-paper", version: "0.1.0" } };
  }
  if (method === "ping") return {};
  if (method === "tools/list") return { tools: MCP_TOOLS };
  if (method === "tools/call") {
    const name = String(params?.name || "");
    const args = params?.arguments || {};
    if (name === "translate_paper") return await mcpTranslatePaper(args);
    if (name === "get_job") {
      const job = jobs.get(String(args.job_id || ""));
      if (!job) return mcpToolResult(`任务不存在：${args.job_id}`, true);
      return mcpToolResult(mcpJobSummary(job));
    }
    if (name === "list_jobs") {
      const items = [...jobs.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)).slice(0, 10)
        .map((job) => `${job.id} ${job.status} ${job.progress}% ${job.fileName}`);
      return mcpToolResult(items.length ? items.join("\n") : "(暂无任务)");
    }
    return mcpToolResult(`未知工具：${name}`, true);
  }
  const error = new Error(`Unknown method: ${method}`);
  error.code = -32601;
  throw error;
}

function mcpJobSummary(job) {
  const lines = [`任务 ID：${job.id}`, `状态：${job.status}（${job.progress}%）`, `文件：${job.fileName}`];
  if (job.error) lines.push(`错误：${job.error}`);
  if (job.outputs?.length) lines.push(`输出：\n${job.outputs.map((o) => path.join(job.outputDir, o)).join("\n")}`);
  return lines.join("\n");
}

async function mcpTranslatePaper(args) {
  const pdfPath = path.resolve(String(args.pdf_path || ""));
  if (!/\.pdf$/i.test(pdfPath) || !existsSync(pdfPath)) {
    return mcpToolResult(`文件不存在或不是 PDF：${pdfPath}`, true);
  }
  let job;
  try {
    const buffer = await readFile(pdfPath);
    if (!isPdf(buffer)) return mcpToolResult(`文件不是有效的 PDF：${pdfPath}`, true);
    const config = validateConfig({
      provider: "ollama",
      output: args.output === "mono" ? "mono" : "dual",
      pages: String(args.pages || ""),
      ...(args.model ? { model: String(args.model) } : {}),
      ocr: true,
      table: false,
      glossary: false,
      aiSummary: true,
    });
    job = await createJobRecord({
      buffer,
      originalName: sanitizeFileName(path.basename(pdfPath)),
      fileSize: buffer.length,
      config,
      apiKey: "ollama",
    });
    queue.push(job.id);
    await persistJobs();
    void pump();
  } catch (error) {
    return mcpToolResult(`任务创建失败：${error.message}`, true);
  }
  const wait = args.wait !== false;
  const timeoutMs = Math.min(Number(args.timeout_sec) > 0 ? Number(args.timeout_sec) * 1000 : 600_000, 1_800_000);
  const deadline = Date.now() + timeoutMs;
  while (wait && Date.now() < deadline && ["queued", "running"].includes(job.status)) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  let text = mcpJobSummary(job);
  if (["queued", "running"].includes(job.status)) text += `\n进度：${job.progress}%（未结束，可用 get_job 继续查询）`;
  return mcpToolResult(text);
}

async function mcpHandler(req, res) {
  let rpc;
  try {
    rpc = JSON.parse((await readRawBody(req, 1024 * 1024)).toString("utf8"));
  } catch {
    return json(res, 400, { jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
  }
  const { id, method } = rpc || {};
  if (!method || (id === undefined && method.startsWith("notifications/"))) {
    res.writeHead(202);
    return res.end();
  }
  try {
    const result = await mcpDispatch(method, rpc.params || {});
    return json(res, 200, { jsonrpc: "2.0", id, result });
  } catch (error) {
    const code = error.code === -32601 ? -32601 : -32603;
    return json(res, 200, { jsonrpc: "2.0", id, error: { code, message: error.message } });
  }
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

// 模型测速结果缓存：同一 地址|模型 15 分钟内不重复探测
const speedCache = new Map();

// 实测模型生成速度（tok/s）：发一个要求确定性长输出的小请求,按 completion tokens 计时。
// 用于提交前估算翻译耗时;单路测量,不并发。
export async function measureModelSpeed({ baseUrl, model, apiKey }) {
  const key = `${parseHttpUrl(baseUrl)}|${model}`;
  const hit = speedCache.get(key);
  if (hit && Date.now() - hit.measuredAt < 15 * 60_000) return { ...hit, cached: true };
  const started = Date.now();
  let response;
  try {
    response = await fetch(`${parseHttpUrl(baseUrl)}/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${apiKey || "ollama"}` },
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: "List the numbers from 1 to 40 separated by commas, digits only, no other text." }],
        max_tokens: 256,
        temperature: 0,
        stream: false,
      }),
      signal: AbortSignal.timeout(90_000),
    });
  } catch (error) {
    if (error.name === "TimeoutError") throw new Error("测速请求超时（90 秒），请确认模型服务是否在运行");
    throw new Error(`测速失败：${error.message}`);
  }
  if (!response.ok) throw new Error(`测速失败：HTTP ${response.status}`);
  const data = await response.json().catch(() => null);
  const completionTokens = Number(data?.usage?.completion_tokens) || 0;
  // 极快响应下时钟差可能为 0,设下限避免除零
  const seconds = Math.max((Date.now() - started) / 1000, 0.001);
  if (!completionTokens) throw new Error("测速失败：接口未返回用量数据");
  const entry = {
    tokensPerSec: Math.round((completionTokens / seconds) * 10) / 10,
    completionTokens,
    seconds: Math.round(seconds * 10) / 10,
    measuredAt: Date.now(),
  };
  speedCache.set(key, entry);
  return { ...entry, cached: false };
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
    const child = spawnWorker();
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
  const filePath = path.resolve(path.dirname(job.inputPath), name);
  if (!filePath.startsWith(path.resolve(JOBS_DIR) + path.sep)) return json(res, 400, { error: "非法路径" });
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

function runRevise({ translatedPdf, originalPdf, outPath, keepOriginal, outputMode }) {
  return new Promise((resolve, reject) => {
    const child = spawnWorker();
    let stderrTail = "";
    const timer = setTimeout(() => {
      stopChild(child);
      reject(new Error("修订版生成超时"));    }, 120_000);
    child.stderr.on("data", (chunk) => { stderrTail = (stderrTail + chunk).slice(-300); });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && existsSync(outPath)) return resolve(true);
      reject(new Error(`修订版生成失败：${stderrTail.trim() || `退出码 ${code}`}`));
    });
    child.stdin.end(JSON.stringify({ mode: "revise", translatedPdf, originalPdf, outPath, keepOriginal, outputMode }), "utf8");
  });
}

async function buildRevisedPdf(res, job, body) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法生成修订版" });
  const pages = [...new Set((Array.isArray(body?.pages) ? body.pages : []).map(Number))].sort((a, b) => a - b);
  if (pages.some((p) => !Number.isInteger(p) || p < 1)) return json(res, 400, { error: "页码必须为正整数" });
  if (!pages.length) return json(res, 400, { error: "请先选择要保留原文的页码" });
  // 修订必须始终以原始译文为源:把上一次的 revised-output.pdf 当源会让
  // 已"保留原文"的页永久丢失译文,之后想恢复也恢复不回来
  const translatedPdf = (job.outputs || []).find((name) => name.toLowerCase().endsWith(".pdf") && name !== "revised-output.pdf");
  if (!translatedPdf) return json(res, 409, { error: "任务没有可用的译文 PDF" });
  const translatedPath = path.join(job.outputDir, translatedPdf);
  const outPath = path.join(job.outputDir, "revised-output.pdf");
  try {
    await runRevise({ translatedPdf: translatedPath, originalPdf: job.inputPath, outPath, keepOriginal: pages, outputMode: job.config.output });
  } catch (error) {
    return json(res, 500, { error: error.message });
  }
  if (!job.outputs.includes("revised-output.pdf")) job.outputs.push("revised-output.pdf");
  await persistJobs();
  return json(res, 200, { ok: true, file: "revised-output.pdf", keptOriginalPages: pages });
}

function runEstimate(filePath) {
  return new Promise((resolve, reject) => {
    const child = spawnWorker();
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
  await removeJobFiles(job);
  await persistJobs();
  return json(res, 200, { ok: true, id: jobId });
}

async function clearFinishedJobs(res) {
  const finished = [...jobs.values()].filter(isFinishedJob);
  try {
    for (const job of finished) await removeJobFiles(job);
  } finally {
    // 即使某个目录被占用，也保存此前已成功清理的记录。
    await persistJobs();
  }
  return json(res, 200, { ok: true, removed: finished.length });
}

async function removeJobFiles(job) {
  const target = path.resolve(JOBS_DIR, job.id);
  if (path.dirname(target) !== path.resolve(JOBS_DIR)) throw new Error("非法任务目录");
  try {
    await rm(target, { recursive: true, force: true, maxRetries: 2, retryDelay: 100 });
  } catch {
    const error = new Error("任务文件清理失败，请关闭正在使用的 PDF 后重试；任务记录已保留");
    error.status = 409;
    throw error;
  }
  releaseJobSecrets(job);
  jobs.delete(job.id);
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
      upstreamBody.max_tokens = Math.max(Number(upstreamBody.max_tokens) || 0, upstreamBody.thinking.budget_tokens + 1024);
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

// 用任务配置的模型发一次补全:兼容接口直连,anthropic/gemini 经临时网关转换(用完即释放)
async function callConfiguredModel(config, apiKey, messages, { maxTokens = 1500, temperature = 0.3 } = {}) {
  const payload = JSON.stringify({ model: config.model, messages, max_tokens: maxTokens, temperature, stream: false });
  let response;
  if (needsGateway(config)) {
    const gatewayId = randomUUID();
    gatewayRegistry.set(gatewayId, { protocol: config.protocol, baseUrl: config.baseUrl, apiKey: apiKey || "ollama" });
    try {
      response = await fetch(`http://127.0.0.1:${PORT}/api/llm-gateway/${gatewayId}/v1/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: "Bearer gateway-chat" },
        body: payload,
        signal: AbortSignal.timeout(180_000),
      });
    } finally {
      gatewayRegistry.delete(gatewayId);
    }
  } else {
    response = await fetch(`${parseHttpUrl(config.baseUrl)}/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${apiKey || "ollama"}` },
      body: payload,
      signal: AbortSignal.timeout(180_000),
    });
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.error?.message || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  const answer = String(data?.choices?.[0]?.message?.content || "").trim();
  if (!answer) {
    const error = new Error("模型没有返回内容，请重试");
    error.status = 502;
    throw error;
  }
  return { answer, model: data?.model || config.model };
}

export function readingModelConfig(override, fallback) {
  if (override == null) return fallback;
  if (!override || typeof override !== "object" || !override.baseUrl || !override.model) {
    throw new Error("阅读助手模型配置不完整，请选择接口和模型");
  }
  return validateConfig({ ...fallback, ...override });
}

export function auditCitations(answer, availableRefs, multi = false) {
  const available = new Set(availableRefs || []);
  const pattern = multi
    ? /【文档\s*(\d+)\s*第\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*页】/g
    : /【第\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*页】/g;
  const found = [];
  for (const match of String(answer || "").matchAll(pattern)) {
    const doc = multi ? Number(match[1]) : null;
    const start = Number(match[multi ? 2 : 1]);
    const end = Number(match[multi ? 3 : 2] || start);
    let valid = Number.isInteger(start) && Number.isInteger(end) && end >= start && end - start <= 100;
    for (let page = start; valid && page <= end; page += 1) {
      if (!available.has(multi ? `${doc}:${page}` : String(page))) valid = false;
    }
    found.push({ reference: match[0], valid });
  }
  const invalid = found.filter((item) => !item.valid).map((item) => item.reference);
  const validCount = found.length - invalid.length;
  const warning = !found.length
    ? "回答没有提供页码引用，请结合原文核对"
    : invalid.length
      ? `发现 ${invalid.length} 个超出当前上下文范围的页码引用，请勿直接采信`
      : "";
  return { count: found.length, validCount, invalid, warning };
}

// 任务问答:基于任务译文全文,用任务配置的同一模型回答提问。
// LLM 端点是用户自己在应用内配置的服务(与翻译请求同源),不走任意 URL 抓取。
async function jobChat(req, res, job) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法问答" });
  let body;
  try {
    body = await readJsonBody(req, 64 * 1024);
  } catch {
    return json(res, 400, { error: "请求体无效" });
  }
  const question = String(body?.question || "").trim();
  if (!question) return json(res, 400, { error: "请输入问题" });
  if (question.length > 2000) return json(res, 400, { error: "问题过长（最多 2000 字）" });
  const history = (Array.isArray(body.history) ? body.history : [])
    .filter((m) => m && ["user", "assistant"].includes(m.role) && typeof m.content === "string")
    .slice(-6)
    .map((m) => ({ role: m.role, content: m.content.slice(0, 4000) }));

  const textPath = path.join(job.outputDir, "translated-text.txt");
  let context;
  try {
    // 预算按最坏 1 字符≈1 token 估:30k 字符留足系统提示与输出空间,不超 32k 上下文
    context = (await readFile(textPath, "utf8")).slice(0, 30000);
  } catch {
    return json(res, 409, { error: "该任务没有译文全文数据（较早版本生成的任务），重试一次即可生成" });
  }

  const apiKey = String(req.headers["x-api-key"] || "").trim();
  const config = readingModelConfig(body.model, job.config);
  // 译文全文带【第 N 页】标记时,要求回答标注页码,前端渲染为跳转链接
  const withCitations = context.includes("【第");
  const citationRule = withCitations
    ? "关键论断需在句末标注来源页码，格式如【第 3 页】，页码只能取自上下文中出现的页码标记；"
    : "";
  const messages = [
    {
      role: "system",
      content: `你是论文阅读助手。只依据给出的论文中文译文回答问题；译文里没有的信息就明确说译文中未提及。${citationRule}用中文回答，简明扼要，可直接引用译文。`,
    },
    { role: "user", content: `以下是论文的中文译文全文：\n\n${context}` },
    ...history,
    { role: "user", content: question },
  ];
  try {
    const { answer, model } = await callConfiguredModel(config, apiKey, messages);
    const availableRefs = [...context.matchAll(/【第\s*(\d+)\s*页】/g)].map((match) => match[1]);
    return json(res, 200, { answer, model, citationCheck: auditCitations(answer, availableRefs) });
  } catch (error) {
    if (error.name === "TimeoutError") return json(res, 504, { error: "问答超时（180 秒），请稍后重试或换用更快的模型" });
    return json(res, error.status || 502, { error: `问答失败：${error.message}` });
  }
}

// 论文结构思维导图:基于译文全文生成 markdown 大纲(对标竞品的「AI 思维导图」),
// 结果持久化到 mindmap.json,二次点击直接返回缓存
async function jobMindmap(req, res, job) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法生成思维导图" });
  let body = {};
  try {
    body = await readJsonBody(req, 4096);
  } catch {
    return json(res, 400, { error: "请求体无效" });
  }
  const mmPath = path.join(job.outputDir, "mindmap.json");
  if (body?.refresh !== true) {
    try {
      const cached = JSON.parse(await readFile(mmPath, "utf8"));
      if (typeof cached?.markdown === "string" && cached.markdown.trim()) return json(res, 200, cached);
    } catch {}
  }
  const textPath = path.join(job.outputDir, "translated-text.txt");
  let fullText;
  try {
    fullText = await readFile(textPath, "utf8");
  } catch {
    return json(res, 409, { error: "该任务没有译文全文数据（较早版本生成的任务），重试一次即可生成" });
  }
  // 思维导图只需结构,上下文预算 24k 字符(最坏 1 字符≈1 token)
  const context = fullText.slice(0, 24000);
  const apiKey = String(req.headers["x-api-key"] || "").trim();
  const messages = [
    {
      role: "system",
      content: "你是论文阅读助手。请基于给定的论文中文译文，生成论文结构思维导图，以 markdown 无序列表输出。严格按以下格式：\n- 主题：<用一句话概括论文主题>\n  - 核心问题\n    - <要点，一句话>\n  - 方法\n    - <要点，一句话>\n    - <要点，一句话>\n  - 实验与结果\n    - <要点，一句话>\n  - 结论与局限\n    - <要点，一句话>\n要求：按译文实际内容填满每个节点；总条目不少于 10 行；除列表外不要输出任何其他文字。",
    },
    { role: "user", content: `论文译文：\n\n${context}` },
  ];
  try {
    // 小模型偶发退化输出(只回一两个词),行数过少时自动重试一次
    let result = null;
    for (let attempt = 0; attempt < 2 && !result; attempt += 1) {
      const { answer, model } = await callConfiguredModel(readingModelConfig(body.model, job.config), apiKey, messages, { maxTokens: 1200, temperature: 0.4 });
      if (answer.split("\n").filter((line) => line.trim()).length >= 5) {
        result = { markdown: answer.slice(0, 6000), model, generatedAt: new Date().toISOString() };
      } else if (attempt === 1) {
        result = { markdown: answer.slice(0, 6000), model, generatedAt: new Date().toISOString(), sparse: true };
      }
    }
    await writeFile(mmPath, JSON.stringify(result, null, 2), "utf8");
    return json(res, 200, result);
  } catch (error) {
    if (error.name === "TimeoutError") return json(res, 504, { error: "思维导图生成超时（180 秒），请稍后重试" });
    return json(res, error.status || 502, { error: `思维导图生成失败：${error.message}` });
  }
}

// 多文档问答:跨多篇已完成任务的综合/对比提问(对标 NotebookLM/Scholaread)。
// 上下文按【文档 N：文件名】分节,回答要求以【文档 N 第 M 页】标注来源。
async function chatMulti(req, res) {
  let body;
  try {
    body = await readJsonBody(req, 128 * 1024);
  } catch {
    return json(res, 400, { error: "请求体无效" });
  }
  const question = String(body?.question || "").trim();
  if (!question) return json(res, 400, { error: "请输入问题" });
  if (question.length > 2000) return json(res, 400, { error: "问题过长（最多 2000 字）" });
  const ids = (Array.isArray(body?.job_ids) ? body.job_ids : []).map(String).slice(0, 4);
  if (!ids.length) return json(res, 400, { error: "请先选择要问答的任务" });
  const docs = [];
  const skipped = [];
  // 多文档上下文总预算 32k 字符(最坏 1 字符≈1 token),按篇数均分,单篇不超过 15k
  const perDoc = Math.min(15000, Math.max(4000, Math.floor(32000 / Math.max(1, ids.length))));
  for (const id of ids) {
    const job = jobs.get(id);
    if (!job || job.status !== "completed") { skipped.push(id); continue; }
    let text = null;
    try {
      text = (await readFile(path.join(job.outputDir, "translated-text.txt"), "utf8")).slice(0, perDoc);
    } catch {}
    if (text) docs.push({ job, text });
    else skipped.push(id);
  }
  if (!docs.length) return json(res, 409, { error: "所选任务都没有译文全文数据（较早版本生成的任务请重试一次）" });

  // 模型来源:请求携带当前界面配置(本地 Ollama 或云 API)时优先使用,否则沿用第一篇任务的配置
  const cfg = readingModelConfig(body?.model, docs[0].job.config);

  const history = (Array.isArray(body?.history) ? body.history : [])
    .filter((m) => m && ["user", "assistant"].includes(m.role) && typeof m.content === "string")
    .slice(-6)
    .map((m) => ({ role: m.role, content: m.content.slice(0, 4000) }));
  const context = docs.map((d, i) => `【文档 ${i + 1}：${d.job.fileName}】\n${d.text}`).join("\n\n");
  const messages = [
    {
      role: "system",
      content: `你是论文阅读助手。上下文按【文档 N：文件名】分节给出多篇论文的中文译文。回答跨文档对比或综合问题时，先分文档梳理再综合对比；关键论断句末标注来源，格式如【文档 1 第 3 页】，页码只能取自对应文档内出现的页码标记。用中文回答，结构清晰。`,
    },
    { role: "user", content: `多篇论文译文如下：\n\n${context}` },
    ...history,
    { role: "user", content: question },
  ];
  try {
    const { answer, model } = await callConfiguredModel(cfg, String(req.headers["x-api-key"] || "").trim(), messages, { maxTokens: 2000, temperature: 0.3 });
    const availableRefs = docs.flatMap((doc, index) =>
      [...doc.text.matchAll(/【第\s*(\d+)\s*页】/g)].map((match) => `${index + 1}:${match[1]}`));
    const citationCheck = auditCitations(answer, availableRefs, true);
    citationCheck.skippedCount = skipped.length;
    if (skipped.length) citationCheck.warning = `${citationCheck.warning ? `${citationCheck.warning}；` : ""}有 ${skipped.length} 篇所选论文未纳入回答`;
    return json(res, 200, {
      answer,
      model,
      citationCheck,
      docs: docs.map((d) => ({ id: d.job.id, fileName: d.job.fileName })),
      skipped,
    });
  } catch (error) {
    if (error.name === "TimeoutError") return json(res, 504, { error: "多文档问答超时（180 秒），请稍后重试" });
    return json(res, error.status || 502, { error: `多文档问答失败：${error.message}` });
  }
}

// 解析术语表 CSV 为 [source, target] 数组(简单 CSV:不含嵌套引号转义)
export function parseGlossaryEntries(text) {
  const lines = String(text || "").replace(/^\uFEFF/, "").split(/\r?\n/).filter((line) => line.trim());
  const entries = [];
  for (const line of lines.slice(1)) {
    const cells = line.split(",").map((cell) => cell.trim().replace(/^"|"$/g, ""));
    if (cells.length >= 2 && cells[0] && cells[1]) entries.push([cells[0], cells[1]]);
  }
  return entries;
}

// 挑出原文段落中出现的术语(大小写不敏感),最多 20 条
export function filterGlossaryTerms(entries, enText) {
  const lower = enText.toLowerCase();
  return entries.filter(([src]) => src && lower.includes(src.toLowerCase())).slice(0, 20);
}

function editsPath(job) {
  return path.join(job.outputDir, "edits.json");
}

async function loadEdits(job) {
  try {
    return JSON.parse(await readFile(editsPath(job), "utf8"));
  } catch {
    return {};
  }
}

// 保存人工校对的段落译文:键为"页:段序"。改回与原文一致时视为撤销,删除该条。
async function saveParagraphEdit(req, res, job) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法校对" });
  let body;
  try {
    body = await readJsonBody(req, 128 * 1024);
  } catch {
    return json(res, 400, { error: "请求体无效" });
  }
  const key = String(body?.key || "");
  const text = String(body?.text ?? "");
  const origText = String(body?.origText ?? "");
  if (!key) return json(res, 400, { error: "缺少段落标识" });
  if (!text.trim()) return json(res, 400, { error: "校对内容不能为空" });
  if (text.length > 8000) return json(res, 400, { error: "校对内容过长" });

  const edits = await loadEdits(job);
  const entry = edits[key] || {};
  const norm = (s) => String(s).replace(/\s+/g, "");
  if (origText && norm(text) === norm(origText)) {
    delete edits[key]; // 改回引擎原译 = 撤销校对
  } else {
    edits[key] = {
      text: text.slice(0, 8000),
      origText: origText.slice(0, 8000),
      lastApplied: entry.lastApplied || null,
      updatedAt: new Date().toISOString(),
    };
  }
  await writeFile(editsPath(job), JSON.stringify(edits, null, 2), "utf8");
  return json(res, 200, { ok: true, edits });
}

// 段落重译:对单个原文段落用任务配置的模型生成备选译文,供逐段对照时比较。
// 仅返回译文文本,不写回 PDF;刻意用较高温度以产生与原文有差异的备选。
async function jobRetranslate(req, res, job) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法重译" });
  let body;
  try {
    body = await readJsonBody(req, 64 * 1024);
  } catch {
    return json(res, 400, { error: "请求体无效" });
  }
  const en = String(body?.en || "").trim();
  if (!en) return json(res, 400, { error: "缺少原文段落" });
  if (en.length > 4000) return json(res, 400, { error: "段落过长（最多 4000 字符）" });
  const apiKey = String(req.headers["x-api-key"] || "").trim();
  const target = job.config.target === "zh-TW" ? "繁体中文" : "简体中文";
  // 对比重译:可指定其他模型名(本地已装模型或同端点的其他模型),便于多译文对比
  const wantModel = String(body?.model || "").trim().slice(0, 120);
  const config = wantModel && wantModel !== job.config.model ? { ...job.config, model: wantModel } : job.config;
  // 任务绑定过术语表时,把段落中出现的术语注入提示词,保证备选译文与全文术语一致
  let glossaryBlock = "";
  try {
    const request = JSON.parse(await readFile(job.requestPath, "utf8"));
    if (request.glossaryPath && await readFile(request.glossaryPath, "utf8").then(() => true).catch(() => false)) {
      const glossaryText = await readFile(request.glossaryPath, "utf8");
      const terms = filterGlossaryTerms(parseGlossaryEntries(glossaryText), en);
      if (terms.length) {
        glossaryBlock = "\n术语表（以下术语必须按给定译名翻译）：\n" + terms.map(([src, tgt]) => `${src} → ${tgt}`).join("\n");
      }
    }
  } catch {}
  const messages = [
    {
      role: "system",
      content: `你是资深的学术翻译引擎，把英文科研论文段落翻译成${target}。忠实原文：不增写、不删减、不解释；数字、单位、变量名、公式和引用编号保持原样；作者姓名、单位与邮箱保持英文原样，邮箱地址必须完整不得拆分；使用规范书面语，符合中文学术表达习惯。只输出译文，不要任何其他内容。${glossaryBlock}`,
    },
    { role: "user", content: en },
  ];
  try {
    const { answer, model } = await callConfiguredModel(config, apiKey, messages, { maxTokens: 2000, temperature: 0.7 });
    return json(res, 200, { translation: answer, model });
  } catch (error) {
    if (error.name === "TimeoutError") return json(res, 504, { error: "重译超时（180 秒），请稍后重试" });
    return json(res, error.status || 502, { error: `重译失败：${error.message}` });
  }
}

function runApplyParagraph({ sourcePdf, outPath, page, oldText, newText }) {
  return new Promise((resolve, reject) => {
    const child = spawnWorker();
    let stderrTail = "";
    const timer = setTimeout(() => {
      stopChild(child);
      reject(new Error("应用重译超时"));
    }, 60_000);
    child.stderr.on("data", (chunk) => { stderrTail = (stderrTail + chunk).slice(-300); });
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && existsSync(outPath)) return resolve(true);
      reject(new Error(`应用失败：${stderrTail.trim() || `退出码 ${code}`}`));
    });
    child.stdin.end(JSON.stringify({ mode: "apply_paragraph", translatedPdf: sourcePdf, outPath, page, oldText, newText }), "utf8");
  });
}

async function applyParagraph(req, res, job) {
  if (job.status !== "completed") return json(res, 409, { error: "任务未完成，无法应用重译" });
  let body;
  try {
    body = await readJsonBody(req, 64 * 1024);
  } catch {
    return json(res, 400, { error: "请求体无效" });
  }
  const page = Math.round(Number(body?.page));
  const oldText = String(body?.oldText || "").trim();
  const newText = String(body?.newText || "").trim();
  if (!page || !oldText || !newText) return json(res, 400, { error: "缺少页码、原文或新译文" });
  if (newText.length > 8000) return json(res, 400, { error: "新译文过长" });

  // 累积应用:已存在 adjusted-output.pdf 时在其基础上继续写,多处修正可叠加
  const adjustedName = "adjusted-output.pdf";
  const sourceName = job.outputs.includes(adjustedName) ? adjustedName
    : (job.outputs || []).find((name) => name.toLowerCase().endsWith(".pdf"));
  if (!sourceName) return json(res, 409, { error: "任务没有可用的译文 PDF" });
  const sourcePdf = path.join(job.outputDir, sourceName);
  const outPath = path.join(job.outputDir, adjustedName);
  try {
    await runApplyParagraph({ sourcePdf, outPath, page, oldText, newText });
  } catch (error) {
    return json(res, 502, { error: error.message });
  }
  if (!job.outputs.includes(adjustedName)) job.outputs.push(adjustedName);
  await persistJobs();
  return json(res, 200, { ok: true, file: adjustedName });
}

async function retryJob(req, res, oldJob) {
  const job = oldJob;
  if (!job) return json(res, 404, { error: "任务不存在" });
  if (!["failed", "canceled", "interrupted"].includes(job.status)) {
    return json(res, 409, { error: "只有失败、取消或中断的任务可以重试" });
  }
  if (!(await engineReady())) {
    const error = new Error("翻译引擎尚未安装，请先在项目目录运行 uv sync");
    error.status = 503;
    throw error;
  }
  const apiKey = String(req.headers["x-api-key"] || "").trim();
  if (job.config.provider === "openai" && !apiKey) {
    return json(res, 400, { error: "请在上方 API key 输入框填写后重试（key 不落盘，无法复用上次的）" });
  }
  // 重试继承界面上的"忽略翻译缓存"开关(请求体未携带时沿用原任务配置),
  // 强制绕过缓存重新翻译
  let ignoreCache = null;
  try {
    const body = await readJsonBody(req, 4096);
    if (typeof body?.ignoreCache === "boolean") ignoreCache = body.ignoreCache;
  } catch {}

  // 原地重试:同一个任务重置后重新入队,复用原目录/配置/术语表。
  // 已翻译段落经引擎全局缓存跳过(token 计数为 0 即全部命中),
  // 进度条仍会从 0 走一遍 —— 版式解析与排版必须整篇重放,但不再重复调用模型
  if (job.config?.gatewayId) gatewayRegistry.delete(job.config.gatewayId);
  const config = JSON.parse(JSON.stringify(job.config));
  delete config.gatewayId; // 旧 gatewayId 属于上次运行,重试重新注册
  config.ignoreCache = ignoreCache ?? job.config.ignoreCache === true;
  job.config = config;
  registerGateway(config, apiKey || "ollama");

  const lastError = job.error;
  // 术语表路径记录在原 request.json 里,原地重试必须沿用
  let glossaryPath = null;
  try {
    const prior = JSON.parse(await readFile(job.requestPath, "utf8"));
    if (prior.glossaryPath && await access(prior.glossaryPath).then(() => true).catch(() => false)) {
      glossaryPath = prior.glossaryPath;
    }
  } catch {}
  try {
    await rm(job.outputDir, { recursive: true, force: true });
  } catch {}
  await mkdir(job.outputDir, { recursive: true });
  await writeFile(
    job.requestPath,
    JSON.stringify({ inputPath: job.inputPath, outputDir: job.outputDir, glossaryPath, config }, null, 2),
    "utf8",
  );

  job.status = "queued";
  job.stage = "等待执行（重试）";
  job.progress = 0;
  job.startedAt = null;
  job.finishedAt = null;
  job.outputs = [];
  job.quality = null;
  job.tokensUsed = null;
  job.promptTokens = null;
  job.completionTokens = null;
  job.stats = undefined;
  job.stalled = false;
  job.cancelRequested = false;
  job.lastProgressAt = null;
  job.log = lastError ? [`上次未完成（${lastError}），重试中已译段落将经缓存跳过`] : [];
  secrets.set(job.id, apiKey || "ollama");
  queue.push(job.id);
  await persistJobs();
  void pump();
  return json(res, 202, publicJob(job));
}

async function loadQualityDetail(job) {
  if (job.status !== "completed") return null;
  let detail = null;
  try {
    detail = JSON.parse(await readFile(path.join(job.outputDir, "quality-report.json"), "utf8"));
  } catch {}
  try {
    const summary = JSON.parse(await readFile(path.join(job.outputDir, "summary.json"), "utf8"));
    if (detail) detail.aiSummary = summary;
    else detail = { aiSummary: summary };
  } catch {}
  try {
    const figures = JSON.parse(await readFile(path.join(job.outputDir, "figures.json"), "utf8"));
    if (Array.isArray(figures)) {
      if (detail) detail.figures = figures;
      else detail = { figures };
    }
  } catch {}
  try {
    const paragraphs = JSON.parse(await readFile(path.join(job.outputDir, "paragraphs.json"), "utf8"));
    if (Array.isArray(paragraphs?.pages)) {
      if (detail) detail.paragraphs = paragraphs.pages;
      else detail = { paragraphs: paragraphs.pages };
    }
  } catch {}
  // 人工校对的译文编辑:键为"页:段序",含最终文本与最近写入 PDF 的文本
  try {
    const edits = JSON.parse(await readFile(path.join(job.outputDir, "edits.json"), "utf8"));
    if (edits && Object.keys(edits).length) {
      if (detail) detail.edits = edits;
      else detail = { edits };
    }
  } catch {}
  return detail;
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
  if (req.method === "GET" && url.pathname === "/api/prompt-template") {
    // 返回生效中的模板:用户自定义(数据目录)优先,否则内置默认
    let customized = false;
    let template = "";
    try {
      template = (await readFile(PROMPT_TEMPLATE_FILE, "utf8")).trim();
      customized = Boolean(template);
    } catch {}
    if (!template) {
      const builtin = await getPromptTemplate();
      template = builtin.template;
    }
    return json(res, 200, { template, customized });
  }
  if (req.method === "POST" && url.pathname === "/api/prompt-template") {
    // 保存自定义模板;空内容 = 恢复内置默认
    const body = await readJsonBody(req, 64 * 1024).catch(() => ({}));
    const template = String(body?.template ?? "").trim();
    if (template) {
      if (template.length > 4000) return json(res, 400, { error: "提示词过长（最多 4000 字符）" });
      await mkdir(DATA_DIR, { recursive: true });
      await writeFile(PROMPT_TEMPLATE_FILE, template, "utf8");
    } else {
      await rm(PROMPT_TEMPLATE_FILE, { force: true });
    }
    return json(res, 200, { ok: true, customized: Boolean(template) });
  }
  if (url.pathname === "/mcp") {
    if (req.method === "POST") return mcpHandler(req, res);
    return json(res, 405, { error: "MCP 端点仅支持 POST" });
  }
  if (url.pathname.startsWith("/pdfjs/")) {
    // 本地化的 PDF.js 静态资源(离线优先,不走 CDN)
    const rel = url.pathname.slice("/pdfjs/".length).replaceAll("\\", "/");
    const base = path.resolve(ROOT, "app", "pdfjs") + path.sep;
    const filePath = path.resolve(path.join(ROOT, "app", "pdfjs", rel));
    if (!filePath.startsWith(base) || !existsSync(filePath) || statSync(filePath).isDirectory()) {
      return json(res, 404, { error: "not found" });
    }
    const ext = path.extname(filePath).toLowerCase();
    const type = { ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json", ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream", ".ttf": "font/ttf", ".txt": "text/plain" }[ext] || "application/octet-stream";
    res.writeHead(200, { "content-type": type, "cache-control": "public, max-age=604800" });
    createReadStream(filePath).pipe(res);
    return;
  }
  const originalMatch = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/original$/i);
  if (req.method === "GET" && originalMatch) {
    // 原始上传 PDF 字节(阅读器"原文"视图用)
    const job = jobs.get(originalMatch[1]);
    if (!job || !existsSync(job.inputPath)) return json(res, 404, { error: "原文不存在" });
    res.writeHead(200, { "content-type": "application/pdf", "content-length": statSync(job.inputPath).size, "cache-control": "no-store" });
    createReadStream(job.inputPath).pipe(res);
    return;
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
  if (req.method === "POST" && url.pathname === "/api/providers/speed") {
    const body = await readJsonBody(req);
    if (!String(body?.model || "").trim()) return json(res, 400, { error: "请先填写模型名称" });
    return json(res, 200, await measureModelSpeed({ baseUrl: body.baseUrl, model: body.model, apiKey: req.headers["x-api-key"] }));
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
    if (active?.id === job.id) { stopChild(active.child); }
    else {
      job.status = "canceled";
      job.stage = "已取消";
      releaseJobSecrets(job);
    }
    await persistJobs();
    return json(res, 200, publicJob(job));
  }
  const retry = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/retry$/i);
  if (req.method === "POST" && retry) {
    const job = jobs.get(retry[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return retryJob(req, res, job);
  }
  const chat = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/chat$/i);
  if (req.method === "POST" && chat) {
    const job = jobs.get(chat[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return jobChat(req, res, job);
  }
  const mindmap = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/mindmap$/i);
  if (req.method === "POST" && mindmap) {
    const job = jobs.get(mindmap[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return jobMindmap(req, res, job);
  }
  if (req.method === "POST" && url.pathname === "/api/chat-multi") {
    return chatMulti(req, res);
  }
  const apply = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/apply-paragraph$/i);
  if (req.method === "POST" && apply) {
    const job = jobs.get(apply[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return applyParagraph(req, res, job);
  }
  const retrans = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/retranslate$/i);
  if (req.method === "POST" && retrans) {
    const job = jobs.get(retrans[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return jobRetranslate(req, res, job);
  }
  const saveEdit = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/save-edit$/i);
  if (req.method === "POST" && saveEdit) {
    const job = jobs.get(saveEdit[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    return saveParagraphEdit(req, res, job);
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
  const figImg = url.pathname.match(/^\/api\/jobs\/([0-9a-f-]+)\/figure\/(fig-\d+\.png)$/i);
  if (req.method === "GET" && figImg) {
    const job = jobs.get(figImg[1]);
    if (!job) return json(res, 404, { error: "任务不存在" });
    const filePath = path.resolve(job.outputDir, "figures", figImg[2]);
    const base = path.resolve(job.outputDir) + path.sep;
    if (!filePath.startsWith(base) || !existsSync(filePath)) {
      return json(res, 404, { error: "缩略图不存在" });
    }
    const body = await readFile(filePath);
    res.writeHead(200, { "content-type": "image/png", "content-length": body.length, "cache-control": "no-store" });
    return res.end(body);
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
