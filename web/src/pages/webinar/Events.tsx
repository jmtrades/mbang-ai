import { useState } from "react";
import { ApiError, post, put } from "../../api";
import { Badge, Button, Card, CopyButton, DemoBadge, Empty, ErrorBox, Field, Input, Loading, Select, Stat, Table, TextArea, TraceLink, isDemo, useApi, useApp } from "../../components/ui";

const Q_STATUS: Record<string, string> = {
  pending: "Waiting for approval", needs_human: "Needs a person", held: "Blocked by a guardrail", approved: "Approved",
  rejected: "Rejected", scheduled: "Scheduled", awaiting_manual_send: "Approved",
};
const ZONES = ["UTC", "Europe/London", "Europe/Paris", "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney", "Asia/Dubai"];
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "No data" : `${(v * 100).toFixed(0)}%`);
const clean = (s: string) => (s || "").replace(" (DEMO)", "");
const abs = (u: string) => (u.startsWith("http") ? u : `${location.origin}${u}`);

function NewWebinar({ chains, clients, onCreated }: { chains: any[]; clients: any[]; onCreated: (id: number) => void }) {
  const { toast } = useApp();
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const zones = ZONES.includes(browserTz) ? ZONES : [browserTz, ...ZONES];
  const [f, setF] = useState<any>({ title: "", starts_at: "", tz: browserTz, join_url: "", replay_url: "", duration_min: "60", offer_at_min: "35", chain_id: "", client_id: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/webinars", {
        title: f.title, starts_at: f.starts_at, tz: f.tz, join_url: f.join_url, replay_url: f.replay_url,
        duration_min: Number(f.duration_min) || 60, offer_at_min: Number(f.offer_at_min) || 35,
        chain_id: f.chain_id || null, client_id: f.client_id ? Number(f.client_id) : null,
      });
      toast("Webinar planned. Approve the page and emails in the queue.", "good");
      onCreated(r.id);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Plan a webinar" subtitle="The registration page and every reminder and replay email are drafted for approval. Each email is approved once, then goes to every registrant at the right time.">
      <ErrorBox error={err} />
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="sm:col-span-2"><Field label="Title"><Input value={f.title} placeholder={f.chain_id ? "Leave empty to use the chain's title" : "e.g. The three numbers every coach should track"} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field></div>
        <Field label="Starts at"><Input type="datetime-local" value={f.starts_at} onChange={(e) => setF({ ...f, starts_at: e.target.value })} /></Field>
        <Field label="Time zone"><Select value={f.tz} onChange={(e) => setF({ ...f, tz: e.target.value })} options={zones} /></Field>
        <Field label="Join link"><Input value={f.join_url} placeholder="https://..." onChange={(e) => setF({ ...f, join_url: e.target.value })} /></Field>
        <Field label="Replay link (can be added later)"><Input value={f.replay_url} placeholder="https://..." onChange={(e) => setF({ ...f, replay_url: e.target.value })} /></Field>
        <Field label="Length (minutes)"><Input inputMode="numeric" value={f.duration_min} onChange={(e) => setF({ ...f, duration_min: e.target.value.replace(/\D/g, "") })} /></Field>
        <Field label="Offer starts at minute"><Input inputMode="numeric" value={f.offer_at_min} onChange={(e) => setF({ ...f, offer_at_min: e.target.value.replace(/\D/g, "") })} /></Field>
        <Field label="Use a webinar chain (optional)"><Select value={f.chain_id} onChange={(e) => setF({ ...f, chain_id: e.target.value })} options={[{ value: "", label: "None" }, ...chains.map((c: any) => ({ value: c.chain_id, label: `Content drafted ${new Date(c.created * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` }))]} /></Field>
        <Field label="For a client (optional)"><Select value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} options={[{ value: "", label: "Our own webinar" }, ...clients.map((c: any) => ({ value: String(c.id), label: clean(c.name) }))]} /></Field>
      </div>
      <Button tone="primary" className="mt-3" loading={busy} disabled={!f.starts_at || (!f.title.trim() && !f.chain_id)} onClick={save}>Plan the webinar</Button>
    </Card>
  );
}

