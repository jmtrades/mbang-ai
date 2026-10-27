import React, { Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { AnimatePresence, LazyMotion, MotionConfig, domAnimation, m } from "motion/react";
import {
  ArrowRight, BadgeCheck, Bot, CircleCheck, ChevronDown, ChevronRight, Crown, GraduationCap, Info, LayoutGrid, LogOut, Menu,
  MessageCircle, Monitor, Moon, PhoneCall, Plus, Rocket, Search, Send, ShieldCheck, Sun, TriangleAlert, WifiOff, X, Inbox,
} from "lucide-react";
import { ApiError, TID, get, onConnection, post } from "./api";
import { AppCtx, Avatar, Badge, Button, Citation, ErrorBox, IconButton, Input, Kbd, Loading, Me, Pill, SourceModal, TraceDrawer, cx, modKey } from "./components/ui";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { PAGES, PAGE_BY_KEY, groupOf, navFor, tabbarFor } from "./pages/registry";
import { setThemePref, toggleTheme, useTheme } from "./theme";
import Join from "./components/Join";

class PageBoundary extends React.Component<{ children: React.ReactNode; page: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidUpdate(prev: { page: string }) {
    if (prev.page !== this.props.page && this.state.error) this.setState({ error: null });
  }
  render() {
    if (this.state.error) {
      return (
        <div className="card p-6">
          <h1 className="t-h2 mb-3">Something went wrong on this screen</h1>
          <ErrorBox error="This screen hit an unexpected error. Your data is safe." retry={() => this.setState({ error: null })} />
        </div>
      );
    }
    return this.props.children;
  }
}

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

// ---------------------------------------------------------------- brand
function luminance(hex: string): number | null {
  const m6 = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m6) return null;
  const n = parseInt(m6[1], 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}

function applyBrand(brand: Me["brand"] | undefined) {
  const r = document.documentElement.style;
  if (brand?.primary) {
    r.setProperty("--brand", brand.primary);
    // light brand colours get dark text on buttons; everything else gets white
    const l = luminance(brand.primary);
    r.setProperty("--brand-ink", l !== null && l > 0.42 ? "#0b0b0d" : "#ffffff");
  }
  if (brand?.accent) r.setProperty("--brand-2", brand.accent);
  document.title = brand?.name || "Operator AI";
}

const initial = (s: string) => (s.trim()[0] || "O").toUpperCase();

/** The brand's logo (saved in Setup: an https image link or a small uploaded image), else its monogram. */
function BrandMark({ name, logo, className }: { name: string; logo?: string | null; className?: string }) {
  const [broken, setBroken] = useState(false);
  const ok = typeof logo === "string" && /^(https:\/\/|\/(?!\/))/i.test(logo) && !broken;
  if (ok) {
    return <img src={logo as string} alt="" aria-hidden data-testid="brand-logo" onError={() => setBroken(true)}
      className={cx("size-8 shrink-0 rounded-[9px] bg-panel object-contain ring-1 ring-line", className)} />;
  }
  return <span aria-hidden data-testid="brand-monogram" className={cx("brand-mark grid size-8 shrink-0 place-items-center rounded-[9px] text-[14px] font-semibold", className)}>{initial(name)}</span>;
}

// ---------------------------------------------------------------- small stores
function readJSON<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}
function writeJSON(key: string, v: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(v));
  } catch {
    /* storage unavailable */
  }
}

