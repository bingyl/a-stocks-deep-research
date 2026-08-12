import { $, escapeHtml, simpleMarkdown, showToast } from "./utils.js?v=20260808am";
import { downloadReportAsHtml } from "./report-export.js?v=20260808am";
import { bindFollowupPanel, fetchReportMessages, renderChatBubbles } from "./chat.js?v=20260808af";
import { apiFetch } from "./api.js?v=20260812auth";
import {
  cancelReportAnalysis,
  startAnalyzeInBackground,
} from "./analyze-api.js?v=20260812auth";

let currentDetail = null;
let historyUnsubscribers = [];
let modalChat = null;
let modalTab = "report";
let progressPollTimer = null;
let progressReportId = null;
let selectedCompareIds = new Set();
let historyItemsCache = [];
/** 后台分析中的报告 id：列表尚未刷成 running 时也要继续轮询 */
let backgroundWatchIds = new Set();
/** 正在删除中的报告 id，防止连点与误操作 */
let deletingIds = new Set();

/** 退出/换账号时清空客户端历史状态，避免串号 */
export function clearHistoryClientState() {
  historyItemsCache = [];
  backgroundWatchIds.clear();
  deletingIds.clear();
  selectedCompareIds.clear();
  currentDetail = null;
  if (progressPollTimer) {
    clearInterval(progressPollTimer);
    progressPollTimer = null;
  }
  progressReportId = null;
  closeProgressModal();
  closeReportModal();
  const cm = $("compareModal");
  if (cm) cm.hidden = true;
  document.body.classList.remove("modal-open");
}

function statusClass(status) {
  const s = String(status || "done");
  if (s === "running" || s === "pending") return "is-running";
  if (s === "error") return "is-error";
  if (s === "cancelled") return "is-cancelled";
  return "is-done";
}

function isLiveStatus(status) {
  return status === "running" || status === "pending";
}

function formatElapsedFrom(startIso, endIso) {
  if (!startIso) return "已用 —";
  const start = Date.parse(String(startIso).replace(/-/g, "/"));
  if (!Number.isFinite(start)) return "已用 —";
  const end = endIso ? Date.parse(String(endIso).replace(/-/g, "/")) : Date.now();
  const endMs = Number.isFinite(end) ? end : Date.now();
  const sec = Math.max(0, Math.floor((endMs - start) / 1000));
  if (sec < 60) return `已用 ${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `已用 ${m}分${String(s).padStart(2, "0")}秒`;
}

function progressPct(index, total, running) {
  const t = Math.max(1, Number(total) || 6);
  const i = Math.max(0, Number(index) || 0);
  if (!running && i >= t) return 100;
  if (!running) return Math.min(99, Math.round((i / t) * 100));
  const doneSteps = Math.max(0, i - (i > 0 ? 0.35 : 0));
  return Math.min(99, Math.round((doneSteps / t) * 100));
}

function on(el, event, handler) {
  if (!el) return;
  el.addEventListener(event, handler);
  historyUnsubscribers.push(() => el.removeEventListener(event, handler));
}

function setModalTab(tab) {
  modalTab = tab === "chat" ? "chat" : "report";
  const tabReport = $("reportModalTabReport");
  const tabChat = $("reportModalTabChat");
  const paneReport = $("reportModalReportPane");
  const paneChat = $("reportModalChatPane");
  const downloadBtn = $("reportModalDownload");
  const foot = $("reportModalFoot");
  if (tabReport) tabReport.classList.toggle("is-active", modalTab === "report");
  if (tabChat) tabChat.classList.toggle("is-active", modalTab === "chat");
  if (paneReport) paneReport.hidden = modalTab !== "report";
  if (paneChat) paneChat.hidden = modalTab !== "chat";
  if (downloadBtn) downloadBtn.hidden = modalTab !== "report";
  if (foot) foot.hidden = modalTab !== "report";
}

function ensureReportModal() {
  let modal = $("reportModal");
  if (modal) return modal;

  modal = document.createElement("div");
  modal.id = "reportModal";
  modal.className = "report-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="report-modal-backdrop" data-close="1"></div>
    <div class="report-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="reportModalTitle">
      <header class="report-modal-head">
        <div class="report-modal-titles">
          <h2 id="reportModalTitle">深研报告</h2>
          <p class="report-modal-sub" id="reportModalSub"></p>
        </div>
        <button type="button" class="report-modal-x" id="reportModalClose" aria-label="关闭">×</button>
      </header>
      <div class="report-modal-tabs" role="tablist">
        <button type="button" class="report-modal-tab is-active" id="reportModalTabReport" data-mtab="report">报告</button>
        <button type="button" class="report-modal-tab" id="reportModalTabChat" data-mtab="chat">追问记录</button>
      </div>
      <div class="report-modal-main">
        <div class="report-modal-body agent-md" id="reportModalReportPane">
          <div id="reportModalBody"></div>
        </div>
        <div class="report-modal-chat-pane" id="reportModalChatPane" hidden>
          <div class="chat-list" id="reportModalChatList"></div>
          <div class="chat-compose">
            <textarea id="reportModalChatInput" rows="4" placeholder="基于本报告继续追问（仅限投研相关）"></textarea>
            <button type="button" id="reportModalChatSend">发送</button>
          </div>
          <div class="chat-status" id="reportModalChatStatus"></div>
        </div>
      </div>
      <footer class="report-modal-foot" id="reportModalFoot">
        <button type="button" class="btn-secondary" id="reportModalDownload">下载 HTML</button>
      </footer>
    </div>
  `;
  document.body.appendChild(modal);

  const close = () => closeReportModal();
  modal.querySelectorAll("[data-close], #reportModalClose").forEach((el) => {
    el.addEventListener("click", close);
  });
  modal.querySelector("#reportModalDownload")?.addEventListener("click", () => {
    if (!currentDetail) return;
    const filename = downloadReportAsHtml({
      name: currentDetail.name,
      code: currentDetail.code,
      createdAt: currentDetail.created_at,
      analysis: currentDetail.analysis,
    });
    showToast(`已下载 ${filename}`);
  });
  modal.querySelectorAll("[data-mtab]").forEach((el) => {
    el.addEventListener("click", async () => {
      setModalTab(el.dataset.mtab);
      if (modalTab === "chat" && modalChat) {
        try {
          await modalChat.reload();
        } catch (err) {
          showToast(err.message || String(err), { type: "error" });
        }
      }
    });
  });

  modalChat = bindFollowupPanel({
    listEl: $("reportModalChatList"),
    inputEl: $("reportModalChatInput"),
    sendBtn: $("reportModalChatSend"),
    statusEl: $("reportModalChatStatus"),
    getReportId: () => currentDetail?.id || null,
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal && !modal.hidden) closeReportModal();
  });
  return modal;
}

