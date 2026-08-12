import { $, escapeHtml } from "./utils.js?v=20260808am";
import { NAV_ITEMS, DEFAULT_NAV_ID } from "./nav-config.js?v=20260808am";

const pageCache = new Map();
const pageCleanups = new Map();
let currentNavId = "";

function setSidebarOpen(open) {
  const shell = $("appShell");
  const backdrop = $("sidebarBackdrop");
  if (!shell) return;
  shell.classList.toggle("sidebar-open", open);
  if (backdrop) backdrop.hidden = !open;
}

function renderSidebarNav(activeId) {
  const nav = $("sidebarNav");
  if (!nav) return;

  const groups = new Map();
  for (const item of NAV_ITEMS) {
    const g = item.group || "应用";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(item);
  }

  let html = "";
  for (const [group, items] of groups) {
    html += `<div class="nav-group"><div class="nav-group-title">${escapeHtml(group)}</div>`;
    for (const item of items) {
      const active = item.id === activeId ? "is-active" : "";
      const aria = item.id === activeId ? ' aria-current="page"' : "";
      html += `<a class="nav-item ${active}" href="#${escapeHtml(item.id)}" data-nav="${escapeHtml(item.id)}"${aria}>
        <span class="nav-ico" aria-hidden="true">${escapeHtml(item.icon || "•")}</span>
        <span class="nav-label">${escapeHtml(item.label)}</span>
      </a>`;
    }
    html += `</div>`;
  }
  nav.innerHTML = html;

  nav.querySelectorAll(".nav-item[data-nav]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      navigateTo(el.dataset.nav);
    });
  });
}

async function fetchPageHtml(url) {
  if (pageCache.has(url)) return pageCache.get(url);
  const res = await fetch(`${url}?v=20260808am`);
  if (!res.ok) throw new Error(`页面加载失败：${url}`);
  const html = await res.text();
  pageCache.set(url, html);
  return html;
}

/**
 * 注册页面挂载逻辑：在 nav-config 对应页面加载后调用。
 * app.js 里为各 page id 提供 mountPageHandlers。
 */
let mountPageHandlers = {};

export function registerPageMounts(handlers) {
  mountPageHandlers = handlers || {};
}

export async function navigateTo(navId) {
  const item = NAV_ITEMS.find((x) => x.id === navId) || NAV_ITEMS.find((x) => x.id === DEFAULT_NAV_ID);
  if (!item) return;

  if (currentNavId && pageCleanups.has(currentNavId)) {
    try {
      pageCleanups.get(currentNavId)?.();
    } catch {
      /* ignore */
    }
    pageCleanups.delete(currentNavId);
  }

  const host = $("viewHost");
  const title = $("pageTitle");
  const sub = $("pageSub");
  if (title) title.textContent = item.title || item.label;
  if (sub) sub.textContent = item.sub || "";
  document.title = `工作台 · ${item.title || item.label}`;

  renderSidebarNav(item.id);
  setSidebarOpen(false);

  if (host) host.innerHTML = `<div class="page-loading">正在打开「${escapeHtml(item.label)}」…</div>`;

  try {
    const html = await fetchPageHtml(item.page);
    if (host) host.innerHTML = html;
    currentNavId = item.id;
    const mount = mountPageHandlers[item.id];
    if (typeof mount === "function") {
      const cleanup = mount();
      if (typeof cleanup === "function") pageCleanups.set(item.id, cleanup);
    }
    if (location.hash !== `#${item.id}`) {
      history.replaceState(null, "", `#${item.id}`);
    }
  } catch (err) {
    if (host) {
      host.innerHTML = `<div class="placeholder-card"><h2>加载失败</h2><p>${escapeHtml(err.message || String(err))}</p></div>`;
    }
  }
}

export function bindShellChrome() {
  const toggle = $("sidebarToggle");
  const backdrop = $("sidebarBackdrop");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const shell = $("appShell");
      setSidebarOpen(!shell?.classList.contains("sidebar-open"));
    });
  }
  if (backdrop) backdrop.addEventListener("click", () => setSidebarOpen(false));

  window.addEventListener("hashchange", () => {
    const id = (location.hash || "").replace("#", "") || DEFAULT_NAV_ID;
    if (id !== currentNavId) navigateTo(id);
  });
}

export function bootShell() {
  bindShellChrome();
  const hash = (location.hash || "").replace("#", "");
  const initial = NAV_ITEMS.some((x) => x.id === hash) ? hash : DEFAULT_NAV_ID;
  return navigateTo(initial);
}

/** 退出登录时清空工作区，避免残留上一账号内容 */
export function blankWorkspace(message = "请登录后继续…") {
  if (currentNavId && pageCleanups.has(currentNavId)) {
    try {
      pageCleanups.get(currentNavId)?.();
    } catch {
      /* ignore */
    }
    pageCleanups.delete(currentNavId);
  }
  const host = $("viewHost");
  if (host) {
    host.innerHTML = `<div class="page-loading">${escapeHtml(message)}</div>`;
  }
}

/** 换账号登录后强制重新挂载当前页，拉取新用户数据 */
export function remountCurrentPage() {
  const id = currentNavId || ((location.hash || "").replace("#", "") || DEFAULT_NAV_ID);
  if (currentNavId && pageCleanups.has(currentNavId)) {
    try {
      pageCleanups.get(currentNavId)?.();
    } catch {
      /* ignore */
    }
    pageCleanups.delete(currentNavId);
  }
  currentNavId = "";
  return navigateTo(id);
}

export function getCurrentNavId() {
  return currentNavId;
}
