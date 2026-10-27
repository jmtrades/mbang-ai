import { useEffect, useState } from "react";
import { ArrowLeft, BookOpenCheck, Check, ExternalLink, Info, Library, Search, X } from "lucide-react";
import { ApiError, put } from "../api";
import { Badge, Button, Card, Empty, ErrorBox, Input, Loading, PageHeader, Stat, cx, useApi, useApp } from "../components/ui";

type Summary = {
  id: string; title: string; category: string; domain: string; domain_title: string; as_of: string;
  confidence: string; snippet: string; n_sources?: number; n_benchmarks?: number;
};
type Source = { title: string; url: string; publisher?: string; date?: string };
type Entry = Summary & {
  researched: string; guidance: string; steps: string[]; do: string[]; dont: string[];
  benchmarks: { metric: string; value: string; context?: string }[]; sources: Source[]; disclaimer: string;
};
type Domain = { domain: string; title: string; researched: string; count: number; as_of_range: string[]; confidence: Record<string, number> };

const CONF_TONE: Record<string, "good" | "warn" | "bad"> = { high: "good", medium: "warn", low: "bad" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthYear(d?: string | null) {
  const m = /^(\d{4})-(\d{2})/.exec(d || "");
  return m ? `${MONTHS[Number(m[2]) - 1] || m[2]} ${m[1]}` : "—";
}

function ConfidenceBadge({ c }: { c: string }) {
  return <Badge tone={CONF_TONE[c] || "neutral"} title="How strong and independent the public evidence is">{c} confidence</Badge>;
}

function AsOfBadge({ d }: { d: string }) {
  return <Badge tone="info" title="When this guidance was last checked against its sources">as of {d}</Badge>;
}

function Banner({ enabled, owner }: { enabled: boolean; owner: boolean }) {
  return (
    <div className="mb-4 flex gap-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2.5 text-sm text-sky-950" data-testid="playbooks-banner">
      <Info className="mt-0.5 size-4 shrink-0 text-sky-700" />
      <div className="min-w-0 space-y-0.5">
        <p className="font-medium">Researched public best practice, dated; your course material always comes first.</p>
        <p className="text-xs text-sky-900/80">
          Not the programme's course material. Written in our own words from public sources, each entry dated and sourced; check the sources before acting.
          {enabled
            ? " In coach answers these appear only as separate “External · 2026” [X#] chips, after the course answer."
            : owner ? " While the switch is off, the coach never uses them." : ""}
        </p>
      </div>
    </div>
  );
}

function EntryRow({ e, active, onOpen }: { e: Summary; active: boolean; onOpen: () => void }) {
  return (
    <button onClick={onOpen} data-testid="playbook-entry"
      className={cx("focus-ring block w-full rounded-lg border px-3 py-2 text-left hover:border-brand", active ? "border-brand bg-panel-2" : "border-line bg-white")}>
      <div className="break-words text-sm font-medium leading-snug">{e.title}</div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        <AsOfBadge d={e.as_of} />
        <ConfidenceBadge c={e.confidence} />
        <span className="text-[11px] text-muted">{e.category.replace(/-/g, " ")}</span>
      </div>
      {e.snippet && <p className="mt-1 line-clamp-2 text-xs text-muted">{e.snippet}</p>}
    </button>
  );
}

function EntryDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { data: e, error, loading, reload } = useApi<Entry>(`/playbooks/entry/${encodeURIComponent(id)}`, [id]);
  return (
    <Card
      title={e ? <span className="break-words">{e.title}</span> : "Loading entry…"}
      subtitle={e ? `${e.domain_title} · ${e.category.replace(/-/g, " ")}` : undefined}
      actions={<Button size="sm" tone="ghost" onClick={onBack} aria-label="Back to the list"><ArrowLeft className="size-3.5" /> Back</Button>}
    >
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {e && (
        <div className="space-y-5 text-sm" data-testid="playbook-detail">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone="info">External · 2026</Badge>
            <AsOfBadge d={e.as_of} />
            <ConfidenceBadge c={e.confidence} />
            <span className="text-[11px] text-muted">researched {e.researched}</span>
          </div>
          <p className="leading-relaxed">{e.guidance}</p>

          {e.steps.length > 0 && (
            <section>
              <h4 className="mb-1.5 font-semibold">Steps</h4>
              <ol className="list-decimal space-y-1 pl-5">{e.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </section>
          )}

          {(e.do.length > 0 || e.dont.length > 0) && (
            <section className="grid gap-3 md:grid-cols-2">
              <div className="rounded-lg border border-green-200 bg-green-50/60 p-3">
                <h4 className="mb-1.5 flex items-center gap-1.5 font-semibold text-green-900"><Check className="size-4" /> Do</h4>
                {e.do.length ? <ul className="space-y-1">{e.do.map((s, i) => <li key={i} className="flex gap-1.5"><span aria-hidden className="text-green-700">•</span><span>{s}</span></li>)}</ul>
                  : <p className="text-xs text-muted">No specific do's listed.</p>}
              </div>
              <div className="rounded-lg border border-red-200 bg-red-50/60 p-3">
                <h4 className="mb-1.5 flex items-center gap-1.5 font-semibold text-red-900"><X className="size-4" /> Don't</h4>
                {e.dont.length ? <ul className="space-y-1">{e.dont.map((s, i) => <li key={i} className="flex gap-1.5"><span aria-hidden className="text-red-700">•</span><span>{s}</span></li>)}</ul>
                  : <p className="text-xs text-muted">No specific don'ts listed.</p>}
              </div>
            </section>
          )}

          {e.benchmarks.length > 0 && (
            <section>
              <h4 className="mb-1.5 font-semibold">Benchmarks</h4>
              {/* wraps inside the card (the shared table style keeps tables at max-content width) */}
              <table className="hidden w-full table-fixed text-left text-sm sm:table" style={{ minWidth: 0 }} data-testid="playbook-benchmarks">
                <thead>
                  <tr className="border-b border-line text-xs text-muted">
                    <th className="w-[38%] px-2 py-2 font-medium">Metric</th>
                    <th className="w-[24%] px-2 py-2 font-medium">Value</th>
                    <th className="px-2 py-2 font-medium">Context</th>
                  </tr>
                </thead>
                <tbody>
                  {e.benchmarks.map((b, i) => (
                    <tr key={i} className="border-b border-line/70 last:border-0">
                      <td className="break-words px-2 py-2 align-top">{b.metric}</td>
                      <td className="break-words px-2 py-2 align-top font-medium">{b.value}</td>
                      <td className="break-words px-2 py-2 align-top text-xs text-muted">{b.context || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <ul className="space-y-2 sm:hidden">
                {e.benchmarks.map((b, i) => (
                  <li key={i} className="rounded-lg border border-line px-3 py-2">
                    <div className="text-xs text-muted">{b.metric}</div>
                    <div className="font-medium">{b.value}</div>
                    {b.context && <div className="mt-0.5 text-[11px] text-muted">{b.context}</div>}
                  </li>
                ))}
              </ul>
              <p className="mt-1 text-[11px] text-muted">Public figures with their context. Directional only; build your own baseline from your data.</p>
            </section>
          )}

          <section>
            <h4 className="mb-1.5 font-semibold">Sources</h4>
            <ul className="space-y-1.5">
              {e.sources.map((s, i) => (
                <li key={i} className="min-w-0">
                  <a href={s.url} target="_blank" rel="noopener noreferrer" className="focus-ring inline-flex items-start gap-1 break-words text-brand hover:underline" data-testid="playbook-source">
                    <span className="break-words">{s.title}</span><ExternalLink className="mt-0.5 size-3.5 shrink-0" aria-label="opens in a new tab" />
                  </a>
                  <div className="text-[11px] text-muted">{[s.publisher, s.date].filter(Boolean).join(" · ")}</div>
                </li>
              ))}
            </ul>
          </section>
          <p className="border-t border-line pt-3 text-[11px] text-muted">{e.disclaimer}</p>
        </div>
      )}
    </Card>
  );
}

export default function Playbooks({ query }: { query?: URLSearchParams }) {
  const { toast } = useApp();
  const ov = useApi<any>("/playbooks");
  const [domain, setDomain] = useState<string>(query?.get("domain") || "");
  const [entryId, setEntryId] = useState<string>(query?.get("entry") || "");
  const [q, setQ] = useState<string>(query?.get("q") || "");
  const [term, setTerm] = useState<string>(query?.get("q") || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);

  const linkedEntry = query?.get("entry") || "";
  useEffect(() => {
    if (linkedEntry) setEntryId(linkedEntry);
  }, [linkedEntry]);
  useEffect(() => {
    if (!domain && ov.data?.domains?.length) setDomain(ov.data.domains[0].domain);
  }, [ov.data, domain]);
  useEffect(() => {
    const t = setTimeout(() => setTerm(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  const browse = !!ov.data?.can_browse;
  const searching = term.length >= 2;
  const dom = useApi<any>(browse && domain && !searching ? `/playbooks/domain/${encodeURIComponent(domain)}` : null, [domain, browse, searching]);
  const found = useApi<any>(browse && searching ? `/playbooks/search?q=${encodeURIComponent(term)}` : null, [term, browse]);

  const toggle = async () => {
    if (!ov.data) return;
    setBusy(true);
    setErr(null);
    try {
      await put("/settings", { key: ov.data.switch, value: !ov.data.enabled });
      toast(ov.data.enabled ? "Playbooks switched off: the coach no longer uses them." : "Playbooks switched on: coach answers can add external [X#] best practice.", "good");
      ov.reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const d = ov.data;
  const list: Summary[] = searching ? found.data?.items || [] : dom.data?.entries || [];
  const listLoading = searching ? found.loading : dom.loading;
  const listError = searching ? found.error : dom.error;
  const currentDomain: Domain | undefined = d?.domains?.find((x: Domain) => x.domain === domain);

  return (
    <div>
      <PageHeader
        title="2026 Playbooks"
        subtitle="Researched, dated and sourced public best practice for growth operators: Meta ads, outreach, sales, webinars, fulfilment, content and compliance. A separate library, never mixed into the course material."
        badge={d ? <Badge tone={d.enabled ? "good" : "neutral"} title={`Workspace setting ${d.switch}`}>{d.enabled ? "On in coach answers" : "Off in coach answers"}</Badge> : undefined}
        right={d?.can_toggle ? (
          <Button tone={d.enabled ? "secondary" : "primary"} loading={busy} onClick={toggle} data-testid="playbooks-toggle">
            {d.enabled ? "Turn off for coach answers" : "Turn on for coach answers"}
          </Button>
        ) : undefined}
      />
      <Banner enabled={!!d?.enabled} owner={!!d?.can_toggle} />
      <ErrorBox error={err || ov.error} retry={err ? () => setErr(null) : ov.reload} />
      {ov.loading && !d && <Loading rows={6} />}

      {d && !d.can_browse && (
        <Card>
          <Empty icon={<BookOpenCheck className="size-6" />} title="Switched off for this workspace" hint={d.off_message} />
        </Card>
      )}

      {d && d.can_browse && (
        <>
          {d.off_message && (
            <p className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-950" data-testid="playbooks-off-note">{d.off_message}</p>
          )}
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Entries" value={d.total} hint={`${d.domains.length} domains`} />
            <Stat label="Researched" value={monthYear(d.latest_researched)} hint={d.latest_researched ? `${d.latest_researched}${d.researched.length > 1 ? ` · ${d.researched.length} research dates` : ""}` : "not dated"} />
            <Stat label="High confidence" value={d.confidence.high} hint={`${d.confidence.medium} medium · ${d.confidence.low} low`} />
            <Stat label="Coach answers" value={d.enabled ? "On" : "Off"} hint={d.enabled ? "as separate [X#] chips" : "not used"} />
          </div>

          {d.total === 0 ? (
            <Card><Empty icon={<Library className="size-6" />} title="No playbooks loaded" hint="The playbook files are missing or didn't pass validation. Nothing is shown rather than half-checked guidance." /></Card>
          ) : (
            <div className="grid gap-4 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
              <div className={cx("min-w-0 space-y-3", entryId && "hidden lg:block")}>
                <form className="relative" onSubmit={(e) => { e.preventDefault(); setTerm(q.trim()); }} role="search">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                  <Input className="pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search: speed to lead, show rate, warm-up, refunds…" aria-label="Search the playbooks" />
                </form>
                {!searching && (
                  <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Domains">
                    {d.domains.map((x: Domain) => (
                      <button key={x.domain} role="tab" aria-selected={x.domain === domain} onClick={() => setDomain(x.domain)}
                        className={cx("focus-ring rounded-full border px-2.5 py-1 text-xs", x.domain === domain ? "border-brand bg-brand text-white" : "border-line bg-white text-ink hover:border-brand")}>
                        {x.title} <span className={x.domain === domain ? "text-white/80" : "text-muted"}>{x.count}</span>
                      </button>
                    ))}
                  </div>
                )}
                <Card
                  title={searching ? `Results for “${term}”` : currentDomain?.title || "Entries"}
                  subtitle={searching ? "Ranked across every domain" : currentDomain ? `${currentDomain.count} entries · researched ${currentDomain.researched}` : undefined}
                  actions={searching ? <Button size="sm" tone="ghost" onClick={() => { setQ(""); setTerm(""); }}>Clear</Button> : undefined}
                  pad={false}
                >
                  <div className="scroll-thin max-h-[65vh] space-y-2 overflow-auto p-3">
                    {listLoading && <Loading rows={4} />}
                    <ErrorBox error={listError} retry={searching ? found.reload : dom.reload} />
                    {!listLoading && !listError && list.length === 0 && (
                      <Empty icon={<Search className="size-6" />} title={searching ? "No matching entry" : "No entries"} hint={searching ? found.data?.hint || "Try fewer or different words." : "This domain has no entries yet."} />
                    )}
                    {list.map((e) => <EntryRow key={e.id} e={e} active={e.id === entryId} onOpen={() => setEntryId(e.id)} />)}
                  </div>
                </Card>
                {!searching && dom.data?.open_questions?.length > 0 && (
                  <details className="card px-4 py-3 text-xs">
                    <summary className="cursor-pointer font-medium">Open questions the research couldn't settle ({dom.data.open_questions.length})</summary>
                    <ul className="mt-2 list-disc space-y-1 pl-4 text-muted">{dom.data.open_questions.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
                  </details>
                )}
              </div>
              <div className={cx("min-w-0", !entryId && "hidden lg:block")}>
                {entryId ? (
                  <EntryDetail id={entryId} onBack={() => setEntryId("")} />
                ) : (
                  <Card>
                    <Empty icon={<BookOpenCheck className="size-6" />} title="Pick an entry" hint="Each entry has steps, do's and don'ts, benchmarks with their context, sources that open in a new tab, a research date and a confidence level." />
                  </Card>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
