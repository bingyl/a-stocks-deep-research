import {
  resetAgentPanel,
  runAgentAnalyze,
  downloadAgentReportHtml,
  openLastReportModal,
  bindLiveFollowup,
  cancelCurrentAnalyze,
  retryLastAnalyze,
} from "./agent.js?v=20260812session2";
import { mountResearchHistory, clearHistoryClientState } from "./history.js?v=20260812session";
import {
  bootShell,
  registerPageMounts,
  blankWorkspace,
  remountCurrentPage,
} from "./shell.js?v=20260812session";
import { $ } from "./utils.js?v=20260808am";
import { apiFetch } from "./api.js?v=20260812auth3";
import {
  ensureAuthenticated,
  onAuthSessionChange,
  getCurrentUser,
} from "./auth.js?v=20260812session";
import { renderQuote, renderFinance } from "./render.js?v=20260808am";
import {
  closeSuggest,
  scheduleSuggest,
  getSuggestItems,
  getActiveIndex,
  moveActive,
} from "./suggest.js?v=20260812session2";

const RECENT_KEY_BASE = "a_stock_recent_v1";
let selectedCode = null;
let pageUnsubscribers = [];
let sessionReady = false;

function recentStorageKey() {
  const u = getCurrentUser();
  return u?.id != null ? `${RECENT_KEY_BASE}_u${u.id}` : RECENT_KEY_BASE;
}

function on(el, event, handler) {
  if (!el) return;
  el.addEventListener(event, handler);
  pageUnsubscribers.push(() => el.removeEventListener(event, handler));
}

function loadRecent() {
  try {
    return JSON.parse(localStorage.getItem(recentStorageKey()) || "[]");
  } catch {
    return [];
  }
}

function saveRecent(code, name) {
  const list = loadRecent().filter((x) => x.code !== code);
  list.unshift({ code, name, ts: Date.now() });
  localStorage.setItem(recentStorageKey(), JSON.stringify(list.slice(0, 8)));
  renderRecent();
}

function renderRecent() {
  const box = $("recent");
  if (!box) return;
  const list = loadRecent();
  if (!list.length) {
    box.innerHTML = `<div class="empty-panel">查询过的股票会出现在这里</div>`;
    return;
  }
  box.innerHTML = list
    .map((x, i) => {
      const name = x.name || x.code;
      const label = x.name ? `${x.name} ${x.code}` : x.code;
      return `<button type="button" class="recent-tag c${i % 8}" data-code="${x.code}" title="${label}">
          <span class="name">${name}</span><span class="code">${x.code}</span>
        </button>`;
    })
    .join("");
  box.querySelectorAll(".recent-tag").forEach((el) => {
    el.addEventListener("click", () => {
      const q = $("q");
      if (q) q.value = `${el.dataset.code}`;
      selectedCode = el.dataset.code;
      closeSuggest();
      queryStock(el.dataset.code);
    });
  });
}

async function refreshUniverseBadge() {
  const badge = $("universeBadge");
  if (!badge) return;
  try {
    const res = await apiFetch("/api/search/universe/status");
    const data = await res.json();
    if (data.ready) {
      badge.className = "badge ready topbar-badge";
      badge.textContent = `股票池已就绪 · ${data.count} 只`;
    } else if (data.loading) {
      badge.className = "badge loading topbar-badge";
      badge.textContent = "股票池加载中…";
      setTimeout(refreshUniverseBadge, 1500);
    } else {
      badge.className = "badge loading topbar-badge";
      badge.textContent = "股票池未就绪";
      setTimeout(refreshUniverseBadge, 2000);
    }
  } catch {
    badge.className = "badge topbar-badge";
    badge.textContent = "股票池状态未知";
  }
}

function pickSuggest(idx) {
  const item = getSuggestItems()[idx];
  if (!item) return;
  selectedCode = item.code;
  const q = $("q");
  if (q) q.value = `${item.name} ${item.code}`;
  closeSuggest();
  queryStock(item.code);
}

window.__onPickSuggest = pickSuggest;

async function resolveCode(raw) {
  const text = (raw || "").trim();
  if (!text) throw new Error("请输入股票代码、名称或拼音");
  if (selectedCode) return selectedCode;

  const codeMatch = text.match(/(?:^|\s)([0-9]{6})(?:\s|$)/);
  if (codeMatch) return codeMatch[1];

  const res = await apiFetch(`/api/search/suggest?q=${encodeURIComponent(text)}&limit=1`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "联想失败");
  if (!data.items || !data.items.length) throw new Error(`未找到：${text}`);
  return data.items[0].code;
}

function setFinanceCardsVisible(visible) {
  const empty = $("financeEmpty");
  const quote = $("quoteCard");
  const finance = $("financeCard");
  if (empty) empty.hidden = visible;
  if (quote) quote.hidden = !visible;
  if (finance) finance.hidden = !visible;
  if (!visible) {
    if (quote) quote.innerHTML = "";
    if (finance) finance.innerHTML = "";
  }
}

