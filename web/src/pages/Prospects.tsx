import { useEffect, useMemo, useState } from "react";
import { ExternalLink, LayoutGrid, List as ListIcon, Plus, Send, Upload } from "lucide-react";
import { ApiError, get, post, put } from "../api";
import { InboxPanel } from "./inbox/InboxPanel";
import {
  Badge, Button, Card, CopyButton, DemoBadge, Drawer, Empty, ErrorBox, Field, FlagList, Input, Loading, Modal, PageHeader,
  Select, Stat, Table, Tabs, TextArea, TraceLink, cx, isDemo, useApi, useApp,
} from "../components/ui";

type Opt = { key: string; label: string };
type Overview = {
  prospects: any[]; stages: Opt[]; lists: any[]; usage: Record<string, Usage>; caps: Record<string, number>;
  icp_ready: boolean; sender_ready: boolean; moves: Opt[]; channels: Opt[]; lawful_bases: Opt[]; reply_kinds: Opt[];
  is_owner: boolean; web_research: boolean; ai: string;
};
type Usage = { cap: number; sent_today: number; waiting: number; remaining: number };

const STAGE_TONE: Record<string, any> = {
  new: "neutral", researched: "info", contacted: "brand", replied: "warn", call_booked: "good", proposal: "good",
  won: "good", lost: "neutral", nurture: "neutral", dnc: "bad",
};
const DRAFT_STATUS: Record<string, string> = {
  pending: "Waiting for approval", held: "Held by guardrails", needs_human: "Needs a person", approved: "Approved",
  rejected: "Withdrawn or rejected", scheduled: "Scheduled", awaiting_manual_send: "Ready to send by hand",
};
const STAGE_LABEL: Record<string, string> = {
  new: "New", researched: "Researched", contacted: "Contacted", replied: "Replied", call_booked: "Call booked",
  proposal: "Proposal sent", won: "Won", lost: "Lost", nurture: "Nurture", dnc: "Do not contact",
};
const TOUCH_LABEL: Record<string, string> = { opener: "First message", followup: "Follow-up", booking: "Call times", proposal: "Proposal" };
const FAMILY_LABEL: Record<string, string> = { dm: "Instagram DMs", email: "Emails" };
const EVENT_LABEL: Record<string, string> = {
  added: "Added", updated: "Details updated", researched: "Research refreshed", fact_added: "Fact added", stage: "Stage changed",
  drafted: "Message drafted", sent: "Message sent", reply: "Reply recorded", booked: "Call booked", dnc: "Marked do not contact",
  released: "Claim released by the owner", won: "Won", proposal_created: "Proposal drafted", proposal_queued: "Proposal sent for approval",
  proposal_live: "Proposal approved and live", proposal_sent: "Proposal sent", proposal_viewed: "Proposal opened",
  proposal_accepted: "Proposal accepted",
  proposal_withdrawn: "Proposal withdrawn",
};

const when = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const plainName = (s?: string) => (s || "").replace(" (DEMO)", "");
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
// only ever link to http(s) pages: a stored "javascript:" or "data:" address is shown as text, never as a link
const safeLink = (u?: string | null) => (u && /^https?:\/\//i.test(u) ? u : undefined);

function useAct() {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const act = async <T,>(what: string, fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(what);
    setErr(null);
    try {
      return await fn();
    } catch (e) {
      setErr(e as ApiError);
      return undefined;
    } finally {
      setBusy(null);
    }
  };
  return { busy, err, setErr, act };
}

function NameCell({ p }: { p: any }) {
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1.5">
      <span className="font-medium">{plainName(p.name)}</span>
      {isDemo(p.name) && <DemoBadge />}
      {p.claim_released ? <Badge tone="warn">Released</Badge> : null}
    </span>
  );
}

function FitBadge({ score }: { score?: number | null }) {
  const s = score ?? 0;
  return <Badge tone={s >= 70 ? "good" : s >= 40 ? "warn" : "neutral"} title="Fit score out of 100">Fit {s}</Badge>;
}

