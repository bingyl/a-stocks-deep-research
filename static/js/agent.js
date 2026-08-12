import { $, escapeHtml, showToast, simpleMarkdown } from "./utils.js?v=20260808am";
import { downloadReportAsHtml } from "./report-export.js?v=20260808am";
import { openReportModal } from "./history.js?v=20260812auth";
import { bindFollowupPanel, renderChatBubbles } from "./chat.js?v=20260808af";
import { apiFetch } from "./api.js?v=20260812auth";
import {
  cancelReportAnalysis,
  conflictMessage,
  parseSseChunk,
} from "./analyze-api.js?v=20260812auth";

export { cancelReportAnalysis, startAnalyzeInBackground } from "./analyze-api.js?v=20260812auth";

let progressTimer = null;
let progressStartedAt = 0;
let currentStageIndex = 0;
let currentStageTotal = 6;
let analysisRunning = false;
let lastReport = null;
let liveFollowup = null;
let analyzeAbort = null;
let activeAnalyzeCode = null;
/** 每次开始分析递增；旧流 abort 的 catch 不得再改当前 UI */
let analyzeSession = 0;

function setAnalyzeControls({ running = false, canRetry = false } = {}) {
  const startBtn = $("agentBtn");
  const cancelBtn = $("agentCancelBtn");
  const retryBtn = $("agentRetryBtn");
  if (startBtn) startBtn.disabled = running;
  if (cancelBtn) {
    cancelBtn.hidden = !running;
    cancelBtn.disabled = !running;
  }
  if (retryBtn) {
    retryBtn.hidden = running || !canRetry;
    retryBtn.disabled = running || !canRetry;
  }
}

function setFollowupVisible(visible) {
  const card = $("followupCard");
  if (!card) return;
  card.hidden = !visible;
  if (!visible) {
    renderChatBubbles($("followupChatList"), []);
    const input = $("followupInput");
    if (input) input.value = "";
    const st = $("followupStatus");
    if (st) st.textContent = "";
  }
}

export function bindLiveFollowup({ force = false } = {}) {
  const listEl = $("followupChatList");
  if (!listEl) return null;
  if (!force && liveFollowup && liveFollowup._el === listEl) return liveFollowup;
  liveFollowup = bindFollowupPanel({
    listEl,
    inputEl: $("followupInput"),
    sendBtn: $("followupSend"),
    statusEl: $("followupStatus"),
    getReportId: () => lastReport?.id || null,
  });
  liveFollowup._el = listEl;
  return liveFollowup;
}

function setDownloadButtonsEnabled(enabled) {
  const htmlBtn = $("agentHtmlBtn");
  if (htmlBtn) htmlBtn.disabled = !enabled;
  const viewBtn = $("agentViewBtn");
  if (viewBtn) viewBtn.disabled = !enabled || !lastReport?.id;
}

function ensureProgressUI() {
  let wrap = $("agentProgressWrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "agentProgressWrap";
    wrap.className = "agent-progress-wrap";
    wrap.hidden = true;
    wrap.innerHTML = `
      <div class="agent-progress-head">
        <div class="agent-progress-top">
          <span class="agent-spinner" aria-hidden="true"></span>
          <span class="agent-progress-label" id="agentProgressLabel">准备中…</span>
          <span class="agent-progress-elapsed" id="agentProgressElapsed">已用 0s</span>
        </div>
        <div class="agent-progress-bar" aria-hidden="true">
          <div class="agent-progress-bar-fill" id="agentProgressFill"></div>
        </div>
        <div class="agent-progress-meta">
          <span id="agentProgressPct">0%</span>
          <span class="agent-progress-hint" id="agentProgressHint">分析可能需要几分钟，请稍候</span>
        </div>
      </div>
      <div id="agentProgress" class="agent-progress"></div>
    `;
    const msg = $("agentMsg");
    msg.insertAdjacentElement("afterend", wrap);
  }
  return wrap;
}

function ensureProgressBox() {
  ensureProgressUI();
  return $("agentProgress");
}

