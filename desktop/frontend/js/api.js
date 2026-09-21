/* api.js — 统一 HTTP 封装：经 pywebview bridge 代理（令牌不出 Python 侧）。
   401 → 触发令牌重录；5xx/断连 → 吐司 + 状态灯变灰。 */

const listeners = { status: [], unauthorized: [] };

function bridge() {
  const api = window.pywebview && window.pywebview.api;
  if (!api) throw new Error("BridgeNotReady");
  return api;
}

export function on(event, handler) {
  listeners[event] && listeners[event].push(handler);
}

function emit(event, payload) {
  (listeners[event] || []).forEach((fn) => fn(payload));
}

export async function ready(timeoutMs = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      bridge();
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 120));
    }
  }
  return false;
}

export async function request(method, path, body) {
  let result;
  try {
    result = await bridge().http_request(method, path, body == null ? null : JSON.stringify(body));
  } catch (err) {
    emit("status", { online: false, reason: String(err) });
    return { status: 0, body: { ok: false, error: "Offline", message: "无法连接塔菲（离线）" } };
  }
  const { status, body: payload } = result;
  if (status === 401) emit("unauthorized", { path });
  if (status >= 500 || status === 0) {
    emit("status", { online: false, reason: status });
  } else if (status < 400) {
    emit("status", { online: true });
  }
  return { status, body: payload };
}

export async function data(method, path, body) {
  const { status, body: payload } = await request(method, path, body);
  if (status >= 200 && status < 300 && payload && payload.ok) return payload.data;
  return null;
}

/* admin 端点返回裸 dict（无 ok 信封），仅判状态码。 */
export async function admin(method, path, body) {
  const { status, body: payload } = await request(method, path, body);
  if (status >= 200 && status < 300) return payload;
  return null;
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body);
export const put = (path, body) => request("PUT", path, body);
export const del = (path, body) => request("DELETE", path, body);