// ---------------------------------------------------------------- connection
function ReconnectBar() {
  const [offline, setOffline] = useState(false);
  useEffect(() => onConnection((on) => setOffline(!on)), []);
  return (
    <AnimatePresence>
      {offline && (
        <m.div role="status" initial={{ y: -40, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: -40, opacity: 0 }}
          className="fixed inset-x-0 top-2 z-[90] mx-auto flex w-fit items-center gap-2 rounded-full border border-warn-line bg-warn-soft px-3.5 py-1.5 text-[13px] font-medium text-warn-ink shadow-lg backdrop-blur">
          <WifiOff className="size-4" /> Reconnecting to the server…
        </m.div>
      )}
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------- sign in
const DEMO_ROLES: { role: string; icon: React.ComponentType<{ className?: string }>; desc: string }[] = [
  { role: "owner", icon: Crown, desc: "Sees everything, approves every send" },
  { role: "coach", icon: GraduationCap, desc: "Accountability, reviews and check-ins" },
  { role: "setter", icon: Send, desc: "Prospects, openers and outreach" },
  { role: "closer", icon: PhoneCall, desc: "Booked calls and follow-ups" },
  { role: "student", icon: Rocket, desc: "Missions, practice and a first client" },
];

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
  const promises = [
    { icon: BadgeCheck, t: "Grounded in your material", d: "Every answer cites the lesson it came from, or says the material doesn't cover it." },
    { icon: ShieldCheck, t: "Agentic, but gated", d: "Agents plan, draft and review. You approve every message before it goes out." },
    { icon: Bot, t: "Keeps working without AI", d: "Rules, templates, retrieval and labelled replays keep every screen useful." },
  ];
  return (
    <main className="grid min-h-dvh bg-bg lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <section className="hero-surface hidden flex-col justify-between overflow-hidden border-r border-line p-10 lg:flex xl:p-14" aria-label={`About ${brand}`}>
        <div className="flex items-center gap-2.5">
          <BrandMark name={brand} logo={pub?.brand?.logo} />
          <span className="text-[15px] font-semibold">{brand}</span>
        </div>
        <div className="max-w-lg">
          <p className="t-display text-ink">Your programme's AI operator.</p>
          <p className="t-lead mt-4">Trained on your material, speaking in your brand, doing the work for you and your students.</p>
          <ul className="mt-10 space-y-5">
            {promises.map(({ icon: I, t, d }) => (
              <li key={t} className="flex gap-3.5">
                <span className="grid size-9 shrink-0 place-items-center rounded-[10px] border border-line bg-panel text-brand shadow-xs"><I className="size-[18px]" /></span>
                <span><span className="block font-semibold text-ink">{t}</span><span className="block text-[13.5px] text-muted">{d}</span></span>
              </li>
            ))}
          </ul>
        </div>
        <p className="text-[12px] text-muted">White-label workspace · {isDemo ? "pitch demo with fictional data" : "private to your programme"}</p>
      </section>
      <section className="flex items-center justify-center px-4 py-10 sm:px-8">
        <div className="w-full max-w-[400px]">
          <div className="mb-8 flex items-center gap-2.5 lg:hidden">
            <BrandMark name={brand} logo={pub?.brand?.logo} />
            <span className="text-[15px] font-semibold">{brand}</span>
          </div>
          <h1 className="t-h1">Sign in to {brand}</h1>
          <p className="mt-1.5 text-[14px] text-muted">{isDemo ? "This is the pitch demo workspace. Every person and number in it is fictional." : "Your programme's AI. Nothing goes out without your approval."}</p>
          <form onSubmit={submit} className="mt-6 space-y-3" aria-label="Sign in">
            <Input placeholder="Username" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" aria-label="Username" />
            <Input placeholder="Password" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" aria-label="Password" />
            {err && <ErrorBox error={err} />}
            <Button tone="primary" className="w-full" size="lg" loading={busy === "login"} type="submit">Sign in</Button>
          </form>
          {!isDemo && (
            <p className="mt-3 text-right text-[13px]">
              <a href="#/join?forgot=1" className="focus-ring rounded font-medium text-brand hover:underline" data-testid="forgot-password">Forgot password?</a>
            </p>
          )}
          <p className="mt-3 text-[12px] leading-relaxed text-muted">The owner password is written to a private file on the server when the workspace is created. It is never shown here.</p>

          <div className="my-7 flex items-center gap-3 text-[12px] font-medium text-muted">
            <span className="h-px flex-1 bg-line" /> or open the pitch demo <span className="h-px flex-1 bg-line" />
          </div>
          <div className="space-y-2" data-testid="pitch-demo">
            {DEMO_ROLES.map(({ role, icon: I, desc }) => (
              <button key={role} type="button" onClick={() => demo(role)} disabled={!!busy} aria-label={`Open the demo as ${role}`}
                className="focus-ring group flex w-full items-center gap-3 rounded-[12px] border border-line bg-panel px-3 py-2.5 text-left shadow-xs transition-[border-color,box-shadow,background-color] hover:border-brand-line hover:shadow-md disabled:opacity-60">
                <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-brand-soft text-brand ring-1 ring-brand-line"><I className="size-[18px]" /></span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[14px] font-semibold capitalize text-ink">{role}</span>
                  <span className="block truncate text-[12.5px] text-muted">{desc}</span>
                </span>
                {busy === role ? <span className="size-4 animate-spin rounded-full border-2 border-brand border-t-transparent" /> : <ArrowRight className="size-4 text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-brand" />}
              </button>
            ))}
          </div>
          <p className="mt-3 flex items-center gap-1.5 text-[12px] text-muted"><Badge tone="demo">DEMO</Badge> marks every fictional person, business and number.</p>
        </div>
      </section>
    </main>
  );
}

// ---------------------------------------------------------------- theme toggle
function ThemeToggle({ className, small }: { className?: string; small?: boolean }) {
  const { theme, toggle } = useTheme();
  const dark = theme === "dark";
  return (
    <IconButton label={dark ? "Switch to light mode" : "Switch to dark mode"} onClick={toggle} className={className} size={small ? "icon-sm" : "icon"}>
      <AnimatePresence mode="wait" initial={false}>
        <m.span key={theme} initial={{ rotate: -60, opacity: 0 }} animate={{ rotate: 0, opacity: 1 }} exit={{ rotate: 60, opacity: 0 }} transition={{ duration: 0.18 }} className="grid place-items-center">
          {dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
        </m.span>
      </AnimatePresence>
    </IconButton>
  );
}

// ---------------------------------------------------------------- sidebar
function Sidebar({ me, page, open, onClose, onLogout }: { me: Me; page: string; open: boolean; onClose: () => void; onLogout: () => void }) {
  const sections = useMemo(() => navFor(me.user.role, me.tabs), [me.user.role, me.tabs]);
  const [collapsed, setCollapsed] = useState<string[]>(() => readJSON<string[]>("op.nav.collapsed", []));
  const baseId = useId();
  const toggle = (g: string) => {
    const next = collapsed.includes(g) ? collapsed.filter((x) => x !== g) : [...collapsed, g];
    setCollapsed(next);
    writeJSON("op.nav.collapsed", next);
  };
  const roleLabel = `${me.user.role[0].toUpperCase()}${me.user.role.slice(1)}${me.is_demo ? " (DEMO)" : ""}`;
  const brand = me.brand?.name || "Operator AI";
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (open) setTimeout(() => navRef.current?.querySelector<HTMLElement>("[aria-current=page], a")?.focus({ preventScroll: true }), 50);
  }, [open]);
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Keep the active link visible in a long sidebar, but never move the list when it already is:
    // a sidebar that re-centres on every click loses the reader's place.
    const list = listRef.current;
    const el = list?.querySelector<HTMLElement>("[aria-current=page]");
    if (!list || !el) return;
    const a = el.getBoundingClientRect();
    const b = list.getBoundingClientRect();
    if (a.top < b.top + 12 || a.bottom > b.bottom - 24) el.scrollIntoView({ block: "nearest" });
  }, [page]);
  return (
    <aside
      className={cx(
        "fixed inset-y-0 left-0 z-50 w-[288px] max-w-[86vw] border-r border-line bg-side shadow-xl transition-[transform,visibility] duration-300 ease-[cubic-bezier(.2,.8,.2,1)]",
        "md:relative md:inset-auto md:z-auto md:h-full md:w-[260px] md:max-w-none md:translate-x-0 md:border-0 md:bg-transparent md:shadow-none md:visible",
        open ? "translate-x-0" : "-translate-x-full max-md:invisible",
      )}
    >
      <nav ref={navRef} className="flex h-full flex-col" aria-label="Main">
        <div className="flex items-center gap-2.5 px-4 pb-3 pt-4 md:pt-3">
          <BrandMark name={brand} logo={me.brand?.logo} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-semibold leading-tight text-ink">{brand}</div>
            <div className="truncate text-[12px] leading-tight text-muted">{me.tenant.name}</div>
          </div>
          <ThemeToggle small className="md:hidden" />
          <IconButton label="Close menu" size="icon-sm" className="md:hidden" onClick={onClose}><X className="size-4" /></IconButton>
        </div>
        <div ref={listRef} className="scroll-thin fade-edges flex-1 scroll-py-6 overflow-y-auto px-2.5 pb-6 pt-2">
          {sections.map((s) => {
            const hasActive = s.items.some((i) => i.key === page);
            const isOpen = !collapsed.includes(s.group) || hasActive;
            const listId = `${baseId}-${s.group.replace(/\W+/g, "")}`;
            return (
              <div key={s.group} className="mb-3 last:mb-0">
                <button type="button" onClick={() => toggle(s.group)} aria-expanded={isOpen} aria-controls={listId}
                  className="focus-ring group/g flex h-7 w-full items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:text-ink">
                  <span className="flex-1 text-left">{s.group}</span>
                  <ChevronDown className={cx("size-3.5 opacity-0 transition-[transform,opacity] group-hover/g:opacity-100 group-focus-visible/g:opacity-100", !isOpen && "-rotate-90 opacity-100")} />
                </button>
                <AnimatePresence initial={false}>
                  {isOpen && (
                    <m.ul id={listId} initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }} className="space-y-px overflow-hidden">
                      {s.items.map((p) => {
                        const active = page === p.key;
                        return (
                          <li key={p.key}>
                            <a href={`#/${p.key}`} data-nav={p.key} aria-current={active ? "page" : undefined}
                              className={cx(
                                "focus-ring group/nav relative flex h-8 items-center gap-2.5 rounded-[8px] px-2 text-[13.5px] font-medium transition-colors",
                                active ? "bg-side-active text-ink" : "text-side-ink hover:bg-hover hover:text-ink",
                              )}>
                              <p.icon className={cx("size-4 shrink-0", active ? "text-brand" : "text-muted group-hover/nav:text-ink-2")} strokeWidth={1.9} />
                              <span className="truncate">{p.label}</span>
                            </a>
                          </li>
                        );
                      })}
                    </m.ul>
                  )}
                </AnimatePresence>
              </div>
            );
          })}
        </div>
        <div className="border-t border-line p-2.5">
          <div className="flex items-center gap-2 rounded-[10px] px-1 py-1">
            <Avatar name={me.user.display || me.user.role} small />
            <div className="min-w-0 flex-1 leading-tight" title={`${me.user.display} · ${roleLabel}`}>
              <div className="truncate text-[13px] font-medium text-ink">{(me.user.display || roleLabel).replace(/\s*\(DEMO\)\s*/g, "")}</div>
              <div className="mt-0.5 text-[11px] leading-snug tracking-[-0.003em] text-muted" data-testid="whoami">{roleLabel}{me.is_demo ? " · PITCH DEMO" : ""}</div>
            </div>
            <IconButton label="Sign out" size="icon-sm" onClick={onLogout}><LogOut className="size-4" /></IconButton>
          </div>
        </div>
      </nav>
    </aside>
  );
}

