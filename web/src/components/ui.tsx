import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, ChevronRight, Copy, FileText, Loader2, RotateCw, X, CircleDot, CircleCheck, CircleX, Clock } from "lucide-react";
import { ApiError, get } from "../api";

// ---------------------------------------------------------------- app context
export type Me = {
  user: { id: number; username: string; display: string; role: string };
  tabs: string[];
  tenant: { id: string; name: string; kind: string; is_demo: boolean; kb_scope: string };
  brand: { name?: string; primary?: string; accent?: string; owner_display?: string };
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

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: "primary" | "secondary" | "ghost" | "danger" | "good"; loading?: boolean; size?: "sm" | "md" };
export function Button({ tone = "secondary", loading, size = "md", className, children, disabled, ...rest }: BtnProps) {
  const tones: Record<string, string> = {
    primary: "bg-brand text-white hover:brightness-110 border-transparent",
    secondary: "bg-panel text-ink border-line hover:bg-panel-2",
    ghost: "bg-transparent text-ink border-transparent hover:bg-black/5",
    danger: "bg-panel text-bad border-line hover:bg-red-50",
    good: "bg-good text-white border-transparent hover:brightness-110",
  };
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cx(
        "focus-ring inline-flex items-center justify-center gap-1.5 rounded-lg border font-medium transition disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap",
        size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-2 text-sm",
        tones[tone],
        className,
      )}
    >
      {loading && <Loader2 className="size-4 animate-spin" />}
      {children}
    </button>
  );
}

