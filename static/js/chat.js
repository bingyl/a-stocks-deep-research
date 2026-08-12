import { $, escapeHtml, showToast, simpleMarkdown } from "./utils.js?v=20260808am";
import { apiFetch } from "./api.js?v=20260812auth";

function parseSseChunk(buffer) {
  const events = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() || "";
  for (const part of parts) {
    if (!part.trim()) continue;
    let event = "message";
    const dataLines = [];
    for (const line of part.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) continue;
    try {
      events.push({ event, data: JSON.parse(dataLines.join("\n")) });
    } catch {
      events.push({ event, data: { message: dataLines.join("\n") } });
    }
  }
  return { events, rest };
}

function bubbleHtml(m) {
  const role = m.role === "user" ? "user" : "assistant";
  const refused = m.refused ? " is-refused" : "";
  const body =
    role === "assistant"
      ? `<div class="chat-bubble-body agent-md">${simpleMarkdown(m.content || "")}</div>`
      : `<div class="chat-bubble-body">${escapeHtml(m.content || "").replaceAll("\n", "<br>")}</div>`;
  return `<div class="chat-bubble chat-${role}${refused}" data-role="${role}">
    <div class="chat-bubble-meta">${role === "user" ? "我" : "追问助手"}${
      m.refused ? " · 已拒答" : ""
    } · ${escapeHtml(m.created_at || "")}</div>
    ${body}
  </div>`;
}

export function renderChatBubbles(container, items) {
  if (!container) return;
  if (!items?.length) {
    container.innerHTML = `<div class="chat-empty">暂无追问。可基于本报告继续提问（仅限投研相关）。</div>`;
    return;
  }
  container.innerHTML = items.map((m) => bubbleHtml(m)).join("");
  container.scrollTop = container.scrollHeight;
}

function ensureLiveBlock(container) {
  let block = container.querySelector(".chat-live-block");
  if (block) return block;
  block = document.createElement("div");
  block.className = "chat-live-block";
  block._phase = "思考中…";
  block._tools = [];
  block.innerHTML = `<div class="chat-bubble chat-assistant is-streaming">
      <div class="chat-bubble-meta">追问助手 · 思考中…</div>
      <div class="chat-bubble-body agent-md"></div>
    </div>`;
  container.appendChild(block);
  return block;
}

function _toolShortLabel(data) {
  return (data?.label || data?.message || data?.tool || "工具").trim();
}

function _formatToolsSuffix(tools) {
  if (!tools?.length) return "";
  // 只保留最近几条，避免 meta 过长撑破气泡
  const recent = tools.slice(-4);
  const text = recent
    .map((t) => (t.done ? `${t.label}✓` : `${t.label}…`))
    .join(" · ");
  const more = tools.length > recent.length ? `等${tools.length}项` : "";
  return more ? `${text} · ${more}` : text;
}

function refreshStreamingMeta(container) {
  const block = ensureLiveBlock(container);
  const meta = block.querySelector(".chat-bubble-meta");
  if (!meta) return;
  const phase = block._phase || "生成中…";
  const suffix = _formatToolsSuffix(block._tools || []);
  meta.textContent = suffix
    ? `追问助手 · ${phase} ${suffix}`
    : `追问助手 · ${phase}`;
}

function setStreamingPhase(container, phase) {
  const block = ensureLiveBlock(container);
  block._phase = phase || "生成中…";
  refreshStreamingMeta(container);
}

function setStreamingMeta(container, text) {
  // 兼容旧调用：整段文案写入 phase（不含「追问助手 ·」前缀时自动补）
  const raw = (text || "").trim();
  const phase = raw.replace(/^追问助手\s*·\s*/, "") || "生成中…";
  setStreamingPhase(container, phase);
}

function updateStreamingBubble(container, text) {
  const block = ensureLiveBlock(container);
  const body = block.querySelector(".chat-bubble-body");
  if (body) body.innerHTML = simpleMarkdown(text || "");
  if (text) setStreamingPhase(container, "正在生成回答…");
  container.scrollTop = container.scrollHeight;
}

function noteToolStart(container, data) {
  const block = ensureLiveBlock(container);
  const tools = block._tools || (block._tools = []);
  const seq = data.seq != null ? String(data.seq) : "";
  const tool = data.tool || "";
  const label = _toolShortLabel(data);
  let item =
    (seq && tools.find((t) => t.seq === seq)) ||
    tools.find((t) => !t.done && t.tool === tool) ||
    null;
  if (!item) {
    item = { seq, tool, label, done: false };
    tools.push(item);
  } else {
    item.label = label;
    item.done = false;
  }
  setStreamingPhase(container, "正在调用工具…");
  container.scrollTop = container.scrollHeight;
}

