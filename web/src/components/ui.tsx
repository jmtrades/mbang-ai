import React, { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, m } from "motion/react";
import { AlertTriangle, Check, ChevronRight, Copy, FileText, Loader2, RotateCw, X, CircleDot, CircleCheck, CircleX, Clock, Inbox } from "lucide-react";
import { ApiError, get } from "../api";

// ---------------------------------------------------------------- app context
export type Me = {
  user: { id: number; username: string; display: string; role: string };
  tabs: string[];
  tenant: { id: string; name: string; kind: string; is_demo: boolean; kb_scope: string };
  brand: { name?: string; primary?: string; accent?: string; owner_display?: string; logo?: string };
  is_demo: boolean;
  ai: { enabled: boolean; label: string; pill: string; budget: number; used: number };
  can_decide: string[];
};
export type Citation = { id: string; file: string; lines?: string; title?: string; snippet?: string; ts?: string; kind?: string; url?: string };

type AppCtxT = {
  me: Me;
  reloadMe: () => void;
  openSource: (c: Citation) => void;
  openTrace: (runId: number) => void;
  go: (path: string) => void;
  toast: (msg: string, tone?: "good" | "bad" | "info") => void;
  /** Opens the command palette (⌘K). Optional so older providers keep working. */
  openPalette?: () => void;
};
export const AppCtx = createContext<AppCtxT>(null as any);
export const useApp = () => useContext(AppCtx);

// ---------------------------------------------------------------- data hook
export function useApi<T = any>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const seq = useRef(0);
  const load = useCallback(async () => {
    if (!path) return;
    const n = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const d = await get<T>(path);
      if (n === seq.current) setData(d);
    } catch (e) {
      if (n === seq.current) setError(e as ApiError);
    } finally {
      if (n === seq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    load();
  }, [load]);
  return { data, error, loading, reload: load, setData };
}

// ---------------------------------------------------------------- primitives
export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

export const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
export const modKey = isMac ? "⌘" : "Ctrl";

type BtnTone = "primary" | "secondary" | "ghost" | "danger" | "good" | "subtle";
type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: BtnTone; loading?: boolean; size?: "sm" | "md" | "lg" | "icon" | "icon-sm" };

const BTN_TONES: Record<BtnTone, string> = {
  primary: "border-transparent bg-brand bg-[image:var(--gold-grad)] text-[#0b0b0d] font-semibold shadow-[var(--shadow-primary)] hover:brightness-110",
  secondary: "border-line bg-panel text-ink shadow-[var(--shadow-btn)] hover:border-line-strong hover:bg-[color-mix(in_oklab,var(--panel),var(--ink)_3%)]",
  ghost: "border-transparent bg-transparent text-ink-2 hover:bg-hover hover:text-ink",
  subtle: "border-transparent bg-hover text-ink hover:bg-press",
  danger: "border-line bg-panel text-bad shadow-[var(--shadow-btn)] hover:border-bad-line hover:bg-bad-soft",
  good: "border-transparent bg-good-solid text-white shadow-[var(--shadow-primary)] hover:brightness-110",
};
const BTN_SIZES = {
  sm: "h-7 gap-1 rounded-[7px] px-2.5 text-[12.5px]",
  md: "h-9 gap-1.5 rounded-[9px] px-3.5 text-[13.5px]",
  lg: "h-11 gap-2 rounded-[11px] px-5 text-[14.5px]",
  icon: "size-9 rounded-[9px] p-0",
  "icon-sm": "size-7 rounded-[7px] p-0",
};

export function Button({ tone = "secondary", loading, size = "md", className, children, disabled, ...rest }: BtnProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cx(
        "focus-ring relative inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap border font-medium leading-none",
        "transition-[background-color,border-color,color,box-shadow,transform,filter] duration-150 active:translate-y-px",
        "disabled:pointer-events-none disabled:opacity-50 [&_svg]:shrink-0",
        BTN_SIZES[size],
        BTN_TONES[tone],
        className,
      )}
    >
      {loading && <Loader2 className="size-4 animate-spin" />}
      {children}
    </button>
  );
}

