/** JWT 登录门禁：AUTH_ENABLED 时拦截工作台，直到登录成功 */

const TOKEN_KEY = "a_stock_jwt_v1";
const USER_KEY = "a_stock_user_v1";

let authEnabled = false;
let allowRegister = false;
let currentUser = null;
let unauthorizedHandler = null;
let usernameRule = "2–32 位，字母/数字/下划线/中文";
let passwordRule = "至少 6 位，最长 72 字节";
const sessionListeners = new Set();

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    /* ignore */
  }
  currentUser = null;
}

/** @param {(payload: {user: object|null, reason: string}) => void} fn */
export function onAuthSessionChange(fn) {
  if (typeof fn !== "function") return () => {};
  sessionListeners.add(fn);
  return () => sessionListeners.delete(fn);
}

function emitSessionChange(reason) {
  for (const fn of sessionListeners) {
    try {
      fn({ user: currentUser, reason });
    } catch {
      /* ignore listener errors */
    }
  }
}

export function getCurrentUser() {
  return currentUser;
}

export function isAuthEnabled() {
  return authEnabled;
}

export function notifyUnauthorized() {
  if (typeof unauthorizedHandler === "function") {
    unauthorizedHandler();
  }
}

function saveSession(token, user, { emit = true, reason = "login" } = {}) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user || {}));
  currentUser = user || null;
  if (emit) emitSessionChange(reason);
}

function loadCachedUser() {
  try {
    currentUser = JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch {
    currentUser = null;
  }
}

function ensureOverlay() {
  let el = document.getElementById("authOverlay");
  if (el?.dataset?.ui === "eye1") return el;
  if (el) el.remove();
  el = document.createElement("div");
  el.id = "authOverlay";
  el.className = "auth-overlay";
  el.dataset.ui = "eye1";
  el.hidden = true;
  el.innerHTML = `
    <div class="auth-card" role="dialog" aria-modal="true" aria-labelledby="authTitle">
      <h2 id="authTitle">登录工作台</h2>
      <label class="auth-field">
        <span>用户名</span>
        <input id="authUsername" type="text" autocomplete="username" maxlength="32" />
        <span class="auth-hint" id="authUsernameHint" hidden></span>
      </label>
      <label class="auth-field">
        <span>密码</span>
        <div class="auth-password-wrap">
          <input id="authPassword" type="password" autocomplete="current-password" maxlength="128" />
          <button type="button" class="auth-eye-btn" id="authTogglePwd" aria-label="显示密码" title="显示密码" aria-pressed="false">
            <svg class="auth-eye-icon" id="authEyeIcon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>
              <circle cx="12" cy="12" r="3"/>
            </svg>
          </button>
        </div>
        <span class="auth-hint" id="authPasswordHint" hidden></span>
      </label>
      <p class="auth-error" id="authError" hidden></p>
      <div class="auth-actions">
        <button type="button" class="btn primary" id="authLoginBtn">登录</button>
        <button type="button" class="btn" id="authRegisterBtn" hidden>注册并登录</button>
      </div>
    </div>
  `;
  document.body.appendChild(el);
  el.addEventListener("click", (e) => {
    if (e.target.closest("#authTogglePwd")) {
      e.preventDefault();
      togglePasswordVisible();
      return;
    }
    if (e.target.closest("#authLoginBtn")) submitLogin();
    else if (e.target.closest("#authRegisterBtn")) submitRegister();
  });
  el.querySelector("#authPassword")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitLogin();
  });
  return el;
}

function setAuthError(msg) {
  const box = document.getElementById("authError");
  if (!box) return;
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = String(msg);
}

function updateRuleHints({ forRegister = false } = {}) {
  const uh = document.getElementById("authUsernameHint");
  const ph = document.getElementById("authPasswordHint");
  if (uh) {
    uh.textContent = usernameRule;
    uh.hidden = !forRegister;
  }
  if (ph) {
    ph.textContent = passwordRule;
    ph.hidden = !forRegister;
  }
}

function showLoginOverlay() {
  const el = ensureOverlay();
  el.hidden = false;
  const regBtn = document.getElementById("authRegisterBtn");
  if (regBtn) regBtn.hidden = !allowRegister;
  updateRuleHints({ forRegister: false });
  setAuthError("");
  document.getElementById("authUsername")?.focus();
}

function hideLoginOverlay() {
  const el = document.getElementById("authOverlay");
  if (el) el.hidden = true;
}

const EYE_OPEN = `
  <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>
  <circle cx="12" cy="12" r="3"/>
`;
const EYE_OFF = `
  <path d="M3 3l18 18"/>
  <path d="M10.6 10.6a3 3 0 0 0 4.2 4.2"/>
  <path d="M9.9 5.1A10.5 10.5 0 0 1 12 5c6.5 0 10 7 10 7a17.4 17.4 0 0 1-3.2 3.9"/>
  <path d="M6.1 6.1A17.5 17.5 0 0 0 2 12s3.5 7 10 7a10.4 10.4 0 0 0 4.3-.9"/>
`;

function setEyeIcon(visible) {
  const icon = document.getElementById("authEyeIcon");
  if (!icon) return;
  icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-width", "1.8");
  icon.setAttribute("stroke-linecap", "round");
  icon.setAttribute("stroke-linejoin", "round");
  icon.innerHTML = visible ? EYE_OFF : EYE_OPEN;
}