function noteToolEnd(container, data) {
  const block = container.querySelector(".chat-live-block");
  if (!block) return;
  const tools = block._tools || [];
  const tool = data.tool || "";
  const item =
    tools.find((t) => !t.done && (!tool || t.tool === tool)) ||
    tools.find((t) => !t.done);
  if (item) {
    item.done = true;
    if (data.label) item.label = data.label;
  }
  const stillRunning = tools.some((t) => !t.done);
  setStreamingPhase(
    container,
    stillRunning ? "正在调用工具…" : "正在生成回答…"
  );
  container.scrollTop = container.scrollHeight;
}

export async function fetchReportMessages(reportId) {
  const res = await apiFetch(`/api/reports/${encodeURIComponent(reportId)}/messages`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "加载对话失败");
  return data.items || [];
}

export async function sendFollowupChat(reportId, message, { onStatus, onToken, onTool } = {}) {
  const res = await apiFetch(`/api/reports/${encodeURIComponent(reportId)}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    let detail = "追问失败";
    try {
      const err = await res.json();
      detail = err.detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (!res.body) throw new Error("浏览器不支持流式响应");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalData = null;
  let streamed = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    for (const item of parsed.events) {
      const data = item.data || {};
      if (item.event === "status") {
        onStatus?.(data.message || "处理中…");
      } else if (item.event === "tool_start") {
        onTool?.(data, "start");
        onStatus?.(data.message || `调用工具：${data.tool || ""}`);
      } else if (item.event === "tool_end") {
        onTool?.(data, "end");
        onStatus?.(data.message || `${data.tool || "工具"}完成`);
      } else if (item.event === "token_reset") {
        streamed = "";
        onToken?.("", "");
      } else if (item.event === "token") {
        const piece = data.text || "";
        streamed += piece;
        onToken?.(piece, streamed);
      } else if (item.event === "final") {
        finalData = data;
      } else if (item.event === "error") {
        throw new Error(data.message || "追问失败");
      }
    }
  }
  if (!finalData) throw new Error("未收到追问结果");
  return finalData;
}

/**
 * 绑定一个追问面板：
 * { reportId, listEl, inputEl, sendBtn, statusEl }
 */
export function bindFollowupPanel(opts) {
  const { listEl, inputEl, sendBtn, statusEl, getReportId } = opts;
  let busy = false;

  async function reload() {
    const reportId = getReportId?.() || opts.reportId;
    if (!reportId) {
      renderChatBubbles(listEl, []);
      return [];
    }
    const items = await fetchReportMessages(reportId);
    renderChatBubbles(listEl, items);
    return items;
  }

  async function send() {
    const reportId = getReportId?.() || opts.reportId;
    if (!reportId) {
      showToast("请先完成一次深研分析", { type: "error" });
      return;
    }
    const text = (inputEl?.value || "").trim();
    if (!text || busy) return;
    busy = true;
    if (sendBtn) sendBtn.disabled = true;
    if (statusEl) statusEl.textContent = "发送中…";
    try {
      let prev = [];
      try {
        prev = await fetchReportMessages(reportId);
      } catch {
        prev = [];
      }
      renderChatBubbles(listEl, [
        ...prev,
        {
          role: "user",
          content: text,
          created_at: "刚刚",
          refused: false,
        },
      ]);
      inputEl.value = "";
      setStreamingPhase(listEl, "思考中…");
      const result = await sendFollowupChat(reportId, text, {
        onStatus: (msg) => {
          if (statusEl) statusEl.textContent = msg;
          // 工具细节已在 meta 后拼接，status 只更新阶段，避免盖掉工具列表
          if (!msg) return;
          if (/正在生成|整理结果|思考/.test(msg)) {
            setStreamingPhase(listEl, msg);
          }
        },
        onTool: (data, phase) => {
          if (phase === "end") noteToolEnd(listEl, data);
          else noteToolStart(listEl, data);
        },
        onToken: (_piece, full) => {
          updateStreamingBubble(listEl, full);
        },
      });
      await reload();
      if (result.refused) showToast("该问题与投研无关，已拒答");
      if (statusEl) statusEl.textContent = "";
    } catch (err) {
      showToast(err.message || String(err), { type: "error" });
      if (statusEl) statusEl.textContent = "";
      try {
        await reload();
      } catch {
        /* ignore */
      }
    } finally {
      busy = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  sendBtn?.addEventListener("click", () => send());
  inputEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  return { reload, send };
}