export function closeReportModal() {
  const modal = $("reportModal");
  if (!modal) return;
  modal.hidden = true;
  if ($("progressModal")?.hidden !== false) {
    document.body.classList.remove("modal-open");
  }
  currentDetail = null;
  setModalTab("report");
}

function stopProgressPoll() {
  if (progressPollTimer) {
    clearInterval(progressPollTimer);
    progressPollTimer = null;
  }
  progressReportId = null;
}

function stageKeyOf(step) {
  if (step?.stage) return `stage:${step.stage}`;
  if (step?.index != null && step.index !== "") return `idx:${Number(step.index)}`;
  const message = String(step?.message || "");
  const m = message.match(/阶段\s*(\d+)\s*\/\s*(\d+)/);
  if (m) return `idx:${Number(m[1])}`;
  if (step?.title) return `title:${step.title}`;
  return `msg:${message.replace(/完成/g, "").replace(/\s+/g, " ").trim()}`;
}

function isStageDone(step, forceDone = false) {
  if (forceDone) return true;
  const message = String(step?.message || "");
  return (
    step?.stage_status === "end" ||
    message.includes("完成") ||
    message.startsWith("完成 ·")
  );
}

function renderProgressLogItems(steps, { forceDone = false } = {}) {
  if (!steps?.length) {
    return `<div class="agent-progress-item">暂无步骤明细</div>`;
  }

  const ordered = steps.slice();
  const stageLatest = new Map();
  for (const step of ordered) {
    if ((step.kind || "status") !== "stage") continue;
    const message = String(step.message || "").trim();
    if (!message) continue;
    const stageId = stageKeyOf(step);
    const prev = stageLatest.get(stageId);
    const done = isStageDone(step, forceDone) || Boolean(prev?.done);
    // 同一阶段只保留一条：优先展示「完成」文案
    let finalMessage = message;
    if (prev?.message) {
      if (done && prev.message.includes("完成") && !message.includes("完成")) {
        finalMessage = prev.message;
      } else if (!message.includes("完成") && prev.message.includes("完成")) {
        finalMessage = prev.message;
      } else if (done && message.includes("完成")) {
        finalMessage = message;
      } else {
        finalMessage = message;
      }
    }
    stageLatest.set(stageId, { message: finalMessage, done });
  }
  if (forceDone) {
    for (const [id, info] of stageLatest) {
      stageLatest.set(id, { ...info, done: true });
    }
  }

  const renderedStages = new Set();
  const rows = [];
  for (const step of ordered) {
    const kind = step.kind || "status";
    const message = String(step.message || "").trim();
    if (!message) continue;

    if (kind === "stage") {
      const stageId = stageKeyOf(step);
      if (renderedStages.has(stageId)) continue;
      renderedStages.add(stageId);
      const latest = stageLatest.get(stageId) || {
        message,
        done: isStageDone(step, forceDone),
      };
      const done = Boolean(latest.done) || forceDone;
      const cls = `agent-progress-item stage ${done ? "stage-done" : "stage-active"}`;
      const mark = done
        ? `<span class="stage-mark">✓</span>`
        : `<span class="stage-loading" aria-hidden="true"></span>`;
      const extra = done ? "" : ` <span class="stage-loading-text">进行中…</span>`;
      rows.push(
        `<div class="${cls}" data-stage="${escapeHtml(stageId)}">${mark}<span>${escapeHtml(
          latest.message
        )}</span>${extra}</div>`
      );
      continue;
    }

    if (kind === "tool") {
      rows.push(
        `<div class="agent-progress-item tool">${escapeHtml(`  ↳ ${message}`)}</div>`
      );
      continue;
    }
    if (kind === "error") {
      rows.push(`<div class="agent-progress-item error">${escapeHtml(message)}</div>`);
      continue;
    }
    if (kind === "status" && (message.includes("完成") || message.includes("报告已生成"))) {
      rows.push(`<div class="agent-progress-item done">${escapeHtml(message)}</div>`);
      continue;
    }
    rows.push(`<div class="agent-progress-item status">${escapeHtml(message)}</div>`);
  }
  return rows.join("") || `<div class="agent-progress-item">暂无步骤明细</div>`;
}

