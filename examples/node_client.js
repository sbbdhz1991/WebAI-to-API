// WebAI-to-API Node.js 接入示例（ESM）。
//
// 用 Node 18+ 内置 fetch / FormData / Blob，无需额外依赖。
//
// 运行：
//   WEBAI_URL=http://localhost:6969 GEMINI_API_KEY=xxx node examples/node_client.js
//
// package.json 里加 "type": "module"，或文件名改成 .mjs。

import fs from "node:fs/promises";
import path from "node:path";

const BASE = process.env.WEBAI_URL || "http://localhost:6969";
const API_KEY = process.env.GEMINI_API_KEY || "";

const authHeaders = () => (API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {});

// 默认 MIME 表（够日常用，缺啥自己补）
const EXT_MIME = {
  jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif",
  webp: "image/webp", mp4: "video/mp4", mov: "video/quicktime",
  webm: "video/webm", pdf: "application/pdf",
};
const guessMime = (filename) => {
  const ext = (filename.split(".").pop() || "").toLowerCase();
  return EXT_MIME[ext] || "application/octet-stream";
};

// =============================================================================
// 1. 纯文本（OpenAI 兼容）
// =============================================================================
export async function chatText(prompt, { model = "gemini-3-pro", system } = {}) {
  const messages = [];
  if (system) messages.push({ role: "system", content: system });
  messages.push({ role: "user", content: prompt });

  const r = await fetch(`${BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ model, messages }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
  const data = await r.json();
  return data.choices[0].message.content;
}

// =============================================================================
// 2. 流式（SSE）
// =============================================================================
export async function* chatStream(prompt, { model = "gemini-3-pro" } = {}) {
  const r = await fetch(`${BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      model,
      messages: [{ role: "user", content: prompt }],
      stream: true,
    }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (data === "[DONE]") return;
      try {
        const obj = JSON.parse(data);
        const delta = obj.choices?.[0]?.delta ?? obj.choices?.[0]?.message ?? {};
        if (delta.content) yield delta.content;
      } catch (_) {
        // ignore parse errors
      }
    }
  }
}

// =============================================================================
// 3. multipart 文件上传（推荐用于浏览器/Node 上传场景）
// =============================================================================
export async function geminiGenerate(message, {
  files = [],            // 数组项可以是 string 路径，也可以是 {data: Buffer, filename, mime}
  model = "gemini-3-pro",
  endpoint = "/gemini",  // 也可以传 "/gemini-chat" 保持会话
} = {}) {
  const form = new FormData();
  form.append("message", message);
  form.append("model", model);

  for (const f of files) {
    let blob, name;
    if (typeof f === "string") {
      name = path.basename(f);
      const buf = await fs.readFile(f);
      blob = new Blob([buf], { type: guessMime(name) });
    } else if (f && f.data) {
      name = f.filename || "upload.bin";
      blob = new Blob([f.data], { type: f.mime || guessMime(name) });
    } else {
      throw new TypeError("unsupported file input");
    }
    form.append("files", blob, name);
  }

  const r = await fetch(`${BASE}${endpoint}`, {
    method: "POST",
    headers: authHeaders(), // 注意：FormData 时不要手动设 Content-Type
    body: form,
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
  return (await r.json()).response;
}

// =============================================================================
// 4. OpenAI 多模态（image_url + data: URL）
// =============================================================================
export async function chatVision(prompt, imagePath, { model = "gemini-3-pro" } = {}) {
  const buf = await fs.readFile(imagePath);
  const mime = guessMime(imagePath);
  const dataUrl = `data:${mime};base64,${buf.toString("base64")}`;

  const r = await fetch(`${BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      model,
      messages: [{
        role: "user",
        content: [
          { type: "text", text: prompt },
          { type: "image_url", image_url: { url: dataUrl } },
        ],
      }],
    }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
  const data = await r.json();
  return data.choices[0].message.content;
}

// =============================================================================
// 命令行 demo
// =============================================================================
async function main() {
  const mode = process.argv[2] || "text";
  const arg = process.argv[3];

  try {
    if (mode === "text") {
      const out = await chatText("用一句话介绍 JavaScript");
      console.log("[text]", out);
    } else if (mode === "stream") {
      process.stdout.write("[stream] ");
      for await (const chunk of chatStream("写一个 3 行的 JS 闭包示例")) {
        process.stdout.write(chunk);
      }
      process.stdout.write("\n");
    } else if (mode === "vision") {
      if (!arg) throw new Error("vision 模式需要图片路径: node node_client.js vision photo.jpg");
      console.log("[vision]", await chatVision("描述这张图，30字以内", arg));
    } else if (mode === "video" || mode === "upload") {
      if (!arg) throw new Error("upload 模式需要文件路径");
      console.log("[upload]", await geminiGenerate("请描述这个文件内容", { files: [arg] }));
    } else {
      console.error(`未知模式: ${mode}。可用：text / stream / vision <img> / upload <file>`);
      process.exit(2);
    }
  } catch (err) {
    console.error("[error]", err.message);
    process.exit(1);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