// ---------------------------------------------------------------- header
function Header({ me, page, onMenu, onSearch }: { me: Me; page: string; onMenu: () => void; onSearch: () => void }) {
  const current = PAGE_BY_KEY[page];
  const brand = me.brand?.name || "Operator AI";
  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b border-line bg-[color-mix(in_oklab,var(--bg)_82%,transparent)] px-3 backdrop-blur-xl backdrop-saturate-150 md:px-5">
      <IconButton label="Open menu" className="-ml-1 md:hidden" onClick={onMenu}><Menu className="size-5" /></IconButton>
      <a href="#/" className="focus-ring flex items-center rounded-[9px] md:hidden" aria-label={`${brand} home`}><BrandMark name={brand} logo={me.brand?.logo} className="size-7 text-[13px]" /></a>
      <nav aria-label="Breadcrumb" className="hidden min-w-0 flex-1 md:block">
        <ol className="flex min-w-0 items-center gap-1.5 text-[13px]">
          <li className="shrink-0 max-xl:hidden"><a href="#/" className="focus-ring rounded text-muted transition-colors hover:text-ink">{me.tenant.name}</a></li>
          {current && (
            <>
              <li aria-hidden className="text-faint max-xl:hidden"><ChevronRight className="size-3.5" /></li>
              <li className="shrink-0 text-muted">{groupOf(current, me.user.role)}</li>
              <li aria-hidden className="text-faint"><ChevronRight className="size-3.5" /></li>
              <li aria-current="page" className="truncate font-medium text-ink">{current.label}</li>
            </>
          )}
        </ol>
      </nav>
      <div className="ml-auto flex shrink-0 items-center gap-1.5 md:gap-2">
        <button type="button" onClick={onSearch} aria-keyshortcuts="Meta+K Control+K"
          className="focus-ring hidden h-8 w-56 items-center gap-2 rounded-[9px] border border-line bg-panel px-2.5 text-[13px] text-muted shadow-xs transition-colors hover:border-line-strong hover:text-ink lg:flex">
          <Search className="size-3.5" />
          <span className="flex-1 text-left">Search or jump to…</span>
          <Kbd>{modKey === "⌘" ? "⌘K" : "Ctrl K"}</Kbd>
        </button>
        {me.is_demo && <Badge tone="demo" className="font-semibold tracking-[0.04em]"><span className="max-sm:hidden">PITCH </span>DEMO</Badge>}
        <Pill tone={me.ai.enabled ? "good" : "muted"} className="max-w-[46vw]">
          <span className={cx("size-1.5 shrink-0 rounded-full", me.ai.enabled ? "pulse-dot bg-good" : "bg-faint")} />
          <span data-testid="ai-pill" title={me.ai.label} className="truncate">
            {me.ai.enabled ? "AI connected" : <>AI offline<span className="max-xl:hidden">: saved AI runs replay (labelled)</span></>}
          </span>
        </Pill>
        <IconButton label="Search pages and actions" className="lg:hidden" onClick={onSearch}><Search className="size-[18px]" /></IconButton>
        <ThemeToggle className="max-md:hidden" />
      </div>
    </header>
  );
}

