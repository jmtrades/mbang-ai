import { useEffect, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, post, put } from "../api";
import {
  Badge, Button, Card, Citations, CopyButton, DemoBadge, Empty, ErrorBox, Field, Input, Loading, Modal, PageHeader, Select, Stat,
  Table, Tabs, TextArea, TraceLink, isDemo, useApi, useApp,
} from "../components/ui";

const Q_STATUS: Record<string, string> = {
  pending: "Waiting for approval", needs_human: "Needs a person", held: "Blocked by a guardrail", approved: "Approved",
  rejected: "Rejected", scheduled: "Scheduled", awaiting_manual_send: "Ready to send by hand",
};
const MONEY = new Set(["ad_spend", "revenue"]);
const ENTRY_KEYS: [string, string][] = [["leads", "Leads"], ["booked", "Calls booked"], ["showed", "Calls showed"], ["closed", "Sales closed"], ["ad_spend", "Ad spend ($)"], ["revenue", "Cash collected ($)"]];
const BRIEF_KEYS: [string, string, string][] = [
  ["offer", "What they sell", "e.g. a 12-week coaching programme"], ["niche", "Their market", "e.g. online fitness coaches"],
  ["audience", "Who it's for", "e.g. busy professionals"], ["outcome", "The result customers want", "e.g. five new clients a month"],
  ["mechanism", "Their method", "e.g. content-to-DM system"], ["pain", "What customers are tired of", "e.g. posting with no enquiries"],
  ["timeframe", "Typical timeframe", "e.g. 90 days"],
];
const ACCESS_KEYS: [string, string, string][] = [
  ["ad_account_id", "Ad account ID", "act_1234567890"], ["pixel_id", "Pixel ID", "1234567890"], ["crm", "CRM", "e.g. their pipeline tool"],
  ["calendar_url", "Calendar link", "https://..."], ["domain", "Domain", "example.com"], ["notes", "Notes", "Who granted access and when"],
];
const BRIEF_LABEL: Record<string, string> = {
  business: "business name", niche: "their market", audience: "who it's for", offer: "what they sell", price: "price",
  outcome: "the result customers want", timeframe: "timeframe", mechanism: "their method", pain: "what customers are tired of",
  channels: "channels",
};
const missingList = (keys: string[] = []) => keys.map((k) => BRIEF_LABEL[k] || k.replace(/_/g, " ")).join(", ");
const fmt = (k: string, v: any) => (v === null || v === undefined ? "Not recorded" : MONEY.has(k) ? `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : Number(v).toLocaleString());
const day = (ts: number) => new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
const clean = (s: string) => (s || "").replace(" (DEMO)", "");
const abs = (u: string | null) => (!u ? "" : u.startsWith("http") ? u : `${location.origin}${u}`);

function StatusBadge({ c }: { c: any }) {
  const tone = c.status === "onboarding" ? "info" : c.status === "active" ? "good" : c.status === "paused" ? "warn" : "neutral";
  return <Badge tone={tone as any}>{c.status_label || c.status}</Badge>;
}

function FulfilmentResult({ res }: { res: any }) {
  const { go, me } = useApp();
  return (
    <div className="grid gap-4 xl:grid-cols-2" data-testid="fulfilment">
      {res.brief_missing?.length > 0 && (
        <div className="xl:col-span-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          Written with neutral wording for: {missingList(res.brief_missing)}. Fill in the client's brief (or wait for their intake form) for copy in their own words.
        </div>
      )}
      <Card title={`Money model: ${clean(res.client.name)}`} actions={<TraceLink runId={res.run_id} />}>
        <Table rows={res.money_model.scenarios} cols={[{ key: "name", label: "Scenario" }, { key: "leads", label: "Leads" }, { key: "sales", label: "Sales" }, { key: "revenue", label: "Revenue", render: (s) => `$${Number(s.revenue).toLocaleString()}` }, { key: "assumptions", label: "Assumption" }]} />
        <ul className="mt-2 list-disc pl-5 text-xs text-muted">{res.money_model.labels.map((l: string) => <li key={l}>{l}</li>)}</ul>
      </Card>
      <Card title="Bottleneck">
        {res.analysis.bottleneck ? <p className="text-sm"><b>{res.analysis.bottleneck.label}</b>: {res.analysis.bottleneck.kind === "rate" ? `${(res.analysis.bottleneck.value * 100).toFixed(1)}% against ${(res.analysis.bottleneck.benchmark * 100).toFixed(0)}%` : `$${res.analysis.bottleneck.value}`} · about ${Number(res.analysis.bottleneck.monthly_cost).toLocaleString()} a month</p> : <p className="text-sm">No stage is below its benchmark with enough data to call it.</p>}
      </Card>
      <Card title="Launch plan">
        <ol className="space-y-2 text-sm">{res.launch_plan.map((p: any) => <li key={p.week}><b>{p.week}:</b> {p.step}{p.citation && <Citations items={[p.citation]} />}</li>)}</ol>
      </Card>
      <Card title="Ad hooks (reviewed)" subtitle={res.hooks.regenerated_once ? "Weak hooks were rewritten once" : "All hooks passed first time"}>
        <ul className="space-y-1 text-sm">{res.hooks.hooks.map((h: any) => <li key={h.hook} className="flex items-center justify-between gap-2"><span className="min-w-0">{h.hook}</span><Badge tone={h.score >= 60 ? "good" : "warn"}>{h.score}</Badge></li>)}</ul>
      </Card>
      <Card title="Client report" actions={<CopyButton text={res.report} />}>
        <pre className="prose-pre text-sm">{res.report}</pre>
        <Button size="sm" className="mt-2" onClick={() => go(`/queue?ids=${res.queue_ids.join(",")}`)}>{me.user.role === "owner" ? `Review ${res.queue_ids.length} items waiting for approval` : "Review the report"}</Button>
        {me.user.role !== "owner" && <p className="mt-2 text-xs text-muted">You approve the client report. The ad hooks, VSL outline and launch plan wait for the owner's approval.</p>}
      </Card>
      <Card title="VSL outline"><ol className="list-decimal pl-5 text-sm">{res.vsl_outline.map((v: string) => <li key={v}>{v}</li>)}</ol></Card>
    </div>
  );
}

function Onboarding({ c, reload }: { c: any; reload: () => void }) {
  const { toast, go } = useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const checks = (c.tasks || []).filter((t: any) => t.kind === "check");
  const other = (c.tasks || []).filter((t: any) => t.kind !== "check");
  const act = async (key: string, fn: () => Promise<any>, ok?: (r: any) => void) => {
    setBusy(key);
    setErr(null);
    try {
      const r = await fn();
      ok?.(r);
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const kickoff = (c.queue || []).find((q: any) => q.kind === "booking_dm");
  if (!checks.length) {
    return (
      <Empty title="Onboarding hasn't started for this client" hint="Starting it creates the access checklist and an intake link, drafts a kickoff invite for your approval and runs fulfilment from the client's brief."
        action={<Button tone="primary" loading={busy === "start"} onClick={() => act("start", () => post(`/clients/${c.id}/onboard`), () => toast("Onboarding started", "good"))}>Start onboarding</Button>} />
    );
  }
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_340px]">
      <Card title="Access checklist" subtitle="Ticked by hand once done. Items with an automatic check can be confirmed when the workspace's live connection is on.">
        <ErrorBox error={err} />
        <ul className="space-y-2">
          {checks.map((t: any) => (
            <li key={t.key} className="rounded-lg border border-line px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex min-w-0 flex-1 items-start gap-2 text-sm">
                  <input type="checkbox" className="mt-1" checked={t.status === "done"} disabled={busy === t.key}
                    onChange={(e) => act(t.key, () => post(`/clients/${c.id}/tasks/${t.key}`, { done: e.target.checked }))} aria-label={t.title} />
                  <span className="min-w-0"><span className="font-medium">{t.title}</span><span className="block text-xs text-muted">{t.detail}</span></span>
                </label>
                {t.status === "done" && <Badge tone="good">{t.verified_by === "live" ? "Checked automatically" : t.verified_by === "client" ? "Sent by the client" : "Done"}</Badge>}
                {t.live_check && t.status !== "done" && (
                  <Button size="sm" loading={busy === `v-${t.key}`} onClick={() => act(`v-${t.key}`, () => post(`/clients/${c.id}/tasks/${t.key}/verify`), (r) => toast(r.verified ? "Confirmed automatically" : r.reason, r.verified ? "good" : "info"))}>Check automatically</Button>
                )}
              </div>
            </li>
          ))}
        </ul>
        {other.length > 0 && (
          <div className="mt-4"><div className="mb-1 text-xs font-semibold text-muted">Calls and alerts</div>
            <ul className="space-y-1 text-sm">{other.slice(-6).map((t: any) => <li key={t.id} className="flex flex-wrap items-center gap-2"><Badge tone={t.status === "missed" || t.kind === "risk" ? "warn" : "neutral"}>{t.kind === "call" ? (t.status === "missed" ? "Missed call" : "Call held") : "Alert"}</Badge><span className="min-w-0">{t.detail || t.title}</span></li>)}</ul>
          </div>
        )}
      </Card>
      <div className="space-y-4">
        <Card title="Intake form" subtitle={c.intake_at ? `Answers received ${day(c.intake_at)}` : "Not filled in yet"}>
          {c.intake_url ? (
            <div className="space-y-2">
              <p className="break-all rounded-md bg-panel-2 px-2 py-1 font-mono text-xs">{abs(c.intake_url)}</p>
              <div className="flex flex-wrap gap-2"><CopyButton text={abs(c.intake_url)} label="Copy link" /><a className="text-xs text-brand underline" href={abs(c.intake_url)} target="_blank" rel="noreferrer">Open the form</a></div>
              {c.intake_link_needs_public_url && <p className="text-xs text-muted">Emails show "[your workspace address]" in place of the web address until the server's public address is set.</p>}
            </div>
          ) : <p className="text-sm text-muted">The link appears once onboarding starts.</p>}
        </Card>
        <Card title="Kickoff invite">
          {kickoff ? (
            <div className="space-y-2 text-sm"><p>{kickoff.title}</p><Badge tone={kickoff.status === "approved" ? "good" : "info"}>{Q_STATUS[kickoff.status] || kickoff.status}</Badge>
              <div><Button size="sm" onClick={() => go(`/queue?ids=${kickoff.id}`)}>Open in the approval queue</Button></div></div>
          ) : <p className="text-sm text-muted">No kickoff invite drafted.</p>}
        </Card>
      </div>
    </div>
  );
}

function Numbers({ c, reload }: { c: any; reload: () => void }) {
  const { toast } = useApp();
  const [vals, setVals] = useState<Record<string, string>>({});
  const [csv, setCsv] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const series = (c.history || []).reduce((acc: any[], h: any) => {
    const last = acc[acc.length - 1];
    if (last && last.period_start === h.period_start) Object.assign(last, h.metrics);
    else acc.push({ period_start: h.period_start, label: day(h.period_start), ...h.metrics });
    return acc;
  }, []);
  const save = async () => {
    const metrics: Record<string, number> = {};
    for (const [k] of ENTRY_KEYS) if (vals[k] !== undefined && vals[k] !== "") metrics[k] = Number(vals[k]);
    setBusy("save");
    setErr(null);
    try {
      await post(`/clients/${c.id}/metrics`, { metrics });
      setVals({});
      toast("This week's numbers saved", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const importCsv = async () => {
    setBusy("csv");
    setErr(null);
    try {
      const r = await post(`/clients/${c.id}/metrics/csv`, { csv });
      setCsv("");
      toast(`Imported ${r.ads} ads from the export`, "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const readFile = (f: File | undefined) => f && f.text().then(setCsv);
  const call = async (outcome: string) => {
    setBusy(outcome);
    try {
      await post(`/clients/${c.id}/calls`, { outcome });
      toast(outcome === "held" ? "Call logged" : "Missed call logged", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4">
      <ErrorBox error={err} />
      <Card title="This week against target">
        {c.kpis?.length ? (
          <Table rows={c.kpis} cols={[
            { key: "label", label: "Number" }, { key: "value", label: "Latest", render: (k) => fmt(k.key, k.value) },
            { key: "target", label: "Target", render: (k) => (k.target === null || k.target === undefined ? "No target" : fmt(k.key, k.target)) },
            { key: "ok", label: "", render: (k) => (k.ok === null ? null : <Badge tone={k.ok ? "good" : "warn"}>{k.ok ? "On target" : "Below target"}</Badge>) },
          ]} />
        ) : <Empty title="No numbers yet" hint="Add this week's numbers below or import an Ads Manager export." />}
      </Card>
      {series.length > 1 && (
        <Card title="Trend" subtitle={c.is_demo || isDemo(c.name) ? "Fictional demo numbers" : "Week by week"}>
          <div className="h-64"><ResponsiveContainer><LineChart data={series}><CartesianGrid strokeDasharray="3 3" stroke="#e4e3dd" /><XAxis dataKey="label" /><YAxis allowDecimals={false} width={36} /><Tooltip /><Legend />
            <Line type="monotone" dataKey="leads" name="Leads" stroke="#5b6cff" /><Line type="monotone" dataKey="booked" name="Calls booked" stroke="#16a34a" /><Line type="monotone" dataKey="closed" name="Sales" stroke="#b45309" />
          </LineChart></ResponsiveContainer></div>
        </Card>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Enter this week's numbers" subtitle="A second entry in the same week updates it.">
          <div className="grid gap-3 sm:grid-cols-2">
            {ENTRY_KEYS.map(([k, label]) => <Field key={k} label={label}><Input inputMode="decimal" value={vals[k] || ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value.replace(/[^\d.]/g, "") })} aria-label={label} /></Field>)}
          </div>
          <Button tone="primary" className="mt-3" loading={busy === "save"} disabled={!Object.values(vals).some((v) => v !== "")} onClick={save}>Save numbers</Button>
        </Card>
        <Card title="Import an Ads Manager export" subtitle="CSV with amount spent, results or leads, impressions and link clicks. Nothing is written to the ad account.">
          <input type="file" accept=".csv,text/csv" className="mb-2 block w-full text-xs" onChange={(e) => readFile(e.target.files?.[0])} aria-label="Choose a CSV file" />
          <TextArea rows={4} value={csv} onChange={(e) => setCsv(e.target.value)} placeholder="Or paste the CSV here" aria-label="CSV text" />
          <Button className="mt-2" loading={busy === "csv"} disabled={csv.length < 10} onClick={importCsv}>Import</Button>
        </Card>
      </div>
      <Card title="Client calls" subtitle="Two missed calls in a row raise a churn-risk alert.">
        <div className="flex flex-wrap gap-2"><Button size="sm" loading={busy === "held"} onClick={() => call("held")}>Log a call held</Button><Button size="sm" tone="danger" loading={busy === "missed"} onClick={() => call("missed")}>Log a missed call</Button></div>
      </Card>
    </div>
  );
}

function Weekly({ c, reload }: { c: any; reload: () => void }) {
  const { go, toast } = useApp();
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post(`/clients/${c.id}/weekly`);
      setRes(r);
      toast("Weekly report drafted for approval", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const risk = c.risk;
  const reports = (c.queue || []).filter((q: any) => q.kind === "client_report");
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Weekly report" subtitle={`Runs every ${c.weekday_label}: numbers, diagnosis, ad change proposals, a report draft and churn checks. Every email waits for approval.`}>
        <ErrorBox error={err} />
        <Button tone="primary" loading={busy} onClick={run}>Draft this week's report now</Button>
        {res && (
          <div className="mt-3 space-y-2 text-sm">
            <p className="flex flex-wrap items-center gap-2"><Badge tone={res.mode === "rules" ? "neutral" : "good"}>{res.mode_label}</Badge><TraceLink runId={res.run_id} /></p>
            {!res.fresh && <p className="text-xs text-muted">No numbers for this week yet, so the report uses the latest on file.</p>}
            {res.analysis?.bottleneck && <p>Stage to fix first: <b>{res.analysis.bottleneck.label}</b></p>}
            {res.proposals?.length > 0 && <p>{res.proposals.length} ad change(s) proposed for the owner's approval.</p>}
            <div className="flex flex-wrap gap-2"><Button size="sm" onClick={() => go(`/queue?ids=${[res.report_queue_id, res.monthly_queue_id].filter(Boolean).join(",")}`)}>Review the report{res.monthly_queue_id ? " and monthly review" : ""}</Button></div>
          </div>
        )}
        {reports.length > 0 && (
          <div className="mt-4"><div className="mb-1 text-xs font-semibold text-muted">Recent reports</div>
            <ul className="space-y-1 text-sm">{reports.slice(0, 5).map((q: any) => <li key={q.id} className="flex flex-wrap items-center justify-between gap-2"><button className="min-w-0 text-left text-brand underline-offset-2 hover:underline" onClick={() => go(`/queue?ids=${q.id}`)}>{q.title}</button><Badge tone={q.status === "approved" ? "good" : "info"}>{Q_STATUS[q.status] || q.status}</Badge></li>)}</ul>
          </div>
        )}
      </Card>
      <Card title="Churn risk" subtitle="Falling numbers, numbers far below target, missed calls, no contact for two weeks, late onboarding.">
        {risk?.signals?.length ? (
          <ul className="space-y-1 text-sm">{risk.signals.map((s: any) => <li key={s.code} className="flex items-start gap-2"><Badge tone={s.severity === "high" ? "bad" : "warn"}>{s.severity === "high" ? "High" : "Medium"}</Badge><span className="min-w-0">{s.label}</span></li>)}</ul>
        ) : <p className="text-sm text-muted">No risk signals at the last check.</p>}
        {risk?.escalation_id && <p className="mt-2 text-xs text-muted">Raised as alert #{risk.escalation_id}.</p>}
      </Card>
    </div>
  );
}

function BriefAccess({ c, reload }: { c: any; reload: () => void }) {
  const { toast, me } = useApp();
  const owner = me.user.role === "owner";
  const [b, setB] = useState<Record<string, string>>({});
  const [a, setA] = useState<Record<string, string>>({});
  const [f, setF] = useState<any>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    setB(Object.fromEntries(BRIEF_KEYS.map(([k]) => [k, (c.brief_missing || []).includes(k) ? "" : c.brief?.[k] ?? ""])));
    setA({ ...(c.access || {}) });
    setF({ status: c.status, report_weekday: String(c.report_weekday ?? 0), email: c.email || "", contact_name: c.contact_name || "" });
  }, [c.id]);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const brief = Object.fromEntries(Object.entries(b).filter(([, v]) => v.trim()));
      await put(`/clients/${c.id}`, { brief, access: a, status: f.status, report_weekday: Number(f.report_weekday), email: f.email || undefined, contact_name: f.contact_name || undefined });
      toast("Saved", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-4">
      <ErrorBox error={err} />
      <Card title="The client's brief" subtitle="Every ad, page, report and email for this client is written from these words. The intake form fills them in too.">
        {c.brief_missing?.length > 0 && <p className="mb-3 text-xs text-muted">Still using neutral wording for: {missingList(c.brief_missing)}.</p>}
        <div className="grid gap-3 sm:grid-cols-2">{BRIEF_KEYS.map(([k, label, ph]) => <Field key={k} label={label}><Input value={b[k] || ""} placeholder={ph} onChange={(e) => setB({ ...b, [k]: e.target.value })} aria-label={label} /></Field>)}</div>
        {c.proof && <p className="mt-3 text-xs text-muted">Proof on file: {c.proof.permission ? "the client confirmed permission to use it" : "no permission given, so it isn't used in any asset"}.</p>}
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Access" subtitle="IDs and links only. Never store passwords or tokens here.">
          <div className="grid gap-3 sm:grid-cols-2">{ACCESS_KEYS.map(([k, label, ph]) => {
            const locked = k === "ad_account_id" && !owner;
            return <Field key={k} label={label} hint={locked ? "The workspace owner links the ad account, because ad reads and change proposals run on it." : undefined}><Input value={a[k] || ""} placeholder={locked ? "Linked by the owner" : ph} disabled={locked} onChange={(e) => setA({ ...a, [k]: e.target.value })} aria-label={label} /></Field>;
          })}</div>
        </Card>
        <Card title="Contact and schedule">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Contact name"><Input value={f.contact_name} onChange={(e) => setF({ ...f, contact_name: e.target.value })} /></Field>
            <Field label="Report email"><Input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
            <Field label="Weekly report day"><Select value={f.report_weekday} onChange={(e) => setF({ ...f, report_weekday: e.target.value })} options={["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => ({ value: String(i), label: d }))} /></Field>
            <Field label="Status"><Select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })} options={[{ value: "onboarding", label: "Onboarding" }, { value: "active", label: "Active" }, { value: "paused", label: "Paused" }, { value: "ended", label: "Ended" }]} /></Field>
          </div>
        </Card>
      </div>
      <Button tone="primary" loading={busy} onClick={save}>Save changes</Button>
    </div>
  );
}

function ClientDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error, loading, reload } = useApi<any>(`/clients/${id}`, [id]);
  const [tab, setTab] = useState("onboarding");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const fulfil = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post(`/clients/${id}/fulfil`));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  if (loading && !data) return <Card><Loading rows={5} /></Card>;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  return (
    <Card title={<span className="flex flex-wrap items-center gap-2">{clean(data.name)} {isDemo(data.name) && <DemoBadge />} <StatusBadge c={data} /></span>}
      subtitle={[data.niche, data.offer && clean(data.offer)].filter(Boolean).join(" · ")} actions={<Button size="sm" tone="ghost" onClick={onClose}>Close</Button>}>
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "onboarding", label: "Onboarding" }, { key: "numbers", label: "Numbers" }, { key: "weekly", label: "Weekly report" }, { key: "fulfilment", label: "Fulfilment" }, { key: "brief", label: "Brief and access" }]} />
      {tab === "onboarding" && <Onboarding c={data} reload={reload} />}
      {tab === "numbers" && <Numbers c={data} reload={reload} />}
      {tab === "weekly" && <Weekly c={data} reload={reload} />}
      {tab === "fulfilment" && (
        <div className="space-y-4">
          <Card title="Fulfilment" subtitle="Money model, bottleneck, cited launch plan, reviewed ad hooks, VSL outline and a client report, written from this client's brief and waiting for approval.">
            <ErrorBox error={err} retry={fulfil} />
            <Button tone="primary" loading={busy} onClick={fulfil}>Run fulfilment</Button>
          </Card>
          {res && <FulfilmentResult res={res} />}
        </div>
      )}
      {tab === "brief" && <BriefAccess c={data} reload={reload} />}
    </Card>
  );
}

function AddClient({ open, onClose, onAdded }: { open: boolean; onClose: () => void; onAdded: (id: number) => void }) {
  const { me, toast } = useApp();
  const blank = { name: me.is_demo ? "Harbour Yoga Studio (DEMO)" : "", niche: "", offer: "", price: "", contact_name: "", email: "" };
  const [f, setF] = useState<any>(blank);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/clients", { ...f, price: Number(f.price) || 0, onboard: true });
      toast("Client added and onboarding started", "good");
      setF(blank);
      onAdded(r.id);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={onClose} title="Add a client you've won">
      <p className="mb-3 text-sm text-muted">Adding a client starts onboarding: an access checklist, an intake link, a kickoff invite for your approval and a first fulfilment run.</p>
      <ErrorBox error={err} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Business name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Their market"><Input value={f.niche} placeholder="e.g. online fitness coaches" onChange={(e) => setF({ ...f, niche: e.target.value })} /></Field>
        <Field label="What they sell"><Input value={f.offer} onChange={(e) => setF({ ...f, offer: e.target.value })} /></Field>
        <Field label="Their offer price ($)"><Input inputMode="decimal" value={f.price} onChange={(e) => setF({ ...f, price: e.target.value.replace(/[^\d.]/g, "") })} /></Field>
        <Field label="Contact name"><Input value={f.contact_name} onChange={(e) => setF({ ...f, contact_name: e.target.value })} /></Field>
        <Field label="Contact email"><Input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
      </div>
      <Button tone="primary" className="mt-3" loading={busy} disabled={!f.name.trim()} onClick={save}>Add and start onboarding</Button>
    </Modal>
  );
}

export default function Clients() {
  const { me } = useApp();
  const { data, error, loading, reload } = useApi<any>("/clients");
  const [sel, setSel] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const items: any[] = data?.items || [];
  const owner = me.user.role === "owner";
  const count = (s: string) => items.filter((c) => c.status === s).length;
  return (
    <div>
      <PageHeader title="Clients" subtitle={owner ? "Onboarding after the win, weekly numbers and reports, and fulfilment for every client. Every email waits for approval." : "Your clients: onboarding, weekly numbers and reports. Every email waits for your approval."}
        right={<Button tone="primary" onClick={() => setAdding(true)}>Add a client</Button>} />
      {loading && !data && <Loading rows={4} />}
      <ErrorBox error={error} retry={reload} />
      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Onboarding" value={count("onboarding")} />
            <Stat label="Active" value={count("active")} />
            <Stat label="At risk" value={items.filter((c) => c.risk?.signals?.length).length} tone={items.some((c) => c.risk?.signals?.length) ? "warn" : undefined} />
            <Stat label="Paused or ended" value={count("paused") + count("ended")} />
          </div>
          <Card pad={false} className="mb-4"><div className="p-3">
            {items.length ? (
              <Table rows={items} onRow={(c) => setSel(c.id)} cols={[
                { key: "name", label: "Client", render: (c) => <span className="flex items-center gap-1.5">{clean(c.name)} {isDemo(c.name) && <DemoBadge />}</span> },
                { key: "status", label: "Status", render: (c) => <StatusBadge c={c} /> },
                { key: "checklist", label: "Onboarding", render: (c) => (c.checklist.total ? `${c.checklist.done} of ${c.checklist.total} done` : c.status === "onboarding" ? "Not started" : <span className="text-muted">No checklist</span>) },
                { key: "risk", label: "Risk", render: (c) => (c.risk?.signals?.length ? <Badge tone={c.risk.severity === "high" ? "bad" : "warn"}>{c.risk.signals.length} signal(s)</Badge> : <span className="text-muted">None</span>) },
                { key: "go", label: "", render: (c) => <Button size="sm" onClick={(e) => { e.stopPropagation(); setSel(c.id); }}>Open</Button> },
              ]} />
            ) : (
              <Empty title={owner ? "No clients yet" : "No clients yet"} hint={owner ? "When a prospect is marked as won, the client appears here with onboarding already started. You can also add one by hand." : "When one of your prospects says yes, mark them as won and the client appears here with onboarding started."}
                action={<Button onClick={() => setAdding(true)}>Add a client</Button>} />
            )}
          </div></Card>
        </>
      )}
      {sel !== null && <ClientDetail key={sel} id={sel} onClose={() => setSel(null)} />}
      <AddClient open={adding} onClose={() => setAdding(false)} onAdded={(id) => { setAdding(false); reload(); setSel(id); }} />
    </div>
  );
}
