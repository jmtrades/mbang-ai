import { useEffect, useState } from "react";
import { Zap } from "lucide-react";
import { ApiError, get, post } from "../api";
import { Badge, Button, Card, Citations, DemoBadge, ErrorBox, Input, Loading, PageHeader, Stat, Table, Tabs, TraceLink, isDemo, useApi, useApp } from "../components/ui";

const TIER_TONE: Record<string, any> = { hot: "bad", warm: "warn", cold: "info" };

export function BurstPanel() {
  const [batch, setBatch] = useState<string | null>(null);
  const [st, setSt] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!batch) return;
    let stop = false;
    const tick = async () => {
      try {
        const s = await get(`/burst/${batch}`);
        if (stop) return;
        setSt(s);
        if (!s.complete) setTimeout(tick, 600);
      } catch (e) {
        setErr(e as ApiError);
      }
    };
    tick();
    return () => { stop = true; };
  }, [batch]);
  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/burst", { n: 500 });
      setBatch(r.batch);
      setSt(null);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const pct = st ? Math.round(((st.done + st.failed) / Math.max(1, st.total)) * 100) : 0;
  return (
    <Card title="500-lead burst" subtitle="DEMO leads through the durable job queue: rules scoring + template replies, no AI." actions={<Button tone="primary" size="sm" onClick={start} loading={busy}><Zap className="size-3.5" /> Run burst</Button>}>
      <ErrorBox error={err} />
      {!batch && <p className="text-sm text-muted">Creates 500 fictional leads (DEMO), scores and routes each one, and queues a reply draft per lead. Nothing sends.</p>}
      {st && (
        <div className="space-y-2" data-testid="burst">
          <div className="h-2 overflow-hidden rounded-full bg-black/5"><div className="h-full bg-brand transition-all" style={{ width: `${pct}%` }} /></div>
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge>{st.done + st.failed}/{st.total}</Badge>
            <Badge tone={st.failed ? "bad" : "good"}>{st.failed} failures</Badge>
            {Object.entries(st.tiers || {}).map(([k, v]) => <Badge key={k} tone={TIER_TONE[k]}>{k}: {v as number}</Badge>)}
            <DemoBadge />
          </div>
          {st.complete && <p className="text-sm text-good">Burst complete.</p>}
        </div>
      )}
    </Card>
  );
}

