import { useEffect, useState } from "react";
import { Ban, Check, Clock, ExternalLink, Inbox, Pencil, RotateCw, Send, X } from "lucide-react";
import { ApiError, post, put } from "../api";
import { Badge, Button, Card, CopyButton, DemoBadge, Empty, ErrorBox, Field, FlagList, Input, Loading, PageHeader, Reveal, Select, Stat, Table, Tabs, TextArea, TraceLink, cx, humanize, isDemo, useApi, useApp } from "../components/ui";
import { InboxPanel } from "./inbox/InboxPanel";

const STATUS_TONE: Record<string, any> = {
  pending: "info", held: "bad", needs_human: "warn", approved: "good", rejected: "neutral", scheduled: "info",
  awaiting_manual_send: "warn", sending: "info", sent: "good", send_failed: "bad", cancelled: "neutral", needs_attention: "bad",
};
const STATUS_LABEL: Record<string, string> = {
  pending: "Pending approval", held: "Held by guardrails", needs_human: "Needs a person", approved: "Approved", rejected: "Rejected",
  scheduled: "Scheduled", awaiting_manual_send: "Waiting for you to send", sending: "Sending", sent: "Sent",
  send_failed: "Failed to send", cancelled: "Cancelled", needs_attention: "Check if it went out",
};
// most urgent first, whatever order the server counts arrive in
const STATUS_ORDER = ["held", "needs_attention", "needs_human", "pending", "send_failed", "awaiting_manual_send", "scheduled", "sending", "sent", "approved", "cancelled", "rejected"];
const DOT: Record<string, string> = {
  held: "bg-bad", needs_human: "bg-warn", pending: "bg-info", approved: "bg-good", rejected: "bg-faint", scheduled: "bg-info",
  awaiting_manual_send: "bg-warn", sending: "bg-info", sent: "bg-good", send_failed: "bg-bad", cancelled: "bg-faint",
  needs_attention: "bg-bad",
};
const FILTERS = [
  { value: "open", label: "Needs a decision" }, { value: "scheduled", label: "Scheduled" },
  { value: "awaiting_manual_send", label: "Waiting for you to send" }, { value: "sent", label: "Sent" },
  { value: "send_failed", label: "Failed" }, { value: "needs_attention", label: "Interrupted: check if it went out" },
  { value: "cancelled", label: "Cancelled" }, { value: "approved", label: "Approved (nothing to send)" },
  { value: "rejected", label: "Rejected" }, { value: "all", label: "All" },
];
const REJECT_REASONS = [
  { value: "tone", label: "Tone is off" }, { value: "wrong_fact", label: "Wrong fact" }, { value: "too_long", label: "Too long" },
  { value: "off_brand", label: "Off brand" }, { value: "compliance", label: "Compliance risk" }, { value: "timing", label: "Wrong timing" },
  { value: "wrong_person", label: "Wrong person" }, { value: "other", label: "Something else" },
];
const REASON_LABEL: Record<string, string> = Object.fromEntries(REJECT_REASONS.map((r) => [r.value, r.label]));
const CHANNEL_LABEL: Record<string, string> = {
  instagram_dm: "Instagram DM", linkedin: "LinkedIn", phone: "Phone", whatsapp: "WhatsApp", email: "Email", sms: "Text message",
  telegram: "Telegram", slack: "Slack", calendar: "Calendar", ads: "Ads", crm: "CRM", manual: "Internal", web: "Web",
};
const KIND_LABEL: Record<string, string> = {
  dm: "DM", dm_opener: "Opener", followup: "Follow-up", setter_reply: "Reply to lead", booking_dm: "Booking times",
  calendar_invite: "Calendar invite", reminder: "Reminder", nudge: "Student nudge", welcome: "Welcome", kickoff: "Kickoff call",
  email: "Email", email_reply: "Email reply", channel_reply: "Reply", ad_change: "Ad change", crm_move: "CRM move",
  intervention: "Coach escalation", prospect_dm: "Prospect DM", prospect_email: "Prospect email", proposal: "Proposal",
  client_report: "Client report", mission_draft: "Mission draft", precall_brief: "Pre-call brief",
};
const MANUAL = ["instagram_dm", "linkedin", "phone", "whatsapp", "facebook_dm", "tiktok_dm", "x_dm"];
const INTERNAL = ["manual", "ads", "crm"];

