import { $, escapeHtml } from "./utils.js?v=20260808am";
import { apiFetch } from "./api.js?v=20260812auth";

let suggestItems = [];
let activeIndex = -1;
let suggestTimer = null;
let lastSuggestQuery = "";

export function getSuggestItems() {
  return suggestItems;
}

export function getActiveIndex() {
  return activeIndex;
}

export function closeSuggest() {
  const box = $("suggest");
  if (box) {
    box.classList.remove("open");
    box.innerHTML = "";
  }
  suggestItems = [];
  activeIndex = -1;
}

function openSuggest(html) {
  const box = $("suggest");
  box.innerHTML = html;
  box.classList.add("open");
}

function renderSuggest(items, total, query) {
  if (!items.length) {
    openSuggest(`<div class="suggest-empty">未找到与「${escapeHtml(query)}」相关的股票</div>`);
    suggestItems = [];
    activeIndex = -1;
    return;
  }
  const meta = `匹配 ${total} 只，显示前 ${items.length} 只`;
  const rows = items
    .map(
      (it, idx) => `
      <button type="button" class="suggest-item${idx === 0 ? " active" : ""}" data-idx="${idx}" role="option">
        <span class="code">${it.code}</span>
        <span class="name">${escapeHtml(it.name)}</span>
        <span class="tag">${it.market || "A"}${it.board ? " · " + it.board : ""}${
        it.industry
          ? " · " + escapeHtml(it.industry)
          : it.initials
            ? " · " + it.initials.toUpperCase()
            : ""
      }</span>
      </button>`
    )
    .join("");
  openSuggest(`<div class="suggest-meta">${meta}</div>${rows}`);
  suggestItems = items;
  activeIndex = 0;
  $("suggest").querySelectorAll(".suggest-item").forEach((el) => {
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const handler = window.__onPickSuggest;
      if (handler) handler(Number(el.dataset.idx));
    });
  });
}

export function highlightActive() {
  $("suggest").querySelectorAll(".suggest-item").forEach((el, idx) => {
    el.classList.toggle("active", idx === activeIndex);
  });
  const active = $("suggest").querySelector(".suggest-item.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

export function moveActive(delta) {
  if (!suggestItems.length) return;
  activeIndex = (activeIndex + delta + suggestItems.length) % suggestItems.length;
  highlightActive();
}

async function fetchSuggest(query, attempt = 1) {
  lastSuggestQuery = query;
  let res;
  try {
    res = await apiFetch(`/api/search/suggest?q=${encodeURIComponent(query)}&limit=200`);
  } catch {
    if (attempt < 3) {
      await new Promise((r) => setTimeout(r, 280 * attempt));
      if (query !== lastSuggestQuery) return;
      return fetchSuggest(query, attempt + 1);
    }
    throw new Error("服务连接失败，请确认后端已启动后重试");
  }
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error("联想接口返回异常");
  }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = Array.isArray(detail)
      ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ")
      : detail || "联想失败";
    throw new Error(msg);
  }
  if (query !== lastSuggestQuery) return;
  renderSuggest(data.items || [], data.total || 0, query);
  if (data.universe_ready) {
    const badge = $("universeBadge");
    if (badge) {
      badge.className = "badge ready topbar-badge";
      badge.textContent = `股票池已就绪 · ${data.universe_count} 只`;
    }
  }
}

export function scheduleSuggest() {
  const query = $("q").value.trim();
  clearTimeout(suggestTimer);
  if (!query) {
    closeSuggest();
    return;
  }
  suggestTimer = setTimeout(async () => {
    try {
      await fetchSuggest(query);
    } catch (err) {
      openSuggest(`<div class="suggest-empty">${escapeHtml(err.message || String(err))}</div>`);
    }
  }, 180);
}
