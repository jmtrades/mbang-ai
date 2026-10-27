import { useEffect, useState } from "react";
import { ArrowRight, CircleCheck, Circle, CircleDashed, Info, Palette, Terminal, TriangleAlert } from "lucide-react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, CopyButton, DemoBadge, ErrorBox, Field, Input, Loading, PageHeader, cx, useApi, useApp } from "../components/ui";

type Next = { label: string; href?: string; command?: string } | null;
type Item = { key: string; title: string; status: "done" | "todo" | "warn" | "optional" | "info"; why: string; detail: string; next: Next; required: boolean; data: any };
type Checklist = { items: Item[]; done: number; total: number; label: string; is_demo: boolean; tenant: string };

const STATUS: Record<Item["status"], { label: string; tone: "good" | "warn" | "bad" | "neutral" | "info"; Icon: any; cls: string }> = {
  done: { label: "Done", tone: "good", Icon: CircleCheck, cls: "text-good" },
  todo: { label: "To do", tone: "warn", Icon: Circle, cls: "text-muted" },
  warn: { label: "Needs attention", tone: "bad", Icon: TriangleAlert, cls: "text-bad" },
  optional: { label: "Optional", tone: "neutral", Icon: CircleDashed, cls: "text-muted" },
  info: { label: "For your information", tone: "info", Icon: Info, cls: "text-sky-600" },
};

function Command({ text }: { text: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel-2 px-2.5 py-1.5">
      <Terminal className="size-3.5 shrink-0 text-muted" />
      <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{text}</code>
      <CopyButton text={text} />
    </div>
  );
}