function renderProgressBody(data) {
  const detail = data.status_detail || {};
  const steps = Array.isArray(detail.steps) ? detail.steps : [];
  const idx = Number(detail.stage_index || 0);
  const total = Number(detail.stage_total || 6) || 6;
  const title = detail.stage_title || "";
  const msg = detail.message || data.status_detail_text || "分析进行中…";
  const running = isLiveStatus(data.status);
  const errored = data.status === "error";
  const done = data.status === "done";
  const pct = progressPct(idx, total, running && !done);
  const label = title
    ? `${done || !running ? "已完成" : "进行中"} ${Math.min(idx, total)}/${total} · ${title}`
    : done
      ? "分析完成"
      : running
        ? "分析进行中…"
        : data.status_label || "进度";
  const startedAt =
    detail.started_at || data.started_at || data.created_at || null;
  const elapsed = formatElapsedFrom(
    startedAt,
    done || errored ? detail.updated_at || startedAt : null
  );
  const wrapCls = [
    "agent-progress-wrap",
    "progress-modal-progress",
    running ? "is-running" : "",
    done ? "is-done" : "",
    errored ? "is-error" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return `
    <div class="${wrapCls}">
      <div class="agent-progress-head">
        <div class="agent-progress-top">
          <span class="agent-spinner" aria-hidden="true"></span>
          <span class="agent-progress-label">${escapeHtml(label)}</span>
          <span class="agent-progress-elapsed">${escapeHtml(elapsed)}</span>
        </div>
        <div class="agent-progress-bar" aria-hidden="true">
          <div class="agent-progress-bar-fill${running ? " indeterminate" : ""}" style="width:${pct}%"></div>
        </div>
        <div class="agent-progress-meta">
          <span>${pct}%</span>
          <span class="agent-progress-hint">${escapeHtml(
            running ? `正在执行：${msg}` : msg
          )}</span>
        </div>
      </div>
      <div class="agent-progress progress-modal-log">
        ${renderProgressLogItems(steps, { forceDone: done || errored })}
      </div>
    </div>
  `;
}

function ensureProgressModal() {
  let modal = $("progressModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "progressModal";
  modal.className = "report-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="report-modal-backdrop" data-close="1"></div>
    <div class="report-modal-dialog progress-modal-dialog" role="dialog" aria-modal="true">
      <header class="report-modal-head">
        <div class="report-modal-titles">
          <h2 id="progressModalTitle">状态详情</h2>
          <p class="report-modal-sub" id="progressModalSub"></p>
        </div>
        <button type="button" class="report-modal-x" id="progressModalClose" aria-label="关闭">×</button>
      </header>
      <div class="progress-modal-body" id="progressModalBody"></div>
    </div>
  `;
  document.body.appendChild(modal);
  const close = () => closeProgressModal();
  modal.querySelectorAll("[data-close], #progressModalClose").forEach((el) => {
    el.addEventListener("click", close);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal && !modal.hidden) closeProgressModal();
  });
  return modal;
}

export function closeProgressModal() {
  stopProgressPoll();
  const modal = $("progressModal");
  if (!modal) return;
  modal.hidden = true;
  if ($("reportModal")?.hidden !== false) {
    document.body.classList.remove("modal-open");
  }
}

async function refreshProgressModal() {
  if (!progressReportId) return;
  const res = await apiFetch(`/api/reports/${encodeURIComponent(progressReportId)}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "加载进度失败");

  const title = $("progressModalTitle");
  const sub = $("progressModalSub");
  const body = $("progressModalBody");
  if (title) {
    title.textContent = "状态详情";
  }
  if (sub) {
    sub.textContent = [
      data.status_label || data.status || "",
      data.created_at ? `创建于 ${data.created_at}` : "",
      data.id ? `#${data.id}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  if (body) body.innerHTML = renderProgressBody(data);
  const log = body?.querySelector(".progress-modal-log");
  if (log) log.scrollTop = log.scrollHeight;

  if (!isLiveStatus(data.status)) {
    stopProgressPoll();
    // 完成后顺带刷新列表状态（静默，避免闪烁）
    loadHistoryList({ silent: true }).catch(() => {});
  }
}

export async function openProgressModal(reportId) {
  const modal = ensureProgressModal();
  const body = $("progressModalBody");
  if (body) body.innerHTML = `<div class="page-loading">加载状态详情…</div>`;
  modal.hidden = false;
  document.body.classList.add("modal-open");
  stopProgressPoll();
  progressReportId = reportId;
  await refreshProgressModal();
  progressPollTimer = setInterval(() => {
    refreshProgressModal().catch(() => {});
  }, 1500);
}

export async function openReportModal(reportId, { tab = "report" } = {}) {
  const modal = ensureReportModal();
  const title = $("reportModalTitle");
  const sub = $("reportModalSub");
  const body = $("reportModalBody");
  if (title) title.textContent = "加载中…";
  if (sub) sub.textContent = "";
  if (body) body.innerHTML = `<div class="page-loading">正在打开报告…</div>`;
  renderChatBubbles($("reportModalChatList"), []);
  setModalTab(tab);
  modal.hidden = false;
  document.body.classList.add("modal-open");

  const res = await apiFetch(`/api/reports/${encodeURIComponent(reportId)}`);
  const data = await res.json();
  if (!res.ok) {
    if (title) title.textContent = "打开失败";
    if (body) {
      body.innerHTML = `<div class="msg error">${escapeHtml(data.detail || "报告不存在")}</div>`;
    }
    return;
  }

  currentDetail = data;
  if (title) {
    title.textContent = `${data.name || "个股"}${data.code ? `（${data.code}）` : ""}`;
  }
  if (sub) {
    const bits = [
      data.created_at ? `分析时间 ${data.created_at}` : "",
      data.model ? `模型 ${data.model}` : "",
      data.id ? `#${data.id}` : "",
      data.message_count ? `追问 ${data.message_count} 条` : "",
    ].filter(Boolean);
    sub.textContent = bits.join(" · ");
  }
  if (body) {
    if (data.analysis) {
      body.innerHTML = simpleMarkdown(data.analysis || "");
    } else if (isLiveStatus(data.status)) {
      body.innerHTML = `<div class="msg">${escapeHtml(
        data.status_detail_text || "分析进行中，可点击「状态详情」查看实时进度。"
      )}</div>`;
    } else if (data.status === "error") {
      body.innerHTML = `<div class="msg error">${escapeHtml(
        data.status_detail_text || "分析失败"
      )}</div>`;
    } else {
      body.innerHTML = `<div class="msg">暂无报告正文</div>`;
    }
  }

  if (modalTab === "chat" && modalChat) {
    try {
      await modalChat.reload();
    } catch (err) {
      showToast(err.message || String(err), { type: "error" });
    }
  }
}

function syncCompareButton() {
  const btn = $("historyCompareBtn");
  if (!btn) return;
  const n = selectedCompareIds.size;
  btn.disabled = n !== 2;
  btn.textContent = n ? `对比选中（${n}/2）` : "对比选中";
}

function ensureCompareModal() {
  let modal = $("compareModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "compareModal";
  modal.className = "report-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="report-modal-backdrop" data-close="1"></div>
    <div class="report-modal-dialog compare-modal-dialog" role="dialog" aria-modal="true">
      <header class="report-modal-head">
        <div class="report-modal-titles">
          <h2 id="compareModalTitle">报告对比</h2>
          <p class="report-modal-sub" id="compareModalSub"></p>
        </div>
        <button type="button" class="report-modal-x" id="compareModalClose" aria-label="关闭">×</button>
      </header>
      <div class="compare-modal-body" id="compareModalBody"></div>
    </div>
  `;
  document.body.appendChild(modal);
  const close = () => {
    modal.hidden = true;
    if ($("reportModal")?.hidden !== false && $("progressModal")?.hidden !== false) {
      document.body.classList.remove("modal-open");
    }
  };
  modal.querySelectorAll("[data-close], #compareModalClose").forEach((el) => {
    el.addEventListener("click", close);
  });
  return modal;
}

export async function openCompareModal(idA, idB) {
  const modal = ensureCompareModal();
  const body = $("compareModalBody");
  const sub = $("compareModalSub");
  if (body) body.innerHTML = `<div class="page-loading">加载对比…</div>`;
  modal.hidden = false;
  document.body.classList.add("modal-open");

  const res = await apiFetch(
    `/api/reports/compare?ids=${encodeURIComponent(`${idA},${idB}`)}`
  );
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "对比失败");

  const reps = data.reports || [];
  if (sub) {
    sub.textContent = [
      data.same_stock ? "同股对比" : "跨股/同业对比",
      ...reps.map(
        (r) =>
          `${r.name || ""}（${r.code || ""}）#${r.id}${
            r.created_at ? ` · ${r.created_at}` : ""
          }`
      ),
    ].join(" · ");
  }

  const head = `
    <div class="compare-grid compare-grid-head">
      <div class="compare-sec-title">章节</div>
      ${reps
        .map(
          (r) =>
            `<div class="compare-col-head">${escapeHtml(r.name || "报告")}（${escapeHtml(
              r.code || ""
            )}）<div class="compare-col-sub">#${r.id} · ${escapeHtml(
              r.created_at || ""
            )}</div></div>`
        )
        .join("")}
    </div>`;

  const rows = (data.sections || [])
    .map((sec) => {
      const cells = (sec.cells || [])
        .map(
          (c) =>
            `<div class="compare-cell agent-md">${
              c.body ? simpleMarkdown(c.body) : `<span class="compare-empty">（无此章节）</span>`
            }</div>`
        )
        .join("");
      return `<div class="compare-grid compare-grid-row">
        <div class="compare-sec-title">${escapeHtml(sec.title || "")}</div>
        ${cells}
      </div>`;
    })
    .join("");

  if (body) {
    body.innerHTML =
      head +
      (rows || `<div class="chat-empty">没有可对比的章节内容</div>`);
  }
}

function historyItemKey(item) {
  return [
    item.id,
    item.status,
    item.status_label,
    item.status_detail_text,
    item.preview,
    item.message_count,
    item.created_at,
    item.model,
    item.name,
    item.code,
  ].join("\t");
}

function historyActionsHtml(item) {
  const id = String(item.id);
  if (deletingIds.has(id)) {
    return `<span class="hist-deleting-label" aria-live="polite">正在删除…</span>`;
  }
  const st = item.status || "done";
  const done = st === "done";
  const live = isLiveStatus(st);
  const canRetry = st === "error" || st === "cancelled";
  return `
    ${
      live
        ? `<button type="button" class="linkish" data-act="cancel" data-id="${item.id}">取消</button>`
        : ""
    }
    ${
      canRetry
        ? `<button type="button" class="linkish" data-act="retry" data-id="${item.id}" data-code="${escapeHtml(
            item.code || ""
          )}">重跑</button>`
        : ""
    }
    <button type="button" class="linkish" data-act="view" data-id="${item.id}" ${
      done ? "" : "disabled"
    }>查看</button>
    <button type="button" class="linkish" data-act="chat" data-id="${item.id}" ${
      done ? "" : "disabled"
    }>追问</button>
    <button type="button" class="linkish" data-act="download" data-id="${item.id}" ${
      done ? "" : "disabled"
    }>下载</button>
    <button type="button" class="linkish danger" data-act="delete" data-id="${item.id}">删除</button>
  `;
}

function markRowDeleting(id, on) {
  const rid = String(id);
  const tr = document.querySelector(`#historyList tr[data-id="${CSS.escape(rid)}"]`);
  if (!tr) return;
  tr.classList.toggle("is-deleting", on);
  tr.setAttribute("aria-busy", on ? "true" : "false");
  const actions = tr.querySelector(".col-actions");
  if (actions) {
    if (on) {
      actions.innerHTML = `<span class="hist-deleting-label" aria-live="polite">正在删除…</span>`;
    }
  }
  tr.querySelectorAll("button, input").forEach((el) => {
    el.disabled = on;
  });
  const detailBtn = tr.querySelector(".hist-status-detail-btn");
  if (detailBtn) detailBtn.disabled = on;
}

function historyRowHtml(item) {
  const mc = Number(item.message_count || 0);
  const chatBadge =
    mc > 0
      ? `<span class="hist-chat-badge">${mc}</span>`
      : `<span class="hist-chat-zero">0</span>`;
  const st = item.status || "done";
  const stLabel = item.status_label || st;
  const detailText =
    item.status_detail_text || (isLiveStatus(st) ? "查看实时进度" : "查看进度");
  const done = st === "done";
  const isDeleting = deletingIds.has(String(item.id));
  const checked = selectedCompareIds.has(String(item.id)) ? "checked" : "";
  return `
    <tr data-id="${item.id}" data-sig="${escapeHtml(historyItemKey(item))}" class="${
      isDeleting ? "is-deleting" : ""
    }" ${isDeleting ? 'aria-busy="true"' : ""}>
      <td class="col-check">
        <input type="checkbox" class="hist-compare-check" data-id="${item.id}" ${
          done && !isDeleting ? "" : "disabled"
        } ${checked} title="${done ? "勾选以对比" : "仅已完成可对比"}" />
      </td>
      <td class="col-time">${escapeHtml(item.created_at || "-")}</td>
      <td class="col-stock">
        <div class="hist-name">${escapeHtml(item.name || "-")}</div>
        <div class="hist-code">${escapeHtml(item.code || "")}</div>
      </td>
      <td class="col-status">
        <span class="hist-status ${statusClass(st)}">${escapeHtml(stLabel)}</span>
      </td>
      <td class="col-status-detail">
        <button type="button" class="hist-status-detail-btn" data-act="progress" data-id="${item.id}" title="查看状态详情">
          ${escapeHtml(detailText)}
        </button>
      </td>
      <td class="col-preview"><div class="col-preview-text">${escapeHtml(item.preview || "")}</div></td>
      <td class="col-chat">${chatBadge}</td>
      <td class="col-model">${escapeHtml(item.model || "-")}</td>
      <td class="col-actions">${historyActionsHtml(item)}</td>
    </tr>`;
}

function patchHistoryRow(tr, item) {
  const key = historyItemKey(item);
  const rid = String(item.id);
  if (deletingIds.has(rid)) {
    // 删除中不刷新操作列，避免把「正在删除…」冲掉
    markRowDeleting(rid, true);
    return;
  }
  if (tr.dataset.sig === key) return;
  tr.dataset.sig = key;

  const st = item.status || "done";
  const stLabel = item.status_label || st;
  const detailText =
    item.status_detail_text || (isLiveStatus(st) ? "查看实时进度" : "查看进度");
  const done = st === "done";
  const mc = Number(item.message_count || 0);

  const statusEl = tr.querySelector(".hist-status");
  if (statusEl) {
    statusEl.className = `hist-status ${statusClass(st)}`;
    statusEl.textContent = stLabel;
  }
  const detailBtn = tr.querySelector(".hist-status-detail-btn");
  if (detailBtn) detailBtn.textContent = detailText;

  const preview = tr.querySelector(".col-preview-text");
  if (preview) preview.textContent = item.preview || "";

  const chat = tr.querySelector(".col-chat");
  if (chat) {
    chat.innerHTML =
      mc > 0
        ? `<span class="hist-chat-badge">${mc}</span>`
        : `<span class="hist-chat-zero">0</span>`;
  }

  const time = tr.querySelector(".col-time");
  if (time) time.textContent = item.created_at || "-";

  const name = tr.querySelector(".hist-name");
  if (name) name.textContent = item.name || "-";
  const code = tr.querySelector(".hist-code");
  if (code) code.textContent = item.code || "";

  const model = tr.querySelector(".col-model");
  if (model) model.textContent = item.model || "-";

  const ck = tr.querySelector(".hist-compare-check");
  if (ck) {
    ck.disabled = !done;
    ck.title = done ? "勾选以对比" : "仅已完成可对比";
    if (!done) ck.checked = false;
  }

  const actions = tr.querySelector(".col-actions");
  if (actions) actions.innerHTML = historyActionsHtml(item);
}

/** 同行同序时就地更新，避免整表替换闪烁 */
function tryPatchHistoryTable(box, items) {
  const tbody = box.querySelector(".history-table tbody");
  if (!tbody) return false;
  const rows = [...tbody.querySelectorAll("tr[data-id]")];
  if (rows.length !== items.length) return false;
  if (rows.some((tr, i) => String(tr.dataset.id) !== String(items[i].id))) return false;
  items.forEach((item, i) => patchHistoryRow(rows[i], item));
  return true;
}

function renderHistoryTable(box, items) {
  box.innerHTML = `
    <div class="history-table-wrap">
      <table class="history-table">
        <thead>
          <tr>
            <th class="col-check" title="勾选两份已完成报告进行对比" aria-label="勾选对比"></th>
            <th>分析时间</th>
            <th>股票</th>
            <th>状态</th>
            <th>状态详情</th>
            <th>摘要</th>
            <th>追问</th>
            <th>模型</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((item) => historyRowHtml(item)).join("")}
        </tbody>
      </table>
    </div>`;
}

async function loadHistoryList({ silent = false } = {}) {
  const box = $("historyList");
  const meta = $("historyMeta");
  const q = ($("historyQ")?.value || "").trim();
  if (!silent && box) {
    box.innerHTML = `<div class="page-loading">加载历史记录…</div>`;
  }

  const params = new URLSearchParams({ limit: "50", offset: "0" });
  if (q) params.set("q", q);

  try {
    const res = await apiFetch(`/api/reports?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "加载失败");

    historyItemsCache = data.items || [];
    // 清理已不存在的勾选
    const alive = new Set(historyItemsCache.map((x) => String(x.id)));
    selectedCompareIds = new Set(
      [...selectedCompareIds].filter((id) => alive.has(String(id)))
    );
    // 后台 watch：任务已离开 live 态则移除
    for (const id of [...backgroundWatchIds]) {
      const row = historyItemsCache.find((x) => String(x.id) === id);
      if (row && !isLiveStatus(row.status)) backgroundWatchIds.delete(id);
    }
    syncCompareButton();

    if (meta) meta.textContent = `共 ${data.total} 条深研记录`;

    if (!data.items?.length) {
      if (box) {
        box.innerHTML = `<div class="history-empty">
          <h3>还没有深研记录</h3>
          <p>在「A股深研」完成一次分析后，报告会自动保存到这里。</p>
        </div>`;
      }
      return;
    }

    if (!box) return;
    if (silent && tryPatchHistoryTable(box, data.items)) return;
    renderHistoryTable(box, data.items);
  } catch (err) {
    if (silent) return; // 静默轮询失败不打断当前表格
    if (meta) meta.textContent = "";
    if (box) {
      box.innerHTML = `<div class="msg error">${escapeHtml(err.message || String(err))}</div>`;
    }
  }
}

async function downloadById(id) {
  const res = await apiFetch(`/api/reports/${encodeURIComponent(id)}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "下载失败");
  return downloadReportAsHtml({
    name: data.name,
    code: data.code,
    createdAt: data.created_at,
    analysis: data.analysis,
  });
}

export function mountResearchHistory() {
  historyUnsubscribers = [];
  ensureReportModal();
  ensureCompareModal();
  selectedCompareIds = new Set();
  syncCompareButton();

  on($("historySearchBtn"), "click", () => loadHistoryList());
  on($("historyQ"), "keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      loadHistoryList();
    }
  });
  on($("historyRefreshBtn"), "click", () => loadHistoryList());
  on($("historyCompareBtn"), "click", async () => {
    const ids = [...selectedCompareIds];
    if (ids.length !== 2) {
      showToast("请勾选恰好两份已完成报告", { type: "error" });
      return;
    }
    try {
      await openCompareModal(ids[0], ids[1]);
    } catch (err) {
      showToast(err.message || String(err), { type: "error" });
    }
  });
  on($("historyList"), "change", (e) => {
    const ck = e.target.closest(".hist-compare-check");
    if (!ck) return;
    if (deletingIds.size) {
      ck.checked = !ck.checked;
      showToast("正在删除记录，请稍候…");
      return;
    }
    const id = String(ck.dataset.id || "");
    if (!id) return;
    if (ck.checked) {
      if (selectedCompareIds.size >= 2 && !selectedCompareIds.has(id)) {
        ck.checked = false;
        showToast("最多勾选两份报告进行对比");
        return;
      }
      selectedCompareIds.add(id);
    } else {
      selectedCompareIds.delete(id);
    }
    syncCompareButton();
  });
  on($("historyList"), "click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    if (deletingIds.size) {
      showToast("正在删除记录，请稍候…");
      return;
    }
    try {
      if (act === "progress") {
        await openProgressModal(id);
      } else if (act === "cancel") {
        if (!confirm("确定取消该分析任务？")) return;
        const result = await cancelReportAnalysis(id);
        showToast(result.message || "已取消分析");
        await loadHistoryList();
      } else if (act === "retry") {
        const code = btn.dataset.code || "";
        if (!code) throw new Error("缺少股票代码");
        if (!confirm(`确定在原任务 #${id} 上重跑 ${code}？不会新建记录。`)) return;
        showToast("正在原任务上重跑…");
        const rid = String(id);
        backgroundWatchIds.add(rid);
        // 立刻禁用按钮，避免连点；完整状态以 onStarted 后的列表为准
        btn.disabled = true;
        btn.textContent = "启动中…";
        startAnalyzeInBackground(code, id, {
          onStarted: () => {
            loadHistoryList({ silent: true }).catch(() => {});
          },
        })
          .then(() => {
            backgroundWatchIds.delete(rid);
            return loadHistoryList({ silent: true });
          })
          .catch((err) => {
            backgroundWatchIds.delete(rid);
            showToast(err.message || String(err), { type: "error" });
            return loadHistoryList({ silent: true });
          });
      } else if (act === "view") {
        await openReportModal(id, { tab: "report" });
      } else if (act === "chat") {
        await openReportModal(id, { tab: "chat" });
      } else if (act === "download") {
        const name = await downloadById(id);
        showToast(`已下载 ${name}`);
      } else if (act === "delete") {
        const cached = historyItemsCache.find((x) => String(x.id) === String(id));
        const label = cached
          ? `#${id} ${cached.name || ""}（${cached.code || ""}）`.trim()
          : `#${id}`;
        if (!confirm(`确定删除这条深研记录及其追问？\n${label}`)) return;

        const rid = String(id);
        deletingIds.add(rid);
        markRowDeleting(rid, true);
        if (progressReportId != null && String(progressReportId) === rid) {
          closeProgressModal();
        }
        showToast(`正在删除 ${label}…`, { duration: 120000 });

        try {
          const res = await apiFetch(`/api/reports/${encodeURIComponent(id)}`, {
            method: "DELETE",
          });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || "删除失败");
          }
          selectedCompareIds.delete(rid);
          backgroundWatchIds.delete(rid);
          showToast(`已删除 ${label}`);
          await loadHistoryList();
        } catch (delErr) {
          showToast(delErr.message || String(delErr), { type: "error" });
          await loadHistoryList({ silent: true }).catch(() => {});
        } finally {
          deletingIds.delete(rid);
        }
      }
    } catch (err) {
      showToast(err.message || String(err), { type: "error" });
    }
  });

  loadHistoryList();
  // 有进行中的分析，或刚点了重跑但列表尚未刷成 running 时，轻量静默刷新（就地 patch，不闪屏）
  const autoRefresh = setInterval(() => {
    if (deletingIds.size) return; // 删除进行中不轮询，避免打断 UI
    const hasRunning = Boolean($("historyList")?.querySelector(".hist-status.is-running"));
    if (hasRunning || backgroundWatchIds.size) {
      loadHistoryList({ silent: true }).catch(() => {});
    }
  }, 2500);
  historyUnsubscribers.push(() => clearInterval(autoRefresh));

  return () => {
    historyUnsubscribers.forEach((fn) => fn());
    historyUnsubscribers = [];
    closeProgressModal();
    closeReportModal();
    const cm = $("compareModal");
    if (cm) cm.hidden = true;
  };
}