/** Square icon-only button with a required accessible label. */
export function IconButton({ label, children, className, tone = "ghost", size = "icon", ...rest }: Omit<BtnProps, "aria-label"> & { label: string }) {
  return (
    <Button tone={tone} size={size} aria-label={label} title={rest.title ?? label} className={className} {...rest}>
      {children}
    </Button>
  );
}

export function Card({ title, subtitle, actions, children, className, pad = true, bodyClassName }: { title?: React.ReactNode; subtitle?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode; className?: string; pad?: boolean; bodyClassName?: string }) {
  return (
    <section className={cx("card", className)}>
      {(title || actions) && (
        <header className={cx("flex flex-wrap justify-between gap-x-3 gap-y-2 border-b border-line-2 px-4 py-3 sm:px-5", subtitle ? "items-start" : "min-h-[52px] items-center")}>
          {/* the title block grows and wraps its own text first, so a short action stays on the title's line */}
          <div className="min-w-0 flex-[1_1_7rem]">
            {title && <h3 className="t-h3 text-ink">{title}</h3>}
            {subtitle && <p className="mt-0.5 text-[12.5px] leading-snug text-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx(pad && "p-4 sm:p-5", bodyClassName)}>{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------- display text
const ACRONYMS: Record<string, string> = { ai: "AI", dm: "DM", dms: "DMs", sms: "SMS", vsl: "VSL", kpi: "KPI", faq: "FAQ", crm: "CRM", cta: "CTA", url: "URL", api: "API", id: "ID", mcp: "MCP", meta: "Meta", usp: "USP", roas: "ROAS", cpl: "CPL", ctr: "CTR" };

/** Turns a raw key ("needs_human", "meta_lead_form", "hot") into sentence-case display text ("Needs human", "Meta lead form", "Hot"). */
export function humanize(v: unknown): string {
  const s = String(v ?? "").trim();
  if (!s) return "";
  if (!/^[a-z0-9_\-\s]+$/.test(s)) return s; // already display text
  const words = s.replace(/[_-]+/g, " ").split(/\s+/).map((w) => ACRONYMS[w] || w);
  const first = words[0];
  words[0] = first === first.toLowerCase() ? first[0].toUpperCase() + first.slice(1) : first;
  return words.join(" ");
}

/** "3m ago", "5h ago", "Oct 25" from an epoch in seconds. */
export function ago(sec?: number): string {
  if (!sec) return "";
  const d = Date.now() / 1000 - sec;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)}d ago`;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(sec * 1000));
}

/** Run titles repeat the agent ("Coach: How do I…"); the agent is shown separately, so drop the prefix. */
export const runTitle = (r: any) => String(r.title || "").replace(new RegExp(`^${String(r.agent || "").replace(/[^\w]/g, ".")}\\s*:\\s*`, "i"), "");

/** "Fix 1 held draft(s) that need a person" → "Fix 1 held draft that needs a person"; "4 student(s)" → "4 students". */
export function tidyCount(text: string): string {
  const m6 = /(\d+)/.exec(text);
  if (!m6 || !text.includes("(s)")) return text;
  const one = Number(m6[1]) === 1;
  let out = text.replace(/(\w+)\(s\)/g, (_, w) => (one ? w : `${w}s`));
  if (one) out = out.replace(/\b(that|who) need\b/g, "$1 needs");
  return out;
}

type Tone = "neutral" | "good" | "bad" | "warn" | "brand" | "demo" | "info";
const BADGE_TONES: Record<Tone, string> = {
  neutral: "bg-[var(--neutral-soft)] text-ink-2 ring-[var(--neutral-line)]",
  good: "bg-good-soft text-good-ink ring-good-line",
  bad: "bg-bad-soft text-bad-ink ring-bad-line",
  warn: "bg-warn-soft text-warn-ink ring-warn-line",
  brand: "bg-brand-soft text-brand-fg ring-brand-line",
  demo: "bg-demo-soft text-demo-ink ring-demo-line",
  info: "bg-info-soft text-info-ink ring-info-line",
};

export function Badge({ tone = "neutral", children, className, title, dot }: { tone?: Tone; children: React.ReactNode; className?: string; title?: string; dot?: boolean }) {
  return (
    <span title={title} className={cx("inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 text-[11.5px] font-medium leading-none ring-1 ring-inset", BADGE_TONES[tone] || BADGE_TONES.neutral, className)}>
      {dot && <span className="size-1.5 rounded-full bg-current opacity-80" />}
      {children}
    </span>
  );
}

export const DemoBadge = () => <Badge tone="demo" title="Fictional demo data" className="font-semibold tracking-[0.04em]">DEMO</Badge>;

export function isDemo(v: unknown): boolean {
  if (!v) return false;
  if (typeof v === "string") return v.includes("(DEMO)");
  if (typeof v === "object") return !!(v as any).demo || JSON.stringify(v).includes("(DEMO)");
  return false;
}

export function StatusBadge({ s }: { s: string }) {
  const tone = s === "REAL_OK" ? "good" : s === "NEEDS_LOGIN" ? "warn" : s === "NEEDS_AI_BUDGET" ? "info" : "neutral";
  return <Badge tone={tone as Tone} dot>{s}</Badge>;
}

export function Skeleton({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return <div className={cx("skeleton", className)} style={style} />;
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2.5 py-1" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-4" style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

export function ErrorBox({ error, retry }: { error: ApiError | Error | string | null; retry?: () => void }) {
  if (!error) return null;
  const msg = typeof error === "string" ? error : error.message;
  return (
    <div role="alert" className="my-2 flex flex-wrap items-center gap-3 rounded-[10px] border border-bad-line bg-bad-soft px-3.5 py-2.5 text-[13.5px] text-bad-ink">
      <AlertTriangle className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">{msg}</span>
      {retry && (
        <Button size="sm" onClick={retry}>
          <RotateCw className="size-3.5" /> Retry
        </Button>
      )}
    </div>
  );
}

export function Empty({ title, hint, icon, action, className }: { title: string; hint?: React.ReactNode; icon?: React.ReactNode; action?: React.ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col items-center justify-center gap-3 rounded-[var(--radius-lg)] border border-dashed border-line-strong bg-panel-2/60 px-6 py-10 text-center", className)}>
      <div className="grid size-11 place-items-center rounded-full bg-panel text-muted shadow-xs ring-1 ring-line [&_svg]:size-5">{icon || <Inbox />}</div>
      <div>
        <p className="font-semibold text-ink">{title}</p>
        {hint && <p className="mx-auto mt-1 max-w-md text-[13px] leading-relaxed text-muted">{hint}</p>}
      </div>
      {action && <div className="mt-1 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="block text-[12.5px] font-medium text-ink-2">{label}</span>
      {children}
      {hint && <span className="block text-[12px] text-muted">{hint}</span>}
    </label>
  );
}

const inputCls =
  "w-full rounded-[9px] border border-line bg-panel px-3 py-2 text-[14px] text-ink shadow-xs outline-none transition-[border-color,box-shadow] placeholder:text-placeholder hover:border-line-strong focus:border-brand focus:ring-[3px] focus:ring-[var(--brand-ring)] disabled:cursor-not-allowed disabled:opacity-60";
export const Input = (p: React.InputHTMLAttributes<HTMLInputElement>) => (
  <input {...p} className={cx(inputCls, p.type === "color" ? "h-9 cursor-pointer p-1" : "h-9", p.className)} />
);
export const TextArea = (p: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea rows={5} {...p} className={cx(inputCls, "font-[inherit] leading-relaxed", p.className)} />;
export function Select({ options, ...p }: React.SelectHTMLAttributes<HTMLSelectElement> & { options: (string | { value: string; label: string })[] }) {
  return (
    <select {...p} className={cx(inputCls, "select-native h-9 cursor-pointer py-0 pr-8", p.className)}>
      {options.map((o) => (typeof o === "string" ? <option key={o} value={o}>{o}</option> : <option key={o.value} value={o.value}>{o.label}</option>))}
    </select>
  );
}

export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd className={cx("inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-line bg-panel px-1 font-sans text-[11px] font-medium text-muted shadow-[0_1px_0_var(--line)]", className)}>
      {children}
    </kbd>
  );
}

export function PageHeader({ title, subtitle, right, badge, eyebrow }: { title: React.ReactNode; subtitle?: React.ReactNode; right?: React.ReactNode; badge?: React.ReactNode; eyebrow?: React.ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-x-4 gap-y-3 md:mb-7">
      <div className="min-w-0 max-w-3xl">
        {eyebrow && <div className="t-overline mb-2">{eyebrow}</div>}
        <h1 className="t-h1 flex flex-wrap items-center gap-2 text-ink">{title} {badge}</h1>
        {subtitle && <p className="mt-1.5 text-[14px] leading-relaxed text-muted">{subtitle}</p>}
      </div>
      {right && <div className="flex flex-wrap items-center gap-2 [&_select]:w-auto [&_select]:min-w-36">{right}</div>}
    </div>
  );
}

export function Stat({ label, value, hint, tone, demo, icon }: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "good" | "bad" | "warn"; demo?: boolean; icon?: React.ReactNode }) {
  return (
    <div className="card p-4">
      {/* fixed-height label row so values line up across a KPI row whether or not a DEMO badge is shown */}
      <div className="flex h-5 items-center justify-between gap-2 text-[12.5px] font-medium text-muted">
        <span className="flex min-w-0 items-center gap-1.5 truncate leading-tight [&_svg]:size-3.5">{icon}{label}</span>
        {demo && <DemoBadge />}
      </div>
      <div className={cx("mt-2.5 text-[26px] font-semibold leading-none tracking-[-0.02em] tabular-nums", tone === "good" && "text-good", tone === "bad" && "text-bad", tone === "warn" && "text-warn")}>{value}</div>
      {hint && <div className="mt-2 truncate text-[12px] text-muted">{hint}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- motion helpers
const EASE = [0.2, 0.8, 0.2, 1] as const;

/** Fades and lifts children in once; `i` staggers siblings. Respects reduced motion via <MotionConfig>. */
export function Reveal({ i = 0, children, className, as = "div", ...rest }: { i?: number; children: React.ReactNode; className?: string; as?: "div" | "li" | "section" } & Record<string, any>) {
  const C: any = as === "li" ? m.li : as === "section" ? m.section : m.div;
  return (
    <C initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.28, ease: EASE, delay: Math.min(i, 10) * 0.035 }} className={className} {...rest}>
      {children}
    </C>
  );
}

function useDialogFocus(open: boolean, ref: React.RefObject<HTMLElement>) {
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => {
      const el = ref.current;
      if (!el) return;
      const first = el.querySelector<HTMLElement>("[data-autofocus], input, textarea, select, button:not([data-close])");
      (first || el).focus({ preventScroll: true });
    }, 30);
    return () => {
      clearTimeout(t);
      prev?.focus?.({ preventScroll: true });
    };
  }, [open, ref]);
}

function trapTab(e: React.KeyboardEvent, root: HTMLElement | null) {
  if (e.key !== "Tab" || !root) return;
  const els = Array.from(root.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input, textarea, select, [tabindex]:not([tabindex="-1"])')).filter((el) => el.offsetParent !== null);
  if (!els.length) return;
  const first = els[0];
  const last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: React.ReactNode; children: React.ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  useDialogFocus(open, ref);
  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-6" role="dialog" aria-modal="true" aria-labelledby={id} onKeyDown={(e) => trapTab(e, ref.current)}>
          <m.div className="absolute inset-0 bg-[var(--overlay)] backdrop-blur-[2px]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.18 }} onClick={onClose} />
          <m.div
            ref={ref}
            tabIndex={-1}
            initial={{ opacity: 0, y: 14, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.985 }}
            transition={{ duration: 0.22, ease: EASE }}
            className={cx("relative flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-[var(--radius-xl)] border border-line bg-panel shadow-xl outline-none sm:rounded-[var(--radius-xl)]", wide ? "sm:max-w-4xl" : "sm:max-w-2xl")}
          >
            <div className="flex items-center justify-between gap-3 border-b border-line-2 px-4 py-3 sm:px-5">
              <h2 id={id} className="t-h3 min-w-0 text-ink">{title}</h2>
              <IconButton label="Close" size="icon-sm" onClick={onClose} data-close>
                <X className="size-4" />
              </IconButton>
            </div>
            <div className="scroll-thin overflow-auto p-4 sm:p-5">{children}</div>
          </m.div>
        </div>
      )}
    </AnimatePresence>
  );
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: React.ReactNode; children: React.ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  useDialogFocus(open, ref as React.RefObject<HTMLElement>);
  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-labelledby={id} onKeyDown={(e) => trapTab(e, ref.current)}>
          <m.div className="absolute inset-0 bg-[var(--overlay)]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }} onClick={onClose} />
          <m.aside
            ref={ref as any}
            tabIndex={-1}
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.28, ease: EASE }}
            className="relative flex h-full w-full max-w-xl flex-col border-l border-line bg-panel shadow-xl outline-none"
          >
            <div className="flex items-center justify-between gap-3 border-b border-line-2 px-4 py-3 sm:px-5">
              <h2 id={id} className="t-h3 text-ink">{title}</h2>
              <IconButton label="Close" size="icon-sm" onClick={onClose} data-close>
                <X className="size-4" />
              </IconButton>
            </div>
            <div className="scroll-thin flex-1 overflow-auto p-4 sm:p-5">{children}</div>
          </m.aside>
        </div>
      )}
    </AnimatePresence>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size="sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          /* clipboard unavailable: still show the text */
        }
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
    >
      {done ? <Check className="size-3.5 text-good" /> : <Copy className="size-3.5" />} {done ? "Copied" : label}
    </Button>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: { key: string; label: React.ReactNode }[]; value: string; onChange: (k: string) => void }) {
  const listRef = useRef<HTMLDivElement>(null);
  const [bar, setBar] = useState<{ x: number; w: number } | null>(null);
  // one indicator that slides between tabs (CSS transition on measured position; no layout-animation runtime)
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const measure = () => {
      const el = list.querySelector<HTMLElement>(`[data-tab="${CSS.escape(value)}"]`);
      setBar(el ? { x: el.offsetLeft + 8, w: Math.max(0, el.offsetWidth - 16) } : null);
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(list);
    return () => ro?.disconnect();
  }, [value, tabs.length]);
  const onKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    onChange(next.key);
    (e.currentTarget.parentElement?.querySelector(`[data-tab="${next.key}"]`) as HTMLElement | null)?.focus();
  };
  return (
    <div ref={listRef} className="scroll-none relative mb-5 flex gap-1 overflow-x-auto shadow-[inset_0_-1px_0_var(--line)]" role="tablist">
      {bar && (
        <span aria-hidden className="pointer-events-none absolute bottom-0 left-0 h-[2px] rounded-full bg-brand transition-[transform,width] duration-300 ease-[cubic-bezier(.2,.8,.2,1)]"
          style={{ width: bar.w, transform: `translateX(${bar.x}px)` }} />
      )}
      {tabs.map((t, i) => (
        <button
          key={t.key}
          type="button"
          role="tab"
          data-tab={t.key}
          aria-selected={value === t.key}
          tabIndex={value === t.key ? 0 : -1}
          onClick={() => onChange(t.key)}
          onKeyDown={(e) => onKey(e, i)}
          className={cx("focus-ring relative whitespace-nowrap rounded-t-md px-3 pb-2.5 pt-2 text-[13.5px] font-medium transition-colors focus-visible:-outline-offset-2", value === t.key ? "text-ink" : "text-muted hover:text-ink")}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Table({ cols, rows, empty = "Nothing here yet", onRow, selected }: { cols: { key: string; label: string; render?: (r: any) => React.ReactNode; className?: string }[]; rows: any[]; empty?: string; onRow?: (r: any) => void; selected?: (r: any) => boolean }) {
  if (!rows.length) return <Empty title={empty} />;
  return (
    <div className="scroll-thin -mx-1 overflow-x-auto px-1">
      <table className="w-full border-separate border-spacing-0 text-left text-[13.5px]">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.key} scope="col" className={cx("whitespace-nowrap border-b border-line px-3 pb-2 pt-1 text-[12px] font-medium text-muted first:pl-2", c.className)}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const on = selected?.(r) || false;
            return (
            <tr
              key={r.id ?? i}
              aria-selected={selected ? on : undefined}
              className={cx("group/row transition-colors [&:last-child>td]:border-b-0", onRow && "cursor-pointer hover:bg-hover focus-visible:bg-hover",
                on && "bg-brand-soft hover:bg-brand-soft [&>td:first-child]:shadow-[inset_2px_0_0_var(--brand-solid)]")}
              onClick={() => onRow?.(r)}
              onKeyDown={onRow ? (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onRow(r)) : undefined}
              tabIndex={onRow ? 0 : undefined}
            >
              {cols.map((c) => (
                <td key={c.key} className={cx("border-b border-line-2 px-3 py-2.5 align-top first:pl-2", c.className)}>{c.render ? c.render(r) : String(r[c.key] ?? "")}</td>
              ))}
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- citations
export function Citations({ items }: { items?: Citation[] }) {
  const { openSource } = useApp();
  if (!items || !items.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {items.map((c) => (
        <button
          key={c.id + c.file + (c.lines || "")}
          type="button"
          onClick={() => openSource(c)}
          className="focus-ring inline-flex h-6 max-w-full items-center gap-1 rounded-md border border-line bg-panel px-2 text-[11.5px] text-ink-2 shadow-xs transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-ink"
          title={c.snippet}
          data-testid="citation-chip"
        >
          <FileText className="size-3 shrink-0 text-brand" />
          <span className="shrink-0 font-semibold tabular-nums">[{c.id}]</span>
          <span className="min-w-0 truncate">{c.title || c.file}</span>
          {c.lines && <span className="shrink-0 whitespace-nowrap text-muted">L{c.lines}</span>}
          {c.ts && <span className="shrink-0 whitespace-nowrap text-muted">{c.ts}</span>}
        </button>
      ))}
    </div>
  );
}

export function SourceModal({ cite, onClose }: { cite: Citation | null; onClose: () => void }) {
  const path = cite && cite.kind !== "external" ? `/source?file=${encodeURIComponent(cite.file)}&lines=${encodeURIComponent(cite.lines || "")}` : null;
  const { data, error, loading, reload } = useApi<any>(path, [cite?.file, cite?.lines]);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current?.querySelector("[data-hit='1']");
    el?.scrollIntoView({ block: "center" });
  }, [data]);
  return (
    <Modal open={!!cite} onClose={onClose} title={<span className="flex items-center gap-2"><FileText className="size-4 text-brand" />{data?.title || cite?.title || cite?.file}</span>} wide>
      {cite?.kind === "external" ? (
        <p className="text-sm">External source (not course material): {cite.title} {cite.url}</p>
      ) : loading ? (
        <Loading rows={6} />
      ) : error ? (
        <ErrorBox error={error} retry={reload} />
      ) : data ? (
        <div>
          <p className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted">
            <span className="font-mono">{data.file}</span> · lines {data.start}–{data.end} {data.quality && data.quality !== "ok" && <Badge tone="warn">{data.quality} transcript</Badge>}
          </p>
          <div ref={ref} className="scroll-thin max-h-[60vh] overflow-auto rounded-[10px] border border-line bg-panel-2 py-2 font-mono text-[12px] leading-relaxed">
            {data.lines.map((l: any) => (
              <div key={l.n} data-hit={l.hit ? "1" : "0"} className={cx("flex gap-3 px-3", l.hit && "bg-brand-soft-2 shadow-[inset_2px_0_0_var(--brand)]")}>
                <span className="w-10 shrink-0 select-none text-right text-muted">{l.n}</span>
                <span className="prose-pre">{l.text}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

// ---------------------------------------------------------------- runs & traces
export type Step = { tool: string; input?: any; output?: any; ms?: number; status: string; label?: string };
export type Run = { id: number; agent: string; title: string; plan: string[]; steps: Step[]; result: any; status: string; ms: number; ai_mode: string; created: number };

export function StepIcon({ s }: { s: string }) {
  if (s === "ok" || s === "done") return <CircleCheck className="size-4 text-good" />;
  if (s === "error" || s === "failed") return <CircleX className="size-4 text-bad" />;
  if (s === "skipped") return <CircleDot className="size-4 text-muted" />;
  if (s === "running") return <Loader2 className="size-4 animate-spin text-brand" />;
  return <Clock className="size-4 text-faint" />;
}

export function AiModeBadge({ mode }: { mode?: string }) {
  if (!mode) return null;
  if (mode === "live") return <Badge tone="good">AI</Badge>;
  if (mode === "replay") return <Badge tone="info">Replayed AI</Badge>;
  return <Badge tone="neutral">Rules</Badge>;
}

export function RunSteps({ run, compact }: { run: Run; compact?: boolean }) {
  const [open, setOpen] = useState<number | null>(null);
  const planned = run.plan || [];
  const rows = planned.length ? planned.map((p, i) => run.steps[i] || { tool: p, status: run.status === "running" ? (i === run.steps.length ? "running" : "pending") : "pending" }) : run.steps;
  return (
    <ol className="space-y-1.5" data-testid="run-steps">
      {rows.map((s, i) => (
        <li key={i} className={cx("overflow-hidden rounded-[10px] border bg-panel-2 transition-colors", s.status === "running" ? "border-brand-line" : "border-line")}>
          <button type="button" className="focus-ring flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13.5px]" onClick={() => setOpen(open === i ? null : i)} data-status={s.status} aria-expanded={!compact ? open === i : undefined}>
            <StepIcon s={s.status} />
            <span className={cx("flex-1 font-medium", s.status === "pending" && "text-muted")}>{s.label || s.tool}</span>
            {s.ms !== undefined && s.ms !== null && <span className="tabular-nums text-xs text-muted">{s.ms} ms</span>}
            {!compact && <ChevronRight className={cx("size-4 text-muted transition-transform", open === i && "rotate-90")} />}
          </button>
          {!compact && open === i && (
            <div className="grid gap-2 border-t border-line px-3 py-2.5 text-xs md:grid-cols-2">
              <div>
                <div className="t-overline mb-1">Input</div>
                <pre className="prose-pre max-h-60 overflow-auto rounded-md border border-line-2 bg-panel p-2 font-mono">{JSON.stringify(s.input, null, 2)}</pre>
              </div>
              <div>
                <div className="t-overline mb-1">Output</div>
                <pre className="prose-pre max-h-60 overflow-auto rounded-md border border-line-2 bg-panel p-2 font-mono">{JSON.stringify(s.output, null, 2)}</pre>
              </div>
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

export function TraceDrawer({ runId, onClose }: { runId: number | null; onClose: () => void }) {
  const { data, error, loading, reload } = useApi<Run>(runId ? `/runs/${runId}` : null, [runId]);
  return (
    <Drawer open={!!runId} onClose={onClose} title={<span>Run #{runId} trace</span>}>
      {loading && <Loading rows={5} />}
      <ErrorBox error={error} retry={reload} />
      {data && (
        <div className="space-y-5" data-testid="trace">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge>{data.agent}</Badge>
            <AiModeBadge mode={data.ai_mode} />
            <Badge tone={data.status === "done" ? "good" : data.status === "error" ? "bad" : "info"} dot>{data.status}</Badge>
            <span className="tabular-nums text-muted">{data.ms} ms</span>
          </div>
          <p className="t-h3">{data.title}</p>
          <div>
            <div className="t-overline mb-1.5">Plan</div>
            <ol className="list-decimal space-y-0.5 pl-5 text-sm text-ink-2 marker:text-muted">{(data.plan || []).map((p, i) => <li key={i}>{p}</li>)}</ol>
          </div>
          <div>
            <div className="t-overline mb-1.5">Tool calls</div>
            <RunSteps run={data} />
          </div>
          <div>
            <div className="t-overline mb-1.5">Result</div>
            <Json data={data.result} />
          </div>
        </div>
      )}
    </Drawer>
  );
}

export function TraceLink({ runId }: { runId?: number | null }) {
  const { openTrace } = useApp();
  if (!runId) return null;
  return (
    <button type="button" className="focus-ring rounded text-xs font-medium text-brand underline-offset-2 hover:underline" onClick={() => openTrace(runId)} data-testid="trace-link">
      Trace #{runId}
    </button>
  );
}

// ---------------------------------------------------------------- guardrail flags
export type Flag = { code: string; severity: "block" | "review"; message: string; match?: string };
export function FlagList({ flags }: { flags?: Flag[] }) {
  if (!flags || !flags.length) return <Badge tone="good"><Check className="size-3" /> Guardrails clean</Badge>;
  return (
    <ul className="space-y-1.5">
      {flags.map((f, i) => (
        <li key={i} className={cx("rounded-lg border px-2.5 py-1.5 text-xs leading-relaxed", f.severity === "block" ? "border-bad-line bg-bad-soft text-bad-ink" : "border-warn-line bg-warn-soft text-warn-ink")}>
          <b className="font-semibold">{f.severity === "block" ? "Blocked" : "Needs a person"}:</b> {f.message} {f.match && <span className="font-mono">“{f.match}”</span>}
        </li>
      ))}
    </ul>
  );
}

export function Json({ data }: { data: unknown }) {
  return <pre className="prose-pre max-h-96 overflow-auto rounded-[10px] border border-line bg-panel-2 p-3 font-mono text-xs leading-relaxed text-ink-2">{JSON.stringify(data, null, 2)}</pre>;
}

export function Pill({ children, tone = "neutral", className }: { children: React.ReactNode; tone?: "neutral" | "good" | "muted"; className?: string }) {
  return (
    <span className={cx("inline-flex h-7 min-w-0 items-center gap-1.5 rounded-full border px-2.5 text-[12px] font-medium", tone === "good" ? "border-good-line bg-good-soft text-good-ink" : "border-line bg-panel text-muted", className)}>
      {children}
    </span>
  );
}

/** Segmented control (radio group) for small sets of mutually exclusive options. */
export function Segmented<T extends string>({ value, onChange, options, label }: { value: T; onChange: (v: T) => void; options: { value: T; label: React.ReactNode; icon?: React.ReactNode }[]; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-[10px] border border-line bg-panel-2 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cx("focus-ring inline-flex h-8 items-center gap-1.5 rounded-[8px] px-3 text-[13px] font-medium transition-colors [&_svg]:size-3.5", value === o.value ? "bg-panel text-ink shadow-sm ring-1 ring-line" : "text-muted hover:text-ink")}
        >
          {o.icon}
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Initials avatar; colour is derived from the brand so it stays on-palette for every tenant. */
export function Avatar({ name, className, small }: { name: string; className?: string; small?: boolean }) {
  const initials = name.replace(/\(.*?\)/g, "").trim().split(/\s+/).map((w) => w[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() || "?";
  return <span aria-hidden className={cx("grid shrink-0 place-items-center rounded-full bg-brand-soft-2 font-semibold text-brand-fg ring-1 ring-brand-line", small ? "size-6 text-[10px]" : "size-7 text-[11px]", className)}>{initials}</span>;
}
