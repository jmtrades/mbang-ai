import { useEffect, useState } from "react";
import { AlertTriangle, Check, ClipboardPaste, Inbox as InboxIcon, Link2, Pencil, ShieldAlert, X } from "lucide-react";
import { ApiError, get, post, put } from "../../api";
import {
  Badge, Button, Card, Citations, DemoBadge, Drawer, Empty, ErrorBox, Field, FlagList, Input, Loading, Select, Stat, Tabs,
  TextArea, TraceLink, cx, isDemo, useApi, useApp,
} from "../../components/ui";

// ---------------------------------------------------------------- plain-English labels
const CLASS_TONE: Record<string, any> = {
  positive: "good", question: "info", price_ask: "info", objection_not_now: "warn", objection_cost: "warn",
  objection_partner: "warn", not_interested: "neutral", unsubscribe: "bad", out_of_office: "neutral", legal_complaint: "bad",
};
const MODE_LABEL: Record<string, string> = { rules: "sorted by rules", ai: "sorted by AI", replay: "sorted by saved AI", person: "sorted by a person" };
const Q_STATUS: Record<string, { label: string; tone: any }> = {
  pending: { label: "Waiting for approval", tone: "info" },
  held: { label: "Held by guardrails", tone: "bad" },
  needs_human: { label: "Needs a person", tone: "warn" },
  approved: { label: "Approved", tone: "good" },
  rejected: { label: "Not sent", tone: "neutral" },
  scheduled: { label: "Scheduled", tone: "info" },
  awaiting_manual_send: { label: "Approved, send it by hand", tone: "info" },
  sent: { label: "Sent", tone: "good" },
};
const SEVERITY: Record<string, { label: string; tone: any }> = {
  high: { label: "Urgent", tone: "bad" }, medium: { label: "Soon", tone: "warn" }, low: { label: "For your info", tone: "neutral" },
};
const ESC_STATUS: Record<string, string> = { open: "Open", acked: "Seen", resolved: "Resolved" };
const STAGE_LABEL: Record<string, string> = {
  new: "New", researched: "Researched", contacted: "Contacted", replied: "Replied", call_booked: "Call booked",
  proposal: "Proposal sent", won: "Won", lost: "Lost", nurture: "Check back later", dnc: "Do not contact",
};
const CLASS_OPTIONS = [
  ["positive", "Interested"], ["question", "Asked a question"], ["price_ask", "Asked about price"], ["objection_not_now", "Not right now"],
  ["objection_cost", "Worried about cost"], ["objection_partner", "Needs to check with someone"], ["not_interested", "Not interested"],
  ["unsubscribe", "Asked to stop"], ["out_of_office", "Auto-reply / away"], ["legal_complaint", "Complaint or legal"],
].map(([value, label]) => ({ value, label }));

export function ago(ts?: number | null) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
const human = (s?: string | null) => (s ? s.replace(/_/g, " ") : "");

function ClassChip({ c, label, mode }: { c?: string; label?: string; mode?: string }) {
  if (!c) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge tone={CLASS_TONE[c] || "neutral"}>{label || human(c)}</Badge>
      {mode && <span className="text-[11px] text-muted">{MODE_LABEL[mode] || ""}</span>}
    </span>
  );
}