// ---------------------------------------------------------------- phone tab bar
function TabBar({ me, page, onMore }: { me: Me; page: string; onMore: () => void }) {
  const tabs = tabbarFor(me.user.role, me.tabs);
  const inTabs = tabs.some((t) => t.key === page);
  return (
    <nav aria-label="Shortcuts" className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-[color-mix(in_oklab,var(--panel)_86%,transparent)] pb-[env(safe-area-inset-bottom)] backdrop-blur-xl backdrop-saturate-150 md:hidden">
      <ul className="grid" style={{ gridTemplateColumns: `repeat(${tabs.length + 1}, minmax(0, 1fr))` }}>
        {tabs.map((p) => {
          const active = page === p.key;
          return (
            <li key={p.key}>
              <a href={`#/${p.key}`} aria-current={active ? "page" : undefined}
                className={cx("focus-ring relative flex h-[58px] flex-col items-center justify-center gap-1 text-[11px] font-medium transition-colors", active ? "text-brand" : "text-muted")}>
                {active && <span aria-hidden className="absolute inset-x-5 top-0 h-[2px] rounded-full bg-brand" />}
                <p.icon className="size-[21px]" strokeWidth={active ? 2.1 : 1.8} />
                <span className="max-w-full truncate px-1">{p.short || p.label}</span>
              </a>
            </li>
          );
        })}
        <li>
          <button type="button" onClick={onMore} aria-label="More pages"
            className={cx("focus-ring flex h-[58px] w-full flex-col items-center justify-center gap-1 text-[11px] font-medium", !inTabs ? "text-brand" : "text-muted")}>
            <LayoutGrid className="size-[21px]" strokeWidth={1.8} />
            <span>More</span>
          </button>
        </li>
      </ul>
    </nav>
  );
}

