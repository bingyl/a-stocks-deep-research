/** 分析任务 API（独立模块，避免 agent ↔ history 循环依赖） */

import { apiFetch } from "./api.js?v=20260812auth";

export function conflictMessage(detail) {
  if (!detail) return "该股票已有进行中的分析任务，请等待完成后再试";
  if (typeof detail === "string") return detail;
  return detail.message || "该股票已有进行中的分析任务，请等待完成后再试";
}

export function parseSseChunk(buffer) {
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

export async function cancelReportAnalysis(reportId) {
  if (!reportId) throw new Error("缺少报告 id");
  const res = await apiFetch(`/api/reports/${encodeURIComponent(reportId)}/cancel`, {
    method: "POST",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : detail?.message || data.message || "取消失败";
    throw new Error(msg);
  }
  return data;
}

/** 后台启动分析（历史页重跑用）；传 reportId 则在原任务上重跑。
 *
 * @param {string} code
 * @param {number|string|null} reportId
 * @param {{ onStarted?: (info: { reportId: number|null, data: object }) => void }} [options]
 *   onStarted：收到首个有效 SSE（任务已落库为 running）时回调，便于立刻刷新列表
 */
export async function startAnalyzeInBackground(code, reportId = null, options = {}) {
  const { onStarted } = options;
  const payload = { code, question: null };
  if (reportId) payload.report_id = Number(reportId);
  const res = await apiFetch("/api/agent/analyze/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    let detail = "启动分析失败";
    try {
      const err = await res.json();
      detail = err.detail || detail;
    } catch {
      /* ignore */
    }
    if (res.status === 409) throw new Error(conflictMessage(detail));
    throw new Error(typeof detail === "string" ? detail : conflictMessage(detail));
  }
  if (!res.body) return null;
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let outId = reportId != null ? Number(reportId) : null;
  let startedNotified = false;
  const notifyStarted = (data) => {
    if (startedNotified) return;
    startedNotified = true;
    try {
      onStarted?.({ reportId: outId, data });
    } catch {
      /* ignore UI callback errors */
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    for (const item of parsed.events) {
      const data = item.data || {};
      if (data.report_id) outId = data.report_id;
      if (item.event === "error") throw new Error(data.message || "分析失败");
      if (
        item.event === "status" ||
        item.event === "stage" ||
        item.event === "tool_start" ||
        data.report_id
      ) {
        notifyStarted(data);
      }
      if (item.event === "final" || item.event === "cancelled") {
        notifyStarted(data);
      }
    }
  }
  return outId;
}