function switchResultTab(tab) {
  const finance = tab === "finance";
  const tabFinance = $("tabFinance");
  const tabAnalyze = $("tabAnalyze");
  const panelFinance = $("panelFinance");
  const panelAnalyze = $("panelAnalyze");
  if (!tabFinance || !tabAnalyze || !panelFinance || !panelAnalyze) return;

  tabFinance.classList.toggle("is-active", finance);
  tabAnalyze.classList.toggle("is-active", !finance);
  tabFinance.setAttribute("aria-selected", finance ? "true" : "false");
  tabAnalyze.setAttribute("aria-selected", finance ? "false" : "true");
  panelFinance.hidden = !finance;
  panelAnalyze.hidden = finance;
}

async function queryStock(codeHint) {
  const btn = $("btn");
  const msg = $("msg");
  if (btn) btn.disabled = true;
  if (msg) {
    msg.className = "msg";
    msg.textContent = "查询中…";
  }
  try {
    const code = codeHint || (await resolveCode(($("q") || {}).value));
    const res = await apiFetch(`/api/stock/${encodeURIComponent(code)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "查询失败");
    renderQuote(data.quote);
    renderFinance(data.finance);
    setFinanceCardsVisible(true);
    resetAgentPanel();
    switchResultTab("finance");
    if (msg) msg.textContent = `已查询 ${data.name}（${data.code}）`;
    const q = $("q");
    if (q) q.value = `${data.name} ${data.code}`;
    selectedCode = data.code;
    saveRecent(data.code, data.name);
  } catch (err) {
    setFinanceCardsVisible(false);
    if (msg) {
      msg.className = "msg error";
      msg.textContent = err.message || String(err);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function mountAStockResearch() {
  pageUnsubscribers = [];
  selectedCode = null;

  const q = $("q");
  on(q, "input", () => {
    selectedCode = null;
    scheduleSuggest();
  });
  on(q, "focus", () => {
    if (!q) return;
    if (q.value.trim() && getSuggestItems().length) {
      $("suggest")?.classList.add("open");
    } else if (q.value.trim()) {
      scheduleSuggest();
    }
  });
  on(q, "keydown", (e) => {
    const suggest = $("suggest");
    const open = suggest?.classList.contains("open");
    const items = getSuggestItems();
    if (e.key === "ArrowDown" && open && items.length) {
      e.preventDefault();
      moveActive(1);
    } else if (e.key === "ArrowUp" && open && items.length) {
      e.preventDefault();
      moveActive(-1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (open && getActiveIndex() >= 0 && items[getActiveIndex()]) {
        pickSuggest(getActiveIndex());
      } else {
        closeSuggest();
        queryStock();
      }
    } else if (e.key === "Escape") {
      closeSuggest();
    }
  });

  const onDocClick = (e) => {
    if (!e.target.closest(".search-wrap")) closeSuggest();
  };
  document.addEventListener("click", onDocClick);
  pageUnsubscribers.push(() => document.removeEventListener("click", onDocClick));

  on($("btn"), "click", () => {
    closeSuggest();
    queryStock();
  });
  on($("tabFinance"), "click", () => switchResultTab("finance"));
  on($("tabAnalyze"), "click", () => switchResultTab("analyze"));
  on($("agentBtn"), "click", () => {
    switchResultTab("analyze");
    runAgentAnalyze(selectedCode);
  });
  on($("agentCancelBtn"), "click", () => cancelCurrentAnalyze());
  on($("agentRetryBtn"), "click", () => {
    switchResultTab("analyze");
    retryLastAnalyze();
  });
  on($("agentHtmlBtn"), "click", () => downloadAgentReportHtml());
  on($("agentViewBtn"), "click", () => openLastReportModal());
  bindLiveFollowup({ force: true });
  on($("examples"), "click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip || !q) return;
    q.value = chip.dataset.q;
    selectedCode = null;
    scheduleSuggest();
    q.focus();
  });

  renderRecent();

  return () => {
    pageUnsubscribers.forEach((fn) => fn());
    pageUnsubscribers = [];
    closeSuggest();
  };
}

registerPageMounts({
  "a-stock-research": mountAStockResearch,
  "research-history": mountResearchHistory,
});

onAuthSessionChange(({ user, reason }) => {
  selectedCode = null;
  try {
    clearHistoryClientState();
  } catch {
    /* ignore */
  }
  try {
    closeSuggest();
  } catch {
    /* ignore */
  }
  try {
    resetAgentPanel({ abort: true });
  } catch {
    /* ignore */
  }

  if (!user) {
    blankWorkspace(reason === "unauthorized" ? "登录已失效，请重新登录…" : "请登录后继续…");
    return;
  }

  // 首次启动由 ensureAuthenticated → bootShell 负责；换号登录才 remount
  if (sessionReady) {
    remountCurrentPage();
    refreshUniverseBadge();
  }
});

ensureAuthenticated()
  .then(() => {
    sessionReady = true;
    bootShell();
    refreshUniverseBadge();
  })
  .catch((err) => {
    console.error(err);
    const host = document.getElementById("viewHost");
    if (host) {
      host.innerHTML = `<div class="page-loading">认证初始化失败：${String(
        err?.message || err
      )}</div>`;
    }
  });