const label = (map: Record<string, string>, k?: string) => (k ? map[k] || k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " ") : "");
const when = (ts?: number) =>
  ts ? new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

function deliveredLabel(d?: string) {
  if (!d) return "";
  if (d.startsWith("mock:")) return "Demo transport (practice only, nobody received it)";
  if (d.startsWith("live:google_calendar")) return "Google Calendar invite sent";
  if (d.startsWith("live:")) return `Sent by ${label(CHANNEL_LABEL, d.split(":")[1])}`;
  if (d.startsWith("manual:")) return "Sent by hand and confirmed";
  if (d === "action") return "Applied through the change log";
  if (d === "ready to send manually") return "Ready to use";
  return d;
}

function profileLink(it: any): string | null {
  const r = String(it.recipient_ref || "").trim();
  if (!r) return null;
  if (/^https?:\/\//i.test(r)) return r;
  if (it.channel === "instagram_dm" && /^@?[A-Za-z0-9._]{1,64}$/.test(r)) return `https://www.instagram.com/${r.replace(/^@/, "")}/`;
  const email = r.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  if (it.channel === "email" && email) {
    const q = new URLSearchParams({ subject: it.meta?.subject || "", body: it.send_text || it.body_display || "" }).toString().replace(/\+/g, "%20");
    return `mailto:${email[0]}?${q}`;
  }
  if ((it.channel === "phone" || it.channel === "sms") && /\d{6,}/.test(r.replace(/\D/g, ""))) return `tel:${r.replace(/[^\d+]/g, "")}`;
  return null;
}

function Item({ it, index = 0, onChange }: { it: any; index?: number; onChange: () => void }) {
  const { toast } = useApp();
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [body, setBody] = useState(it.body_display);
  const [reason, setReason] = useState("tone");
  const [note, setNote] = useState("");
  const [permalink, setPermalink] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | string | null>(null);
  useEffect(() => setBody(it.body_display), [it.body_display]);

  const run = async (key: string, fn: () => Promise<any>, after?: (r: any) => void) => {
    setBusy(key);
    setErr(null);
    try {
      const r = await fn();
      after?.(r);
      onChange();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const approve = () =>
    run("approve", () => post(`/queue/${it.id}/approve`), (r) => {
      if (r.status === "scheduled") toast(`Approved — sends ${when(r.send_after)}`, "good");
      else if (r.status === "awaiting_manual_send") toast("Approved — now send it yourself and mark it sent", "info");
      else if (r.status === "sent") toast("Approved and sent", "good");
      else toast("Approved", "good");
    });
  const save = () => run("save", () => put(`/queue/${it.id}`, { body }), () => setEditing(false));
  const reject = () => run("reject", () => post(`/queue/${it.id}/reject`, { reason_code: reason, reason: note }), () => { setRejecting(false); toast("Rejected", "info"); });
  const markSent = () => {
    const link = permalink.trim();
    if (link && !/^https?:\/\/\S+$/i.test(link)) {
      setErr("The link should start with https://");
      return;
    }
    run("sent", () => post(`/queue/${it.id}/sent`, link ? { permalink: link } : {}), () => { setConfirming(false); toast("Marked as sent", "good"); });
  };
  const cancel = () => run("cancel", () => post(`/queue/${it.id}/cancel`, {}), () => toast("Cancelled", "info"));
  const sendNow = () =>
    run("send", () => post(`/queue/${it.id}/send-now`), (r) => toast(r.status === "sent" ? "Sent" : r.status === "send_failed" ? "It couldn't be sent — see the reason" : label(STATUS_LABEL, r.status), r.status === "send_failed" ? "bad" : "good"));

  const open = ["pending", "held", "needs_human"].includes(it.status);
  const link = profileLink(it);
  const ch = it.channel || "manual";
  return (
    <Reveal as="li" i={index} className="card min-w-0 overflow-hidden" data-testid="queue-item" data-status={it.status}>
      <div className="p-4 sm:p-5">
      {/* phones: badges drop below the title instead of squeezing it into a narrow column */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[12px] text-muted">
            <span className="tabular-nums">#{it.id}</span>
            <span aria-hidden>·</span>
            <span>{label(KIND_LABEL, it.kind)}</span>
            <span aria-hidden>·</span>
            <span>{label(CHANNEL_LABEL, ch)}</span>
            {it.recipient_ref && <><span aria-hidden>·</span><span className="min-w-0 break-all">To {it.recipient_ref}</span></>}
            {it.run_id ? <><span aria-hidden>·</span><TraceLink runId={it.run_id} /></> : null}
          </div>
          <h3 className="mt-1 break-words text-[15px] font-semibold leading-snug text-ink">{it.title || "(untitled)"}</h3>
          {it.meta?.subject && <div className="mt-0.5 text-[12px] text-muted">Subject: {it.meta.subject}</div>}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 sm:justify-end">
          {(it.demo || isDemo(it.title) || isDemo(it.recipient_ref)) && <DemoBadge />}
          {it.meta?.client_asset && <Badge tone="info">Client asset</Badge>}
          {it.meta?.prospect_facing && <Badge tone="warn">Goes to a prospect</Badge>}
          {it.meta?.ai_mode === "rules" && <Badge tone="neutral" title="Written from a template; AI was not used">Template (AI off)</Badge>}
          <Badge tone={STATUS_TONE[it.status]} dot>{STATUS_LABEL[it.status] || label({}, it.status)}</Badge>
        </div>
      </div>
      {editing ? (
        <TextArea rows={5} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Edit draft" className="mt-3" />
      ) : (
        <p className="prose-pre mt-3 break-words rounded-[10px] border border-line-2 bg-panel-2 px-3.5 py-3 text-[14px] leading-relaxed text-ink">{it.status === "awaiting_manual_send" ? it.send_text || it.body_display : it.body_display}</p>
      )}
      <div className="mt-3"><FlagList flags={it.flags} /></div>

      {it.status === "scheduled" && (
        <p className="mt-2 flex items-center gap-1.5 text-sm"><Clock className="size-4 text-muted" /> Approved — sends {when(it.send_after)}{it.status_reason ? `. ${it.status_reason}` : ""}</p>
      )}
      {it.status === "awaiting_manual_send" && <p className="mt-2 text-sm">Approved — waiting for you to send it. {it.status_reason}</p>}
      {it.status === "sent" && (
        <p className="mt-2 text-xs text-muted">
          Sent {when(it.sent_at)} · {deliveredLabel(it.delivered)}{it.status_reason ? ` · ${it.status_reason}` : ""}
          {it.meta?.permalink && <> · <a className="text-brand underline" href={it.meta.permalink} target="_blank" rel="noreferrer">View sent message</a></>}
        </p>
      )}
      {it.status === "send_failed" && <p className="mt-2 text-sm text-bad">Couldn't send: {it.status_reason || "the provider refused it"}</p>}
      {it.status === "cancelled" && <p className="mt-2 text-sm text-muted">Cancelled: {it.status_reason || "by a person"}</p>}
      {it.status === "rejected" && <p className="mt-2 text-xs text-muted">Rejected{it.reject_code ? ` — ${REASON_LABEL[it.reject_code] || "other reason"}` : ""}{it.status_reason ? `: ${it.status_reason}` : ""}{it.decided_by ? ` · by ${it.decided_by}` : ""}</p>}
      {it.status === "approved" && it.delivered && <p className="mt-2 text-xs text-muted">{deliveredLabel(it.delivered)} · approved by {it.decided_by}{it.status_reason ? ` · ${it.status_reason}` : ""}</p>}
      {it.status === "sending" && <p className="mt-2 text-xs text-muted">Being sent right now. If it stays like this, it turns into a failed send you can check and retry.</p>}
      {open && it.status_reason && <p className="mt-2 text-xs text-muted">{it.status_reason}</p>}
      <ErrorBox error={err} />

      {open && !rejecting && (
        <div className="mt-3 flex flex-wrap gap-2">
          {editing ? (
            <>
              <Button tone="primary" size="sm" loading={busy === "save"} onClick={save}>Save & recheck</Button>
              <Button size="sm" onClick={() => { setEditing(false); setBody(it.body_display); }}>Cancel</Button>
            </>
          ) : (
            <>
              <Button tone="good" size="sm" loading={busy === "approve"} onClick={approve} disabled={it.status === "held"} title={it.status === "held" ? "Edit it clean first" : ""}>
                <Check className="size-3.5" /> Approve
              </Button>
              <Button size="sm" onClick={() => setEditing(true)}><Pencil className="size-3.5" /> Edit</Button>
              <Button tone="danger" size="sm" onClick={() => setRejecting(true)}><X className="size-3.5" /> Reject</Button>
              {it.status === "held" && <span className="text-[12px] text-muted">Edit it clean, then approve.</span>}
            </>
          )}
        </div>
      )}
      {open && rejecting && (
        <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]">
          <Field label="Why reject it?">
            <Select value={reason} onChange={(e) => setReason(e.target.value)} options={REJECT_REASONS} aria-label="Reason" />
          </Field>
          <Field label="Note (optional)">
            <Input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="What should change next time?" />
          </Field>
          <div className="flex flex-wrap gap-2 sm:col-span-2">
            <Button tone="danger" size="sm" loading={busy === "reject"} onClick={reject}><X className="size-3.5" /> Reject</Button>
            <Button size="sm" onClick={() => setRejecting(false)}>Keep it</Button>
          </div>
        </div>
      )}
      {it.status === "scheduled" && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button tone="primary" size="sm" loading={busy === "send"} onClick={sendNow}><Send className="size-3.5" /> Send now</Button>
          <Button size="sm" loading={busy === "cancel"} onClick={cancel}><Ban className="size-3.5" /> Cancel</Button>
        </div>
      )}
      {it.status === "awaiting_manual_send" && (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap gap-2">
            <CopyButton text={it.send_text || it.body_display} label="Copy message" />
            {link && (
              <a className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-2.5 py-1 text-xs font-medium hover:bg-panel-2" href={link} target="_blank" rel="noreferrer">
                <ExternalLink className="size-3.5" /> {ch === "email" ? "Open in email" : ch === "phone" || ch === "sms" ? "Call or text" : "Open profile"}
              </a>
            )}
            {!confirming && <Button tone="good" size="sm" onClick={() => setConfirming(true)}><Check className="size-3.5" /> I sent it</Button>}
            <Button size="sm" loading={busy === "cancel"} onClick={cancel}><Ban className="size-3.5" /> Cancel</Button>
          </div>
          {confirming && (
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-0 flex-1 basis-56">
                <Field label="Link to the sent message (optional)">
                  <Input value={permalink} maxLength={500} onChange={(e) => setPermalink(e.target.value)} placeholder="https://" inputMode="url" />
                </Field>
              </div>
              <Button tone="good" size="sm" loading={busy === "sent"} onClick={markSent}><Check className="size-3.5" /> Confirm sent</Button>
              <Button size="sm" onClick={() => setConfirming(false)}>Not yet</Button>
            </div>
          )}
        </div>
      )}
      {it.status === "needs_attention" && (
        <div className="mt-3 space-y-2" data-testid="needs-attention">
          <p className="text-sm text-bad">Sending was interrupted, so it may or may not have gone out. Check the conversation first: nothing is sent again by itself.</p>
          <div className="flex flex-wrap gap-2">
            <Button tone="good" size="sm" loading={busy === "sent"} onClick={() => run("sent", () => post(`/queue/${it.id}/sent`, {}), () => toast("Marked as sent", "good"))}><Check className="size-3.5" /> It went out</Button>
            {!INTERNAL.includes(ch) && <Button size="sm" loading={busy === "send"} onClick={sendNow}><RotateCw className="size-3.5" /> It didn't: send it again</Button>}
            <Button size="sm" loading={busy === "cancel"} onClick={cancel}><Ban className="size-3.5" /> Cancel</Button>
          </div>
        </div>
      )}
      {it.status === "send_failed" && (
        <div className="mt-3 flex flex-wrap gap-2">
          {!INTERNAL.includes(ch) && <Button tone="primary" size="sm" loading={busy === "send"} onClick={sendNow}><RotateCw className="size-3.5" /> Try again</Button>}
          <Button size="sm" loading={busy === "cancel"} onClick={cancel}><Ban className="size-3.5" /> Cancel</Button>
        </div>
      )}
      {MANUAL.includes(ch) && open && <p className="mt-2 text-[11px] text-muted">{label(CHANNEL_LABEL, ch)}s are sent by a person: after approval you'll copy it, send it from your account and mark it sent.</p>}
      </div>
    </Reveal>
  );
}

function Approvals({ query }: { query: URLSearchParams }) {
  const { me, go } = useApp();
  const [status, setStatus] = useState(query.get("status") || "open");
  const ids = query.get("ids");
  const tag = query.get("tag");
  const params = new URLSearchParams();
  if (status !== "all") params.set("status", status);
  if (ids) params.set("ids", ids);
  if (tag) params.set("tag", tag);
  if (query.get("chain_id")) params.set("chain_id", query.get("chain_id")!);
  const { data, error, loading, reload } = useApi<any>(`/queue?${params.toString()}`, [status, ids, tag]);
  const empty: Record<string, string> = {
    open: "Nothing waiting for a decision", scheduled: "Nothing scheduled", awaiting_manual_send: "Nothing waiting for you to send",
    sent: "Nothing sent yet", send_failed: "No failed sends", cancelled: "Nothing cancelled",
  };
  // counts come from the server already scoped to what this viewer may see (their own items, never the workspace's)
  const counts: Record<string, number> = data?.counts || {};
  const chips = STATUS_ORDER.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !STATUS_ORDER.includes(k) && counts[k]));
  return (
    <div>
      {(ids || tag) && <p className="mb-3 text-sm text-muted">Filtered to {ids ? `${ids.split(",").length} items from a run` : `tag ${tag}`}. <a className="text-brand" href="#/queue">Show all</a></p>}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        {chips.length > 0 && (
          <div className="flex flex-wrap gap-2" role="group" aria-label="Show only">
            {chips.map((k) => (
              <button key={k} type="button" onClick={() => setStatus(status === k ? "open" : k)} aria-pressed={status === k}
                className={cx("focus-ring inline-flex h-8 items-center gap-2 rounded-full border px-3 text-[12.5px] font-medium transition-colors", status === k ? "border-brand-line bg-brand-soft text-ink" : "border-line bg-panel text-ink-2 shadow-xs hover:border-line-strong hover:text-ink")}>
                <span className={cx("size-1.5 rounded-full", DOT[k] || "bg-faint")} />
                {STATUS_LABEL[k] || humanize(k)}
                <span className="tabular-nums text-muted">{counts[k]}</span>
              </button>
            ))}
          </div>
        )}
        <Select className="sm:ml-auto sm:w-56" value={status} onChange={(e) => setStatus(e.target.value)} options={FILTERS} aria-label="Show" />
      </div>
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {data && data.items.length === 0 && (
        <Empty icon={<Inbox />} title={empty[status] || "Nothing here"} hint="Drafts from agents, the Studio and the builder land here. Nothing is sent until someone approves it."
          action={<>
            {me.tabs.includes("studio") && <Button size="sm" onClick={() => go("/studio")}>Draft something in Studio</Button>}
            {me.tabs.includes("coach") && <Button size="sm" tone="ghost" onClick={() => go("/coach")}>Ask the coach</Button>}
            {status !== "all" && <Button size="sm" tone="ghost" onClick={() => setStatus("all")}>Show everything</Button>}
          </>} />
      )}
      <ul className="space-y-3">{data?.items.map((it: any, i: number) => <Item key={`${it.id}-${it.status}`} it={it} index={i} onChange={reload} />)}</ul>
    </div>
  );
}

function Sequences() {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/delivery/cadences");
  const [busy, setBusy] = useState<number | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const stop = async (id: number) => {
    setBusy(id);
    setErr(null);
    try {
      await post(`/delivery/cadences/${id}/stop`);
      toast("Sequence stopped", "info");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const live = ["waiting_first_send", "active", "drafting", "waiting_send"];
  return (
    <Card title="Follow-up sequences" subtitle="A sequence starts only once the first message is confirmed sent. Each next step is drafted on its day — only if they haven't replied and haven't opted out — and still needs your approval.">
      {loading && <Loading rows={4} />}
      <ErrorBox error={err || error} retry={reload} />
      {data && (
        <Table rows={data.items} empty="No follow-up sequences yet" cols={[
          { key: "contact", label: "Contact", render: (r) => <span className="flex items-center gap-1.5">{String(r.contact).replace(" (DEMO)", "")} {(r.demo || isDemo(r.contact)) && <DemoBadge />}</span> },
          { key: "channel_label", label: "Channel" },
          { key: "step", label: "Progress", render: (r) => `${Math.min(r.step, r.steps)} of ${r.steps} follow-ups` },
          { key: "status_label", label: "Status", render: (r) => <span>{r.status_label}{r.reason && !live.includes(r.status) ? <span className="block text-xs text-muted">{r.reason}</span> : null}</span> },
          { key: "due_at", label: "Next step", render: (r) => (r.due_at ? when(r.due_at) : "—") },
          { key: "x", label: "", render: (r) => (live.includes(r.status) ? <Button size="sm" loading={busy === r.id} onClick={() => stop(r.id)}>Stop</Button> : null) },
        ]} />
      )}
    </Card>
  );
}

function SendingSettings() {
  const { toast, go } = useApp();
  const { data, error, loading, reload } = useApi<any>("/delivery/overview");
  const [s, setS] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { if (data?.settings) setS({ ...data.settings, cadence_days: (data.settings.cadence_days || []).join(", ") }); }, [data]);
  const save = async () => {
    setBusy("save");
    setErr(null);
    try {
      const days = String(s.cadence_days).split(/[,\s]+/).filter(Boolean).map(Number);
      await put("/delivery/settings", {
        email_daily_cap: Number(s.email_daily_cap), warmup_enabled: !!s.warmup_enabled, warmup_first_day: Number(s.warmup_first_day),
        warmup_step: Number(s.warmup_step), daily_touch_cap: Number(s.daily_touch_cap), cadence_days: days,
      });
      toast("Sending settings saved", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const runNow = async () => {
    setBusy("run");
    setErr(null);
    try {
      const r = await post("/delivery/run-now");
      toast(`Checked ${r.dispatch.due || 0} due messages and ${r.cadences.due || 0} due follow-ups`, "good");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const liveList = data ? Object.entries(data.live_channels || {}).filter(([, v]) => v).map(([k]) => label(CHANNEL_LABEL, k)) : [];
  return (
    <div className="space-y-4">
      {loading && <Loading rows={4} />}
      <ErrorBox error={err || error} retry={reload} />
      {data && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Stat label="Emails sent today" value={`${data.emails_sent_today} of ${data.email_cap_today}`} hint={data.settings?.warmup_enabled ? "Warm-up limit for this mailbox" : "Daily limit for this mailbox"} />
            <Stat label="Unsubscribed or blocked" value={data.suppressed} hint="Never contacted again" />
            <Stat label="Sending automatically" value={liveList.length ? liveList.join(", ") : "None"} hint={liveList.length ? "Approved messages on these go out by themselves" : "You send approved messages yourself"} />
          </div>
          {!data.postal_address && (
            <Card>
              <p className="text-sm">Emails to prospects are held until your business postal address is set — the law requires it in every marketing email.</p>
              <div className="mt-2"><Button size="sm" onClick={() => go("/profile")}>Add it in Business profile</Button></div>
            </Card>
          )}
          {!data.public_url_set && (
            <p className="text-xs text-muted">Emails to prospects can't be approved until the server's public web address is set, so every unsubscribe link works (ask whoever runs the server).</p>
          )}
          {s && (
            <Card title="Limits" subtitle="Caps count messages that really went out, per local day." actions={<Button size="sm" loading={busy === "run"} onClick={runNow}><RotateCw className="size-3.5" /> Check due items now</Button>}>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Daily email limit per mailbox" hint="Live emails a day from your sending address">
                  <Input type="number" min={0} max={2000} value={s.email_daily_cap} onChange={(e) => setS({ ...s, email_daily_cap: e.target.value })} />
                </Field>
                <Field label="Daily follow-up limit per person" hint="New follow-ups wait for tomorrow once this many messages went out today">
                  <Input type="number" min={1} max={1000} value={s.daily_touch_cap} onChange={(e) => setS({ ...s, daily_touch_cap: e.target.value })} />
                </Field>
                <label className="flex items-center gap-2 text-sm sm:col-span-2">
                  <input type="checkbox" checked={!!s.warmup_enabled} onChange={(e) => setS({ ...s, warmup_enabled: e.target.checked })} />
                  Warm up a new mailbox slowly (protects deliverability)
                </label>
                <Field label="Warm-up: emails on day one">
                  <Input type="number" min={1} max={500} value={s.warmup_first_day} disabled={!s.warmup_enabled} onChange={(e) => setS({ ...s, warmup_first_day: e.target.value })} />
                </Field>
                <Field label="Warm-up: extra emails each day">
                  <Input type="number" min={0} max={200} value={s.warmup_step} disabled={!s.warmup_enabled} onChange={(e) => setS({ ...s, warmup_step: e.target.value })} />
                </Field>
                <Field label="Follow-up days after the first message" hint="For example 2, 4, 7, 37, 67 (up to 8 steps)">
                  <Input value={s.cadence_days} onChange={(e) => setS({ ...s, cadence_days: e.target.value })} />
                </Field>
              </div>
              <div className="mt-3"><Button tone="primary" size="sm" loading={busy === "save"} onClick={save}>Save limits</Button></div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

export default function Queue({ query }: { query: URLSearchParams }) {
  const { me } = useApp();
  const [tab, setTab] = useState(query.get("tab") || "approvals");
  const owner = me.user.role === "owner";
  // the owner reads every conversation on the Channels page; everyone else reads their own replies here
  const tabs = [{ key: "approvals", label: "Approvals" }, ...(!owner ? [{ key: "inbox", label: "Inbox" }] : []),
    { key: "sequences", label: "Follow-up sequences" }, ...(owner ? [{ key: "sending", label: "Sending settings" }] : [])];
  return (
    <div>
      <PageHeader
        title="Approval queue"
        subtitle="Everything outbound waits here for a person. After approval a message goes out at its scheduled time through a connected channel, or you send it yourself and mark it sent — nothing counts as sent until it really is."
      />
      <Tabs value={tab} onChange={setTab} tabs={tabs} />
      {tab === "approvals" && <Approvals query={query} />}
      {tab === "inbox" && !owner && <div data-testid="queue-inbox"><InboxPanel /></div>}
      {tab === "sequences" && <Sequences />}
      {tab === "sending" && me.user.role === "owner" && <SendingSettings />}
    </div>
  );
}