function ReplyPanel({ lead, onDone }: { lead: any; onDone: () => void }) {
  const { go, toast } = useApp();
  const [text, setText] = useState("");
  const [booking, setBooking] = useState<any>(null);
  const [next, setNext] = useState<string>("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async (key: string, fn: () => Promise<any>, after: (r: any) => void) => {
    setBusy(key);
    setErr(null);
    try {
      after(await fn());
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const record = (classification: string | null) =>
    run(`reply-${classification || "auto"}`, () => post(`/leads/${lead.id}/reply`, { text: text.trim() || "(reply noted without its text)", classification }), (r) => {
      setBooking(r.booking);
      setNext(r.next || "");
      toast(`Reply recorded (${r.classification})`, "good");
    });
  const offer = () => run("offer", () => post(`/leads/${lead.id}/offer-times`), (r) => { setBooking(r); setNext("Approve the booking message in the queue, then record the time they pick."); });
  const pick = (i: number) =>
    run(`pick-${i}`, () => post(`/leads/${lead.id}/pick-slot`, { index: i }), (r) => {
      toast("Invite drafted — the owner approves it", "good");
      go(`/queue?ids=${r.invite}`);
    });
  return (
    <div className="space-y-2" data-testid="lead-reply">
      <div className="text-xs font-semibold text-muted">When they reply</div>
      <Input value={text} maxLength={2000} onChange={(e) => setText(e.target.value)} placeholder="Paste what they said (optional)" aria-label="Their reply" />
      <div className="flex flex-wrap gap-2">
        <Button size="sm" tone="good" loading={busy === "reply-positive"} onClick={() => record("positive")}>Wants a call</Button>
        <Button size="sm" loading={busy === "reply-negative"} onClick={() => record("negative")}>Not now</Button>
        <Button size="sm" loading={busy === "reply-auto"} disabled={!text.trim()} title={text.trim() ? "" : "Paste their reply first"} onClick={() => record(null)}>Read it for me (rules)</Button>
        <Button size="sm" tone="danger" loading={busy === "reply-stop"} onClick={() => record("stop")}>Asked us to stop</Button>
        <Button size="sm" loading={busy === "offer"} onClick={offer}>Offer fresh times</Button>
      </div>
      <ErrorBox error={err} />
      {next && <p className="text-sm text-muted">{next}</p>}
      {booking?.no_slots && <p className="text-sm text-warn">{booking.message}</p>}
      {booking?.slots?.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs text-muted">Times offered — once the booking message is approved and sent, record the one they pick:</div>
          {booking.slots.map((s: any, i: number) => (
            <div key={s.start} className="flex flex-wrap items-center gap-2 text-sm">
              <span>{s.label}</span>
              <Button size="sm" loading={busy === `pick-${i}`} onClick={() => pick(i)}>They picked this</Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function LeadDetail({ lead, onDone }: { lead: any; onDone: () => void }) {
  const { go } = useApp();
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post(`/leads/${lead.id}/process`));
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={<span className="flex items-center gap-2">{lead.name} {isDemo(lead.name) && <DemoBadge />}</span>} subtitle={`${lead.source} · ${lead.answers?.business || ""}`}
      actions={<Button tone="primary" size="sm" loading={busy} onClick={run}>Qualify & draft</Button>}>
      <ErrorBox error={err} retry={run} />
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {Object.entries(lead.answers || {}).flatMap(([k, v]) => [<dt key={k + "k"} className="text-muted">{k}</dt>, <dd key={k + "v"}>{String(v)}</dd>])}
      </dl>
      {res && (
        <div className="mt-4 space-y-3" data-testid="lead-result">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={TIER_TONE[res.tier]}>{res.tier}</Badge><Badge>score {res.score}</Badge><Badge>→ {res.route}</Badge><TraceLink runId={res.run_id} />
          </div>
          <Table rows={res.factors} cols={[{ key: "factor", label: "Factor" }, { key: "value", label: "Value", render: (f) => String(f.value) }, { key: "points", label: "Points" }]} />
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Setter script</div>
            <ol className="list-decimal space-y-1 pl-5 text-sm">{res.script.lines.map((l: string, i: number) => <li key={i}>{l}</li>)}</ol>
            <Citations items={res.script.citations} />
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Pre-call brief</div>
            <p className="text-sm">Goal: {res.brief.goal} · Numbers: {res.brief.current_numbers} · Timeline: {res.brief.timeline}</p>
            <Citations items={res.brief.citations} />
          </div>
          {res.next && <p className="text-sm">{res.next}</p>}
          <Button size="sm" onClick={() => go(`/queue?ids=${res.queue_ids.join(",")}`)}>Review {res.queue_ids.length === 1 ? "the reply" : `${res.queue_ids.length} drafts`} in the queue</Button>
        </div>
      )}
      <div className="mt-4 border-t border-line pt-3"><ReplyPanel lead={lead} onDone={onDone} /></div>
    </Card>
  );
}

function LiveChain() {
  const { go } = useApp();
  const { data, error, loading, reload } = useApi<any>("/chain");
  const [err, setErr] = useState<ApiError | null>(null);
  const act = async (fn: () => Promise<any>, after?: (r: any) => void) => {
    setErr(null);
    try {
      const r = await fn();
      reload();
      after?.(r);
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <Card title="Live lead chain" subtitle={data ? `Meta: ${data.live.meta ? "LIVE" : "practice (switch off)"} · Calendar: ${data.live.calendar ? "Google" : "local"}` : ""}>
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading />}
      {data && <Table rows={data.leads} empty="No chain leads yet. Signed Meta lead webhooks land here." cols={[
        { key: "leadgen_id", label: "Lead" }, { key: "tier", label: "Tier", render: (l) => <Badge tone={TIER_TONE[l.tier]}>{l.tier}</Badge> },
        { key: "stage", label: "Stage" }, { key: "citations", label: "Cited from", render: (l) => <Citations items={l.citations} /> },
        { key: "x", label: "", render: (l) => l.stage === "qualified" ? <Button size="sm" onClick={() => act(() => post(`/chain/leads/${l.id}/reply`, { text: "Replied: interested in a call (noted by the team)", classification: "positive" }), (r) => r.booking_dm ? go(`/queue?ids=${r.booking_dm}`) : setErr(new ApiError(409, r.message || "No free times to offer")))}>They want a call</Button>
          : l.stage === "replied" ? <Button size="sm" onClick={() => act(() => post(`/chain/leads/${l.id}/pick-slot`, { index: 0 }), (r) => go(`/queue?ids=${r.invite}`))}>They picked the first time</Button>
          : l.stage === "booked" ? <Button size="sm" onClick={() => act(() => post(`/chain/leads/${l.id}/outcome`, { outcome: "showed" }))}>Mark showed</Button> : null },
      ]} />}
      {data && <pre className="prose-pre mt-3 rounded-lg bg-panel-2 p-2 text-xs">{JSON.stringify({ stages: data.report.leads_by_stage, inbox: data.report.inbox }, null, 1)}</pre>}
    </Card>
  );
}

export default function Sales() {
  const { me } = useApp();
  const [tab, setTab] = useState("leads");
  const { data, error, loading, reload } = useApi<any>("/leads");
  const rescue = useApi<any>(tab === "rescue" ? "/rescue" : null, [tab]);
  const [sel, setSel] = useState<any>(null);
  return (
    <div>
      <PageHeader title="Leads & booking" subtitle="Transparent scoring, routing to setter, closer or nurture, cited scripts, fresh booking times after a positive reply, and the show-up ladder — all queued for approval." />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "leads", label: "Leads" }, { key: "rescue", label: "Lead Rescue Desk" }, { key: "chain", label: "Live chain" }, ...(me.user.role === "owner" ? [{ key: "burst", label: "Burst" }] : [])]} />
      {tab === "leads" && (
        <>
          {data?.tiers && (
            <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
              {["hot", "warm", "cold", "unscored"].map((t) => <Stat key={t} label={t} value={data.tiers[t] || 0} demo={me.is_demo} />)}
            </div>
          )}
          <div className="grid gap-4 xl:grid-cols-2">
            <Card pad={false}>
              <div className="p-3">
                {loading && <Loading rows={6} />}
                <ErrorBox error={error} retry={reload} />
                {data && <Table rows={data.items} onRow={setSel} empty="No leads yet" cols={[
                  { key: "name", label: "Lead", render: (l) => <span className="flex items-center gap-1.5">{l.name.replace(" (DEMO)", "")} {isDemo(l.name) && <DemoBadge />}</span> },
                  { key: "source", label: "Source" },
                  { key: "tier", label: "Tier", render: (l) => l.tier ? <Badge tone={TIER_TONE[l.tier]}>{l.tier}</Badge> : <span className="text-muted">—</span> },
                  { key: "stage", label: "Stage" },
                ]} />}
              </div>
            </Card>
            <div>{sel ? <LeadDetail key={sel.id} lead={sel} onDone={reload} /> : <Card><p className="text-sm text-muted">Pick a lead to qualify it and draft the setter reply, follow-ups, booking and reminders.</p></Card>}</div>
          </div>
        </>
      )}
      {tab === "rescue" && (
        <Card title="Lead Rescue Desk" subtitle="Stalled leads by anonymous id only, with a value-only next step.">
          {rescue.loading && <Loading />}
          <ErrorBox error={rescue.error} retry={rescue.reload} />
          {rescue.data && <Table rows={rescue.data.items} empty="No stalled leads" cols={[
            { key: "anon_id", label: "Lead (anonymous)" }, { key: "stage", label: "Stage" }, { key: "days_stalled", label: "Days stalled" },
            { key: "suggestion", label: "Value-only next step" },
          ]} />}
        </Card>
      )}
      {tab === "burst" && <BurstPanel />}
      {tab === "chain" && <LiveChain />}
    </div>
  );
}
