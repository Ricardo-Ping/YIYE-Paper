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
      const request = JSON.parse(Buffer.concat(body).toString() || "{}");
      lastSystem = (request.messages || []).map((m) => m.content || "").join("\n");
      let content = "深度学习模型改变了自然语言处理。";
      if (/attention mechanism/i.test(request.messages?.at(-1)?.content || "")) content = "注意力机制。";
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({
        model: request.model,
        choices: [{ message: { role: "assistant", content }, finish_reason: "stop" }],
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      }));
    }
    if (req.url === "/anthropic/v1/messages") {
      const request = JSON.parse(Buffer.concat(body).toString() || "{}");
      let text = "深度学习模型改变了自然语言处理。";
      if (/attention mechanism/i.test([...(request.system || ""), ...request.messages.map((m) => m.content)].join(" "))) text = "注意力机制。";
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