export function Card({ title, subtitle, actions, children, className, pad = true }: { title?: React.ReactNode; subtitle?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={cx("card", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-2 border-b border-line px-4 py-3">
          <div className="min-w-0">
            {title && <h3 className="text-[15px] font-semibold leading-tight">{title}</h3>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </section>
  );
}

export function Badge({ tone = "neutral", children, className, title }: { tone?: "neutral" | "good" | "bad" | "warn" | "brand" | "demo" | "info"; children: React.ReactNode; className?: string; title?: string }) {
  const tones: Record<string, string> = {
    neutral: "bg-black/5 text-ink/80",
    good: "bg-green-100 text-green-800",
    bad: "bg-red-100 text-red-800",
    warn: "bg-amber-100 text-amber-900",
    brand: "bg-[color-mix(in_oklab,var(--brand)_14%,white)] text-[var(--brand)]",
    demo: "bg-violet-100 text-violet-800",
    info: "bg-sky-100 text-sky-800",
  };
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-semibold tracking-wide", tones[tone], className)}>
      {children}
    </span>
  );
}

export const DemoBadge = () => <Badge tone="demo" title="Fictional demo data">DEMO</Badge>;

export function isDemo(v: unknown): boolean {
  if (!v) return false;
  if (typeof v === "string") return v.includes("(DEMO)");
  if (typeof v === "object") return !!(v as any).demo || JSON.stringify(v).includes("(DEMO)");
  return false;
}

export function StatusBadge({ s }: { s: string }) {
  const tone = s === "REAL_OK" ? "good" : s === "NEEDS_LOGIN" ? "warn" : s === "NEEDS_AI_BUDGET" ? "info" : "neutral";
  return <Badge tone={tone as any}>{s}</Badge>;
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-5" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function ErrorBox({ error, retry }: { error: ApiError | Error | string | null; retry?: () => void }) {
  if (!error) return null;
  const msg = typeof error === "string" ? error : error.message;
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
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

export function Empty({ title, hint, icon, action }: { title: string; hint?: React.ReactNode; icon?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line px-6 py-10 text-center">
      <div className="text-muted">{icon || <CircleDot className="size-6" />}</div>
      <p className="font-medium">{title}</p>
      {hint && <p className="max-w-md text-xs text-muted">{hint}</p>}
      {action}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-muted">{hint}</span>}
    </label>
  );
}

const inputCls = "focus-ring w-full rounded-lg border border-line bg-white px-3 py-2 text-sm placeholder:text-muted/60";
export const Input = (p: React.InputHTMLAttributes<HTMLInputElement>) => <input {...p} className={cx(inputCls, p.className)} />;
export const TextArea = (p: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea rows={5} {...p} className={cx(inputCls, "font-[inherit]", p.className)} />;
export function Select({ options, ...p }: React.SelectHTMLAttributes<HTMLSelectElement> & { options: (string | { value: string; label: string })[] }) {
  return (
    <select {...p} className={cx(inputCls, "pr-8", p.className)}>
      {options.map((o) => (typeof o === "string" ? <option key={o} value={o}>{o}</option> : <option key={o.value} value={o.value}>{o.label}</option>))}
    </select>
  );
}

export function PageHeader({ title, subtitle, right, badge }: { title: string; subtitle?: React.ReactNode; right?: React.ReactNode; badge?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="flex flex-wrap items-center gap-2 text-[22px] font-semibold tracking-tight">{title} {badge}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-muted">{subtitle}</p>}
      </div>
      {right && <div className="flex flex-wrap items-center gap-2">{right}</div>}
    </div>
  );
}

export function Stat({ label, value, hint, tone, demo }: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: "good" | "bad" | "warn"; demo?: boolean }) {
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between gap-2 text-xs font-medium text-muted">
        <span>{label}</span>
        {demo && <DemoBadge />}
      </div>
      <div className={cx("mt-1 text-2xl font-semibold tabular-nums", tone === "good" && "text-good", tone === "bad" && "text-bad", tone === "warn" && "text-warn")}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
    </div>
  );
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: React.ReactNode; children: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-6" onClick={onClose} role="dialog" aria-modal="true">
      <div className={cx("card flex max-h-[92vh] w-full flex-col overflow-hidden shadow-2xl", wide ? "sm:max-w-4xl" : "sm:max-w-2xl")} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0 font-semibold">{title}</div>
          <Button tone="ghost" size="sm" onClick={onClose} aria-label="Close">
            <X className="size-4" />
          </Button>
        </div>
        <div className="scroll-thin overflow-auto p-4">{children}</div>
      </div>
    </div>
  );
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: React.ReactNode; children: React.ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <aside className="flex h-full w-full max-w-xl flex-col bg-panel shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <div className="font-semibold">{title}</div>
          <Button tone="ghost" size="sm" onClick={onClose} aria-label="Close">
            <X className="size-4" />
          </Button>
        </div>
        <div className="scroll-thin flex-1 overflow-auto p-4">{children}</div>
      </aside>
    </div>
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
      {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />} {done ? "Copied" : label}
    </Button>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: { key: string; label: React.ReactNode }[]; value: string; onChange: (k: string) => void }) {
  return (
    <div className="scroll-thin mb-4 flex gap-1 overflow-x-auto border-b border-line" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.key}
          role="tab"
          aria-selected={value === t.key}
          onClick={() => onChange(t.key)}
          className={cx("focus-ring -mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm", value === t.key ? "border-brand font-semibold text-ink" : "border-transparent text-muted hover:text-ink")}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Table({ cols, rows, empty = "Nothing here yet", onRow }: { cols: { key: string; label: string; render?: (r: any) => React.ReactNode; className?: string }[]; rows: any[]; empty?: string; onRow?: (r: any) => void }) {
  if (!rows.length) return <Empty title={empty} />;
  return (
    <div className="scroll-thin overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-line text-xs text-muted">
            {cols.map((c) => (
              <th key={c.key} className={cx("px-2 py-2 font-medium", c.className)}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? i} className={cx("border-b border-line/70 last:border-0", onRow && "cursor-pointer hover:bg-panel-2")} onClick={() => onRow?.(r)}>
              {cols.map((c) => (
                <td key={c.key} className={cx("px-2 py-2 align-top", c.className)}>{c.render ? c.render(r) : String(r[c.key] ?? "")}</td>
              ))}
            </tr>
          ))}
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
          onClick={() => openSource(c)}
          className="focus-ring inline-flex max-w-full items-center gap-1 rounded-md border border-line bg-panel-2 px-2 py-0.5 text-[11px] text-ink hover:border-brand"
          title={c.snippet}
          data-testid="citation-chip"
        >
          <FileText className="size-3 text-brand" />
          <span className="font-semibold">[{c.id}]</span>
          <span className="truncate">{c.title || c.file}</span>
          {c.lines && <span className="text-muted">L{c.lines}</span>}
          {c.ts && <span className="text-muted">{c.ts}</span>}
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
    <Modal open={!!cite} onClose={onClose} title={<span className="flex items-center gap-2"><FileText className="size-4" />{data?.title || cite?.title || cite?.file}</span>} wide>
      {cite?.kind === "external" ? (
        <p className="text-sm">External source (not course material): {cite.title} {cite.url}</p>
      ) : loading ? (
        <Loading rows={6} />
      ) : error ? (
        <ErrorBox error={error} retry={reload} />
      ) : data ? (
        <div>
          <p className="mb-2 text-xs text-muted">
            {data.file} · lines {data.start}–{data.end} {data.quality && data.quality !== "ok" && <Badge tone="warn">{data.quality} transcript</Badge>}
          </p>
          <div ref={ref} className="scroll-thin max-h-[60vh] overflow-auto rounded-lg border border-line bg-panel-2 font-mono text-[12px] leading-relaxed">
            {data.lines.map((l: any) => (
              <div key={l.n} data-hit={l.hit ? "1" : "0"} className={cx("flex gap-3 px-3", l.hit && "bg-[color-mix(in_oklab,var(--brand)_12%,white)]")}>
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
  return <Clock className="size-4 text-muted" />;
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
    <ol className="space-y-1" data-testid="run-steps">
      {rows.map((s, i) => (
        <li key={i} className="rounded-lg border border-line bg-panel-2">
          <button className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm" onClick={() => setOpen(open === i ? null : i)} data-status={s.status}>
            <StepIcon s={s.status} />
            <span className="flex-1 font-medium">{s.label || s.tool}</span>
            {s.ms !== undefined && s.ms !== null && <span className="tabular-nums text-xs text-muted">{s.ms} ms</span>}
            {!compact && <ChevronRight className={cx("size-4 text-muted transition", open === i && "rotate-90")} />}
          </button>
          {!compact && open === i && (
            <div className="grid gap-2 border-t border-line px-3 py-2 text-xs md:grid-cols-2">
              <div>
                <div className="mb-1 font-semibold text-muted">Input</div>
                <pre className="prose-pre max-h-60 overflow-auto rounded bg-white p-2 font-mono">{JSON.stringify(s.input, null, 2)}</pre>
              </div>
              <div>
                <div className="mb-1 font-semibold text-muted">Output</div>
                <pre className="prose-pre max-h-60 overflow-auto rounded bg-white p-2 font-mono">{JSON.stringify(s.output, null, 2)}</pre>
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
        <div className="space-y-4" data-testid="trace">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge>{data.agent}</Badge>
            <AiModeBadge mode={data.ai_mode} />
            <Badge tone={data.status === "done" ? "good" : data.status === "error" ? "bad" : "info"}>{data.status}</Badge>
            <span className="text-muted">{data.ms} ms</span>
          </div>
          <p className="font-medium">{data.title}</p>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Plan</div>
            <ol className="list-decimal pl-5 text-sm">{(data.plan || []).map((p, i) => <li key={i}>{p}</li>)}</ol>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Tool calls</div>
            <RunSteps run={data} />
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Result</div>
            <pre className="prose-pre max-h-96 overflow-auto rounded-lg border border-line bg-panel-2 p-2 font-mono text-xs">{JSON.stringify(data.result, null, 2)}</pre>
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
    <button className="focus-ring text-xs text-brand underline-offset-2 hover:underline" onClick={() => openTrace(runId)} data-testid="trace-link">
      Trace #{runId}
    </button>
  );
}

// ---------------------------------------------------------------- guardrail flags
export type Flag = { code: string; severity: "block" | "review"; message: string; match?: string };
export function FlagList({ flags }: { flags?: Flag[] }) {
  if (!flags || !flags.length) return <Badge tone="good">Guardrails: clean</Badge>;
  return (
    <ul className="space-y-1">
      {flags.map((f, i) => (
        <li key={i} className={cx("rounded-md px-2 py-1 text-xs", f.severity === "block" ? "bg-red-50 text-red-900" : "bg-amber-50 text-amber-900")}>
          <b>{f.severity === "block" ? "Blocked" : "Needs a person"}:</b> {f.message} {f.match && <span className="font-mono">“{f.match}”</span>}
        </li>
      ))}
    </ul>
  );
}

export function Json({ data }: { data: unknown }) {
  return <pre className="prose-pre max-h-96 overflow-auto rounded-lg border border-line bg-panel-2 p-2 font-mono text-xs">{JSON.stringify(data, null, 2)}</pre>;
}

export function Pill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "good" | "muted" }) {
  return <span className={cx("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium", tone === "good" ? "border-green-200 bg-green-50 text-green-800" : "border-line bg-panel text-muted")}>{children}</span>;
}
