export const $ = (id) => document.getElementById(id);

export function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

let toastTimer = null;

/** 右上角轻量提示，不占用布局 */
export function showToast(message, { type = "ok", duration = 2600 } = {}) {
  let host = document.getElementById("appToastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "appToastHost";
    host.className = "app-toast-host";
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  host.innerHTML = "";
  const el = document.createElement("div");
  el.className = `app-toast app-toast-${type}`;
  el.textContent = String(message || "");
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add("is-show"));
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    el.classList.remove("is-show");
    window.setTimeout(() => {
      if (el.parentNode === host) el.remove();
    }, 200);
  }, Math.max(1200, duration));
}

export function cls(pct) {
  return pct > 0 ? "up" : pct < 0 ? "down" : "";
}

export function kv(label, value) {
  return `<div><span>${label}</span><b>${value}</b></div>`;
}

function isTableSeparator(line) {
  const cells = line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());
  return cells.length > 0 && cells.every((c) => /^:?-{3,}:?$/.test(c));
}

function inlineMarkdown(text) {
  let html = escapeHtml(text || "");
  // 兼容 **text** 与 ** text **（模型常在表格单元格里加空格）
  html = html.replace(/\*\*\s*([^*]+?)\s*\*\*/g, "<strong>$1</strong>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  return html;
}

function parseTableBlock(lines, start) {
  const block = [];
  let i = start;
  while (i < lines.length && lines[i].trim().includes("|")) {
    block.push(lines[i]);
    i += 1;
  }
  if (block.length < 2 || !isTableSeparator(block[1])) {
    return null;
  }
  const rows = block
    .filter((_, idx) => idx !== 1)
    .map((line) =>
      line
        .trim()
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map((c) => c.trim())
    );
  if (!rows.length) return null;
  const head = rows[0];
  const body = rows.slice(1);
  let html = "<table><thead><tr>";
  html += head.map((c) => `<th>${inlineMarkdown(c)}</th>`).join("");
  html += "</tr></thead><tbody>";
  for (const row of body) {
    html += "<tr>";
    for (let c = 0; c < head.length; c += 1) {
      html += `<td>${inlineMarkdown(row[c] ?? "")}</td>`;
    }
    html += "</tr>";
  }
  html += "</tbody></table>";
  return { html, next: i };
}

export function simpleMarkdown(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  const parts = [];
  let i = 0;
  let listBuf = [];
  let quoteBuf = [];

  const flushList = () => {
    if (!listBuf.length) return;
    parts.push(`<ul>${listBuf.map((item) => `<li>${item}</li>`).join("")}</ul>`);
    listBuf = [];
  };

  const flushQuote = () => {
    if (!quoteBuf.length) return;
    parts.push(`<blockquote>${quoteBuf.map((p) => `<p>${p}</p>`).join("")}</blockquote>`);
    quoteBuf = [];
  };

  while (i < lines.length) {
    const line = lines[i].trimEnd();
    const trimmed = line.trim();

    if (!trimmed) {
      flushList();
      flushQuote();
      i += 1;
      continue;
    }

    if (trimmed.includes("|")) {
      const table = parseTableBlock(lines, i);
      if (table) {
        flushList();
        flushQuote();
        parts.push(table.html);
        i = table.next;
        continue;
      }
    }

    if (/^>\s?/.test(trimmed)) {
      flushList();
      quoteBuf.push(inlineMarkdown(trimmed.replace(/^>\s?/, "")));
      i += 1;
      continue;
    }
    if (quoteBuf.length) flushQuote();

    if (/^###\s+/.test(trimmed)) {
      flushList();
      parts.push(`<h3>${inlineMarkdown(trimmed.replace(/^###\s+/, ""))}</h3>`);
      i += 1;
      continue;
    }
    if (/^##\s+/.test(trimmed)) {
      flushList();
      parts.push(`<h2>${inlineMarkdown(trimmed.replace(/^##\s+/, ""))}</h2>`);
      i += 1;
      continue;
    }
    if (/^#\s+/.test(trimmed)) {
      flushList();
      parts.push(`<h1>${inlineMarkdown(trimmed.replace(/^#\s+/, ""))}</h1>`);
      i += 1;
      continue;
    }
    if (/^[-*]\s+/.test(trimmed)) {
      listBuf.push(inlineMarkdown(trimmed.replace(/^[-*]\s+/, "")));
      i += 1;
      continue;
    }

    flushList();
    parts.push(`<p>${inlineMarkdown(trimmed)}</p>`);
    i += 1;
  }

  flushList();
  flushQuote();
  return parts.join("");
}