function WebinarDetail({ id, onBack }: { id: number; onBack: () => void }) {
  const { go, toast } = useApp();
  const { data, error, loading, reload } = useApi<any>(`/webinars/${id}`, [id]);
  const [csv, setCsv] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [diag, setDiag] = useState<any>(null);
  const [add, setAdd] = useState({ name: "", email: "", consent: false });
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
  if (loading && !data) return <Card><Loading rows={6} /></Card>;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  const r = data.rates;
  const ended = Date.now() / 1000 >= data.ends_at;
  return (
    <div className="space-y-4">
      <Card title={<span className="flex flex-wrap items-center gap-2">{clean(data.title)} {isDemo(data.title) && <DemoBadge />}</span>}
        subtitle={`${data.when} · ${data.status_label}`} actions={<Button size="sm" tone="ghost" onClick={onBack}>All webinars</Button>}>
        <ErrorBox error={err} />
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-2 text-sm">
            <div className="text-xs font-semibold text-muted">Registration page</div>
            {data.page ? (
              <>
                <p className="break-all rounded-md bg-panel-2 px-2 py-1 font-mono text-xs">{abs(data.public_url)}</p>
                <div className="flex flex-wrap items-center gap-2"><CopyButton text={abs(data.public_url)} label="Copy link" /><a className="text-xs text-brand underline" href={abs(data.public_url)} target="_blank" rel="noreferrer">Open the page</a></div>
              </>
            ) : (
              <p className="flex flex-wrap items-center gap-2"><Badge tone="info">{Q_STATUS[data.page_status] || "Waiting for approval"}</Badge><span className="text-muted">The page goes live when its copy is approved.</span>
                {data.page_qid && <Button size="sm" onClick={() => go(`/queue?ids=${data.page_qid}`)}>Review the page</Button>}</p>
            )}
            {data.needs_public_url && <p className="text-xs text-muted">Links inside emails show "[your workspace address]" until the server's public address is set.</p>}
          </div>
          <div className="space-y-1 text-sm">
            <div className="text-xs font-semibold text-muted">Event</div>
            <p>Join link: {data.join_url ? <span className="break-all">{data.join_url}</span> : <span className="text-muted">not set</span>}</p>
            <p>Replay link: {data.replay_url ? <span className="break-all">{data.replay_url}</span> : <span className="text-muted">not set yet</span>}</p>
            <p className="text-muted">{data.duration_min} minutes · offer from minute {data.offer_at_min}</p>
          </div>
        </div>
      </Card>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Registrants" value={r.registrants} hint={`Registration rate ${pct(r.reg_rate)} of ${r.visits} visits`} />
        <Stat label="Showed live" value={r.attended} hint={`Show rate ${pct(r.show_rate)}`} />
        <Stat label="Stayed for the offer" value={r.stayed_past_offer} hint={`${pct(r.offer_reach)} of attendees`} />
        <Stat label="Booked a call" value={r.booked} hint={`${pct(r.booked_rate)} of attendees`} />
      </div>
      <Card title="Emails" subtitle="Approve each email once. It then goes to every registrant who agreed to emails, timed from the start (reminders) or the end (replays). Anyone who unsubscribes is skipped.">
        <Table rows={data.templates} cols={[
          { key: "label", label: "Email" }, { key: "timing", label: "When" },
          { key: "status", label: "Approval", render: (t) => <Badge tone={t.status === "approved" ? "good" : "info"}>{t.status === "approved" ? "Approved" : Q_STATUS[t.queue_status] || "Waiting for approval"}</Badge> },
          { key: "fanned_out", label: "Queued for people", render: (t) => t.fanned_out },
          { key: "go", label: "", render: (t) => (t.status !== "approved" && t.queue_id ? <Button size="sm" onClick={() => go(`/queue?ids=${t.queue_id}`)}>Review</Button> : null) },
        ]} />
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Attendance" subtitle="Import the attendee report from your webinar tool (CSV with email and minutes), or tick people below.">
          <input type="file" accept=".csv,text/csv" className="mb-2 block w-full text-xs" onChange={(e) => e.target.files?.[0]?.text().then(setCsv)} aria-label="Choose an attendance CSV" />
          <TextArea rows={4} value={csv} onChange={(e) => setCsv(e.target.value)} placeholder="Or paste the CSV here" aria-label="Attendance CSV" />
          <div className="mt-2 flex flex-wrap gap-2">
            <Button loading={busy === "csv"} disabled={csv.length < 10} onClick={() => act("csv", () => post(`/webinars/${id}/attendance`, { csv }), (res) => { setCsv(""); toast(`${res.matched} matched, ${res.added_without_consent} attended without registering (they won't be emailed)`, "good"); })}>Import attendance</Button>
            <Button loading={busy === "post"} disabled={!ended || data.status === "done"} onClick={() => act("post", () => post(`/webinars/${id}/post-event`), (res) => toast(`${res.setter_drafts.length} setter follow-ups drafted, ${res.replay_items.length} replay emails queued`, "good"))}>Follow up now</Button>
          </div>
          {!ended && <p className="mt-2 text-xs text-muted">Follow-ups unlock when the webinar ends. They also run on their own two hours after the end.</p>}
          {data.status === "done" && <p className="mt-2 text-xs text-muted">Followed up: people who stayed for the offer have setter follow-ups waiting for approval; everyone else is on the replay emails.</p>}
        </Card>
        <Card title="Diagnosis" subtitle="Registration and show rates against the SOP benchmarks.">
          <Button loading={busy === "diag"} onClick={() => act("diag", () => post(`/webinars/${id}/diagnose`), setDiag)}>Diagnose this webinar</Button>
          {diag && (
            <div className="mt-3 space-y-2 text-sm">
              <p>{diag.prescription}</p>
              <TraceLink runId={diag.run_id} />
            </div>
          )}
          <ul className="mt-3 list-disc pl-5 text-xs text-muted">{Object.values(r.definitions || {}).map((d: any) => <li key={d}>{d}</li>)}</ul>
        </Card>
      </div>
      <Card title="Registrants" subtitle={r.unsubscribed ? `${r.unsubscribed} unsubscribed` : undefined}>
        {data.registrants.length ? (
          <Table rows={data.registrants} cols={[
            { key: "name", label: "Name", render: (p) => <span className="flex items-center gap-1.5">{clean(p.name)} {isDemo(p.name) && <DemoBadge />}</span> },
            { key: "source", label: "Came from", render: (p) => (p.source === "attendance_import" ? "Attended only" : p.source.replace(/_/g, " ")) },
            { key: "attended", label: "Showed", render: (p) => <input type="checkbox" checked={!!p.attended} aria-label={`${clean(p.name)} showed`} onChange={(e) => act(`a${p.id}`, () => put(`/webinars/${id}/registrants/${p.id}`, { attended: e.target.checked }))} /> },
            { key: "minutes_watched", label: "Minutes", render: (p) => Math.round(p.minutes_watched || 0) },
            { key: "stayed_past_offer", label: "Stayed for offer", render: (p) => <input type="checkbox" checked={!!p.stayed_past_offer} aria-label={`${clean(p.name)} stayed for the offer`} onChange={(e) => act(`s${p.id}`, () => put(`/webinars/${id}/registrants/${p.id}`, { stayed_past_offer: e.target.checked }))} /> },
            { key: "booked", label: "Booked", render: (p) => <input type="checkbox" checked={!!p.booked} aria-label={`${clean(p.name)} booked`} onChange={(e) => act(`b${p.id}`, () => put(`/webinars/${id}/registrants/${p.id}`, { booked: e.target.checked }))} /> },
            { key: "consent", label: "Emails", render: (p) => (p.unsubscribed ? <Badge tone="warn">Unsubscribed</Badge> : p.consent ? <Badge tone="good">Agreed</Badge> : <Badge>No consent</Badge>) },
          ]} />
        ) : <Empty title="No registrants yet" hint={data.page ? "Share the registration link to start getting sign-ups." : "Approve the registration page first."} />}
        <div className="mt-4 grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <Field label="Add someone by hand: name"><Input value={add.name} onChange={(e) => setAdd({ ...add, name: e.target.value })} /></Field>
          <Field label="Email"><Input type="email" value={add.email} onChange={(e) => setAdd({ ...add, email: e.target.value })} /></Field>
          <Button loading={busy === "add"} disabled={!add.name || !add.email || !add.consent} onClick={() => act("add", () => post(`/webinars/${id}/registrants`, { ...add, source: "manual" }), () => { setAdd({ name: "", email: "", consent: false }); toast("Registrant added", "good"); })}>Add</Button>
          <label className="flex items-start gap-2 text-xs text-muted sm:col-span-3"><input type="checkbox" className="mt-0.5" checked={add.consent} onChange={(e) => setAdd({ ...add, consent: e.target.checked })} /> They agreed to get emails about this training.</label>
        </div>
      </Card>
    </div>
  );
}

export default function Events() {
  const { me } = useApp();
  const list = useApi<any>("/webinars");
  const chains = useApi<any>("/webinar/chains");
  const clients = useApi<any>(me.tabs.includes("clients") ? "/clients" : null);
  const [sel, setSel] = useState<number | null>(null);
  const [planning, setPlanning] = useState(false);
  if (sel !== null) return <WebinarDetail key={sel} id={sel} onBack={() => { setSel(null); list.reload(); }} />;
  const items: any[] = list.data?.items || [];
  return (
    <div className="space-y-4">
      {list.loading && !list.data && <Loading rows={4} />}
      <ErrorBox error={list.error} retry={list.reload} />
      {list.data && (
        <Card title="Webinars" actions={<Button tone="primary" size="sm" onClick={() => setPlanning(!planning)}>{planning ? "Close" : "Plan a webinar"}</Button>}>
          {items.length ? (
            <Table rows={items} onRow={(w) => setSel(w.id)} cols={[
              { key: "title", label: "Webinar", render: (w) => <span className="flex items-center gap-1.5">{clean(w.title)} {isDemo(w.title) && <DemoBadge />}</span> },
              { key: "when", label: "When" },
              { key: "status", label: "Status", render: (w) => <Badge tone={w.status === "open" ? "good" : w.status === "draft" ? "info" : "neutral"}>{w.status_label}</Badge> },
              { key: "reg", label: "Registrants", render: (w) => w.rates.registrants },
              { key: "show", label: "Show rate", render: (w) => pct(w.rates.show_rate) },
              { key: "go", label: "", render: (w) => <Button size="sm" onClick={(e) => { e.stopPropagation(); setSel(w.id); }}>Open</Button> },
            ]} />
          ) : <Empty title="No webinars planned yet" hint="Plan one to get a registration page, reminder and replay emails, attendance tracking and follow-ups." action={<Button onClick={() => setPlanning(true)}>Plan a webinar</Button>} />}
        </Card>
      )}
      {planning && <NewWebinar chains={chains.data?.items || []} clients={clients.data?.items || []} onCreated={(id) => { setPlanning(false); list.reload(); setSel(id); }} />}
    </div>
  );
}