function BrandEditor({ brand, onSaved }: { brand: any; onSaved: () => void }) {
  const { toast, reloadMe } = useApp();
  const [b, setB] = useState({ name: brand?.name || "", owner_display: brand?.owner_display || "", primary: brand?.primary || "#5b6cff", accent: brand?.accent || "#16a34a", logo: brand?.logo || "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      await post("/setup/brand", b);
      toast("Brand saved", "good");
      reloadMe();
      onSaved();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-3 space-y-3 rounded-lg border border-line p-3" data-testid="brand-editor">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Programme name"><Input value={b.name} maxLength={60} onChange={(e) => setB({ ...b, name: e.target.value })} /></Field>
        <Field label="How you're shown to your team"><Input value={b.owner_display} maxLength={60} onChange={(e) => setB({ ...b, owner_display: e.target.value })} /></Field>
        <Field label="Main colour"><Input type="color" value={b.primary} onChange={(e) => setB({ ...b, primary: e.target.value })} className="h-10 p-1" /></Field>
        <Field label="Accent colour"><Input type="color" value={b.accent} onChange={(e) => setB({ ...b, accent: e.target.value })} className="h-10 p-1" /></Field>
      </div>
      <Field label="Logo" hint="An https:// link to your logo image. It shows on sign-in and in the menu.">
        <Input value={b.logo} maxLength={500} placeholder="https://…/logo.png" onChange={(e) => setB({ ...b, logo: e.target.value.trim() })} />
      </Field>
      {b.logo.startsWith("https://") && <img src={b.logo} alt="Logo preview" className="h-10 max-w-[12rem] rounded border border-line object-contain" />}
      <ErrorBox error={err} />
      <Button tone="primary" onClick={save} loading={busy} disabled={!b.name.trim()}><Palette className="size-4" /> Save brand</Button>
    </div>
  );
}

function Extra({ it, reload }: { it: Item; reload: () => void }) {
  const { toast } = useApp();
  const [err, setErr] = useState<ApiError | null>(null);
  const decide = async (item: string, value: string) => {
    setErr(null);
    try {
      await post("/setup/decide", { item, value });
      toast("Saved", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  if (it.key === "material") {
    const d = it.data || {};
    return (
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        {[["Files", d.files], ["Words", d.words], ["Transcripts", d.transcripts], ["With speaker labels", d.transcripts_with_speakers]].map(([k, v]) => (
          <div key={k as string} className="rounded-lg border border-line px-2.5 py-1.5"><div className="text-muted">{k}</div><div className="text-base font-semibold tabular-nums">{Number(v || 0).toLocaleString()}</div></div>
        ))}
      </div>
    );
  }
  if (it.key === "live" && it.data?.sentences) {
    return <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs">{it.data.sentences.map((s: string) => <li key={s}>{s}</li>)}</ul>;
  }
  if (it.key === "integrations" && it.data?.items) {
    return (
      <div className="mt-2 flex flex-wrap gap-1.5">
        {it.data.items.map((i: any) => <Badge key={i.name} tone={i.connected ? "good" : "neutral"}>{i.name}{i.connected ? "" : ": not connected"}</Badge>)}
      </div>
    );
  }
  if (it.key === "persona" && it.status !== "done") {
    return (
      <div className="mt-2 flex flex-wrap gap-2">
        <ErrorBox error={err} />
        <Button size="sm" onClick={() => decide("persona", "use")}>I'll set one up</Button>
        <Button size="sm" tone="ghost" onClick={() => decide("persona", "not_now")}>Not now</Button>
      </div>
    );
  }
  if (it.key === "backups" && it.status !== "done") {
    return (
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <ErrorBox error={err} />
        <Button size="sm" tone="ghost" onClick={() => decide("backups", "configured_elsewhere")}>Backups are handled outside this server</Button>
      </div>
    );
  }
  if (it.key === "preflight" && it.data?.rows) {
    return (
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-brand">Show all {it.data.rows.length} checks</summary>
        <ul className="mt-2 space-y-1">
          {it.data.rows.map((r: any) => (
            <li key={r.name} className="flex flex-wrap items-start gap-2">
              <Badge tone={r.ok ? "good" : "neutral"}>{r.ok ? "OK" : "Missing"}</Badge>
              <span className="min-w-0 flex-1">{r.name}{!r.ok && r.need ? <span className="block break-all text-muted">{r.need}</span> : null}</span>
            </li>
          ))}
        </ul>
      </details>
    );
  }
  return null;
}

export default function Setup({ query }: { query?: URLSearchParams }) {
  const { me } = useApp();
  const { data, error, loading, reload } = useApi<Checklist>("/setup");
  const wantBrand = query?.get("brand") === "1";
  const [brandOpen, setBrandOpen] = useState(wantBrand);
  useEffect(() => { if (wantBrand) setBrandOpen(true); }, [wantBrand]);
  const pct = data ? Math.round((data.done / Math.max(1, data.total)) * 100) : 0;
  return (
    <div>
      <PageHeader title="Workspace setup" badge={me.is_demo ? <DemoBadge /> : null}
        subtitle="From an empty workspace to a real one. Each step checks itself from your workspace and tells you the exact next action. Keys and live switches are only ever changed on the server."
        right={data ? <Badge tone={data.done === data.total ? "good" : "info"}>{data.done} of {data.total} required steps done</Badge> : null} />
      <ErrorBox error={error} retry={reload} />
      {loading && !data && <Loading rows={8} />}
      {data && (
        <>
          <div className="mb-5 h-2 overflow-hidden rounded-full bg-black/5" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Setup progress">
            <div className="h-full rounded-full bg-brand transition-all" style={{ width: `${pct}%` }} />
          </div>
          <ol className="space-y-3" data-testid="setup-checklist">
            {data.items.map((it) => {
              const st = STATUS[it.status];
              return (
                <li key={it.key}>
                  <Card className={cx(it.status === "warn" && "border-red-200")}>
                    <div className="flex items-start gap-3">
                      <st.Icon className={cx("mt-0.5 size-5 shrink-0", st.cls)} aria-hidden />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="text-[15px] font-semibold">{it.title}</h2>
                          <Badge tone={st.tone}>{st.label}</Badge>
                        </div>
                        <p className="mt-0.5 text-sm text-muted">{it.why}</p>
                        {it.detail && <p className={cx("mt-1.5 text-sm", it.status === "warn" && "font-medium text-red-900")}>{it.detail}</p>}
                        <Extra it={it} reload={reload} />
                        {it.next && it.status !== "done" && (
                          <div className="mt-2.5">
                            {it.key === "brand" ? (
                              <Button size="sm" onClick={() => setBrandOpen(!brandOpen)}><Palette className="size-3.5" /> {brandOpen ? "Close the brand editor" : it.next.label}</Button>
                            ) : it.next.href ? (
                              <a href={it.next.href} className="focus-ring inline-flex items-center gap-1 rounded-lg border border-line bg-panel px-2.5 py-1 text-xs font-medium hover:bg-panel-2">{it.next.label} <ArrowRight className="size-3.5" /></a>
                            ) : it.next.command ? (
                              <div><div className="mb-1 text-xs font-medium text-muted">{it.next.label}</div><Command text={it.next.command} /></div>
                            ) : null}
                          </div>
                        )}
                        {it.key === "brand" && it.status === "done" && !brandOpen && (
                          <Button size="sm" tone="ghost" className="mt-2" onClick={() => setBrandOpen(true)}><Palette className="size-3.5" /> Edit brand</Button>
                        )}
                        {it.key === "brand" && brandOpen && <BrandEditor brand={it.data?.brand} onSaved={reload} />}
                      </div>
                    </div>
                  </Card>
                </li>
              );
            })}
          </ol>
          <p className="mt-4 text-xs text-muted">{data.label}.</p>
        </>
      )}
    </div>
  );
}