function formatElapsed(ms) {
  const sec = Math.max(0, Math.floor(ms / 1000));
  if (sec < 60) return `已用 ${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `已用 ${m}分${String(s).padStart(2, "0")}秒`;
}

function setProgressBar({ index = 0, total = 6, label = "", running = true } = {}) {
  ensureProgressUI();
  const wrap = $("agentProgressWrap");
  wrap.hidden = false;

  currentStageIndex = Math.max(0, Number(index) || 0);
  currentStageTotal = Math.max(1, Number(total) || 6);

  // 未跑完时即使 running=false（取消/中断）也按已完成阶段算比例，禁止误显示 100%
  const finishedAll = !running && currentStageIndex >= currentStageTotal;
  let pct;
  if (finishedAll) {
    pct = 100;
  } else if (running) {
    const doneSteps = Math.max(
      0,
      currentStageIndex - (currentStageIndex > 0 ? 0.35 : 0)
    );
    pct = Math.min(99, Math.round((doneSteps / currentStageTotal) * 100));
  } else {
    pct = Math.min(
      99,
      Math.round((currentStageIndex / currentStageTotal) * 100)
    );
  }

  wrap.classList.toggle("is-running", running);
  wrap.classList.toggle("is-done", finishedAll);

  const fill = $("agentProgressFill");
  const pctEl = $("agentProgressPct");
  const labelEl = $("agentProgressLabel");
  if (fill) {
    fill.style.width = `${pct}%`;
    fill.classList.toggle("indeterminate", running);
  }
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (labelEl) {
    labelEl.textContent = label || (running ? "分析进行中…" : "分析完成");
  }
}

function startProgressClock() {
  stopProgressClock();
  progressStartedAt = Date.now();
  analysisRunning = true;
  const tick = () => {
    const el = $("agentProgressElapsed");
    if (el) el.textContent = formatElapsed(Date.now() - progressStartedAt);
  };
  tick();
  progressTimer = window.setInterval(tick, 1000);
}

function stopProgressClock({ success = false } = {}) {
  analysisRunning = false;
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  const wrap = $("agentProgressWrap");
  if (wrap) {
    wrap.classList.remove("is-running");
    wrap.classList.toggle("is-done", success);
    wrap.classList.toggle("is-error", !success && wrap.dataset.hadError === "1");
  }
  const fill = $("agentProgressFill");
  if (fill) fill.classList.remove("indeterminate");
}

function pushProgress(text, kind = "status") {
  const box = ensureProgressBox();
  const wrap = $("agentProgressWrap");
  wrap.hidden = false;
  const row = document.createElement("div");
  row.className = `agent-progress-item ${kind}`;
  row.textContent = text;
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
}

function upsertStageRow(data) {
  const box = ensureProgressBox();
  const wrap = $("agentProgressWrap");
  wrap.hidden = false;

  const stageId = data.stage || data.title || "stage";
  let row = [...box.querySelectorAll("[data-stage]")].find(
    (el) => el.dataset.stage === stageId
  );
  if (!row) {
    row = document.createElement("div");
    row.className = "agent-progress-item stage";
    row.dataset.stage = stageId;
    box.appendChild(row);
  }

  const done = data.status === "end";
  row.classList.toggle("stage-done", done);
  row.classList.toggle("stage-active", !done);

  const idx = Number(data.index) || 0;
  const total = Number(data.total) || 5;
  const title = data.title || data.message || "阶段进行中";

  if (done) {
    row.innerHTML = `<span class="stage-mark">✓</span> <span>${escapeHtml(
      data.message || `阶段 ${idx}/${total} 完成 · ${title}`
    )}</span>`;
    setProgressBar({
      index: idx,
      total,
      label: `已完成 ${idx}/${total} · ${title}`,
      running: analysisRunning,
    });
  } else {
    row.innerHTML = `<span class="stage-mark stage-loading"></span> <span>${escapeHtml(
      data.message || `阶段 ${idx}/${total} · ${title}`
    )}</span> <span class="stage-loading-text">加载中…</span>`;
    setProgressBar({
      index: Math.max(idx, 0.5),
      total,
      label: `进行中 ${idx}/${total} · ${title}`,
      running: true,
    });
  }

  box.scrollTop = box.scrollHeight;
}

export function downloadAgentReportHtml() {
  if (!lastReport || !lastReport.html) {
    $("agentMsg").className = "msg error";
    $("agentMsg").textContent = "请先完成分析再下载报告";
    return;
  }
  try {
    const filename = downloadReportAsHtml({
      name: lastReport.name,
      code: lastReport.code,
      createdAt: lastReport.createdAt,
      analysisHtml: lastReport.html,
    });
    if ($("agentMsg")) {
      $("agentMsg").className = "msg";
      $("agentMsg").textContent = `HTML 已下载：${filename}`;
    }
  } catch (err) {
    if ($("agentMsg")) {
      $("agentMsg").className = "msg error";
      $("agentMsg").textContent = err.message || String(err);
    }
  }
}

export function openLastReportModal() {
  if (lastReport?.id) {
    openReportModal(lastReport.id);
    return;
  }
  $("agentMsg").className = "msg error";
  $("agentMsg").textContent = "当前没有可预览的已保存报告";
}

export function resetAgentPanel({ abort = false } = {}) {
  if (abort) {
    analyzeSession += 1;
    try {
      analyzeAbort?.abort();
    } catch {
      /* ignore */
    }
    analyzeAbort = null;
  }
  stopProgressClock();
  lastReport = null;
  activeAnalyzeCode = null;
  setDownloadButtonsEnabled(false);
  setFollowupVisible(false);
  setAnalyzeControls({ running: false, canRetry: false });
  // 换页/退出时 agent DOM 可能不存在，必须空安全，否则会阻断会话切换 remount
  const msg = $("agentMsg");
  if (msg) {
    msg.className = "msg";
    msg.textContent =
      "点击「开始分析」：六维分析（财务→股性估值→板块联动→技术面→情报→报告）";
  }
  const out = $("agentOut");
  if (out) {
    out.hidden = true;
    out.innerHTML = "";
  }
  const tools = $("agentTools");
  if (tools) {
    tools.hidden = true;
    tools.innerHTML = "";
  }
  const wrap = $("agentProgressWrap");
  if (wrap) {
    wrap.hidden = true;
    wrap.dataset.hadError = "";
    wrap.classList.remove("is-running", "is-done", "is-error");
  }
  const box = $("agentProgress");
  if (box) box.innerHTML = "";
}

export async function cancelCurrentAnalyze() {
  const rid = lastReport?.id;
  if (!rid) {
    showToast("当前没有可取消的分析任务", { type: "error" });
    return;
  }
  try {
    await cancelReportAnalysis(rid);
    analyzeAbort?.abort();
    showToast("已请求取消分析");
  } catch (err) {
    showToast(err.message || String(err), { type: "error" });
  }
}

export async function runAgentAnalyze(selectedCode, { reportId = null } = {}) {
  if (!selectedCode) {
    $("agentMsg").className = "msg error";
    $("agentMsg").textContent = "请先查询一只股票";
    return;
  }
  const session = ++analyzeSession;
  // 只断开上一路 SSE 预览（避免进度 UI 串台）。分析任务在后端独立 Task 中继续，
  // 不会因 abort / 换股票而被取消；显式取消请点「取消」按钮。
  if (analyzeAbort) {
    try {
      analyzeAbort.abort();
    } catch {
      /* ignore */
    }
  }
  analyzeAbort = new AbortController();
  activeAnalyzeCode = selectedCode;
  const reuseId = reportId || null;
  setAnalyzeControls({ running: true, canRetry: false });
  setDownloadButtonsEnabled(false);
  setFollowupVisible(false);
  lastReport = reuseId
    ? { id: reuseId, code: selectedCode, name: "", html: "", createdAt: "", analysis: "", status: "running" }
    : null;
  $("agentMsg").className = "msg";
  $("agentMsg").textContent = reuseId
    ? `正在原任务 #${reuseId} 上重新分析…`
    : "分析进行中（六维分析进度）…";
  $("agentOut").hidden = true;
  $("agentTools").hidden = true;

  const wrap = ensureProgressUI();
  wrap.hidden = false;
  wrap.dataset.hadError = "";
  wrap.classList.remove("is-done", "is-error");
  $("agentProgress").innerHTML = "";
  startProgressClock();
  setProgressBar({ index: 0, total: 6, label: "已连接，等待六维分析启动…", running: true });
  pushProgress("已连接分析通道，等待主编排调度 research_pipeline…", "status");

  let cancelled = false;
  try {
    const res = await apiFetch("/api/agent/analyze/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        code: selectedCode,
        question: null,
        ...(reuseId ? { report_id: Number(reuseId) } : {}),
      }),
      signal: analyzeAbort.signal,
    });
    if (!res.ok) {
      let detail = "分析失败";
      try {
        const err = await res.json();
        detail = err.detail || detail;
      } catch {
        /* ignore */
      }
      if (res.status === 409) {
        const msg = conflictMessage(detail);
        window.alert(msg);
        showToast(msg, { type: "error" });
        throw new Error(msg);
      }
      throw new Error(typeof detail === "string" ? detail : conflictMessage(detail));
    }
    if (!res.body) throw new Error("浏览器不支持流式响应");

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let finalData = null;

    while (true) {
      if (session !== analyzeSession) return;
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseChunk(buffer);
      buffer = parsed.rest;
      for (const item of parsed.events) {
        if (session !== analyzeSession) return;
        const data = item.data || {};
        if (data.report_id && !lastReport?.id) {
          lastReport = {
            id: data.report_id,
            code: data.code || selectedCode,
            name: data.name || "",
            html: "",
            createdAt: data.created_at || "",
            analysis: "",
            status: data.status || "running",
          };
          setDownloadButtonsEnabled(false);
        }
        if (item.event === "stage") {
          upsertStageRow(data);
          $("agentMsg").textContent = data.message || "阶段进行中…";
        } else if (item.event === "status") {
          pushProgress(data.message || "处理中…", "status");
          $("agentMsg").textContent = data.message || "分析中…";
          if (analysisRunning) {
            const label = $("agentProgressLabel");
            if (label && data.message) {
              const base = label.textContent.split(" · ")[0];
              label.textContent = `${base} · ${data.message}`;
            }
          }
        } else if (item.event === "tool_start") {
          pushProgress(`  ↳ ${data.message || data.tool || "调用工具"}`, "tool");
          const hint = $("agentProgressHint");
          if (hint) hint.textContent = `正在执行：${data.message || data.tool || "工具"}`;
        } else if (item.event === "tool_end") {
          /* 阶段行已表达进度，工具完成不刷屏 */
        } else if (item.event === "final") {
          finalData = data;
          pushProgress("报告已生成", "done");
        } else if (item.event === "cancelled") {
          cancelled = true;
          throw new Error(data.message || "分析已取消");
        } else if (item.event === "error") {
          throw new Error(data.message || "分析失败");
        }
      }
    }

    if (session !== analyzeSession) return;
    if (!finalData) throw new Error("未收到完整分析结果");
    setProgressBar({
      index: currentStageTotal,
      total: currentStageTotal,
      label: "分析完成，报告已生成",
      running: false,
    });
    stopProgressClock({ success: true });
    const hint = $("agentProgressHint");
    if (hint) hint.textContent = "全部阶段已完成";

    const html = simpleMarkdown(finalData.analysis || "");
    $("agentOut").innerHTML = html;
    $("agentOut").hidden = false;
    lastReport = {
      id: finalData.id || null,
      code: finalData.code || selectedCode,
      name: finalData.name || "",
      html,
      createdAt: finalData.created_at || "",
      analysis: finalData.analysis || "",
    };
    setDownloadButtonsEnabled(true);
    if (finalData.id) {
      setFollowupVisible(true);
      bindLiveFollowup();
      try {
        await liveFollowup?.reload();
      } catch {
        /* ignore */
      }
    } else {
      setFollowupVisible(false);
    }

    const tools = finalData.tool_calls || [];
    if (tools.length) {
      const uniq = [...new Set(tools.map((t) => t.tool))];
      $("agentTools").innerHTML =
        `框架：${escapeHtml(finalData.framework || "-")} · 模型：${escapeHtml(
          finalData.model || "-"
        )} · 工具调用 ${tools.length} 次（种类 ${uniq.length}）：` +
        tools
          .slice(0, 24)
          .map((t) => {
            const codeArg = t.arguments && t.arguments.code ? `(${t.arguments.code})` : "";
            const q =
              t.arguments && (t.arguments.query || t.arguments.topic)
                ? `(${String(t.arguments.query || t.arguments.topic).slice(0, 18)})`
                : "";
            return escapeHtml(`${t.tool}${codeArg || q}`);
          })
          .join(" → ") +
        (tools.length > 24 ? " …" : "");
      $("agentTools").hidden = false;
    }
    const savedHint = finalData.id
      ? `已入库 #${finalData.id}${finalData.created_at ? ` · ${finalData.created_at}` : ""}，可预览或下载`
      : "可下载 HTML（入库失败时仍可本地下载）";
    $("agentMsg").textContent = `已完成 ${finalData.name || ""}（${finalData.code}）六维分析，${savedHint}`;
    const viewBtn = $("agentViewBtn");
    if (viewBtn) viewBtn.disabled = !finalData.id;
    setAnalyzeControls({ running: false, canRetry: false });
  } catch (err) {
    // 被新一轮分析顶替而 abort：绝不能再改 UI（否则会出现 100%、已用 0s、「分析已取消」串台）
    if (session !== analyzeSession) return;
    const aborted = err?.name === "AbortError";
    const wrapEl = $("agentProgressWrap");
    if (wrapEl) wrapEl.dataset.hadError = cancelled || aborted ? "" : "1";
    stopProgressClock({ success: false });
    setProgressBar({
      index: currentStageIndex,
      total: currentStageTotal,
      label: cancelled || aborted ? "已取消" : "分析中断",
      running: false,
    });
    $("agentMsg").className = "msg error";
    const msg = cancelled || aborted ? "分析已取消" : err.message || String(err);
    $("agentMsg").textContent = msg;
    pushProgress(cancelled || aborted ? "分析已取消" : `错误：${msg}`, cancelled || aborted ? "status" : "error");
    setDownloadButtonsEnabled(false);
    setAnalyzeControls({ running: false, canRetry: true });
  } finally {
    if (session === analyzeSession) analyzeAbort = null;
  }
}

export function retryLastAnalyze() {
  if (!activeAnalyzeCode) {
    showToast("没有可重跑的股票", { type: "error" });
    return;
  }
  // 优先在原任务上重跑，避免历史列表堆出多条取消记录
  const rid = lastReport?.id || null;
  return runAgentAnalyze(activeAnalyzeCode, { reportId: rid });
}