// ---------------------------------------------------------------- the draft, approved or edited through the queue
function DraftBox({ m, onChange }: { m: any; onChange: () => void }) {
  const { toast } = useApp();
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(m.body);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const open = ["pending", "held", "needs_human"].includes(m.status);
  const act = async (what: "approve" | "reject" | "save") => {
    setBusy(what);
    setErr(null);
    try {
      if (what === "save") {
        await put(`/queue/${m.id}`, { body });
        setEditing(false);
        toast("Saved and re-checked", "good");
      } else {
        await post(`/queue/${m.id}/${what}`, what === "reject" ? { reason: "Not sent from the inbox" } : {});
        toast(what === "approve" ? "Approved" : "Marked not to send", what === "approve" ? "good" : "info");
      }
      onChange();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const st = Q_STATUS[m.status] || { label: human(m.status), tone: "neutral" };
  return (
    <div className="min-w-0 rounded-lg border border-line bg-panel p-3" data-testid="inbox-draft">
      <div className="mb-1 flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-medium">Our reply · {m.channel_label}</span>
        <Badge tone={st.tone}>{st.label}</Badge>
        {m.holding && <Badge tone="neutral">holding reply</Badge>}
        {m.source && <span className="text-muted">{m.source}</span>}
      </div>
      {editing ? (
        <TextArea rows={5} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Edit the reply" />
      ) : (
        <p className="prose-pre whitespace-pre-wrap break-words text-sm">{m.body}</p>
      )}
      {open && <div className="mt-2"><FlagList flags={m.flags} /></div>}
      {m.playbook && open && (
        <p className="mt-2 rounded-md bg-panel-2 p-2 text-xs text-muted">
          <b className="text-ink">Playbook tip:</b> {m.playbook.tip} <span className="block">{m.playbook.source}</span>
        </p>
      )}
      {open && m.citations?.length > 0 && <div className="text-xs text-muted">How the course handles this (for you, not sent): <Citations items={m.citations} /></div>}
      <ErrorBox error={err} />
      {open && m.can_decide === false && (
        <p className="mt-2 text-xs text-muted">The person who owns this conversation approves this reply. You can read it here.</p>
      )}
      {open && m.can_decide !== false && (
        <div className="mt-2 flex flex-wrap gap-2">
          {editing ? (
            <>
              <Button tone="primary" size="sm" loading={busy === "save"} onClick={() => act("save")}>Save & re-check</Button>
              <Button size="sm" onClick={() => { setEditing(false); setBody(m.body); }}>Cancel</Button>
            </>
          ) : (
            <>
              <Button tone="good" size="sm" loading={busy === "approve"} disabled={m.status === "held"} title={m.status === "held" ? "Edit it clean first" : ""} onClick={() => act("approve")}>
                <Check className="size-3.5" /> Approve
              </Button>
              <Button size="sm" onClick={() => setEditing(true)}><Pencil className="size-3.5" /> Edit</Button>
              <Button tone="danger" size="sm" loading={busy === "reject"} onClick={() => act("reject")}><X className="size-3.5" /> Don't send</Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- triage: link an unknown sender to a record
function LinkRecord({ inboundId, onDone }: { inboundId: number; onDone: () => void }) {
  const { toast } = useApp();
  const [q, setQ] = useState("");
  const [items, setItems] = useState<any[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const search = async () => {
    setErr(null);
    try {
      setItems((await get(`/inbox/parties?q=${encodeURIComponent(q.trim())}`)).items);
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const link = async (p: any) => {
    setBusy(true);
    setErr(null);
    try {
      await post(`/inbox/${inboundId}/assign`, { party_type: p.party_type, party_id: p.party_id });
      toast(`Linked to ${p.name}`, "good");
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-lg border border-dashed border-line p-3">
      <p className="mb-2 text-sm font-medium"><Link2 className="mr-1 inline size-4" />Who is this?</p>
      <p className="mb-2 text-xs text-muted">Link them to a prospect, lead, student or client. Their next messages from this address will match automatically.</p>
      <div className="flex flex-wrap gap-2">
        <Input className="min-w-0 flex-1" placeholder="Search by name, email or handle" value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && q.trim().length >= 2 && search()} aria-label="Search records" />
        <Button size="sm" disabled={q.trim().length < 2} onClick={search}>Search</Button>
      </div>
      <ErrorBox error={err} />
      {items && items.length === 0 && <p className="mt-2 text-xs text-muted">No records match. Add them on the Prospects page first, then link.</p>}
      {items && items.length > 0 && (
        <ul className="mt-2 space-y-1">
          {items.map((p) => (
            <li key={p.party_type + p.party_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-panel-2 px-2 py-1 text-sm">
              <span className="min-w-0 break-words"><Badge>{p.party_label}</Badge> {p.name}</span>
              <Button size="sm" loading={busy} onClick={() => link(p)}>Link</Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- re-sort: pick a label, then apply (never on change)
function Resort({ latestIn, busy, onApply, onDismiss }: {
  latestIn: any; busy: string | null; onApply: (label: string) => void; onDismiss: () => void;
}) {
  const current = latestIn.classification || "question";
  const [label, setLabel] = useState(current);
  const [confirmStop, setConfirmStop] = useState(false);
  useEffect(() => { setLabel(current); setConfirmStop(false); }, [latestIn.id, current]);
  const changed = label !== current;
  const apply = () => {
    if (label === "unsubscribe" && !confirmStop) { setConfirmStop(true); return; }
    onApply(label);
  };
  return (
    <Card title="Sorting wrong?" subtitle="Pick the right label and apply it. The pipeline stage and the drafted reply follow it.">
      <div className="flex flex-wrap items-end gap-2">
        <div className="w-full min-w-0 sm:w-auto sm:flex-1">
          <Select aria-label="Correct label" value={label} options={CLASS_OPTIONS}
            onChange={(e) => { setLabel(e.target.value); setConfirmStop(false); }} />
        </div>
        <Button size="sm" tone={confirmStop ? "danger" : "primary"} disabled={!changed} loading={busy === "classify"} onClick={apply}>
          {confirmStop ? "Yes, stop all contact" : "Apply"}
        </Button>
        <Button size="sm" loading={busy === "dismiss"} onClick={onDismiss}>Dismiss (no reply needed)</Button>
      </div>
      {confirmStop && (
        <p className="mt-2 text-xs text-bad">
          "Asked to stop" marks them do not contact, adds them to the never-contact list and holds every waiting draft to them.
          Click again to confirm.
        </p>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- one person's thread
function Thread({ k, canTriage, onChange }: { k: string; canTriage: boolean; onChange: () => void }) {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>(`/inbox/thread?key=${encodeURIComponent(k)}`, [k]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const refresh = () => { reload(); onChange(); };
  const latestIn = data?.messages?.filter((m: any) => m.direction === "in").slice(-1)[0];
  const act = async (what: string, fn: () => Promise<any>, done: string) => {
    setBusy(what);
    setErr(null);
    try {
      await fn();
      toast(done, "good");
      refresh();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  if (loading && !data) return <Loading rows={5} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={data.party_type === "unknown" ? "warn" : "brand"}>{data.party_label}</Badge>
        {data.stage && data.stage !== "dnc" && <Badge>Stage: {STAGE_LABEL[data.stage] || human(data.stage)}</Badge>}
        {data.dnc && <Badge tone="bad">Do not contact</Badge>}
        {isDemo(data.name) && <DemoBadge />}
      </div>
      <p className="text-xs text-muted break-words">{data.channels.map((c: any) => `${c.label}: ${c.address}`).join(" · ")}</p>
      {data.escalations.filter((e: any) => e.status !== "resolved").map((e: any) => (
        <p key={e.id} className="flex items-start gap-2 rounded-md bg-amber-50 p-2 text-xs text-amber-900">
          <ShieldAlert className="size-4 shrink-0" /> <span><b>{e.label}.</b> {e.detail}</span>
        </p>
      ))}
      <ol className="space-y-2">
        {data.messages.map((m: any) => m.direction === "in" ? (
          <li key={`in-${m.id}`} className="min-w-0 rounded-lg bg-panel-2 p-3">
            <div className="mb-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <span className="font-medium text-ink">They wrote · {m.channel_label}</span>
              <span>{ago(m.created)}</span>
              <ClassChip c={m.classification} label={m.label} mode={m.class_mode} />
              <TraceLink runId={m.run_id} />
            </div>
            {m.subject && <p className="text-xs text-muted">Subject: {m.subject}</p>}
            <p className="whitespace-pre-wrap break-words text-sm">{m.body}</p>
          </li>
        ) : (
          <li key={`out-${m.id}`} className="ml-0 sm:ml-6"><DraftBox m={m} onChange={refresh} /></li>
        ))}
      </ol>
      <ErrorBox error={err} />
      {latestIn && (
        <Resort latestIn={latestIn} busy={busy}
          onApply={(label) => act("classify", () => post(`/inbox/${latestIn.id}/classify`, { classification: label }), "Re-sorted")}
          onDismiss={() => act("dismiss", () => post(`/inbox/${latestIn.id}/dismiss`, {}), "Dismissed")} />
      )}
      {data.party_type === "unknown" && canTriage && latestIn && <LinkRecord inboundId={latestIn.id} onDone={refresh} />}
    </div>
  );
}

// ---------------------------------------------------------------- paste a reply (no inbox API for DMs)
function Capture({ onDone }: { onDone: () => void }) {
  const { toast } = useApp();
  const [channel, setChannel] = useState("instagram_dm");
  const [handle, setHandle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/inbox/capture", { channel, handle, body });
      toast(r.party_type === "unknown" ? "Saved. We couldn't match the handle, so it's waiting for a person." : `Sorted as "${r.label}"${r.queued ? " and a reply is drafted" : ""}`, "good");
      setBody("");
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={<span className="inline-flex items-center gap-1.5"><ClipboardPaste className="size-4" />Paste a reply</span>}
      subtitle="Instagram and LinkedIn DMs have no inbox we can read. Paste what they sent and it is sorted and answered like any other reply.">
      <div className="grid gap-2 sm:grid-cols-2">
        <Field label="Where they replied">
          <Select value={channel} onChange={(e) => setChannel(e.target.value)} options={[{ value: "instagram_dm", label: "Instagram DM" }, { value: "linkedin", label: "LinkedIn" }, { value: "facebook", label: "Facebook" }, { value: "manual", label: "Somewhere else" }]} />
        </Field>
        <Field label="Their handle or profile link"><Input value={handle} onChange={(e) => setHandle(e.target.value)} placeholder="@handle" /></Field>
      </div>
      <div className="mt-2"><Field label="What they wrote"><TextArea rows={3} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Paste their message" /></Field></div>
      <ErrorBox error={err} />
      <Button className="mt-2" tone="primary" loading={busy} disabled={!handle.trim() || !body.trim()} onClick={submit}>Sort and draft a reply</Button>
    </Card>
  );
}

// ---------------------------------------------------------------- inbox
export function InboxPanel() {
  const [view, setView] = useState("all");
  const [open, setOpen] = useState<any>(null);
  const { data, error, loading, reload } = useApi<any>(`/inbox?view=${view}`, [view]);
  const c = data?.counts || {};
  return (
    <div className="space-y-4" data-testid="inbox">
      {data?.ai && <p className="text-xs text-muted">{data.ai.label}. Nothing is sent until a person approves it.</p>}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Need a reply" value={c.needs_reply ?? "–"} tone={c.needs_reply ? "warn" : undefined} />
        <Stat label="Unknown senders" value={c.triage ?? "–"} />
        <Stat label="Open escalations" value={data?.escalations?.open ?? "–"} tone={data?.escalations?.high ? "bad" : undefined} />
        <Stat label="Asked to stop" value={c.opted_out ?? "–"} />
      </div>
      <Tabs value={view} onChange={setView} tabs={[
        { key: "all", label: `All (${c.conversations ?? 0})` },
        { key: "needs_reply", label: `Need a reply (${c.needs_reply ?? 0})` },
        { key: "triage", label: `Unknown senders (${c.triage ?? 0})` },
        { key: "escalated", label: `Escalated (${c.escalated ?? 0})` },
      ]} />
      {loading && !data && <Loading rows={5} />}
      <ErrorBox error={error} retry={reload} />
      {data && data.items.length === 0 && (
        <Empty icon={<InboxIcon className="size-6" />} title={view === "all" ? "No replies yet" : "Nothing in this view"}
          hint="Replies from email, text, Telegram and Slack land here once those channels are connected. You can also paste a DM reply below." />
      )}
      {data && data.items.length > 0 && (
        <ul className="space-y-2">
          {data.items.map((g: any) => (
            <li key={g.key}>
              <button className="card focus-ring block w-full min-w-0 p-3 text-left hover:bg-panel-2" onClick={() => setOpen(g)} data-testid="inbox-row">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="min-w-0 break-words font-medium">{g.name}</span>
                  <Badge tone={g.party_type === "unknown" ? "warn" : "neutral"}>{g.party_label}</Badge>
                  {g.channels.slice(0, 3).map((ch: any) => <Badge key={ch.channel + ch.address} tone="info">{ch.label}</Badge>)}
                  {g.dnc && <Badge tone="bad">Do not contact</Badge>}
                  {g.open_escalations > 0 && <Badge tone="bad"><AlertTriangle className="size-3" /> Escalated</Badge>}
                  {(g.demo || isDemo(g.name)) && <DemoBadge />}
                  <span className="ml-auto text-xs text-muted">{ago(g.last_in)}</span>
                </div>
                <p className="mt-1 line-clamp-2 break-words text-sm text-muted">{g.last_message.body}</p>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                  <ClassChip c={g.last_message.classification} label={g.last_message.label} mode={g.last_message.class_mode} />
                  {g.draft ? <span className={cx("text-xs", g.draft.status === "held" ? "text-bad" : "text-muted")}>Reply drafted · {(Q_STATUS[g.draft.status] || { label: human(g.draft.status) }).label}</span>
                    : g.needs_reply ? <span className="text-warn">Needs a person</span> : <span className="text-muted">Nothing waiting</span>}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data?.can_capture && <Capture onDone={reload} />}
      <Drawer open={!!open} onClose={() => setOpen(null)} title={open ? <span className="break-words">{open.name}</span> : ""}>
        {open && <Thread k={open.key} canTriage={!!data?.can_triage} onChange={reload} />}
      </Drawer>
    </div>
  );
}

// ---------------------------------------------------------------- escalations
function AlertSettings() {
  const { toast } = useApp();
  const { data, error, reload } = useApi<any>("/inbox/settings");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  const flip = async () => {
    setBusy(true);
    setErr(null);
    try {
      await put("/inbox/settings", { telegram_alerts: !data.telegram_alerts });
      toast(data.telegram_alerts ? "Telegram alerts off" : "Telegram alerts on", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Rules and alerts" subtitle={`A positive reply with no approved answer after ${data.sla_minutes} minutes, a draft held for ${data.held_hours} hours, work that keeps failing, complaints and stop requests all raise an escalation here.`}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span>Also draft an alert to your own Telegram (urgent and soon items):</span>
        <Badge tone={data.telegram_alerts ? "good" : "neutral"}>{data.telegram_alerts ? "On" : "Off"}</Badge>
        <Button size="sm" loading={busy} onClick={flip}>{data.telegram_alerts ? "Turn off" : "Turn on"}</Button>
      </div>
      <p className="mt-1 text-xs text-muted">
        {data.telegram_linked ? "Your Telegram is linked." : "Link your Telegram first (Channel setup → Get my link code)."} Alerts are internal, still wait in the approval queue, and never go to a prospect.
      </p>
      <ErrorBox error={err} />
    </Card>
  );
}

export function EscalationsPanel({ owner }: { owner: boolean }) {
  const { toast } = useApp();
  const [status, setStatus] = useState("open");
  const { data, error, loading, reload } = useApi<any>(`/escalations?status=${status}`, [status]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});
  useEffect(() => setErr(null), [status]);
  const act = async (key: string, fn: () => Promise<any>, msg: string) => {
    setBusy(key);
    setErr(null);
    try {
      await fn();
      toast(msg, "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4" data-testid="escalations">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="w-48 max-w-full">
          <Select aria-label="Show" value={status} onChange={(e) => setStatus(e.target.value)} options={[{ value: "open", label: "Open and seen" }, { value: "resolved", label: "Resolved" }, { value: "all", label: "All" }]} />
        </div>
        {owner && <Button size="sm" loading={busy === "check"} onClick={() => act("check", () => post("/escalations/check", {}), "Checked the rules")}>Check the rules now</Button>}
      </div>
      <ErrorBox error={err} />
      {loading && !data && <Loading rows={4} />}
      <ErrorBox error={error} retry={reload} />
      {data && data.items.length === 0 && <Empty icon={<ShieldAlert className="size-6" />} title={status === "open" ? "Nothing needs you right now" : "No escalations here"} hint="Escalations appear when a reply waits too long, a draft is stuck, work fails, or someone complains or asks to stop." />}
      {data && data.items.length > 0 && (
        <ul className="space-y-2">
          {data.items.map((e: any) => (
            <li key={e.id} className="card p-3" data-testid="escalation">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={(SEVERITY[e.severity] || SEVERITY.low).tone}>{(SEVERITY[e.severity] || SEVERITY.low).label}</Badge>
                <span className="font-medium">{e.label}</span>
                <Badge>{ESC_STATUS[e.status] || human(e.status)}</Badge>
                {e.overdue && <Badge tone="bad">Overdue</Badge>}
                {(e.demo || isDemo(e.detail)) && <DemoBadge />}
                <span className="ml-auto text-xs text-muted">{ago(e.created)}</span>
              </div>
              <p className="mt-1 break-words text-sm">{e.detail}</p>
              {e.note && <p className="mt-1 text-xs text-muted">Note: {e.note}</p>}
              {e.status !== "resolved" && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {e.status === "open" && <Button size="sm" loading={busy === `ack${e.id}`} onClick={() => act(`ack${e.id}`, () => post(`/escalations/${e.id}/ack`, {}), "Marked as seen")}>Mark as seen</Button>}
                  {e.can_resolve === false ? (
                    <span className="text-xs text-muted">The owner closes legal threats, refund requests and complaints.</span>
                  ) : (
                    <>
                      <Input className="min-w-0 flex-1 sm:max-w-xs" placeholder="What did you do? (optional)" value={notes[e.id] || ""} onChange={(ev) => setNotes({ ...notes, [e.id]: ev.target.value })} aria-label="Resolution note" />
                      <Button size="sm" tone="primary" loading={busy === `res${e.id}`} onClick={() => act(`res${e.id}`, () => post(`/escalations/${e.id}/resolve`, { note: notes[e.id] || "" }), "Resolved")}>Resolve</Button>
                    </>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {owner && <AlertSettings />}
    </div>
  );
}
