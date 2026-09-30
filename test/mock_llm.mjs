// Minimal OpenAI-compatible mock for local end-to-end testing only. Not part of the app.
import { createServer } from "node:http";

const PORT = Number(process.env.MOCK_PORT || 5180);

// 记录最近一次 OpenAI 风格请求的全部消息内容,供 E2E 断言提示词模板传递链路
// (BabelDOC 的 llm_translate 把角色块+规则整体作为 user 消息发送,不一定有 system 角色)
let lastSystem = "";

const server = createServer((req, res) => {
  const body = [];
  req.on("data", (chunk) => body.push(chunk));
  req.on("end", () => {
    if (req.url === "/v1/models") {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ object: "list", data: [{ id: "mock-large" }, { id: "mock-small" }] }));
    }
    if (req.url === "/v1/last-system") {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ system: lastSystem }));
    }
    if (req.url === "/v1/chat/completions") {
      let request;
      try { request = JSON.parse(Buffer.concat(body).toString() || "{}"); } catch { request = {}; }
      lastSystem = (request.messages || []).map((m) => m.content || "").join("\n");
      // 重译分支:system 含"资深的学术翻译引擎"且段落含 layout/tables 时,
      // 返回与首轮翻译不同的文本,用于验证"备选译文 ≠ 原译文"的写回链路
      const allMsgs = (request.messages || []).map((m) => m.content || "").join("\n");
      if (/资深的学术翻译引擎/.test(lastSystem) && /tables|figures/i.test(allMsgs)) {
        return res.writeHead(200, { "content-type": "application/json" }), res.end(JSON.stringify({
          model: request.model,
          choices: [{ message: { role: "assistant", content: "重译生成的不同译文版本。" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
        }));
      }
      let content = "深度学习模型改变了自然语言处理。";
      if (/attention mechanism/i.test(request.messages?.at(-1)?.content || "")) content = "注意力机制。";
      if (/关键论断句末标注来源/.test(lastSystem)) content = "多文档比较结论。【文档 1 第 1 页】";
      else if (/关键论断需在句末标注来源页码/.test(lastSystem)) content = "论文核心贡献来自所提出的方法。【第 1 页】";
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({
        model: request.model,
        choices: [{ message: { role: "assistant", content }, finish_reason: "stop" }],
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      }));
    }
    if (req.url === "/anthropic/v1/messages") {
      let request;
      try { request = JSON.parse(Buffer.concat(body).toString() || "{}"); } catch { request = {}; }
      // 网关发送的 system 是纯字符串(Anthropic 协议也允许块数组),两种形态都要兼容
      const systemText = typeof request.system === "string"
        ? request.system
        : (request.system || []).map((block) => block?.text || "").join(" ");
      const messagesText = (request.messages || []).map((m) => (typeof m.content === "string" ? m.content : "")).join(" ");
      let text = "深度学习模型改变了自然语言处理。";
      if (/attention mechanism/i.test([systemText, messagesText].join(" "))) text = "注意力机制。";
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({
        id: "msg_mock",
        model: request.model,
        content: [{ type: "text", text }],
        usage: { input_tokens: 10, output_tokens: 10 },
      }));
    }
    if (req.url.startsWith("/gemini/") && req.url.includes(":generateContent")) {
      const request = JSON.parse(Buffer.concat(body).toString() || "{}");
      const allText = [
        ...(request.systemInstruction?.parts || []).map((p) => p.text),
        ...(request.contents || []).flatMap((c) => c.parts.map((p) => p.text)),
      ].join(" ");
      let text = "深度学习模型改变了自然语言处理。";
      if (/attention mechanism/i.test(allText)) text = "注意力机制。";
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({
        candidates: [{ content: { parts: [{ text }] } }],
        usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 10, totalTokenCount: 20 },
      }));
    }
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "not found" } }));
  });
});

server.listen(PORT, "127.0.0.1", () => console.log(`mock LLM on http://127.0.0.1:${PORT}/v1`));
