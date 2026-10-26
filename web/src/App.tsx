import React, { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { LogOut, Menu, WifiOff, X, Bot, Sparkles } from "lucide-react";
import { ApiError, TID, get, onConnection, post } from "./api";
import { AppCtx, Badge, Button, Citation, ErrorBox, Input, Loading, Me, Pill, SourceModal, TraceDrawer, cx } from "./components/ui";
import { GROUPS, PAGES } from "./pages/registry";

function useHash() {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const h = () => setHash(location.hash || "#/");
    window.addEventListener("hashchange", h);
    return () => window.removeEventListener("hashchange", h);
  }, []);
  const [path, qs] = hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  return { page: parts[0] || "home", rest: parts.slice(1), query: new URLSearchParams(qs || "") };
}

function applyBrand(brand: Me["brand"] | undefined) {
  const r = document.documentElement.style;
  if (brand?.primary) r.setProperty("--brand", brand.primary);
  if (brand?.accent) r.setProperty("--brand-2", brand.accent);
  document.title = brand?.name || "Operator AI";
}

function ReconnectBar() {
  const [offline, setOffline] = useState(false);
  useEffect(() => onConnection((on) => setOffline(!on)), []);
  if (!offline) return null;
  return (
    <div role="status" className="fixed inset-x-0 top-0 z-[60] flex items-center justify-center gap-2 bg-amber-500 px-3 py-1.5 text-sm font-medium text-white">
      <WifiOff className="size-4" /> Reconnecting to the server…
    </div>
  );
}

