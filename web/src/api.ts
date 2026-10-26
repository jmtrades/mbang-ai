// Tenant-scoped API client. The tenant id comes from the path: /t/<tid>/#/page
export const TID = (() => {
  const m = location.pathname.match(/^\/t\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : "owner";
})();
export const BASE = `/t/${TID}/api`;

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : status === 0 ? "Can't reach the server" : `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

type Listener = (online: boolean) => void;
const listeners = new Set<Listener>();
let online = true;
export function onConnection(fn: Listener) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
function setOnline(v: boolean) {
  if (v !== online) {
    online = v;
    listeners.forEach((l) => l(v));
    if (!v) watchReconnect();
  }
}
let watching = false;
function watchReconnect() {
  if (watching) return;
  watching = true;
  const tick = async () => {
    try {
      const r = await fetch("/health", { cache: "no-store" });
      if (r.ok) {
        watching = false;
        setOnline(true);
        return;
      }
    } catch {
      /* still down */
    }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1000);
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; form?: FormData } = {}): Promise<T> {
  const init: RequestInit = { method: opts.method || (opts.body || opts.form ? "POST" : "GET"), credentials: "same-origin" };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers = { "content-type": "application/json" };
  }
  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch {
    setOnline(false);
    throw new ApiError(0, "Can't reach the server");
  }
  setOnline(true);
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    if (res.status === 401 && !path.startsWith("/auth") && path !== "/me") {
      window.dispatchEvent(new CustomEvent("op:signed-out"));
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d: any) => d.msg).join("; ") : detail);
  }
  return data as T;
}

export const get = <T = any>(p: string) => api<T>(p);
export const post = <T = any>(p: string, body?: unknown) => api<T>(p, { method: "POST", body: body ?? {} });
export const put = <T = any>(p: string, body?: unknown) => api<T>(p, { method: "PUT", body: body ?? {} });
export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });
