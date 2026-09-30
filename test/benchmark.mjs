// 多 provider 论文翻译对比脚本（调研文档第 9 节「推荐的下一步」）。
// 用同一组 PDF × 多个翻译服务跑相同配置，收集耗时、token 用量与质检结果，
// 生成 Markdown 对比报告。质量结论仍需人工盲评，脚本只负责把数据摆在一起。
//
// 用法：
//   1. npm start                       （先启动应用，默认 http://127.0.0.1:4173）
//   2. node test/benchmark.mjs --dir ./papers --providers ./providers.json
//
// providers.json 示例：
// [
//   { "name": "qwen3-8b-local", "provider": "ollama", "baseUrl": "http://127.0.0.1:11434/v1", "model": "qwen3:8b" },
//   { "name": "deepseek",       "provider": "openai", "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat", "apiKey": "sk-..." }
// ]
//
// 可选参数：--pages 1-3（每篇只翻前几页，控制成本） --target zh-CN --output ./docs/benchmark.md

import { readFile, readdir, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";

const args = process.argv.slice(2);
function argValue(flag, fallback = undefined) {
  const index = args.indexOf(flag);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

const BASE = process.env.YIYE_BASE_URL || "http://127.0.0.1:4173";
const pdfDir = argValue("--dir");
const providersFile = argValue("--providers");
const pages = argValue("--pages", "");
const target = argValue("--target", "zh-CN");
const outputFile = argValue("--output", `docs/benchmark-${new Date().toISOString().slice(0, 10)}.md`);
const POLL_INTERVAL_MS = 3000;
const JOB_TIMEOUT_MS = 30 * 60 * 1000;

if (!pdfDir || !providersFile) {
  console.error("用法：node test/benchmark.mjs --dir <PDF目录> --providers <providers.json> [--pages 1-3] [--output docs/benchmark.md]");
  process.exit(2);
}

async function main() {
  const health = await fetch(`${BASE}/api/health`).then((r) => r.json()).catch(() => null);
  if (!health?.ok) {
    console.error(`应用未运行或不可达：${BASE}。请先 npm start。`);
    process.exit(2);
  }
  if (!health.engineReady) {
    console.error("翻译引擎未安装（先运行 uv sync）。");
    process.exit(2);
  }

  const providers = JSON.parse(await readFile(providersFile, "utf8"));
  if (!Array.isArray(providers) || !providers.length) {
    console.error("providers.json 需要是非空数组。");
    process.exit(2);
  }
  const pdfs = (await readdir(pdfDir))
    .filter((name) => name.toLowerCase().endsWith(".pdf"))
    .sort();
  if (!pdfs.length) {
    console.error(`目录 ${pdfDir} 里没有 PDF 文件。`);
    process.exit(2);
  }

  console.log(`对比 ${providers.length} 个服务 × ${pdfs.length} 篇论文（pages=${pages || "全部"}）\n`);
  const results = [];
  for (const provider of providers) {
    for (const pdf of pdfs) {
      const label = `[${provider.name}] ${pdf}`;
      process.stdout.write(`${label} 提交…`);
      try {
        const job = await submitJob(provider, path.join(pdfDir, pdf));
        const outcome = await waitForJob(job.id);
        results.push({
          provider: provider.name,
          model: provider.model,
          pdf,
          ...outcome,
        });
        process.stdout.write(` ${outcome.status}，${outcome.durationSeconds ?? "?"} 秒\n`);
      } catch (error) {
        results.push({ provider: provider.name, model: provider.model, pdf, status: "error", error: error.message });
        process.stdout.write(` 失败：${error.message}\n`);
      }
    }
  }

  const markdown = renderReport(providers, pdfs, results, { pages, target });
  await mkdir(path.dirname(outputFile), { recursive: true });
  await writeFile(outputFile, markdown, "utf8");
  console.log(`\n报告已写入 ${outputFile}`);
}

async function submitJob(provider, filePath) {
  const form = new FormData();
  form.append("file", new Blob([await readFile(filePath)]), path.basename(filePath));
  form.append("config", JSON.stringify({
    provider: provider.provider,
    baseUrl: provider.baseUrl,
    model: provider.model,
    output: "mono",
    target,
    pages,
    qps: 4,
    ocr: true,
    table: false,
    glossary: true,
  }));
  form.append("apiKey", provider.apiKey || "");
  // 基准测试要反复跑同一批 PDF:必须 force,否则服务端 409 去重会让第二次运行全部失败
  form.append("force", "true");
  const response = await fetch(`${BASE}/api/jobs`, { method: "POST", body: form });
  const job = await response.json();
  if (!response.ok) throw new Error(job.error || `HTTP ${response.status}`);
  return job;
}

async function waitForJob(id) {
  const started = Date.now();
  while (Date.now() - started < JOB_TIMEOUT_MS) {
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    const job = await fetch(`${BASE}/api/jobs/${id}`).then((r) => r.json());
    if (["completed", "failed", "canceled", "interrupted"].includes(job.status)) {
      return {
        status: job.status,
        durationSeconds: job.startedAt && job.finishedAt
          ? Math.max(1, Math.round((new Date(job.finishedAt) - new Date(job.startedAt)) / 1000))
          : null,
        tokensUsed: job.tokensUsed ?? null,
        quality: job.quality ?? null,
        error: job.error,
        jobId: id,
      };
    }
  }
  return { status: "timeout", jobId: id, error: "超过 30 分钟未完成" };
}

function renderReport(providers, pdfs, results, { pages, target }) {
  const lines = [];
  lines.push("# 论文翻译服务对比");
  lines.push("");
  lines.push(`- 时间：${new Date().toLocaleString("zh-CN")}`);
  lines.push(`- 样本：${pdfs.length} 篇 × ${providers.length} 个服务；输出：中文单语；页码范围：${pages || "全部"}`);
  lines.push(`- 指标：耗时与 token 来自任务记录；质检来自 quality-report（空白页/疑似未翻译/占位符残留/页数不符）；术语应用来自质检的术语一致性检查。`);
  lines.push("- 注意：本表不含翻译质量主观评价。译文优劣仍需按调研文档第 8 节做人工盲评后才能下结论。");
  lines.push("");
  lines.push("| 服务 | 模型 | 论文 | 结果 | 耗时(秒) | tokens | 质检问题 | 术语应用 | 备注 |");
  lines.push("|---|---|---|---|---:|---:|---:|---|---|");
  for (const row of results) {
    const glossary = row.quality?.glossary;
    const glossaryText = glossary ? `${glossary.applied}/${glossary.terms}` : "-";
    lines.push([
      row.provider,
      row.model,
      row.pdf,
      row.status === "completed" ? "✓" : `✗ ${row.status}`,
      row.durationSeconds ?? "-",
      row.tokensUsed ?? "-",
      row.quality ? (row.quality.issueCount ?? "-") : "-",
      glossaryText,
      row.error ? String(row.error).slice(0, 60) : "",
    ].map((cell) => String(cell).replaceAll("|", "\\|")).join(" | ").replace(/^/, "| ").concat(" |"));
  }
  lines.push("");
  lines.push("## 汇总");
  lines.push("");
  lines.push("| 服务 | 完成/总数 | 平均耗时(秒) | tokens 合计 | 质检问题合计 |");
  lines.push("|---|---|---:|---:|---:|");
  for (const provider of providers) {
    const rows = results.filter((row) => row.provider === provider.name);
    const completed = rows.filter((row) => row.status === "completed");
    const avgDuration = completed.length
      ? Math.round(completed.reduce((sum, row) => sum + (row.durationSeconds || 0), 0) / completed.length)
      : "-";
    const tokens = completed.reduce((sum, row) => sum + (row.tokensUsed || 0), 0);
    const issues = completed.reduce((sum, row) => sum + (row.quality?.issueCount || 0), 0);
    lines.push(`| ${provider.name} | ${completed.length}/${rows.length} | ${avgDuration} | ${tokens || "-"} | ${issues} |`);
  }
  lines.push("");
  return lines.join("\n");
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