function SignIn({ onDone }: { onDone: () => void }) {
  const [pub, setPub] = useState<any>(null);
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    get("/public").then((d) => { setPub(d); applyBrand(d.brand); }).catch(() => setPub({}));
  }, []);
  const brand = pub?.brand?.name || "Operator AI";
  const isDemo = !!pub?.is_demo;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("login");
    setErr(null);
    try {
      await post("/auth/login", { username: u, password: p });
      onDone();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const demo = async (role: string) => {
    setBusy(role);
    setErr(null);
    try {
      const res = await fetch(`/t/demo/api/auth/demo-login`, { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify({ role }) });
      if (!res.ok) throw new Error((await res.json()).detail || "Demo sign-in failed");
      if (TID === "demo") onDone();
      else location.href = "/t/demo/#/";
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <div className="grid w-full max-w-4xl gap-4 md:grid-cols-2">
        <div className="card p-6">
          <div className="mb-6 flex items-center gap-2">
            <div className="grid size-9 place-items-center rounded-xl bg-side text-[#8b96ff]"><Bot className="size-5" /></div>
            <div>
              <div className="text-lg font-semibold">{brand}</div>
              <div className="text-xs text-muted">{isDemo ? "Pitch demo workspace (fictional data)" : "Your programme's AI. Nothing goes out without your approval."}</div>
            </div>
          </div>
          <form onSubmit={submit} className="space-y-3" aria-label="Sign in">
            <Input placeholder="Username" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" aria-label="Username" />
            <Input placeholder="Password" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" aria-label="Password" />
            {err && <ErrorBox error={err} />}
            <Button tone="primary" className="w-full" loading={busy === "login"} type="submit">Sign in</Button>
          </form>
          <p className="mt-4 text-xs text-muted">Owner password is written to a mode-600 file on the server when the workspace is created. It is never shown here.</p>
        </div>
        <div className="card p-6">
          <div className="mb-1 flex items-center gap-2 text-lg font-semibold"><Sparkles className="size-5 text-demo" /> Pitch demo</div>
          <p className="mb-4 text-sm text-muted">Opens the separate demo database: a believable, fictional cohort. Every person, business and number there is labelled <Badge tone="demo">DEMO</Badge>.</p>
          <div className="grid grid-cols-2 gap-2" data-testid="pitch-demo">
            {["owner", "coach", "setter", "closer", "student"].map((r) => (
              <Button key={r} onClick={() => demo(r)} loading={busy === r} className="capitalize">{r}</Button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Shell({ me, reloadMe }: { me: Me; reloadMe: () => void }) {
  const { page, rest, query } = useHash();
  const [navOpen, setNavOpen] = useState(false);
  const [cite, setCite] = useState<Citation | null>(null);
  const [trace, setTrace] = useState<number | null>(null);
  const [toastMsg, setToast] = useState<{ msg: string; tone: string } | null>(null);
  const allowed = useMemo(() => PAGES.filter((p) => me.tabs.includes(p.key)), [me.tabs]);
  const current = allowed.find((p) => p.key === page);
  const ctx = useMemo(
    () => ({
      me,
      reloadMe,
      openSource: setCite,
      openTrace: setTrace,
      go: (path: string) => { location.hash = path.startsWith("#") ? path : `#${path}`; },
      toast: (msg: string, tone: "good" | "bad" | "info" = "info") => {
        setToast({ msg, tone });
        setTimeout(() => setToast(null), 3500);
      },
    }),
    [me, reloadMe],
  );
  useEffect(() => setNavOpen(false), [page]);
  const logout = async () => {
    await post("/auth/logout").catch(() => null);
    location.reload();
  };
  const roleLabel = `${me.user.role[0].toUpperCase()}${me.user.role.slice(1)}${me.is_demo ? " (DEMO)" : ""}`;
  const nav = (
    <nav className="scroll-thin flex h-full flex-col overflow-y-auto bg-side text-side-ink" aria-label="Main">
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="grid size-8 place-items-center rounded-lg bg-white/10 text-[#8b96ff]"><Bot className="size-4" /></div>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-white">{me.brand?.name || "Operator AI"}</div>
          <div className="truncate text-[11px]">{me.tenant.name}</div>
        </div>
      </div>
      <div className="flex-1 space-y-4 px-2 pb-4">
        {GROUPS.map((g) => {
          const items = allowed.filter((p) => p.group === g);
          if (!items.length) return null;
          return (
            <div key={g}>
              <div className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-white/40">{g}</div>
              {items.map((p) => (
                <a key={p.key} href={`#/${p.key}`} data-nav={p.key}
                  className={cx("focus-ring flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-[13px] hover:bg-white/5 hover:text-white", page === p.key && "bg-side-active text-white")}>
                  <p.icon className="size-4 shrink-0 opacity-80" /> {p.label}
                </a>
              ))}
            </div>
          );
        })}
      </div>
      <div className="border-t border-white/10 px-3 py-3 text-xs">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-white" data-testid="whoami">{roleLabel}{me.is_demo ? " · PITCH DEMO" : ""}</span>
          <button onClick={logout} className="focus-ring rounded p-1 hover:bg-white/10" aria-label="Sign out"><LogOut className="size-4" /></button>
        </div>
        <div className="truncate text-white/40">{me.user.display}</div>
      </div>
    </nav>
  );
  return (
    <AppCtx.Provider value={ctx}>
      <div className="flex h-full">
        <aside className="hidden w-60 shrink-0 md:block">{nav}</aside>
        {navOpen && (
          <div className="fixed inset-0 z-40 flex md:hidden" onClick={() => setNavOpen(false)}>
            <div className="w-64" onClick={(e) => e.stopPropagation()}>{nav}</div>
            <div className="flex-1 bg-black/40" />
          </div>
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex items-center gap-2 border-b border-line bg-bg/90 px-3 py-2 backdrop-blur md:px-6">
            <button className="focus-ring rounded p-1.5 md:hidden" onClick={() => setNavOpen(true)} aria-label="Open menu"><Menu className="size-5" /></button>
            <div className="min-w-0 flex-1 truncate text-sm text-muted">{current?.group} {current && "·"} <span className="text-ink">{current?.label}</span></div>
            {me.is_demo && <Badge tone="demo">PITCH DEMO</Badge>}
            <Pill tone={me.ai.enabled ? "good" : "muted"}>
              <span className={cx("size-2 rounded-full", me.ai.enabled ? "bg-good" : "bg-gray-400")} />
              <span data-testid="ai-pill" title={me.ai.label}>{me.ai.enabled ? "AI connected" : "AI offline: saved AI runs replay (labelled)"}</span>
            </Pill>
          </header>
          <main className="min-w-0 flex-1 px-3 py-5 md:px-8 md:py-7">
            <Suspense fallback={<Loading rows={6} />}>
              {current ? <current.C rest={rest} query={query} /> : <div className="card p-6"><p className="font-medium">That page isn't available for your role.</p><a className="text-brand" href="#/">Back to Day One</a></div>}
            </Suspense>
          </main>
        </div>
      </div>
      <SourceModal cite={cite} onClose={() => setCite(null)} />
      <TraceDrawer runId={trace} onClose={() => setTrace(null)} />
      {toastMsg && (
        <div role="status" className={cx("fixed bottom-4 right-4 z-[70] max-w-sm rounded-lg px-4 py-2 text-sm text-white shadow-lg", toastMsg.tone === "bad" ? "bg-bad" : toastMsg.tone === "good" ? "bg-good" : "bg-side")}>
          {toastMsg.msg}
          <button className="ml-3 opacity-70" onClick={() => setToast(null)} aria-label="Dismiss"><X className="inline size-3.5" /></button>
        </div>
      )}
    </AppCtx.Provider>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<"loading" | "signin" | "ready" | "error">("loading");
  const [err, setErr] = useState<ApiError | null>(null);
  const load = useCallback(async () => {
    try {
      const m = await get<Me & { signed_in: boolean }>("/auth/session");
      if (!m.signed_in) {
        setState("signin");
        return;
      }
      applyBrand(m.brand);
      setMe(m);
      setState("ready");
    } catch (e) {
      const ae = e as ApiError;
      if (ae.status === 401 || ae.status === 404) setState("signin");
      else {
        setErr(ae);
        setState("error");
      }
    }
  }, []);
  useEffect(() => {
    load();
    const h = () => setState("signin");
    window.addEventListener("op:signed-out", h);
    return () => window.removeEventListener("op:signed-out", h);
  }, [load]);
  return (
    <>
      <ReconnectBar />
      {state === "loading" && <div className="p-8"><Loading rows={4} /></div>}
      {state === "error" && <div className="p-8"><ErrorBox error={err} retry={load} /></div>}
      {state === "signin" && <SignIn onDone={load} />}
      {state === "ready" && me && <Shell me={me} reloadMe={load} />}
    </>
  );
}
