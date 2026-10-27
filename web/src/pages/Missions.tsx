import { useState } from "react";
import { ArrowLeft, CheckCircle2, Circle, CircleX, Clock, Hand, Inbox, Loader2, Pause, Phone, Play, Plus, RotateCw, Send, SkipForward, Sparkles, Target, X } from "lucide-react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, DemoBadge, Empty, ErrorBox, Field, Input, Loading, Modal, PageHeader, Select, Stat, Tabs, TextArea, TraceLink, cx, useApi, useApp } from "../components/ui";

// ---------------------------------------------------------------- plain-English labels
const MISSION_STATUS: Record<string, { label: string; tone: any }> = {
  active: { label: "Running", tone: "info" },
  paused: { label: "Paused", tone: "warn" },
  done: { label: "Complete", tone: "good" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};
const STEP_TONE: Record<string, any> = {
  done: "good", skipped: "neutral", failed: "bad", waiting_approval: "warn", blocked_human: "warn", waiting_event: "info",
  waiting_time: "info", running: "brand", ready: "brand", pending: "neutral",
};
const ACTION_LABEL: Record<string, string> = {
  replan: "Plan change", answer: "Answer", fix: "Fix a draft", approve: "Approve", send_by_hand: "Send by hand", call: "Call",
  do: "Your task", retry: "Problem", waiting_owner: "Waiting",
};
const OUTCOME_LABEL: Record<string, string> = {
  positive: "Positive reply", not_interested: "Not interested", booked: "Call booked", showed: "They showed up",
  no_show: "They didn't show", rescheduled: "Rescheduled", won: "Won the client", lost: "Lost this one", done: "Done",
};
const RULE_LABEL: Record<string, string> = {
  low_reply_rate: "Reply rate is low", low_show_rate: "Show rate is low", zero_calls_day14: "No calls booked by day 14",
  ai_plan: "AI suggestion",
};
const MOVE_LABEL: Record<string, string> = {
  question: "Ask one short question", resource: "Offer a free resource", curiosity: "Ask how they find clients now",
};
const moveLabel = (m?: string) => (m ? MOVE_LABEL[m] || "A different opener" : "");
const NUMBER_FIELDS = [
  { key: "dms_sent", label: "DMs sent" }, { key: "replies", label: "Replies" }, { key: "booked", label: "Calls booked" },
  { key: "showed", label: "Calls that showed" }, { key: "closed", label: "Sales closed" },
];

const usd = (n: number) => `$${Math.round(n).toLocaleString()}`;
const when = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const pct = (x?: number | null) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(1)}%`);

function StepStatusIcon({ s }: { s: string }) {
  const c = "size-5 shrink-0";
  if (s === "done") return <CheckCircle2 className={cx(c, "text-good")} aria-label="Done" />;
  if (s === "failed") return <CircleX className={cx(c, "text-bad")} aria-label="Failed" />;
  if (s === "running" || s === "ready") return <Loader2 className={cx(c, "animate-spin text-brand")} aria-label="Working" />;
  if (s === "waiting_approval") return <Inbox className={cx(c, "text-warn")} aria-label="Waiting for approval" />;
  if (s === "blocked_human") return <Hand className={cx(c, "text-warn")} aria-label="Needs you" />;
  if (s === "waiting_event" || s === "waiting_time") return <Clock className={cx(c, "text-sky-700")} aria-label="Waiting" />;
  return <Circle className={cx(c, "text-muted")} aria-label={s === "skipped" ? "Skipped" : "Not started"} />;
}

function ProgressBar({ value, tone = "brand" }: { value: number; tone?: "brand" | "good" | "warn" }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-black/5" role="progressbar" aria-valuenow={v} aria-valuemin={0} aria-valuemax={100}>
      <div className={cx("h-full rounded-full", tone === "good" ? "bg-good" : tone === "warn" ? "bg-warn" : "bg-brand")} style={{ width: `${v}%` }} />
    </div>
  );
}

// ---------------------------------------------------------------- next actions
function useAct(onDone: () => void) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async (key: string, fn: () => Promise<any>) => {
    setBusy(key);
    setErr(null);
    try {
      const r = await fn();
      onDone();
      return r;
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return { busy, err, run, setErr };
}

function MarkDoneModal({ target, onClose, onDone }: { target: { mission_id: number; step_id: number; step?: string; outcomes?: string[]; type?: string; title?: string } | null; onClose: () => void; onDone: () => void }) {
  const { toast } = useApp();
  const outcomes = target?.outcomes || [];
  const [outcome, setOutcome] = useState<string>("");
  const [note, setNote] = useState("");
  const [value, setValue] = useState("");
  const [callAt, setCallAt] = useState("");
  const [nums, setNums] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  if (!target) return null;
  const chosen = outcome || outcomes[0] || "";
  const isNumbers = target.type === "remeasure";
  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      const body: any = { note };
      if (isNumbers) {
        body.numbers = Object.fromEntries(Object.entries(nums).filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, Number(v)]));
      } else {
        if (chosen) body.outcome = chosen;
        if (chosen === "won" && value.trim()) body.value = Number(value.replace(/[$,]/g, ""));
        if (chosen === "booked" && callAt) body.starts_at = Math.round(new Date(callAt).getTime() / 1000);
      }
      await post(`/missions/${target.mission_id}/steps/${target.step_id}/mark-done`, body);
      toast("Recorded", "good");
      onDone();
      onClose();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={!!target} onClose={onClose} title={target.step || "Record what happened"}>
      <div className="space-y-3">
        {target.title && <p className="text-sm text-muted">{target.title}</p>}
        {isNumbers ? (
          <div className="grid gap-3 sm:grid-cols-2">
            {NUMBER_FIELDS.map((f) => (
              <Field key={f.key} label={f.label}>
                <Input inputMode="decimal" value={nums[f.key] || ""} onChange={(e) => setNums({ ...nums, [f.key]: e.target.value })} />
              </Field>
            ))}
          </div>
        ) : outcomes.length > 1 ? (
          <Field label="What happened?">
            <Select value={chosen} onChange={(e) => setOutcome(e.target.value)} options={outcomes.map((o) => ({ value: o, label: OUTCOME_LABEL[o] || o }))} />
          </Field>
        ) : null}
        {!isNumbers && chosen === "won" && (
          <Field label="Deal value in USD (optional)" hint="Your own recorded figure. It's shown as recorded, never as a projection.">
            <Input inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder="e.g. 2000" />
          </Field>
        )}
        {!isNumbers && chosen === "booked" && (
          <Field label="Call time (optional)" hint="Used for the confirmation and reminders, which still wait for your approval.">
            <Input type="datetime-local" value={callAt} onChange={(e) => setCallAt(e.target.value)} />
          </Field>
        )}
        <Field label="Note (optional)">
          <TextArea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <p className="text-xs text-muted">This records what happened. It never sends anything.</p>
        <ErrorBox error={err} />
        <div className="flex flex-wrap justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button tone="primary" loading={busy} onClick={submit}>Save</Button>
        </div>
      </div>
    </Modal>
  );
}

function ActionIcon({ kind }: { kind: string }) {
  const c = "size-4 shrink-0";
  if (kind === "approve" || kind === "fix") return <Inbox className={cx(c, "text-warn")} />;
  if (kind === "send_by_hand") return <Send className={cx(c, "text-brand")} />;
  if (kind === "call") return <Phone className={cx(c, "text-brand")} />;
  if (kind === "replan") return <Sparkles className={cx(c, "text-brand")} />;
  if (kind === "retry") return <CircleX className={cx(c, "text-bad")} />;
  if (kind === "waiting_owner") return <Clock className={cx(c, "text-muted")} />;
  return <Hand className={cx(c, "text-warn")} />;
}

function NextActions({ items, onChanged, compact }: { items: any[]; onChanged: () => void; compact?: boolean }) {
  const { go, toast } = useApp();
  const [mark, setMark] = useState<any>(null);
  const { busy, err, run } = useAct(onChanged);
  if (!items.length) return <Empty title="Nothing waiting on you" hint="When a mission needs an approval, an answer or a call, it shows up here." icon={<CheckCircle2 className="size-6" />} />;
  const click = (a: any) => {
    if (a.kind === "approve" || a.kind === "fix") return go(a.href.replace(/^#/, ""));
    if (a.kind === "send_by_hand") return run(`s${a.queue_id}`, async () => { await post(`/queue/${a.queue_id}/sent`, {}); toast("Marked as sent", "good"); });
    if (a.kind === "retry") return run(`r${a.step_id}`, () => post(`/missions/${a.mission_id}/steps/${a.step_id}/retry`, {}));
    if (a.kind === "answer") return go(`/missions?id=${a.mission_id}&tab=answers`);
    if (a.kind === "replan") return go(`/missions?id=${a.mission_id}&tab=changes`);
    if (a.outcomes?.length || a.kind === "call") return setMark(a);
    if (a.href) return go(a.href.replace(/^#/, ""));
    return go(`/missions?id=${a.mission_id}`);
  };
  return (
    <>
      <ErrorBox error={err} />
      <ul className="divide-y divide-line" data-testid="next-actions">
        {items.map((a, i) => (
          <li key={`${a.kind}-${a.mission_id}-${a.step_id ?? ""}-${a.queue_id ?? ""}-${a.replan_id ?? ""}-${i}`} className="flex flex-wrap items-start gap-2 py-2.5">
            <ActionIcon kind={a.kind} />
            <div className="min-w-0 flex-1">
              <p className="break-words text-sm">{a.title}</p>
              <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                <Badge tone={a.kind === "fix" || a.kind === "retry" ? "bad" : a.kind === "waiting_owner" ? "neutral" : "warn"}>{ACTION_LABEL[a.kind] || "To do"}</Badge>
                {!compact && <span className="break-words">{a.mission}</span>}
                {a.demo && <DemoBadge />}
              </p>
            </div>
            {a.cta && (
              <Button size="sm" tone={a.kind === "approve" ? "primary" : "secondary"} loading={busy === `s${a.queue_id}` || busy === `r${a.step_id}`} onClick={() => click(a)}>
                {a.cta}
              </Button>
            )}
          </li>
        ))}
      </ul>
      <MarkDoneModal target={mark && { mission_id: mark.mission_id, step_id: mark.step_id, step: mark.step, outcomes: mark.outcomes, title: mark.title }} onClose={() => setMark(null)} onDone={onChanged} />
    </>
  );
}

// ---------------------------------------------------------------- new mission dialog
function QuestionInput({ q, value, onChange, clients }: { q: any; value: any; onChange: (v: any) => void; clients: any[] }) {
  if (q.kind === "select") return <Select value={value ?? ""} onChange={(e) => onChange(e.target.value)} options={[{ value: "", label: "Choose…" }, ...q.options.map((o: string) => ({ value: o, label: o }))]} />;
  if (q.kind === "client") return clients.length ? <Select value={value ?? ""} onChange={(e) => onChange(e.target.value)} options={[{ value: "", label: "Choose a client…" }, ...clients.map((c) => ({ value: String(c.id), label: c.name }))]} /> : <p className="text-xs text-muted">No clients yet. You can choose one later.</p>;
  if (q.kind === "date") return <Input type="date" value={value ?? ""} onChange={(e) => onChange(e.target.value)} />;
  if (q.kind === "number" || q.kind === "money") return <Input inputMode="decimal" value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder={q.kind === "money" ? "e.g. 2000" : q.placeholder} />;
  return <Input value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder={q.placeholder} maxLength={200} />;
}

function cleanAnswers(raw: Record<string, any>) {
  return Object.fromEntries(Object.entries(raw).filter(([, v]) => v !== "" && v !== null && v !== undefined));
}

function NewMission({ open, onClose, initialGoal = "" }: { open: boolean; onClose: () => void; initialGoal?: string }) {
  const { go, toast } = useApp();
  const tpls = useApi<any>(open ? "/missions/templates" : null, [open]);
  const [goal, setGoal] = useState(initialGoal);
  const [tpl, setTpl] = useState("custom");
  const [answers, setAnswers] = useState<Record<string, any>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const chosen = tpls.data?.templates?.find((t: any) => t.key === tpl);
  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/missions", { goal: goal.trim() || chosen?.label || "My goal", template: tpl, answers: cleanAnswers(answers) });
      toast("Mission started", "good");
      onClose();
      go(`/missions?id=${r.id}`);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={onClose} title="New mission" wide>
      {tpls.loading && <Loading rows={4} />}
      <ErrorBox error={tpls.error} retry={tpls.reload} />
      {tpls.data && (
        <div className="space-y-4">
          <Field label="What do you want to achieve?" hint="In your own words, for example 'Get my first client' or 'Book 20 calls this month'.">
            <TextArea rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={300} aria-label="Goal" />
          </Field>
          <div>
            <p className="mb-1.5 text-xs font-medium text-muted">Plan</p>
            <div className="grid gap-2 sm:grid-cols-2">
              <button type="button" onClick={() => setTpl("custom")} className={cx("focus-ring rounded-lg border p-3 text-left text-sm", tpl === "custom" ? "border-brand bg-panel-2" : "border-line hover:bg-panel-2")}>
                <span className="font-medium">Choose for me</span>
                <span className="mt-0.5 block text-xs text-muted">Picks the closest plan from your goal. It asks for anything it still needs.</span>
              </button>
              {tpls.data.templates.map((t: any) => (
                <button key={t.key} type="button" onClick={() => { setTpl(t.key); if (!goal.trim()) setGoal(t.examples?.[0] || t.label); }} className={cx("focus-ring rounded-lg border p-3 text-left text-sm", tpl === t.key ? "border-brand bg-panel-2" : "border-line hover:bg-panel-2")}>
                  <span className="font-medium">{t.label}</span>
                  <span className="mt-0.5 block text-xs text-muted">{t.description}</span>
                </button>
              ))}
            </div>
          </div>
          {chosen && chosen.questions.length > 0 && (
            <Card title="A few answers" subtitle="Required answers can also be given later; the plan waits for them.">
              <div className="grid gap-3 sm:grid-cols-2">
                {chosen.questions.map((q: any) => (
                  <Field key={q.key} label={q.label + (q.required ? " *" : "")} hint={q.help}>
                    <QuestionInput q={q} value={answers[q.key]} onChange={(v) => setAnswers({ ...answers, [q.key]: v })} clients={tpls.data.clients} />
                  </Field>
                ))}
              </div>
            </Card>
          )}
          {chosen && (
            <details className="text-sm">
              <summary className="cursor-pointer text-muted">The {chosen.steps.length} steps in this plan</summary>
              <ol className="mt-2 list-decimal space-y-0.5 pl-5">{chosen.steps.map((s: any) => <li key={s.skey}>{s.title}</li>)}</ol>
            </details>
          )}
          <p className="text-xs text-muted">{tpls.data.note} {tpls.data.ai?.label}.</p>
          <ErrorBox error={err} />
          <div className="flex flex-wrap justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button tone="primary" loading={busy} onClick={start} disabled={!goal.trim() && tpl === "custom"}><Target className="size-4" /> Start mission</Button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- overview
function MissionCard({ m, mine }: { m: any; mine: boolean }) {
  const { go } = useApp();
  const st = MISSION_STATUS[m.status] || { label: m.status, tone: "neutral" };
  const h = m.headline || {};
  return (
    <button type="button" onClick={() => go(`/missions?id=${m.id}`)} className="card focus-ring block w-full p-4 text-left hover:border-brand" data-testid="mission-card">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={st.tone}>{st.label}</Badge>
        <Badge>{m.template_label}</Badge>
        {m.demo && <DemoBadge />}
        {m.progress.needs_you > 0 && <Badge tone="warn">Needs you: {m.progress.needs_you}</Badge>}
        {m.progress.waiting_approval > 0 && <Badge tone="warn">Waiting for approval</Badge>}
      </div>
      <p className="mt-2 break-words font-semibold">{m.title}</p>
      {!mine && m.owner_name && <p className="text-xs text-muted">Run by {m.owner_name}</p>}
      <div className="mt-3 flex items-center gap-2">
        <ProgressBar value={m.progress.pct} tone={m.status === "done" ? "good" : "brand"} />
        <span className="shrink-0 text-xs tabular-nums text-muted">{m.progress.done}/{m.progress.total} steps</span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs sm:grid-cols-4">
        <span><span className="text-muted">Openers sent </span><b className="tabular-nums">{h.openers_sent ?? 0}</b></span>
        <span><span className="text-muted">Replies </span><b className="tabular-nums">{h.replies ?? 0}</b></span>
        <span><span className="text-muted">Calls booked </span><b className="tabular-nums">{h.calls_booked ?? 0}</b></span>
        <span>{h.time_to_first_client_days != null ? <><span className="text-muted">Won in </span><b>{h.time_to_first_client_days} days</b></> : <><span className="text-muted">Day </span><b className="tabular-nums">{Math.floor(h.days_active ?? 0) + 1}</b></>}</span>
      </div>
    </button>
  );
}

function Overview({ initialGoal }: { initialGoal?: string }) {
  const { me } = useApp();
  const [scope, setScope] = useState("mine");
  const [open, setOpen] = useState(!!initialGoal);
  const list = useApi<any>(`/missions?scope=${scope}`, [scope]);
  const acts = useApi<any>("/missions/next-actions");
  const reload = () => { list.reload(); acts.reload(); };
  const scopes: string[] = list.data?.scopes || ["mine"];
  const scopeLabel: Record<string, string> = { mine: "Mine", all: "Everyone's", students: me.user.role === "coach" ? "My students'" : "Students'" };
  return (
    <div>
      <PageHeader title="Missions" subtitle="Give it a goal. It plans the steps, drafts the work and waits for your approval before anything goes out."
        right={<Button tone="primary" onClick={() => setOpen(true)}><Plus className="size-4" /> New mission</Button>} />
      <Card title="Your next actions" subtitle="Approvals, answers, calls and messages to send by hand, across your missions." className="mb-4">
        {acts.loading && !acts.data && <Loading rows={3} />}
        <ErrorBox error={acts.error} retry={acts.reload} />
        {acts.data && <NextActions items={acts.data.items} onChanged={reload} />}
      </Card>
      {scopes.length > 1 && <Tabs value={scope} onChange={setScope} tabs={scopes.map((s) => ({ key: s, label: `${scopeLabel[s] || s} missions` }))} />}
      {list.loading && !list.data && <Loading rows={4} />}
      <ErrorBox error={list.error} retry={list.reload} />
      {list.data && (list.data.missions.length === 0 ? (
        <Empty icon={<Target className="size-6" />} title={scope === "mine" ? "No missions yet" : "No missions here yet"}
          hint={scope === "mine" ? "Start with a goal like 'Get my first client' or 'Book 20 calls'. The plan works without AI." : "Missions your team starts show up here."}
          action={scope === "mine" ? <Button tone="primary" className="mt-2" onClick={() => setOpen(true)}><Plus className="size-4" /> Start a mission</Button> : undefined} />
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {list.data.missions.map((m: any) => <MissionCard key={m.id} m={m} mine={m.user_id === me.user.id} />)}
        </div>
      ))}
      {list.data && <p className="mt-4 text-xs text-muted">{list.data.ai?.label}.</p>}
      <NewMission open={open} onClose={() => { setOpen(false); reload(); }} initialGoal={initialGoal} />
    </div>
  );
}

// ---------------------------------------------------------------- detail
function StepRow({ s, m, onChanged, onMark }: { s: any; m: any; onChanged: () => void; onMark: (s: any) => void }) {
  const { go, me } = useApp();
  const skippable = s.type !== "intake" && (s.type !== "cert_gate" || me.user.role === "owner");
  const { busy, err, run } = useAct(onChanged);
  const open = !["done", "skipped"].includes(s.status);
  const markable = m.can_act && m.status === "active" && s.human_action && (["blocked_human", "waiting_event", "waiting_time"].includes(s.status));
  const brief = s.output?.brief;
  return (
    <li className="relative flex gap-3 pb-4 last:pb-0" data-testid="mission-step" data-status={s.status}>
      <div className="flex flex-col items-center">
        <StepStatusIcon s={s.status} />
        <span className="mt-1 w-px flex-1 bg-line" aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium">{s.title}</span>
          <Badge tone={STEP_TONE[s.status]}>{s.status_label}</Badge>
          {s.recurring && <Badge tone="neutral">Repeats</Badge>}
          {s.outbound && <Badge tone="neutral">Needs approval</Badge>}
          {s.cycles > 0 && s.recurring && <span className="text-xs text-muted">{s.cycles} round{s.cycles === 1 ? "" : "s"} done</span>}
        </div>
        {s.message && s.status !== "pending" && <p className="mt-0.5 break-words text-sm text-ink/80">{s.message}</p>}
        {s.status === "pending" && <p className="mt-0.5 text-xs text-muted">{s.due ? `Trying again ${when(s.due)}` : s.success}</p>}
        {brief && (
          <details className="mt-1 text-sm">
            <summary className="cursor-pointer text-brand">Pre-call brief</summary>
            <div className="mt-1 space-y-1 rounded-lg bg-panel-2 p-2 text-xs">
              <p><b>Who:</b> {brief.who} · {brief.what_they_do}</p>
              {brief.facts?.length > 0 && <ul className="list-disc pl-4">{brief.facts.map((f: any, i: number) => <li key={i}>{f.claim}</li>)}</ul>}
              <p><b>Ask:</b> {brief.ask?.join(" · ")}</p>
              <p><b>Prepare for:</b> {brief.prepare_for?.join(" · ")}</p>
              <p className="text-muted">{brief.rules}</p>
            </div>
          </details>
        )}
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <TraceLink runId={s.run_id} />
          {s.queue_ids?.length > 0 && <button className="focus-ring text-xs text-brand underline-offset-2 hover:underline" onClick={() => go(`/queue?ids=${s.queue_ids.join(",")}&status=all`)}>Drafts ({s.queue_ids.length})</button>}
          {s.href && open && <button className="focus-ring text-xs text-brand underline-offset-2 hover:underline" onClick={() => go(s.href.replace(/^#/, ""))}>Open</button>}
          {m.can_act && m.status === "active" && (
            <>
              {markable && <Button size="sm" tone="primary" onClick={() => onMark(s)}>{s.human_action}</Button>}
              {s.type === "intake" && s.status === "blocked_human" && <Button size="sm" tone="primary" onClick={() => go(`/missions?id=${m.id}&tab=answers`)}>Answer</Button>}
              {["failed", "blocked_human"].includes(s.status) && s.type !== "intake" && (
                <Button size="sm" loading={busy === "retry"} onClick={() => run("retry", () => post(`/missions/${m.id}/steps/${s.id}/retry`, {}))}><RotateCw className="size-3.5" /> Retry</Button>
              )}
              {open && skippable && <Button size="sm" tone="ghost" loading={busy === "skip"} onClick={() => run("skip", () => post(`/missions/${m.id}/steps/${s.id}/skip`, {}))}><SkipForward className="size-3.5" /> Skip</Button>}
            </>
          )}
        </div>
        <ErrorBox error={err} />
      </div>
    </li>
  );
}

function AnswersForm({ m, onSaved }: { m: any; onSaved: () => void }) {
  const { toast } = useApp();
  const clients = useApi<any>("/missions/templates");
  const [answers, setAnswers] = useState<Record<string, any>>(() => {
    const a: Record<string, any> = {};
    for (const q of m.questions) {
      const v = m.answers[q.key];
      if (v === undefined || v === null) continue;
      a[q.key] = q.kind === "date" ? new Date(v * 1000).toISOString().slice(0, 10) : String(v);
    }
    return a;
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      await post(`/missions/${m.id}/answers`, { answers: cleanAnswers(answers) });
      toast("Answers saved", "good");
      onSaved();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  if (!m.questions.length) return <Empty title="No questions for this plan" />;
  const editable = m.can_act && ["active", "paused"].includes(m.status);
  return (
    <Card title="Planning answers" subtitle={editable ? "The plan uses these to set targets and draft your offer, openers and proposal." : "Read only: only the person running this mission can change its answers."}>
      <fieldset disabled={!editable} className="grid min-w-0 gap-3 sm:grid-cols-2">
        {m.questions.map((q: any) => (
          <Field key={q.key} label={q.label + (q.required ? " *" : "")} hint={q.help}>
            <QuestionInput q={q} value={answers[q.key]} onChange={(v) => setAnswers({ ...answers, [q.key]: v })} clients={clients.data?.clients?.length ? clients.data.clients : m.client ? [m.client] : []} />
          </Field>
        ))}
      </fieldset>
      <ErrorBox error={err} />
      {editable && <div className="mt-3 flex justify-end"><Button tone="primary" loading={busy} onClick={save}>Save answers</Button></div>}
    </Card>
  );
}

function ProgressTab({ m }: { m: any }) {
  const met = m.metrics;
  const t = m.targets;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Openers sent" value={met.leading.openers_sent} hint="Confirmed sends" />
        <Stat label="Replies" value={met.leading.replies} hint={met.rates.reply_rate == null ? "Reply rate: nothing sent yet" : `Reply rate ${pct(met.rates.reply_rate)}`} />
        <Stat label="Calls booked" value={met.lagging.calls_booked} hint={met.rates.show_rate == null ? "Show rate: no calls yet" : `Show rate ${pct(met.rates.show_rate)}`} />
        <Stat label={met.time_to_first_client_days != null ? "Time to first client" : "Days running"} value={met.time_to_first_client_days != null ? `${met.time_to_first_client_days} days` : met.days_active} tone={met.lagging.wins ? "good" : undefined} />
      </div>
      {m.plan_vs_actual.length > 0 ? (
        <Card title="Plan vs actual" subtitle="Leading indicators move first; lagging ones follow. 'Expected by now' is the plan's model at the programme's benchmark rates.">
          <ul className="space-y-3">
            {m.plan_vs_actual.map((r: any) => {
              const p = r.expected_by_now >= 1 ? Math.min(100, (100 * r.actual) / r.expected_by_now) : r.actual > 0 ? 100 : 0;
              return (
                <li key={r.key}>
                  <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span className="flex items-center gap-1.5"><span className="font-medium">{r.label}</span><Badge tone={r.kind === "leading" ? "info" : "neutral"}>{r.kind === "leading" ? "Leading" : "Lagging"}</Badge></span>
                    <span className="tabular-nums text-xs text-muted"><b className="text-ink">{r.actual}</b> of {Math.round(r.expected_by_now)} expected by now · plan total {r.target.toLocaleString()}</span>
                  </div>
                  <div className="mt-1"><ProgressBar value={p} tone={p >= 90 ? "good" : p >= 50 ? "brand" : "warn"} /></div>
                </li>
              );
            })}
          </ul>
          {t?.summary && <p className="mt-3 text-xs text-muted">{t.summary}</p>}
        </Card>
      ) : (
        <Card title="Plan vs actual"><p className="text-sm text-muted">{t?.summary || "Targets appear once the planning questions are answered."}</p></Card>
      )}
      <Card title="Drafts and results">
        <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3">
          <div><dt className="text-xs text-muted">Waiting for approval</dt><dd className="tabular-nums">{met.drafts.waiting}</dd></div>
          <div><dt className="text-xs text-muted">Approved</dt><dd className="tabular-nums">{met.drafts.approved}</dd></div>
          <div><dt className="text-xs text-muted">Rejected</dt><dd className="tabular-nums">{met.drafts.rejected}</dd></div>
          <div><dt className="text-xs text-muted">Calls that showed</dt><dd className="tabular-nums">{met.lagging.calls_showed}</dd></div>
          <div><dt className="text-xs text-muted">Proposals sent</dt><dd className="tabular-nums">{met.lagging.proposals_sent}</dd></div>
          <div><dt className="text-xs text-muted">Clients won</dt><dd className="tabular-nums">{met.lagging.wins}</dd></div>
        </dl>
        {met.recorded_revenue != null && <p className="mt-3 text-sm">Your recorded revenue: <b>{usd(met.recorded_revenue)}</b> <span className="text-xs text-muted">({met.revenue_note})</span></p>}
      </Card>
    </div>
  );
}

function ChangesTab({ m, onChanged }: { m: any; onChanged: () => void }) {
  const { toast } = useApp();
  const { busy, err, run } = useAct(onChanged);
  const [note, setNote] = useState<string | null>(null);
  const check = (source: "rules" | "ai") => run(source, async () => {
    const r = await post(`/missions/${m.id}/replan`, { source });
    setNote(r.label || null);
    if (r.findings?.length || r.replan_id) toast("Suggestion ready below", "info");
  });
  const decide = (id: number, accept: boolean) => run(`d${id}`, () => post(`/missions/${m.id}/replan/confirm`, { replan_id: id, accept }));
  return (
    <div className="space-y-4">
      {m.can_act && m.status === "active" && (
        <Card title="Check the plan" subtitle="Rules first: low reply rate after 80 sends, low show rate, or no calls by day 14 each suggest a change you confirm.">
          <div className="flex flex-wrap gap-2">
            <Button loading={busy === "rules"} onClick={() => check("rules")}><RotateCw className="size-4" /> Check the numbers now</Button>
            {m.ai?.enabled && <Button loading={busy === "ai"} onClick={() => check("ai")}><Sparkles className="size-4" /> Ask AI for a better plan</Button>}
          </div>
          <p className="mt-2 text-xs text-muted">{m.ai?.label}.</p>
          {note && <p className="mt-2 text-sm">{note}</p>}
        </Card>
      )}
      <ErrorBox error={err} />
      {m.replans.length === 0 ? <Empty title="No plan changes yet" hint="Suggestions appear here after a checkpoint or a check." /> : (
        <ul className="space-y-3">
          {m.replans.map((r: any) => (
            <li key={r.id} className="card p-4" data-testid="replan">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{RULE_LABEL[r.rule] || "Plan change"}</span>
                <Badge tone={r.status === "proposed" ? "warn" : r.status === "applied" ? "good" : "neutral"}>{r.status === "proposed" ? "Waiting for you" : r.status === "applied" ? "Applied" : "Dismissed"}</Badge>
                <Badge>{r.source === "ai" ? "AI, checked against the step list" : "Rules"}</Badge>
              </div>
              {r.diff.why && <p className="mt-1 text-sm">{r.diff.why}</p>}
              <ul className="mt-2 space-y-0.5 text-sm">
                {Object.entries(r.diff.context || {}).map(([k, ch]: any) => <li key={k}>Change {k === "opener_move" ? "the opener" : "a setting"}: <b>{k === "opener_move" ? moveLabel(ch.from) : String(ch.from)}</b> → <b>{k === "opener_move" ? moveLabel(ch.to) : String(ch.to)}</b></li>)}
                {(r.diff.add || []).map((a: any) => <li key={a.skey}>Add step: <b>{a.title}</b>{a.outbound ? " (its drafts wait for approval)" : ""}</li>)}
                {(r.diff.remove || []).map((k: string) => <li key={k}>Remove a step that hasn't started: <b>{m.steps.find((s: any) => s.skey === k)?.title || k}</b></li>)}
              </ul>
              {r.diff.rejected?.length > 0 && <p className="mt-1 text-xs text-muted">{r.diff.rejected.length} AI suggestion(s) were left out because they aren't steps this workspace has.</p>}
              {r.status === "proposed" && m.can_act && (
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button tone="primary" size="sm" loading={busy === `d${r.id}`} onClick={() => decide(r.id, true)}>Apply this change</Button>
                  <Button size="sm" onClick={() => decide(r.id, false)}>Dismiss</Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function LogTab({ m }: { m: any }) {
  const { go } = useApp();
  if (!m.events.length) return <Empty title="Nothing logged yet" />;
  return (
    <Card pad={false}>
      <ul className="divide-y divide-line" data-testid="mission-log">
        {m.events.map((e: any) => (
          <li key={e.id} className="flex flex-wrap items-start gap-2 px-4 py-2 text-sm">
            <span className="w-full shrink-0 text-xs tabular-nums text-muted sm:w-36">{when(e.at)}</span>
            <Badge>{e.label}</Badge>
            <span className="min-w-0 flex-1 break-words">{e.text}</span>
            <TraceLink runId={e.run_id} />
            {e.queue_ids?.length > 0 && <button className="focus-ring text-xs text-brand hover:underline" onClick={() => go(`/queue?ids=${e.queue_ids.join(",")}&status=all`)}>Drafts</button>}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function MemoryTab({ m }: { m: any }) {
  const mem = m.memory || {};
  const ans = Object.entries(m.answers || {});
  const qlabel = (k: string) => m.questions.find((q: any) => q.key === k)?.label || k.replace(/_/g, " ");
  const show = (k: string, v: any) => {
    const q = m.questions.find((x: any) => x.key === k);
    if (q?.kind === "date" && typeof v === "number") return new Date(v * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    if (q?.kind === "client" || k === "client_id") return m.client?.name || "A client you no longer have access to";
    if (typeof v === "number" && (q?.kind === "money" || k === "price" || k === "offer_price" || k === "target_cpl")) return usd(v);
    return String(v);
  };
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card title="What the mission knows" subtitle="Used by later steps: your audience, offer and what has worked.">
        <dl className="space-y-2 text-sm">
          {ans.length === 0 && <p className="text-muted">No answers yet.</p>}
          {ans.map(([k, v]) => <div key={k}><dt className="text-xs text-muted">{qlabel(k)}</dt><dd className="break-words">{show(k, v)}</dd></div>)}
          {mem.opener_move && <div><dt className="text-xs text-muted">Opener in use</dt><dd>{moveLabel(mem.opener_move)}</dd></div>}
          {mem.diagnosis?.bottleneck && <div><dt className="text-xs text-muted">Bottleneck found</dt><dd>{mem.diagnosis.bottleneck.label}</dd></div>}
        </dl>
      </Card>
      <Card title="Decisions and what worked">
        {!(mem.decisions?.length || mem.what_worked?.length) && <p className="text-sm text-muted">Nothing recorded yet.</p>}
        {mem.decisions?.length > 0 && <ul className="mb-3 list-disc space-y-1 pl-4 text-sm">{mem.decisions.map((d: any, i: number) => <li key={i}>{d.why || d.rule} <span className="text-xs text-muted">({when(d.at)})</span></li>)}</ul>}
        {mem.what_worked?.length > 0 && <ul className="list-disc space-y-1 pl-4 text-sm">{mem.what_worked.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul>}
      </Card>
    </div>
  );
}

function Detail({ id, tab0 }: { id: number; tab0: string }) {
  const { go } = useApp();
  const { data: m, error, loading, reload } = useApi<any>(`/missions/${id}`, [id]);
  const [tab, setTab] = useState(tab0 || "plan");
  const [mark, setMark] = useState<any>(null);
  const { busy, err, run } = useAct(reload);
  const back = <Button onClick={() => go("/missions")}><ArrowLeft className="size-4" /> All missions</Button>;
  if (loading && !m) return <div><PageHeader title="Mission" right={back} /><Loading rows={6} /></div>;
  if (error || !m) return <div><PageHeader title="Mission" right={back} /><ErrorBox error={error || "Mission not found"} retry={reload} /></div>;
  const st = MISSION_STATUS[m.status] || { label: m.status, tone: "neutral" };
  const intakeBlocked = m.steps.some((s: any) => s.type === "intake" && s.status === "blocked_human");
  const tabs = [{ key: "plan", label: "Plan" }, { key: "progress", label: "Progress" }, { key: "changes", label: `Plan changes${m.replans.some((r: any) => r.status === "proposed") ? " •" : ""}` },
    { key: "log", label: "Log" }, { key: "memory", label: "Memory" }, { key: "answers", label: "Answers" }];
  return (
    <div>
      <PageHeader title={m.title} badge={m.demo ? <DemoBadge /> : undefined}
        subtitle={<span className="flex flex-wrap items-center gap-1.5"><Badge tone={st.tone}>{st.label}</Badge><span>{m.template_label}</span><span>· {m.plan_source === "rules" ? "Rules plan (works with AI off)" : "Rules plan with AI suggestions you accepted"}</span>{m.owner_name && !m.can_act && <span>· run by {m.owner_name}</span>}</span>}
        right={<>
          {back}
          {m.can_act && m.status === "active" && <Button loading={busy === "pause"} onClick={() => run("pause", () => post(`/missions/${id}/pause`, {}))}><Pause className="size-4" /> Pause</Button>}
          {m.can_act && m.status === "paused" && <Button tone="primary" loading={busy === "resume"} onClick={() => run("resume", () => post(`/missions/${id}/resume`, {}))}><Play className="size-4" /> Resume</Button>}
          {m.can_act && ["active", "paused"].includes(m.status) && <Button tone="danger" loading={busy === "cancel"} onClick={() => { if (confirm("Cancel this mission? Drafts already in the queue stay there for you to decide.")) run("cancel", () => post(`/missions/${id}/cancel`, {})); }}><X className="size-4" /> Cancel</Button>}
        </>} />
      <ErrorBox error={err} />
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Steps done" value={`${m.progress.done}/${m.progress.total}`} hint={<ProgressBar value={m.progress.pct} />} />
        <Stat label="Need you" value={m.progress.needs_you} tone={m.progress.needs_you ? "warn" : undefined} />
        <Stat label="Waiting for approval" value={m.progress.waiting_approval} />
        <Stat label={m.metrics.time_to_first_client_days != null ? "Time to first client" : "Days running"} value={m.metrics.time_to_first_client_days != null ? `${m.metrics.time_to_first_client_days} days` : m.metrics.days_active} />
      </div>
      {m.can_act && m.next_actions.length > 0 && (
        <Card title="Next actions on this mission" className="mb-4"><NextActions items={m.next_actions} onChanged={reload} compact /></Card>
      )}
      {intakeBlocked && tab !== "answers" && (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm">
          <Hand className="size-4 text-warn" /><span className="min-w-0 flex-1">The plan needs a few answers before it can start.</span>
          <Button size="sm" tone="primary" onClick={() => setTab("answers")}>Answer now</Button>
        </div>
      )}
      <Tabs value={tab} onChange={setTab} tabs={tabs} />
      {tab === "plan" && (
        <Card>
          <ol data-testid="mission-steps">{m.steps.map((s: any) => <StepRow key={s.id} s={s} m={m} onChanged={reload} onMark={(x) => setMark({ mission_id: m.id, step_id: x.id, step: x.human_action, outcomes: x.outcomes, type: x.type, title: x.message })} />)}</ol>
          {m.children.length > 0 && (
            <div className="mt-4 border-t border-line pt-3 text-sm">
              <p className="mb-1 text-xs font-medium text-muted">Follow-on missions</p>
              {m.children.map((c: any) => <button key={c.id} className="focus-ring mr-3 text-brand hover:underline" onClick={() => go(`/missions?id=${c.id}`)}>{c.title}</button>)}
            </div>
          )}
        </Card>
      )}
      {tab === "progress" && <ProgressTab m={m} />}
      {tab === "changes" && <ChangesTab m={m} onChanged={reload} />}
      {tab === "log" && <LogTab m={m} />}
      {tab === "memory" && <MemoryTab m={m} />}
      {tab === "answers" && <AnswersForm m={m} onSaved={() => { reload(); setTab("plan"); }} />}
      <MarkDoneModal target={mark} onClose={() => setMark(null)} onDone={reload} />
    </div>
  );
}

export default function Missions({ query }: { query: URLSearchParams }) {
  const id = Number(query.get("id") || 0);
  const tab = query.get("tab") || "plan";
  if (id > 0) return <Detail key={`${id}-${tab}`} id={id} tab0={tab} />;
  return <Overview initialGoal={query.get("goal") || undefined} />;
}