// ---------------------------------------------------------------- toasts
type Toast = { id: number; msg: string; tone: "good" | "bad" | "info" };
function Toaster({ toasts, dismiss }: { toasts: Toast[]; dismiss: (id: number) => void }) {
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(72px+env(safe-area-inset-bottom))] z-[70] flex flex-col items-center gap-2 px-3 md:inset-x-auto md:bottom-5 md:right-5 md:items-end">
      <AnimatePresence initial={false}>
        {toasts.map((t) => {
          const Icon = t.tone === "good" ? CircleCheck : t.tone === "bad" ? TriangleAlert : Info;
          return (
            <m.div key={t.id} initial={{ opacity: 0, y: 14, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.15 } }} transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
              role="status"
              className="pointer-events-auto flex w-full max-w-sm items-start gap-2.5 rounded-[12px] border border-line bg-panel py-2.5 pl-3 pr-2 text-[13.5px] text-ink shadow-lg">
              <Icon className={cx("mt-0.5 size-4 shrink-0", t.tone === "good" ? "text-good" : t.tone === "bad" ? "text-bad" : "text-brand")} />
              <span className="min-w-0 flex-1 leading-snug">{t.msg}</span>
              <button type="button" className="focus-ring -my-0.5 rounded-md p-1 text-muted hover:bg-hover hover:text-ink" onClick={() => dismiss(t.id)} aria-label="Dismiss"><X className="size-3.5" /></button>
            </m.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}

// ---------------------------------------------------------------- shell
function Shell({ me, reloadMe }: { me: Me; reloadMe: () => void }) {
  const { page, rest, query } = useHash();
  const [navOpen, setNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [cite, setCite] = useState<Citation | null>(null);
  const [trace, setTrace] = useState<number | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const scroller = useRef<HTMLDivElement>(null);
  const toastSeq = useRef(0);
  const { theme } = useTheme();
  const allowed = useMemo(() => PAGES.filter((p) => me.tabs.includes(p.key)), [me.tabs]);
  const current = allowed.find((p) => p.key === page);

  const dismiss = useCallback((id: number) => setToasts((ts) => ts.filter((t) => t.id !== id)), []);
  const ctx = useMemo(
    () => ({
      me,
      reloadMe,
      openSource: setCite,
      openTrace: setTrace,
      openPalette: () => setPaletteOpen(true),
      go: (path: string) => { location.hash = path.startsWith("#") ? path : `#${path}`; },
      toast: (msg: string, tone: "good" | "bad" | "info" = "info") => {
        const id = ++toastSeq.current;
        setToasts((ts) => [...ts.slice(-2), { id, msg, tone }]);
        setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), tone === "bad" ? 6000 : 3500);
      },
    }),
    [me, reloadMe],
  );

  useEffect(() => {
    setNavOpen(false);
    scroller.current?.scrollTo({ top: 0 });
    window.scrollTo({ top: 0 });
    const brand = me.brand?.name || "Operator AI";
    document.title = current ? `${current.label} · ${brand}` : brand;
    if (current) {
      const recent = readJSON<string[]>("op.recent", []).filter((k) => k !== page);
      writeJSON("op.recent", [page, ...recent].slice(0, 6));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, me.brand?.name]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      } else if (e.key === "Escape" && navOpen) {
        setNavOpen(false);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [navOpen]);

  const logout = useCallback(async () => {
    await post("/auth/logout").catch(() => null);
    location.reload();
  }, []);

  const commands = useMemo<Command[]>(() => {
    const go = (k: string) => () => { location.hash = `#/${k}`; };
    const has = (k: string) => me.tabs.includes(k);
    const role = me.user.role;
    const out: Command[] = [];
    const recent = readJSON<string[]>("op.recent", []).filter((k) => k !== page && has(k) && PAGE_BY_KEY[k]).slice(0, 4);
    for (const k of recent) {
      const pg = PAGE_BY_KEY[k];
      out.push({ id: k, label: pg.label, section: "Recent", icon: pg.icon, hint: groupOf(pg, role), run: go(k) });
    }
    if (has("coach")) out.push({ id: "act-coach", label: "Ask the coach", section: "Actions", icon: MessageCircle, hint: "Coach", keywords: ["question", "chat", "help"], run: go("coach") });
    if (has("missions")) out.push({ id: "act-mission", label: "New mission", section: "Actions", icon: Plus, hint: "Missions", keywords: ["start", "task", "create"], run: () => { location.hash = "#/missions?new=1"; } });
    if (has("queue")) out.push({ id: "act-queue", label: "Open approval queue", section: "Actions", icon: Inbox, hint: "Approvals", keywords: ["approve", "drafts", "review"], run: go("queue") });
    out.push({ id: "act-theme", label: "Toggle dark mode", section: "Actions", icon: theme === "dark" ? Sun : Moon, hint: theme === "dark" ? "Dark is on" : "Light is on", keywords: ["theme", "light", "appearance", "night"], run: toggleTheme });
    out.push({ id: "act-system", label: "Use system theme", section: "Actions", icon: Monitor, hint: "Appearance", keywords: ["theme", "auto"], run: () => setThemePref("system") });
    out.push({ id: "act-signout", label: "Sign out", section: "Actions", icon: LogOut, keywords: ["log out", "logout", "exit"], run: logout });
    for (const s of navFor(role, me.tabs)) {
      for (const pg of s.items) out.push({ id: pg.key, label: pg.label, section: "Pages", icon: pg.icon, hint: s.group, keywords: pg.keywords, run: go(pg.key) });
    }
    return out;
    // recompute when the palette opens so "Recent" is fresh
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me.tabs, me.user.role, theme, page, paletteOpen, logout]);

  return (
    <AppCtx.Provider value={ctx}>
      <a href="#/" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[100] focus:rounded-[9px] focus:border focus:border-line focus:bg-panel focus:px-3 focus:py-2 focus:text-[13px] focus:font-medium focus:shadow-lg">
        Skip to content
      </a>
      <div className="min-h-dvh md:flex md:h-dvh md:p-2 md:pl-0">
        <AnimatePresence>
          {navOpen && <m.div className="fixed inset-0 z-40 bg-[var(--overlay)] md:hidden" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={() => setNavOpen(false)} />}
        </AnimatePresence>
        <Sidebar me={me} page={page} open={navOpen} onClose={() => setNavOpen(false)} onLogout={logout} />
        <div className="flex min-w-0 flex-1 flex-col md:overflow-hidden md:rounded-[14px] md:border md:border-line md:bg-bg md:shadow-sm">
          <div ref={scroller} className="flex min-h-dvh flex-1 flex-col bg-bg md:min-h-0 md:overflow-y-auto">
            <Header me={me} page={page} onMenu={() => setNavOpen(true)} onSearch={() => setPaletteOpen(true)} />
            <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1320px] flex-1 px-4 pb-28 pt-6 outline-none sm:px-6 md:px-8 md:pb-12 md:pt-8">
              <Suspense fallback={<Loading rows={6} />}>
                {current ? (
                  <PageBoundary page={page}>
                    <m.div key={page} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.24, ease: [0.2, 0.8, 0.2, 1] }}>
                      <current.C rest={rest} query={query} />
                    </m.div>
                  </PageBoundary>
                ) : (
                  <div className="card mx-auto max-w-lg p-8 text-center">
                    <h1 className="t-h2">That page isn't available for your role</h1>
                    <p className="mt-2 text-[14px] text-muted">Ask the workspace owner if you need access.</p>
                    <Button tone="primary" className="mt-5" onClick={() => { location.hash = "#/"; }}>Back to Day One</Button>
                  </div>
                )}
              </Suspense>
            </main>
          </div>
        </div>
      </div>
      <TabBar me={me} page={page} onMore={() => setNavOpen(true)} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} commands={commands} />
      <SourceModal cite={cite} onClose={() => setCite(null)} />
      <TraceDrawer runId={trace} onClose={() => setTrace(null)} />
      <Toaster toasts={toasts} dismiss={dismiss} />
    </AppCtx.Provider>
  );
}

function Boot() {
  return (
    <div className="grid min-h-dvh place-items-center bg-bg p-8">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2.5"><span className="skeleton size-8 rounded-[9px]" /><span className="skeleton h-4 w-32" /></div>
        <Loading rows={4} />
      </div>
    </div>
  );
}

export default function App() {
  useHash(); // re-render on hash changes, so "Forgot password?" (#/join?forgot=1) opens the join screen at once
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
    <MotionConfig reducedMotion="user">
      <LazyMotion features={domAnimation}>
        <ReconnectBar />
        {state === "loading" && <Boot />}
        {state === "error" && <div className="mx-auto max-w-xl p-8"><ErrorBox error={err} retry={load} /></div>}
        {/* the join screen (invite link, reset link, "Forgot password?") replaces the sign-in page while it's open */}
        {state === "signin" && !location.hash.startsWith("#/join") && <SignIn onDone={load} />}
        {location.hash.startsWith("#/join") && <Join onDone={load} />}
        {state === "ready" && me && <Shell me={me} reloadMe={load} />}
      </LazyMotion>
    </MotionConfig>
  );
}
