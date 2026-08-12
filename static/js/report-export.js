import { escapeHtml, simpleMarkdown } from "./utils.js?v=20260808am";

export function safeFilenamePart(text) {
  return String(text || "")
    .replace(/[\\/:*?"<>|]+/g, "_")
    .replace(/\s+/g, "")
    .slice(0, 40);
}

export function reportFilenameBase({ name, code, createdAt }) {
  const stamp = (createdAt || new Date().toISOString()).slice(0, 10);
  return `A股深研_${safeFilenamePart(name || "个股")}_${code || "report"}_${stamp}`;
}

export function renderReportBodyHtml({ name, code, createdAt, analysisHtml }) {
  const stamp = (createdAt || "").slice(0, 19) || new Date().toISOString().slice(0, 10);
  return `
    <div class="agent-pdf-cover">
      <div class="agent-pdf-brand">A股深研 · 最终报告</div>
      <h1>${escapeHtml(name || "个股")}${code ? `（${escapeHtml(code)}）` : ""}</h1>
      <p class="agent-pdf-meta">分析时间：${escapeHtml(stamp)}</p>
    </div>
    <div class="agent-md agent-pdf-body">${analysisHtml || ""}</div>
    <p class="agent-pdf-foot">数据仅供学习参考，非投资建议。</p>
  `;
}

export function fullStandaloneHtml(opts) {
  const { name, code } = opts;
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>A股深研 · ${escapeHtml(name || "")}${code ? `（${escapeHtml(code)}）` : ""}</title>
  <style>
    body {
      margin: 0;
      padding: 32px 28px 48px;
      background: #f4f7fb;
      color: #122033;
      font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
      line-height: 1.7;
    }
    .wrap {
      max-width: 860px;
      margin: 0 auto;
      background: #fff;
      border: 1px solid #d8e0ea;
      border-radius: 14px;
      padding: 28px 32px;
      box-shadow: 0 8px 24px rgba(18, 32, 51, 0.06);
    }
    .agent-pdf-brand {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      color: #1f6feb;
    }
    h1 { margin: 8px 0 6px; font-size: 24px; }
    .agent-pdf-meta, .agent-pdf-foot {
      color: #5b6b7c;
      font-size: 12px;
      margin: 6px 0 16px;
    }
    .agent-pdf-foot {
      margin-top: 24px;
      border-top: 1px solid #e5ebf2;
      padding-top: 12px;
    }
    .agent-md h1, .agent-md h2, .agent-md h3 {
      margin: 16px 0 8px;
      font-size: 16px;
      color: #122033;
    }
    .agent-md ul { margin: 8px 0; padding-left: 1.2em; }
    .agent-md li { margin: 4px 0; }
    .agent-md blockquote {
      margin: 10px 0;
      padding: 8px 12px;
      border-left: 3px solid #1f6feb;
      background: #f3f7fc;
      color: #5b6b7c;
      border-radius: 0 8px 8px 0;
    }
    .agent-md strong { font-weight: 700; }
    .agent-md code {
      background: #eef3f8;
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 12px;
    }
    .agent-md table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      margin: 12px 0;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
    }
    .agent-md th, .agent-md td {
      padding: 8px 10px;
      border-bottom: 1px solid #d8e0ea;
      border-right: 1px solid #eef2f7;
      text-align: left;
    }
    .agent-md tr:last-child td { border-bottom: 0; }
  </style>
</head>
<body>
  <div class="wrap">
    ${renderReportBodyHtml(opts)}
  </div>
</body>
</html>`;
}

export function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

export function downloadReportAsHtml({ name, code, createdAt, analysis, analysisHtml }) {
  const html =
    analysisHtml ||
    simpleMarkdown(analysis || "");
  const filename = `${reportFilenameBase({ name, code, createdAt })}.html`;
  const doc = fullStandaloneHtml({
    name,
    code,
    createdAt,
    analysisHtml: html,
  });
  const blob = new Blob([doc], { type: "text/html;charset=utf-8" });
  triggerBlobDownload(blob, filename);
  return filename;
}
