/** 统一带鉴权的 fetch；401 时清 token 并跳转登录 */

import { clearToken, getToken, notifyUnauthorized } from "./auth.js?v=20260812auth";

export async function apiFetch(url, options = {}) {
  const opts = { ...options };
  const headers = new Headers(opts.headers || {});
  const token = getToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  opts.headers = headers;
  const res = await fetch(url, opts);
  if (res.status === 401) {
    clearToken();
    notifyUnauthorized();
  }
  return res;
}