function togglePasswordVisible() {
  const input = document.getElementById("authPassword");
  const btn = document.getElementById("authTogglePwd");
  if (!input || !btn) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  btn.setAttribute("title", show ? "隐藏密码" : "显示密码");
  btn.setAttribute("aria-pressed", show ? "true" : "false");
  setEyeIcon(show);
}

function clientValidate(username, password, { forRegister = false } = {}) {
  if (!username) return "请输入用户名";
  if (forRegister || username.length >= 2) {
    if (!/^[a-zA-Z0-9_\u4e00-\u9fff]{2,32}$/.test(username)) {
      return `用户名不符合规则：${usernameRule}`;
    }
  }
  if (!password) return "请输入密码";
  if (forRegister && password.length < 6) {
    return `密码不符合规则：${passwordRule}`;
  }
  if (new TextEncoder().encode(password).length > 72) {
    return "密码过长（超过 72 字节）";
  }
  return "";
}

async function postAuth(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : "请求失败");
  }
  return data;
}

async function submitLogin() {
  const username = document.getElementById("authUsername")?.value?.trim() || "";
  const password = document.getElementById("authPassword")?.value || "";
  updateRuleHints({ forRegister: false });
  setAuthError("");
  const localErr = clientValidate(username, password, { forRegister: false });
  if (localErr) {
    setAuthError(localErr);
    return false;
  }
  try {
    const data = await postAuth("/api/auth/login", { username, password });
    saveSession(data.access_token, data.user, { reason: "login" });
    hideLoginOverlay();
    renderAuthChrome();
    return true;
  } catch (err) {
    setAuthError(err.message || String(err));
    return false;
  }
}

async function submitRegister() {
  const username = document.getElementById("authUsername")?.value?.trim() || "";
  const password = document.getElementById("authPassword")?.value || "";
  updateRuleHints({ forRegister: true });
  setAuthError("");
  const localErr = clientValidate(username, password, { forRegister: true });
  if (localErr) {
    setAuthError(localErr);
    return false;
  }
  try {
    const data = await postAuth("/api/auth/register", { username, password });
    saveSession(data.access_token, data.user, { reason: "register" });
    hideLoginOverlay();
    renderAuthChrome();
    return true;
  } catch (err) {
    setAuthError(err.message || String(err));
    return false;
  }
}

export function logout() {
  clearToken();
  emitSessionChange("logout");
  if (authEnabled) {
    showLoginOverlay();
    renderAuthChrome();
  }
}

export function renderAuthChrome() {
  const box = document.getElementById("authUserBox");
  if (!box) return;
  if (!authEnabled) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const name = currentUser?.username || "未登录";
  box.hidden = false;
  box.innerHTML = `
    <div class="auth-user-row">
      <span class="auth-user-avatar" aria-hidden="true">${escapeInitial(name)}</span>
      <div class="auth-user-meta">
        <div class="auth-user-name" title="${name}">${name}</div>
        <button type="button" class="auth-logout" id="authLogoutBtn">退出</button>
      </div>
    </div>
  `;
  document.getElementById("authLogoutBtn")?.addEventListener("click", () => logout());
}

function escapeInitial(name) {
  const s = String(name || "?").trim();
  return (s[0] || "?").toUpperCase();
}

async function applyUiChrome() {
  try {
    const res = await fetch("/api/ui-config");
    const cfg = await res.json().catch(() => ({}));
    const docs = document.getElementById("apiDocsLink");
    if (docs) docs.hidden = !Boolean(cfg.show_api_docs);
  } catch {
    const docs = document.getElementById("apiDocsLink");
    if (docs) docs.hidden = true;
  }
}

/**
 * 启动前调用：探测认证开关；若开启则校验 token，否则弹出登录。
 * @returns {Promise<{enabled: boolean, user: object|null}>}
 */
export async function ensureAuthenticated() {
  await applyUiChrome();
  const cfgRes = await fetch("/api/auth/config");
  const cfg = await cfgRes.json().catch(() => ({ enabled: false }));
  authEnabled = Boolean(cfg.enabled);
  allowRegister = Boolean(cfg.allow_register);
  if (cfg.username_rule) usernameRule = String(cfg.username_rule);
  if (cfg.password_rule) passwordRule = String(cfg.password_rule);
  unauthorizedHandler = () => {
    if (!authEnabled) return;
    clearToken();
    emitSessionChange("unauthorized");
    showLoginOverlay();
    renderAuthChrome();
  };

  ensureOverlay();

  if (!authEnabled) {
    hideLoginOverlay();
    renderAuthChrome();
    return { enabled: false, user: null };
  }

  loadCachedUser();
  const token = getToken();
  if (token) {
    const meRes = await fetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (meRes.ok) {
      const me = await meRes.json();
      // 启动恢复会话：不触发 remount（随后 bootShell 会挂载）
      saveSession(token, me, { emit: false, reason: "restore" });
      hideLoginOverlay();
      renderAuthChrome();
      return { enabled: true, user: me };
    }
    clearToken();
  }

  showLoginOverlay();
  renderAuthChrome();
  await new Promise((resolve) => {
    const timer = setInterval(() => {
      if (getToken()) {
        clearInterval(timer);
        resolve();
      }
    }, 200);
  });
  return { enabled: true, user: currentUser };
}