function UsageStrip({ usage }: { usage: Record<string, Usage> }) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {Object.entries(usage).map(([fam, u]) => (
        <Stat key={fam} label={`${FAMILY_LABEL[fam] || fam} today`} value={`${u.remaining} left`}
          hint={`${u.sent_today} sent · ${plural(u.waiting, "first message")} waiting · cap ${u.cap} a day`} tone={u.remaining === 0 ? "warn" : undefined} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- page
export default function Prospects({ query }: { query?: URLSearchParams }) {
  const { me } = useApp();
  const [tab, setTab] = useState("pipeline");
  const [filters, setFilters] = useState({ list_name: "", stage: "", q: "" });
  const qs = new URLSearchParams(Object.entries(filters).filter(([, v]) => v) as [string, string][]).toString();
  const { data, error, loading, reload } = useApi<Overview>(`/prospects${qs ? `?${qs}` : ""}`, [qs]);
  const [open, setOpen] = useState<{ id: number; tab?: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [bulk, setBulk] = useState(false);
  useEffect(() => {
    const id = Number(query?.get("open"));
    if (id) setOpen({ id });
  }, [query]);
  const tabs = [
    { key: "pipeline", label: "Pipeline" }, { key: "import", label: "Import" }, { key: "icp", label: "Ideal client" },
    { key: "stats", label: "Opener results" }, { key: "proposals", label: "Proposals" },
    // everyone but the owner (who has the Channels page) reads and answers their own replies here
    ...(data && !data.is_owner ? [{ key: "inbox", label: "Inbox" }] : []),
    ...(data?.is_owner ? [{ key: "settings", label: "Scoring & caps" }] : []),
  ];
  return (
    <div>
      <PageHeader
        title="Prospects"
        subtitle="Find, research and win your first client. Drafts only use facts you've researched, and every message waits for your approval before it goes anywhere."
        badge={me.is_demo ? <DemoBadge /> : undefined}
        right={<>
          <Button onClick={() => setAdding(true)}><Plus className="size-4" /> Add prospect</Button>
          <Button tone="primary" onClick={() => setBulk(true)}><Send className="size-4" /> Draft first messages</Button>
        </>}
      />
      <Tabs tabs={tabs} value={tab} onChange={setTab} />
      {tab === "pipeline" && (
        <Pipeline data={data} error={error} loading={loading} reload={reload} filters={filters} setFilters={setFilters}
          onOpen={(id) => setOpen({ id })} goTab={setTab} onAdd={() => setAdding(true)} />
      )}
      {tab === "import" && <ImportTab onDone={reload} bases={data?.lawful_bases || []} />}
      {tab === "icp" && <IcpTab onSaved={reload} />}
      {tab === "stats" && <StatsTab />}
      {tab === "proposals" && <ProposalsTab onOpen={(id) => setOpen({ id, tab: "proposal" })} />}
      {tab === "settings" && data?.is_owner && <SettingsTab />}
      {tab === "inbox" && data && !data.is_owner && <div data-testid="prospects-inbox"><InboxPanel /></div>}
      {open && <ProspectDrawer id={open.id} initialTab={open.tab} overview={data} onClose={() => setOpen(null)} onChanged={reload} />}
      <AddModal open={adding} onClose={() => setAdding(false)} bases={data?.lawful_bases || []} onDone={(id) => { setAdding(false); reload(); if (id) setOpen({ id }); }} />
      <BulkModal open={bulk} onClose={() => setBulk(false)} overview={data} onDone={reload} />
    </div>
  );
}

// ---------------------------------------------------------------- pipeline
function Pipeline({ data, error, loading, reload, filters, setFilters, onOpen, goTab, onAdd }: {
  data: Overview | null; error: ApiError | null; loading: boolean; reload: () => void; filters: any; setFilters: (f: any) => void;
  onOpen: (id: number) => void; goTab: (t: string) => void; onAdd: () => void;
}) {
  const [view, setView] = useState<"board" | "list">("list");
  const filtered = !!(filters.list_name || filters.stage || filters.q);
  if (loading && !data) return <Loading rows={6} />;
  if (error && !data) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  const byStage = (s: string) => data.prospects.filter((p) => p.stage === s);
  return (
    <div className="space-y-4">
      <ErrorBox error={error} retry={reload} />
      {!data.icp_ready && (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm">Set your ideal client first so fit scores mean something and drafts describe what you do.</p>
            <Button size="sm" onClick={() => goTab("icp")}>Set ideal client</Button>
          </div>
        </Card>
      )}
      <UsageStrip usage={data.usage} />
      <div className="grid grid-cols-2 items-end gap-2 sm:flex sm:flex-wrap">
        <div className="min-w-0 sm:w-48">
          <Field label="List">
            <Select value={filters.list_name} onChange={(e) => setFilters({ ...filters, list_name: e.target.value })}
              options={[{ value: "", label: "All lists" }, ...Array.from(new Set(data.lists.map((l) => l.name))).map((n) => ({ value: n, label: n }))]} />
          </Field>
        </div>
        <div className="min-w-0 sm:w-44">
          <Field label="Stage">
            <Select value={filters.stage} onChange={(e) => setFilters({ ...filters, stage: e.target.value })}
              options={[{ value: "", label: "All stages" }, ...data.stages.map((s) => ({ value: s.key, label: s.label }))]} />
          </Field>
        </div>
        <div className="col-span-2 min-w-0 sm:w-56">
          <Field label="Search">
            <Input value={filters.q} placeholder="Name, handle, niche" onChange={(e) => setFilters({ ...filters, q: e.target.value })} />
          </Field>
        </div>
        <div className="col-span-2 flex justify-end gap-1 sm:ml-auto">
          <Button size="sm" tone={view === "list" ? "primary" : "secondary"} onClick={() => setView("list")} aria-label="List view"><ListIcon className="size-3.5" /> List</Button>
          <Button size="sm" tone={view === "board" ? "primary" : "secondary"} onClick={() => setView("board")} aria-label="Board view"><LayoutGrid className="size-3.5" /> Board</Button>
        </div>
      </div>
      {data.prospects.length === 0 ? (
        filtered ? <Empty title="No prospects match these filters" hint="Clear a filter to see more." action={<Button size="sm" onClick={() => setFilters({ list_name: "", stage: "", q: "" })}>Clear filters</Button>} /> : (
          <Empty title="No prospects yet" hint="Import a CSV from any lead tool, or add someone by hand. Every import needs a lawful basis for contacting people."
            action={<div className="flex flex-wrap justify-center gap-2"><Button size="sm" tone="primary" onClick={() => goTab("import")}><Upload className="size-3.5" /> Import a CSV</Button><Button size="sm" onClick={onAdd}>Add by hand</Button></div>} />
        )
      ) : view === "list" ? (
        <Card pad={false}>
          <div className="p-2">
            <Table rows={data.prospects} onRow={(p) => onOpen(p.id)} cols={[
              { key: "name", label: "Prospect", render: (p) => <div className="min-w-0"><NameCell p={p} /><div className="text-xs text-muted">{p.business && plainName(p.business) !== plainName(p.name) ? `${plainName(p.business)} · ` : ""}{p.handle || p.email || ""}</div><div className="mt-1 sm:hidden"><Badge tone={STAGE_TONE[p.stage]}>{p.stage_label}</Badge></div></div> },
              { key: "fit", label: "Fit", render: (p) => <FitBadge score={p.fit_score} /> },
              { key: "stage", label: "Stage", className: "hidden sm:table-cell", render: (p) => <Badge tone={STAGE_TONE[p.stage]}>{p.stage_label}</Badge> },
              { key: "list", label: "List", className: "hidden md:table-cell", render: (p) => <span className="text-xs text-muted">{p.list_name || "My prospects"}</span> },
              ...(data.is_owner ? [{ key: "owner", label: "Worked by", className: "hidden md:table-cell", render: (p: any) => <span className="text-xs">{plainName(p.owner_name)}</span> }] : []),
            ]} />
          </div>
        </Card>
      ) : (
        <div className="scroll-thin -mx-1 flex max-w-full gap-3 overflow-x-auto px-1 pb-2" data-testid="pipeline-board">
          {data.stages.map((s) => (
            <div key={s.key} className="w-64 shrink-0 rounded-lg border border-line bg-panel-2 p-2">
              <div className="mb-2 flex items-center justify-between text-xs font-semibold text-muted"><span>{s.label}</span><span>{byStage(s.key).length}</span></div>
              <div className="space-y-2">
                {byStage(s.key).map((p) => (
                  <button key={p.id} onClick={() => onOpen(p.id)} className="focus-ring card block w-full p-2 text-left text-sm hover:border-brand">
                    <NameCell p={p} />
                    <div className="mt-1 flex items-center gap-1.5"><FitBadge score={p.fit_score} />{p.niche && <span className="truncate text-xs text-muted">{p.niche}</span>}</div>
                  </button>
                ))}
                {byStage(s.key).length === 0 && <p className="px-1 py-2 text-xs text-muted">Nobody here</p>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- add + bulk
function LawfulFields({ bases, value, onChange }: { bases: Opt[]; value: { lawful_basis: string; lawful_note: string }; onChange: (v: any) => void }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="Lawful basis for contacting them">
        <Select value={value.lawful_basis} onChange={(e) => onChange({ ...value, lawful_basis: e.target.value })}
          options={[{ value: "", label: "Choose one" }, ...bases.map((b) => ({ value: b.key, label: b.label }))]} />
      </Field>
      <Field label="Why it applies" hint="Where the contact details came from, e.g. their own website">
        <Input value={value.lawful_note} onChange={(e) => onChange({ ...value, lawful_note: e.target.value })} />
      </Field>
    </div>
  );
}

function AddModal({ open, onClose, bases, onDone }: { open: boolean; onClose: () => void; bases: Opt[]; onDone: (id?: number) => void }) {
  const blank = { name: "", business: "", handle: "", email: "", website: "", niche: "", followers: "", notes: "", ad_activity: "", list_name: "" };
  const [f, setF] = useState<Record<string, string>>(blank);
  const [lb, setLb] = useState({ lawful_basis: "", lawful_note: "" });
  const { busy, err, setErr, act } = useAct();
  const save = async () => {
    const body: any = { ...lb, list_name: f.list_name };
    for (const k of Object.keys(blank)) if (k !== "list_name" && f[k]) body[k] = f[k];
    const r = await act("add", () => post("/prospects", body));
    if (r) {
      setF(blank);
      onDone(r.prospect.id);
    }
  };
  const field = (k: string, label: string, ph = "") => (
    <Field label={label}><Input value={f[k]} placeholder={ph} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></Field>
  );
  return (
    <Modal open={open} onClose={() => { setErr(null); onClose(); }} title="Add a prospect">
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          {field("name", "Name")}{field("business", "Business")}{field("handle", "Instagram", "@handle")}{field("email", "Email")}
          {field("website", "Website")}{field("niche", "Niche")}{field("followers", "Followers", "e.g. 12,400")}{field("ad_activity", "Ads", "e.g. 3 active ads")}
        </div>
        <Field label="Bio or notes" hint="Facts here can be quoted in openers. Only write what you actually saw.">
          <TextArea rows={3} value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} />
        </Field>
        {field("list_name", "List", "My prospects")}
        <LawfulFields bases={bases} value={lb} onChange={setLb} />
        <ErrorBox error={err} />
        <div className="flex justify-end gap-2"><Button onClick={onClose}>Cancel</Button><Button tone="primary" loading={busy === "add"} onClick={save}>Add prospect</Button></div>
      </div>
    </Modal>
  );
}

function BulkModal({ open, onClose, overview, onDone }: { open: boolean; onClose: () => void; overview: Overview | null; onDone: () => void }) {
  const { go } = useApp();
  const [channel, setChannel] = useState("instagram_dm");
  const [limit, setLimit] = useState("10");
  const [list, setList] = useState("");
  const [res, setRes] = useState<any>(null);
  const { busy, err, act } = useAct();
  const run = async () => {
    const r = await act("bulk", () => post("/prospects/draft-openers", { channel, limit: Number(limit) || 10, ...(list ? { list_name: list } : {}) }));
    if (r) {
      setRes(r);
      onDone();
    }
  };
  const fam = channel === "email" ? "email" : "dm";
  const u = overview?.usage?.[fam];
  return (
    <Modal open={open} onClose={() => { setRes(null); onClose(); }} title="Draft first messages">
      <div className="space-y-3">
        <p className="text-sm text-muted">Drafts one first message for each of your own new or researched prospects, best fit first. The style is picked automatically: each style is tried until it has enough sends, then the ones that get more replies are picked more often. Nothing is sent until you approve it.</p>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Channel"><Select value={channel} onChange={(e) => setChannel(e.target.value)} options={(overview?.channels || []).map((c) => ({ value: c.key, label: c.label }))} /></Field>
          <Field label="How many"><Input type="number" min={1} max={100} value={limit} onChange={(e) => setLimit(e.target.value)} /></Field>
          <Field label="List"><Select value={list} onChange={(e) => setList(e.target.value)} options={[{ value: "", label: "All lists" }, ...Array.from(new Set((overview?.lists || []).map((l) => l.name))).map((n) => ({ value: n, label: n }))]} /></Field>
        </div>
        {u && <p className="text-xs text-muted">Today's cap: {u.remaining} of {u.cap} left ({u.sent_today} sent, {u.waiting} waiting).</p>}
        <ErrorBox error={err} />
        {res && (
          <div className="space-y-2 rounded-lg border border-line p-3 text-sm" data-testid="bulk-result">
            <p><b>{res.queued.length}</b> drafted, <b>{res.skipped.length}</b> skipped. <TraceLink runId={res.run_id} /></p>
            {res.skipped.length > 0 && (
              <ul className="max-h-40 space-y-0.5 overflow-auto text-xs text-muted">
                {res.skipped.slice(0, 30).map((s: any) => <li key={s.prospect_id}>{plainName(s.name)}: {s.reason}</li>)}
              </ul>
            )}
            {res.queued.length > 0 && <Button size="sm" onClick={() => go(`/queue?ids=${res.queued.map((q: any) => q.queue_id).join(",")}`)}>Review {res.queued.length} drafts</Button>}
          </div>
        )}
        <div className="flex justify-end gap-2"><Button onClick={() => { setRes(null); onClose(); }}>Close</Button><Button tone="primary" loading={busy === "bulk"} onClick={run}>Draft</Button></div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- import
function ImportTab({ onDone, bases }: { onDone: () => void; bases: Opt[] }) {
  const [text, setText] = useState("");
  const [filename, setFilename] = useState("pasted.csv");
  const [listName, setListName] = useState("");
  const [lb, setLb] = useState({ lawful_basis: "", lawful_note: "" });
  const [summary, setSummary] = useState<any>(null);
  const past = useApi<any>("/prospects/imports");
  const { busy, err, act } = useAct();
  const onFile = (file?: File | null) => {
    if (!file) return;
    setFilename(file.name);
    const r = new FileReader();
    r.onload = () => setText(String(r.result || ""));
    r.readAsText(file);
  };
  const run = async () => {
    const r = await act("import", () => post("/prospects/import", { csv: text, filename, list_name: listName, ...lb }));
    if (r) {
      setSummary(r);
      onDone();
      past.reload();
    }
  };
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Import a CSV" subtitle="From any lead tool. I'll work out the columns (name, Instagram, email, website, niche, followers, bio, ads) and tell you exactly what I read.">
        <div className="space-y-3">
          <Field label="CSV file"><input type="file" accept=".csv,text/csv,text/plain" onChange={(e) => onFile(e.target.files?.[0])} className="block w-full text-sm" /></Field>
          <Field label="Or paste it here"><TextArea rows={6} value={text} placeholder={"Name,Instagram,Email,Website,Niche,Followers,Bio\n..."} onChange={(e) => setText(e.target.value)} /></Field>
          <Field label="List name" hint="Defaults to the file name"><Input value={listName} onChange={(e) => setListName(e.target.value)} /></Field>
          <LawfulFields bases={bases} value={lb} onChange={setLb} />
          <ErrorBox error={err} />
          <Button tone="primary" loading={busy === "import"} disabled={!text.trim()} onClick={run}><Upload className="size-4" /> Import</Button>
        </div>
      </Card>
      <div className="space-y-4">
        {summary ? <ImportSummary s={summary} /> : (
          <Card title="What happens">
            <ul className="list-disc space-y-1 pl-5 text-sm">
              <li>Duplicates are skipped (same email, Instagram handle or website).</li>
              <li>Anyone a teammate is already working is skipped. You won't see who.</li>
              <li>Anyone who asked not to be contacted, or is on the never-invite list, is skipped.</li>
              <li>Each prospect gets research facts from the columns, with the file as the source, and a fit score.</li>
            </ul>
          </Card>
        )}
        <Card title="Past imports">
          {past.loading ? <Loading /> : past.error ? <ErrorBox error={past.error} retry={past.reload} /> : (
            <Table rows={past.data?.imports || []} empty="No imports yet" cols={[
              { key: "filename", label: "File" },
              { key: "n", label: "Imported", render: (r) => `${r.summary?.imported ?? 0} of ${r.summary?.rows_total ?? 0}` },
              { key: "basis", label: "Lawful basis", className: "hidden sm:table-cell", render: (r) => <span className="text-xs">{r.lawful_basis_label}</span> },
              { key: "created", label: "When", render: (r) => <span className="text-xs text-muted">{when(r.created)}</span> },
            ]} />
          )}
        </Card>
      </div>
    </div>
  );
}

function ImportSummary({ s }: { s: any }) {
  return (
    <Card title="What I read" subtitle={`${s.filename} into "${s.list_name}"`} actions={<TraceLink runId={s.run_id} />}>
      <div className="space-y-3 text-sm" data-testid="import-summary">
        <div className="grid grid-cols-3 gap-2">
          <Stat label="Rows" value={s.rows_total} /><Stat label="Imported" value={s.imported} tone="good" /><Stat label="Skipped" value={s.skipped_total} tone={s.skipped_total ? "warn" : undefined} />
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">Columns</div>
          <ul className="space-y-0.5">
            {s.columns.map((c: any, i: number) => <li key={i} className="flex flex-wrap gap-2"><span className="font-medium">{c.header || "(blank)"}</span><span className="text-muted">{c.used_as ? `→ ${c.used_as.replace("_", " ")}` : "not used"}</span></li>)}
          </ul>
        </div>
        {s.reasons.length > 0 && (
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Why rows were skipped</div>
            <ul className="space-y-0.5">{s.reasons.map((r: any) => <li key={r.reason}>{r.reason}: {r.count}</li>)}</ul>
          </div>
        )}
        {s.warnings.map((w: string) => <p key={w} className="rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-900">{w}</p>)}
        <p className="text-xs text-muted">Lawful basis recorded: {s.lawful_basis}</p>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- ICP
function IcpTab({ onSaved }: { onSaved: () => void }) {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/prospects/icp");
  const [f, setF] = useState<any>(null);
  const { busy, err, act } = useAct();
  useEffect(() => {
    if (data?.icp) {
      const i = data.icp;
      setF({ ...i, must_haves: (i.must_haves || []).join(", "), disqualifiers: (i.disqualifiers || []).join(", "), signals: (i.signals || []).join(", "),
        price_min: i.price_min ?? "", price_max: i.price_max ?? "", audience_min: i.audience_min ?? "", audience_max: i.audience_max ?? "" });
    }
  }, [data]);
  if (loading && !f) return <Loading rows={5} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!f) return null;
  const num = (v: any) => (v === "" || v === null || v === undefined ? null : Number(v));
  const split = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    const r = await act("save", () => put("/prospects/icp", {
      niche: f.niche, offer: f.offer, persona: f.persona, price_min: num(f.price_min), price_max: num(f.price_max),
      audience_min: num(f.audience_min), audience_max: num(f.audience_max), must_haves: split(f.must_haves),
      disqualifiers: split(f.disqualifiers), signals: split(f.signals), sender: f.sender,
    }));
    if (r) {
      toast("Saved. Fit scores updated.", "good");
      onSaved();
    }
  };
  const inp = (k: string, label: string, hint?: string, type = "text") => (
    <Field label={label} hint={hint}><Input type={type} value={f[k] ?? ""} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></Field>
  );
  const snd = (k: string, label: string, hint?: string) => (
    <Field label={label} hint={hint}><Input value={f.sender?.[k] ?? ""} onChange={(e) => setF({ ...f, sender: { ...f.sender, [k]: e.target.value } })} /></Field>
  );
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Ideal client" subtitle="Who you can help fastest. Fit scores are worked out from this, and you can see every point.">
        <div className="space-y-3">
          {inp("niche", "Niche", "e.g. fitness coaches")}
          {inp("offer", "What you help them with", "e.g. a booked-call system for their programme")}
          <div className="grid gap-3 sm:grid-cols-2">{inp("price_min", "Their offer price from", undefined, "number")}{inp("price_max", "Their offer price up to", undefined, "number")}</div>
          <div className="grid gap-3 sm:grid-cols-2">{inp("audience_min", "Followers from", undefined, "number")}{inp("audience_max", "Followers up to", undefined, "number")}</div>
          {inp("must_haves", "Must-haves", "Comma separated, e.g. programme, coaching")}
          {inp("disqualifiers", "Disqualifiers", "Comma separated, e.g. no offer yet")}
          {inp("signals", "Buying signals", "Comma separated, e.g. webinar, launch, ads")}
          <Field label="Who decides (persona)"><TextArea rows={3} value={f.persona} onChange={(e) => setF({ ...f, persona: e.target.value })} /></Field>
        </div>
      </Card>
      <Card title="Sender details" subtitle="Used in every email you send to prospects. The law requires your real identity and a postal address.">
        <div className="space-y-3">
          {snd("name", "Your name")}{snd("business", "Your business")}
          {snd("postal_address", "Postal address", "A registered or virtual office address is fine")}
          {snd("reply_email", "Reply-to email")}
          <ErrorBox error={err} />
          <Button tone="primary" loading={busy === "save"} onClick={save}>Save</Button>
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- stats
function StatsTab() {
  const { data, error, loading, reload } = useApi<any>("/prospects/stats");
  if (loading) return <Loading rows={5} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
  const total = (data.pipeline as any[]).reduce((a, r) => a + r.count, 0);
  return (
    <div className="space-y-4">
      <UsageStrip usage={data.usage} />
      <Card title="Pipeline">
        {total === 0 ? <Empty title="No prospects yet" hint="Import or add prospects to see the pipeline." /> : (
          <div className="flex flex-wrap gap-2">{data.pipeline.map((r: any) => <Badge key={r.stage} tone={STAGE_TONE[r.stage]}>{r.label}: {r.count}</Badge>)}</div>
        )}
      </Card>
      {Object.entries(data.moves).map(([fam, rows]: [string, any]) => (
        <Card key={fam} title={`Opener results: ${FAMILY_LABEL[fam] || fam}`}
          subtitle={`Counted on messages that really went out. The 95% range shows how sure we can be. Each style needs ${data.min_sample} sends before the picker starts favouring the best one.`}>
          {(rows as any[]).every((r) => r.sent === 0) ? <Empty title="No first messages sent yet" hint="Results appear here once approved messages go out." /> : (
            <Table rows={rows as any[]} cols={[
              { key: "label", label: "Style" }, { key: "sent", label: "Sent" }, { key: "replied", label: "Replied", className: "hidden sm:table-cell" },
              { key: "reply_rate", label: "Reply rate", render: (r) => <span>{pct(r.reply_rate)}<span className="block text-[11px] text-muted">{pct(r.reply_ci95[0])} to {pct(r.reply_ci95[1])}</span></span> },
              { key: "booked", label: "Booked", className: "hidden sm:table-cell" },
              { key: "book_rate", label: "Booked rate", className: "hidden sm:table-cell", render: (r) => pct(r.book_rate) },
            ]} />
          )}
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- proposals list
function ProposalsTab({ onOpen }: { onOpen: (prospectId: number) => void }) {
  const { data, error, loading, reload } = useApi<any>("/prospects/proposals");
  if (loading) return <Loading rows={4} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  return (
    <Card title="Proposals" subtitle="Build a proposal from a prospect's page after your call.">
      <Table rows={data?.proposals || []} empty="No proposals yet" onRow={(r) => onOpen(r.prospect_id)} cols={[
        { key: "prospect", label: "Prospect", render: (r) => <span className="flex flex-wrap items-center gap-1.5">{plainName(r.prospect_name)} {r.demo ? <DemoBadge /> : null}</span> },
        { key: "status", label: "Status", render: (r) => <Badge tone={r.status === "accepted" ? "good" : r.status === "published" ? "brand" : r.blocked ? "bad" : "neutral"}>{r.status_label}</Badge> },
        { key: "views", label: "Opened", render: (r) => `${r.views || 0} times` },
        { key: "updated", label: "Updated", className: "hidden sm:table-cell", render: (r) => <span className="text-xs text-muted">{when(r.updated || r.created)}</span> },
      ]} />
    </Card>
  );
}

// ---------------------------------------------------------------- owner settings
function SettingsTab() {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/prospects/config");
  const [f, setF] = useState<any>(null);
  const { busy, err, act } = useAct();
  useEffect(() => { if (data) setF({ weights: { ...data.weights }, caps: { ...data.caps }, min_sample: data.min_sample }); }, [data]);
  if (loading && !f) return <Loading rows={4} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!f) return null;
  const save = async () => {
    const ints = (o: any) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, Math.round(Number(v) || 0)]));
    const r = await act("save", () => put("/prospects/config", { weights: ints(f.weights), caps: ints(f.caps), min_sample: Math.round(Number(f.min_sample) || 20) }));
    if (r) {
      toast("Saved. Every prospect has been re-scored.", "good");
      reload();
    }
  };
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Fit score weights" subtitle="Points each factor is worth. Scores are scaled to 100.">
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.keys(f.weights).map((k) => (
            <Field key={k} label={data.weight_labels[k] || k}><Input type="number" min={0} max={100} value={f.weights[k]} onChange={(e) => setF({ ...f, weights: { ...f.weights, [k]: e.target.value } })} /></Field>
          ))}
        </div>
      </Card>
      <Card title="Daily caps and learning" subtitle="Per person, per channel. Counted on messages that really went out, plus first messages still waiting.">
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {Object.keys(f.caps).map((k) => (
              <Field key={k} label={`${FAMILY_LABEL[k] || k} a day`}><Input type="number" min={0} max={500} value={f.caps[k]} onChange={(e) => setF({ ...f, caps: { ...f.caps, [k]: e.target.value } })} /></Field>
            ))}
          </div>
          <Field label="Sends per style before the picker favours the best" hint="Until then every style gets tried"><Input type="number" min={1} max={1000} value={f.min_sample} onChange={(e) => setF({ ...f, min_sample: e.target.value })} /></Field>
          <p className="text-xs text-muted">Website research is {data.web_research ? "on" : "off"} for this workspace. It's a server switch: when on, it reads one page of a prospect's own website and only where their robots.txt allows it.</p>
          <ErrorBox error={err} />
          <Button tone="primary" loading={busy === "save"} onClick={save}>Save</Button>
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- prospect drawer
function ProspectDrawer({ id, initialTab, overview, onClose, onChanged }: { id: number; initialTab?: string; overview: Overview | null; onClose: () => void; onChanged: () => void }) {
  const { data, error, loading, reload } = useApi<any>(`/prospects/${id}`, [id]);
  const [tab, setTab] = useState(initialTab || "research");
  const { busy, err, act } = useAct();
  const refresh = () => { reload(); onChanged(); };
  const p = data?.prospect;
  const locked = p && (p.do_not_contact || p.claim_released);
  const setStage = async (stage: string) => {
    if (!stage) return;
    if (stage === "won" && !window.confirm("Mark as won? This adds them to your clients.")) return;
    const reason = stage === "lost" ? window.prompt("Why was it lost? (optional)") || "" : "";
    if (await act("stage", () => post(`/prospects/${id}/stage`, { stage, reason }))) refresh();
  };
  const dnc = async () => {
    if (!window.confirm("Mark as do not contact? This can't be undone, and any waiting drafts are withdrawn.")) return;
    if (await act("dnc", () => post(`/prospects/${id}/dnc`, { reason: "Marked do not contact" }))) refresh();
  };
  const release = async () => {
    if (!window.confirm("Release this prospect so a teammate can add them? The current holder can no longer message them from this list.")) return;
    if (await act("release", () => post(`/prospects/${id}/release`))) refresh();
  };
  return (
    <Drawer open onClose={onClose} title={p ? <span className="flex flex-wrap items-center gap-2">{plainName(p.name)} {isDemo(p.name) && <DemoBadge />}</span> : "Prospect"}>
      {loading && !data && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {p && (
        <div className="space-y-4" data-testid="prospect-drawer">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={STAGE_TONE[p.stage]}>{p.stage_label}</Badge><FitBadge score={p.fit_score} />
            {p.list_name && <Badge>{p.list_name}</Badge>}
            {p.owner_name && <span className="text-xs text-muted">Worked by {plainName(p.owner_name)}</span>}
          </div>
          <div className="grid gap-1 text-sm">
            {p.business && <div><span className="text-muted">Business:</span> {plainName(p.business)}</div>}
            {p.handle && <div><span className="text-muted">Instagram:</span> {p.handle}</div>}
            {p.email && <div className="break-all"><span className="text-muted">Email:</span> {p.email}</div>}
            {p.website && <div className="break-all"><span className="text-muted">Website:</span> {p.website}</div>}
            <div className="text-xs text-muted">Lawful basis: {p.lawful_basis_label}{p.lawful_note ? ` (${p.lawful_note})` : ""}</div>
            {p.do_not_contact ? <p className="rounded-md bg-red-50 px-2 py-1 text-xs text-red-900">Asked not to be contacted. Nothing can be drafted or sent to them.</p> : null}
            {p.claim_released ? <p className="rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-900">The owner released this prospect to the team. It can't be messaged from this list.</p> : null}
          </div>
          {!p.do_not_contact && (
            <div className="flex flex-wrap items-end gap-2">
              <div className="w-full sm:w-48">
                <Field label="Move to stage">
                  <Select value="" onChange={(e) => setStage(e.target.value)} disabled={busy === "stage"}
                    options={[{ value: "", label: "Choose…" }, ...(overview?.stages || []).filter((s) => s.key !== p.stage && s.key !== "dnc" && (!p.claim_released || ["lost", "nurture"].includes(s.key))).map((s) => ({ value: s.key, label: s.label }))]} />
                </Field>
              </div>
              <Button tone="danger" size="sm" loading={busy === "dnc"} onClick={dnc}>Do not contact</Button>
              {overview?.is_owner && !p.claim_released && <Button size="sm" loading={busy === "release"} onClick={release}>Release claim</Button>}
            </div>
          )}
          <ErrorBox error={err} />
          <Tabs value={tab} onChange={setTab} tabs={[{ key: "research", label: "Research" }, { key: "messages", label: "Messages" }, { key: "reply", label: "Reply & book" }, { key: "proposal", label: "Proposal" }, { key: "timeline", label: "Timeline" }]} />
          {tab === "research" && <ResearchPanel p={p} webOn={!!overview?.web_research} onChanged={refresh} />}
          {tab === "messages" && <MessagesPanel p={p} drafts={data.drafts} usage={data.usage} locked={!!locked} aiLabel={overview?.ai || ""} onChanged={refresh} />}
          {tab === "reply" && <ReplyPanel p={p} kinds={overview?.reply_kinds || []} locked={!!p.do_not_contact} onChanged={refresh} />}
          {tab === "proposal" && <ProposalPanel p={p} proposals={data.proposals} locked={!!locked} onChanged={refresh} />}
          {tab === "timeline" && <Timeline items={data.timeline} />}
        </div>
      )}
    </Drawer>
  );
}

function ResearchPanel({ p, webOn, onChanged }: { p: any; webOn: boolean; onChanged: () => void }) {
  const [web, setWeb] = useState(false);
  const [fact, setFact] = useState({ claim: "", source: "", source_url: "", quote: "" });
  const [res, setRes] = useState<any>(null);
  const { busy, err, act } = useAct();
  const research = async () => {
    const r = await act("research", () => post(`/prospects/${p.id}/research`, { web }));
    if (r) { setRes(r); onChanged(); }
  };
  const addFact = async () => {
    const r = await act("fact", () => post(`/prospects/${p.id}/facts`, { ...fact, source_url: fact.source_url || null }));
    if (r) { setFact({ claim: "", source: "", source_url: "", quote: "" }); onChanged(); }
  };
  return (
    <div className="space-y-4">
      <Card title="Fit score" subtitle="Every point, and why.">
        {(p.fit_breakdown || []).length === 0 ? <Empty title="No score yet" /> : (
          <ul className="divide-y divide-line" data-testid="fit-breakdown">
            {p.fit_breakdown.map((b: any) => (
              <li key={b.key} className="py-2 text-sm">
                <div className="flex items-center justify-between gap-2"><span className="font-medium">{b.label}</span><span className="tabular-nums">{b.key === "disqualified" ? "Score set to 0" : `${b.points} / ${b.max}`}</span></div>
                <div className="text-xs text-muted">{b.why}</div>
              </li>
            ))}
            <li className="flex items-center justify-between py-2 text-sm font-semibold"><span>Fit score</span><span>{p.fit_score ?? 0} / 100</span></li>
          </ul>
        )}
      </Card>
      <Card title="Research facts" subtitle="Openers may only mention what's here." actions={<TraceLink runId={res?.run_id} />}>
        {p.facts.length === 0 ? <Empty title="No facts yet" hint="Add what you saw, with where you saw it." /> : (
          <ul className="space-y-2" data-testid="facts">
            {p.facts.map((f: any, i: number) => (
              <li key={i} className="rounded-lg border border-line p-2 text-sm">
                <div className="font-medium">{f.claim}</div>
                {f.quote && f.quote !== f.claim && <div className="mt-0.5 break-words text-xs text-muted">“{f.quote}”</div>}
                <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
                  <span>Source: {f.source}</span>
                  {safeLink(f.source_url) && <a className="inline-flex items-center gap-0.5 text-brand" href={safeLink(f.source_url)} target="_blank" rel="noreferrer noopener">Open source <ExternalLink className="size-3" /></a>}
                  {f.fetched_at && <span>{when(f.fetched_at)}</span>}
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className={cx("flex items-center gap-2 text-sm", !webOn && "text-muted")}>
            <input type="checkbox" checked={web} disabled={!webOn} onChange={(e) => setWeb(e.target.checked)} /> Also read one page of their website
          </label>
          <Button size="sm" loading={busy === "research"} onClick={research}>Refresh research</Button>
        </div>
        {!webOn && <p className="mt-1 text-xs text-muted">Website research is switched off for this workspace.</p>}
        {res?.web?.message && <p className="mt-1 text-xs text-muted">{res.web.message}</p>}
      </Card>
      <Card title="Add a fact">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="What you saw"><Input value={fact.claim} placeholder="Runs a free 5-day challenge" onChange={(e) => setFact({ ...fact, claim: e.target.value })} /></Field>
          <Field label="Where you saw it"><Input value={fact.source} placeholder="Their Instagram highlights" onChange={(e) => setFact({ ...fact, source: e.target.value })} /></Field>
          <Field label="Link (optional)"><Input value={fact.source_url} placeholder="https://" onChange={(e) => setFact({ ...fact, source_url: e.target.value })} /></Field>
          <Field label="Exact words (optional)"><Input value={fact.quote} onChange={(e) => setFact({ ...fact, quote: e.target.value })} /></Field>
        </div>
        <ErrorBox error={err} />
        <Button className="mt-3" size="sm" tone="primary" loading={busy === "fact"} disabled={fact.claim.trim().length < 3 || fact.source.trim().length < 2} onClick={addFact}>Add fact</Button>
      </Card>
    </div>
  );
}

function MessagesPanel({ p, drafts, usage, locked, aiLabel, onChanged }: { p: any; drafts: any[]; usage: Record<string, Usage>; locked: boolean; aiLabel: string; onChanged: () => void }) {
  const { go } = useApp();
  const [channel, setChannel] = useState(p.handle ? "instagram_dm" : "email");
  const [ideas, setIdeas] = useState<any>(null);
  const [edits, setEdits] = useState<Record<string, { text: string; subject?: string }>>({});
  const { busy, err, act } = useAct();
  const aiOn = aiLabel === "AI connected";
  const load = async (ai: boolean) => {
    const r = await act("ideas", () => get(`/prospects/${p.id}/suggestions?channel=${channel}${ai ? "&ai=true" : ""}`));
    if (r) {
      setIdeas(r);
      setEdits(Object.fromEntries((r as any).variants.filter((v: any) => v.available).map((v: any) => [v.move, { text: v.text, subject: v.subject }])));
    }
  };
  const queueIt = async (v: any) => {
    const e = edits[v.move];
    const edited = e && (e.text !== v.text || e.subject !== v.subject);
    const r = await act("q" + v.move, () => post(`/prospects/${p.id}/drafts`, { channel, move: v.move, ...(edited ? { text: e.text, subject: e.subject } : {}) }));
    if (r) { setIdeas(null); onChanged(); }
  };
  const followups = async () => { if (await act("fu", () => post(`/prospects/${p.id}/followups`))) onChanged(); };
  const fam = channel === "email" ? "email" : "dm";
  const hasFollowups = drafts.some((d) => d.touch === "followup");
  const openerWaiting = drafts.some((d) => d.touch === "opener" && d.status !== "rejected");
  return (
    <div className="space-y-4">
      {!locked && ["new", "researched", "nurture"].includes(p.stage) && openerWaiting && (
        <p className="rounded-md bg-panel-2 px-3 py-2 text-sm">A first message is already written for this prospect. Edit, approve or reject it in approvals; reject it to write a different one.</p>
      )}
      {!locked && ["new", "researched", "nurture"].includes(p.stage) && !openerWaiting && (
        <Card title="First message" subtitle={usage?.[fam] ? `Today: ${usage[fam].remaining} of ${usage[fam].cap} left on this channel` : undefined}>
          <div className="flex flex-wrap items-end gap-2">
            <div className="w-full sm:w-44"><Field label="Channel"><Select value={channel} onChange={(e) => { setChannel(e.target.value); setIdeas(null); }} options={[{ value: "instagram_dm", label: "Instagram DM" }, { value: "email", label: "Email" }]} /></Field></div>
            <Button size="sm" tone="primary" loading={busy === "ideas"} onClick={() => load(false)}>Get opener ideas</Button>
            {aiOn && <Button size="sm" loading={busy === "ideas"} onClick={() => load(true)}>Polish with AI</Button>}
          </div>
          <ErrorBox error={err} />
          {ideas && (
            <div className="mt-3 space-y-3" data-testid="opener-ideas">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                <Badge tone={ideas.mode === "rules" ? "neutral" : "good"}>{ideas.label}</Badge><TraceLink runId={ideas.run_id} />
                {ideas.recommended?.move && <span>Suggested style: <b>{ideas.variants.find((v: any) => v.move === ideas.recommended.move)?.label}</b> ({ideas.recommended.why})</span>}
              </div>
              {ideas.hidden > 0 && <p className="text-xs text-muted">{ideas.hidden} idea{ideas.hidden > 1 ? "s were" : " was"} hidden because {ideas.hidden > 1 ? "they" : "it"} didn't pass the guardrails.</p>}
              {ideas.variants.map((v: any) => (
                <div key={v.move} className="rounded-lg border border-line p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2"><b className="text-sm">{v.label}</b>{ideas.recommended?.move === v.move && <Badge tone="brand">Suggested</Badge>}</div>
                  {!v.available ? <p className="text-xs text-muted">{v.reason}</p> : (
                    <div className="space-y-2">
                      {channel === "email" && <Field label="Subject"><Input value={edits[v.move]?.subject || ""} onChange={(e) => setEdits({ ...edits, [v.move]: { ...edits[v.move], subject: e.target.value } })} /></Field>}
                      <TextArea rows={channel === "email" ? 9 : 4} value={edits[v.move]?.text || ""} onChange={(e) => setEdits({ ...edits, [v.move]: { ...edits[v.move], text: e.target.value } })} />
                      <FlagList flags={v.flags} />
                      <Button size="sm" tone="primary" loading={busy === "q" + v.move} onClick={() => queueIt(v)}>Queue for approval</Button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
      {!locked && p.stage === "contacted" && !hasFollowups && (
        <Card><div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm">No follow-ups yet. They're added on day 2, 4 and 7 after the first message goes out, and stop if they reply.</p><Button size="sm" loading={busy === "fu"} onClick={followups}>Draft follow-ups now</Button></div><ErrorBox error={err} /></Card>
      )}
      <Card title="Messages for this prospect">
        {drafts.length === 0 ? <Empty title="Nothing drafted yet" /> : (
          <ul className="space-y-2">
            {drafts.map((d) => (
              <li key={d.id} className="rounded-lg border border-line p-2 text-sm">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <Badge>{TOUCH_LABEL[d.touch] || "Message"}</Badge>
                  <Badge tone={d.status === "held" ? "bad" : d.status === "approved" ? "good" : d.status === "rejected" ? "neutral" : "info"}>{DRAFT_STATUS[d.status] || d.status}</Badge>
                  {d.send_after && <span className="text-xs text-muted">not before {when(d.send_after)}</span>}
                </div>
                {d.subject && <div className="text-xs"><span className="text-muted">Subject:</span> {d.subject}</div>}
                <p className="prose-pre line-clamp-4 whitespace-pre-wrap break-words text-xs">{d.body}</p>
                {d.flags?.length > 0 && <div className="mt-1"><FlagList flags={d.flags} /></div>}
                {["pending", "held", "needs_human"].includes(d.status) && <Button className="mt-1" size="sm" onClick={() => go(`/queue?ids=${d.id}`)}>Open in approvals</Button>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function ReplyPanel({ p, kinds, locked, onChanged }: { p: any; kinds: Opt[]; locked: boolean; onChanged: () => void }) {
  const { go, toast } = useApp();
  const [reply, setReply] = useState({ text: "", classification: "interested" });
  const [slots, setSlots] = useState<any>(null);
  const [custom, setCustom] = useState("");
  const [brief, setBrief] = useState<any>(null);
  const { busy, err, act } = useAct();
  const record = async () => {
    if (await act("reply", () => post(`/prospects/${p.id}/reply`, reply))) { setReply({ text: "", classification: "interested" }); toast("Reply recorded", "good"); onChanged(); }
  };
  const findSlots = async () => { const r = await act("slots", () => get(`/prospects/${p.id}/slots`)); if (r) setSlots(r); };
  const offer = async () => {
    const r = await act("offer", () => post(`/prospects/${p.id}/book`, { mode: "offer" }));
    if (r) { onChanged(); go(`/queue?ids=${(r as any).queue_id}`); }
  };
  const recordBooking = async (start: number) => {
    const r = await act("book" + start, () => post(`/prospects/${p.id}/book`, { mode: "record", start }));
    if (r) { setBrief((r as any).brief); setSlots(null); onChanged(); }
  };
  const loadBrief = async () => { const r = await act("brief", () => get(`/prospects/${p.id}/brief`)); if (r) setBrief((r as any).brief); };
  if (locked) return <Empty title="Asked not to be contacted" hint="Replies after an opt-out are logged in the timeline, but nothing more can be sent." />;
  return (
    <div className="space-y-4">
      <Card title="Record a reply" subtitle="When they reply on Instagram or by email, log it here. Follow-ups still waiting are withdrawn.">
        <div className="space-y-2">
          <Field label="What kind of reply"><Select value={reply.classification} onChange={(e) => setReply({ ...reply, classification: e.target.value })} options={kinds.map((k) => ({ value: k.key, label: k.label }))} /></Field>
          <Field label="What they said (optional)"><TextArea rows={3} value={reply.text} onChange={(e) => setReply({ ...reply, text: e.target.value })} /></Field>
          <Button size="sm" tone="primary" loading={busy === "reply"} onClick={record}>Record reply</Button>
        </div>
      </Card>
      <Card title="Book a call" subtitle="Times are worked out fresh from the calendar each time.">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" loading={busy === "slots"} onClick={findSlots}>Find free times</Button>
          <Button size="sm" loading={busy === "offer"} onClick={offer}>Offer three times (for approval)</Button>
          <Button size="sm" loading={busy === "brief"} onClick={loadBrief}>Pre-call brief</Button>
        </div>
        {slots && (
          slots.slots.length === 0 ? <p className="mt-2 text-sm text-muted">{slots.message}</p> : (
            <ul className="mt-3 space-y-1">
              {slots.slots.map((s: any) => (
                <li key={s.start} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line px-2 py-1 text-sm">
                  <span>{s.label}</span><Button size="sm" loading={busy === "book" + s.start} onClick={() => recordBooking(s.start)}>They booked this</Button>
                </li>
              ))}
            </ul>
          )
        )}
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <div className="w-full sm:w-60"><Field label="Or record another agreed time"><Input type="datetime-local" value={custom} onChange={(e) => setCustom(e.target.value)} /></Field></div>
          <Button size="sm" disabled={!custom} loading={busy === "bookc"} onClick={() => act("bookc", async () => {
            const start = Math.floor(new Date(custom).getTime() / 1000);
            const r = await post(`/prospects/${p.id}/book`, { mode: "record", start });
            setBrief(r.brief); setCustom(""); onChanged(); return r;
          })}>Record booking</Button>
        </div>
        <ErrorBox error={err} />
      </Card>
      {brief && <BriefCard brief={brief} />}
    </div>
  );
}

function BriefCard({ brief }: { brief: any }) {
  return (
    <Card title={`Pre-call brief: ${plainName(brief.who)}`} subtitle={brief.label}>
      <div className="space-y-3 text-sm" data-testid="precall-brief">
        {brief.their_words?.length > 0 && <div><div className="mb-1 text-xs font-semibold text-muted">In their words</div><ul className="list-disc pl-5">{brief.their_words.map((w: string, i: number) => <li key={i}>“{w}”</li>)}</ul></div>}
        <div><div className="mb-1 text-xs font-semibold text-muted">What we know</div><ul className="list-disc pl-5">{brief.what_we_know.map((f: any, i: number) => <li key={i}>{f.fact} <span className="text-xs text-muted">({f.source})</span></li>)}</ul></div>
        <div><div className="mb-1 text-xs font-semibold text-muted">Questions to ask</div><ul className="list-disc pl-5">{brief.questions.map((q: string) => <li key={q}>{q}</li>)}</ul></div>
        {brief.red_flags?.length > 0 && <div><div className="mb-1 text-xs font-semibold text-muted">Check for</div><ul className="list-disc pl-5">{brief.red_flags.map((q: string) => <li key={q}>{q}</li>)}</ul></div>}
        <div><div className="mb-1 text-xs font-semibold text-muted">Don't</div><ul className="list-disc pl-5">{brief.do_not_say.map((q: string) => <li key={q}>{q}</li>)}</ul></div>
        {brief.playbook?.length > 0 && <div className="flex flex-wrap gap-1">{brief.playbook.map((b: any) => <Badge key={b.id} tone="info" title={b.playbook}>{b.title}</Badge>)}</div>}
      </div>
    </Card>
  );
}

const NUM_FIELDS: [string, string][] = [["leads", "Enquiries a month"], ["booked", "Calls booked a month"], ["showed", "Calls that showed"], ["closed", "Sales a month"], ["offer_price", "Their offer price"], ["ad_spend", "Ad spend a month"]];

function ProposalPanel({ p, proposals, locked, onChanged }: { p: any; proposals: any[]; locked: boolean; onChanged: () => void }) {
  const [selected, setSelected] = useState<number | null>(proposals[0]?.id ?? null);
  const [building, setBuilding] = useState(proposals.length === 0);
  const [form, setForm] = useState<any>({ call_notes: "", numbers: {}, currency: "USD", retainer: "", performance_pct: "10", pilot_fee: "", pilot_days: "30", term_months: "3", options: ["retainer", "retainer_performance", "pilot"], include_projection: true });
  const { busy, err, act } = useAct();
  const build = async () => {
    const numbers = Object.fromEntries(Object.entries(form.numbers).filter(([, v]) => v !== "").map(([k, v]) => [k, Number(v)]));
    const pricing: any = { currency: form.currency, retainer: Number(form.retainer), options: form.options };
    for (const k of ["performance_pct", "pilot_fee", "pilot_days", "term_months"]) if (form[k] !== "") pricing[k] = Number(form[k]);
    const r = await act("build", () => post("/prospects/proposals", { prospect_id: p.id, call_notes: form.call_notes, numbers, pricing, include_projection: form.include_projection }));
    if (r) { setSelected((r as any).proposal.id); setBuilding(false); onChanged(); }
  };
  const toggle = (k: string) => setForm({ ...form, options: form.options.includes(k) ? form.options.filter((x: string) => x !== k) : [...form.options, k] });
  if (locked && proposals.length === 0) return <Empty title="No proposal" hint="This prospect can't be contacted from this list." />;
  return (
    <div className="space-y-4">
      {proposals.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {proposals.map((pr) => <Button key={pr.id} size="sm" tone={selected === pr.id && !building ? "primary" : "secondary"} onClick={() => { setSelected(pr.id); setBuilding(false); }}>{pr.status_label} · {when(pr.created)}</Button>)}
          {!locked && <Button size="sm" onClick={() => setBuilding(true)}><Plus className="size-3.5" /> New proposal</Button>}
        </div>
      )}
      {building && !locked ? (
        <Card title="Build a proposal" subtitle="From your call notes and their own numbers. Lines that read like income or results claims are left out.">
          <div className="space-y-3">
            <Field label="Call notes" hint="Write what they said, in their words where you can"><TextArea rows={5} value={form.call_notes} onChange={(e) => setForm({ ...form, call_notes: e.target.value })} /></Field>
            <div><div className="mb-1 text-xs font-medium text-muted">Their numbers (optional)</div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {NUM_FIELDS.map(([k, label]) => <Field key={k} label={label}><Input type="number" min={0} value={form.numbers[k] ?? ""} onChange={(e) => setForm({ ...form, numbers: { ...form.numbers, [k]: e.target.value } })} /></Field>)}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              <Field label="Currency"><Select value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })} options={["USD", "GBP", "EUR", "CAD", "AUD"]} /></Field>
              <Field label="Your monthly fee"><Input type="number" min={1} value={form.retainer} onChange={(e) => setForm({ ...form, retainer: e.target.value })} /></Field>
              <Field label="Performance share %"><Input type="number" min={0} max={50} value={form.performance_pct} onChange={(e) => setForm({ ...form, performance_pct: e.target.value })} /></Field>
              <Field label="Pilot fee"><Input type="number" min={1} value={form.pilot_fee} placeholder="60% of the fee" onChange={(e) => setForm({ ...form, pilot_fee: e.target.value })} /></Field>
              <Field label="Pilot days"><Input type="number" min={7} max={120} value={form.pilot_days} onChange={(e) => setForm({ ...form, pilot_days: e.target.value })} /></Field>
              <Field label="Term (months)"><Input type="number" min={1} max={24} value={form.term_months} onChange={(e) => setForm({ ...form, term_months: e.target.value })} /></Field>
            </div>
            <div className="flex flex-wrap gap-3 text-sm">
              {[["retainer", "Monthly retainer"], ["retainer_performance", "Retainer + performance share"], ["pilot", "Paid pilot"]].map(([k, l]) => (
                <label key={k} className="flex items-center gap-1.5"><input type="checkbox" checked={form.options.includes(k)} onChange={() => toggle(k)} /> {l}</label>
              ))}
              <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.include_projection} onChange={(e) => setForm({ ...form, include_projection: e.target.checked })} /> Include a projection (with assumptions)</label>
            </div>
            <ErrorBox error={err} />
            {(!Number(form.retainer) || form.options.length === 0) && <p className="text-xs text-muted">Set your monthly fee and pick at least one option to build the proposal.</p>}
            <div className="flex flex-wrap gap-2">
              <Button tone="primary" loading={busy === "build"} disabled={!Number(form.retainer) || form.options.length === 0} onClick={build}>Build proposal</Button>
              {proposals.length > 0 && <Button onClick={() => setBuilding(false)}>Cancel</Button>}
            </div>
          </div>
        </Card>
      ) : selected ? <ProposalEditor key={selected} id={selected} p={p} onChanged={onChanged} /> : null}
    </div>
  );
}

function ProposalEditor({ id, p, onChanged }: { id: number; p: any; onChanged: () => void }) {
  const { go } = useApp();
  const { data, error, loading, reload } = useApi<any>(`/prospects/proposals/${id}`, [id]);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [channel, setChannel] = useState(p.email ? "email" : "instagram_dm");
  const { busy, err, act } = useAct();
  const pr = data?.proposal;
  useEffect(() => { if (pr) setTexts(Object.fromEntries(pr.sections.map((s: any) => [s.key, s.text]))); }, [pr]);
  const dirty = useMemo(() => !!pr && pr.sections.some((s: any) => texts[s.key] !== undefined && texts[s.key] !== s.text), [pr, texts]);
  if (loading && !pr) return <Loading rows={5} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!pr) return null;
  const draft = pr.status === "draft";
  const save = async () => {
    const sections = pr.sections.filter((s: any) => !["options", "no_guarantee"].includes(s.key) && texts[s.key] !== s.text).map((s: any) => ({ key: s.key, text: texts[s.key] }));
    if (await act("save", () => put(`/prospects/proposals/${id}`, { sections }))) { reload(); onChanged(); }
  };
  const send = async () => {
    const r = await act("send", () => post(`/prospects/proposals/${id}/send`, { channel }));
    if (r) { reload(); onChanged(); go(`/queue?ids=${(r as any).queue_id}`); }
  };
  const withdraw = async () => {
    if (!window.confirm("Withdraw this proposal? The link will stop working.")) return;
    if (await act("withdraw", () => post(`/prospects/proposals/${id}/withdraw`))) { reload(); onChanged(); }
  };
  return (
    <Card title={pr.title} subtitle={pr.label} actions={<><Badge tone={pr.status === "accepted" ? "good" : pr.blocked ? "bad" : "neutral"}>{pr.status_label}</Badge><TraceLink runId={pr.run_id} /></>}>
      <div className="space-y-3" data-testid="proposal-editor">
        {pr.url && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg bg-panel-2 p-2 text-xs">
            <span className="min-w-0 flex-1 break-all">{pr.url}</span><CopyButton text={pr.url.startsWith("http") ? pr.url : location.origin + pr.url} label="Copy link" />
            <span className="text-muted">Opened {pr.views || 0} times</span>
          </div>
        )}
        {pr.status === "accepted" && <p className="rounded-md bg-green-50 px-2 py-1 text-sm text-green-900">Accepted by {pr.accepted_name} on {when(pr.accepted_at)}. The prospect is now a client.</p>}
        <FlagList flags={pr.flags} />
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">Options</div>
          <div className="grid gap-2 sm:grid-cols-3">
            {pr.options.map((o: any) => (
              <div key={o.key} className={cx("rounded-lg border p-2 text-sm", pr.accepted_option === o.key ? "border-good" : "border-line")}>
                <div className="font-medium">{o.label}</div>
                <div className="text-lg font-semibold">{new Intl.NumberFormat(undefined, { style: "currency", currency: o.currency, maximumFractionDigits: 0 }).format(o.amount)}<span className="text-xs font-normal text-muted"> {o.per === "month" ? "a month" : "one-off"}</span></div>
                <p className="text-xs text-muted">{o.summary}</p>
              </div>
            ))}
          </div>
        </div>
        {pr.sections.filter((s: any) => s.key !== "options").map((s: any) => (
          <div key={s.key}>
            <div className="mb-1 text-xs font-semibold text-muted">{s.title}</div>
            {draft && s.key !== "no_guarantee" ? <TextArea rows={Math.min(8, Math.max(3, (texts[s.key] || "").split("\n").length + 1))} value={texts[s.key] ?? s.text} onChange={(e) => setTexts({ ...texts, [s.key]: e.target.value })} />
              : <p className="whitespace-pre-wrap text-sm">{s.text}</p>}
            {s.assumptions?.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs text-muted">{s.assumptions.map((a: string) => <li key={a}>Assumption: {a}</li>)}</ul>}
          </div>
        ))}
        <ErrorBox error={err} />
        <div className="flex flex-wrap items-end gap-2">
          {draft && <Button size="sm" disabled={!dirty} loading={busy === "save"} onClick={save}>Save changes</Button>}
          {draft && (
            <>
              <div className="w-full sm:w-40"><Field label="Send by"><Select value={channel} onChange={(e) => setChannel(e.target.value)} options={[{ value: "email", label: "Email" }, { value: "instagram_dm", label: "Instagram DM" }]} /></Field></div>
              <Button size="sm" tone="primary" disabled={dirty || pr.blocked} loading={busy === "send"} onClick={send}><Send className="size-3.5" /> Send for approval</Button>
            </>
          )}
          {["draft", "queued", "published"].includes(pr.status) && <Button size="sm" tone="danger" loading={busy === "withdraw"} onClick={withdraw}>Withdraw</Button>}
        </div>
        {draft && dirty && <p className="text-xs text-muted">Save your changes before sending.</p>}
      </div>
    </Card>
  );
}

function Timeline({ items }: { items: any[] }) {
  if (!items.length) return <Empty title="Nothing yet" />;
  return (
    <ol className="space-y-2" data-testid="timeline">
      {items.map((e) => (
        <li key={e.id} className="rounded-lg border border-line p-2 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium">{EVENT_LABEL[e.kind] || "Update"}</span><span className="text-xs text-muted">{when(e.created)}</span>
          </div>
          {e.kind === "stage" && <div className="text-xs text-muted">{STAGE_LABEL[e.detail.from] || "Start"} → {STAGE_LABEL[e.detail.to] || ""}{e.detail.reason ? ` (${e.detail.reason})` : ""}</div>}
          {e.kind === "reply" && <div className="text-xs">{e.detail.classification_label}{e.detail.text ? `: “${e.detail.text}”` : ""}</div>}
          {e.kind === "dnc" && e.detail.reason && <div className="text-xs text-muted">{e.detail.reason}</div>}
          {e.kind === "fact_added" && <div className="text-xs text-muted">{e.detail.claim} ({e.detail.source})</div>}
          {e.kind === "booked" && <div className="text-xs text-muted">{when(e.detail.start)}</div>}
        </li>
      ))}
    </ol>
  );
}
