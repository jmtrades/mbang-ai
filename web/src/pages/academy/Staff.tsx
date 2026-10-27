// Academy screens for the owner and coaches: Team (cohort, heatmap, misconceptions, sign-offs, impact) and
// Admin (build, drafts, coverage, question stats, thresholds, the outreach certificate switch). Owner-only
// actions are hidden from coaches; the server checks every one of them again.
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Hammer, ShieldCheck } from "lucide-react";
import { ApiError, del, post, put } from "../../api";
import { Badge, Button, Card, Citations, DemoBadge, Drawer, Empty, ErrorBox, Field, Input, Loading, Select, Stat, Table, Tabs, TextArea, TraceLink, cx, useApi, useApp } from "../../components/ui";
import { Bar, pct } from "./Learn";

const ago = (t?: number | null) => {
  if (!t) return "never";
  const d = (Date.now() / 1000 - t) / 86400;
  return d < 1 ? "today" : d < 2 ? "yesterday" : `${Math.floor(d)} days ago`;
};

function Spark({ values }: { values: number[] }) {
  if (!values || values.length < 2) return <span className="text-xs text-muted">–</span>;
  const w = 64, h = 18;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - v * h}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-label="Mastery trend" role="img">
      <polyline points={pts} fill="none" stroke="var(--brand)" strokeWidth="1.5" />
    </svg>
  );
}

function heat(v: number | undefined) {
  if (v === undefined) return "bg-black/5 text-muted";
  if (v >= 0.8) return "bg-green-200 text-green-900";
  if (v >= 0.7) return "bg-green-100 text-green-900";
  if (v >= 0.5) return "bg-amber-100 text-amber-900";
  return "bg-red-100 text-red-900";
}

// ---------------------------------------------------------------- learner drawer (assign, certificates)
function LearnerDrawer({ userId, onClose }: { userId: number | null; onClose: () => void }) {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>(userId ? `/academy/team/${userId}` : null, [userId]);
  const lessons = useApi<any>(userId ? "/academy/admin/drafts?status=published" : null, [userId]);
  const [kind, setKind] = useState("lesson");
  const [ref, setRef] = useState("");
  const [due, setDue] = useState("7");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const assign = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/academy/assign", { user_id: userId, kind, ref, due_days: Number(due) || null, note });
      toast(`Assigned. A note to the learner is waiting in the approval queue (#${r.queue_id}).`, "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const act = async (certId: number, what: "signoff" | "revoke") => {
    setErr(null);
    try {
      if (what === "revoke") {
        const reason = window.prompt("Why revoke this certificate?") || "";
        if (reason.trim().length < 3) return;
        await post(`/academy/certs/${certId}/revoke`, { reason });
      } else {
        await post(`/academy/certs/${certId}/signoff`);
      }
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const lessonOpts = (lessons.data?.lessons || []).map((l: any) => ({ value: String(l.id), label: l.title }));
  return (
    <Drawer open={!!userId} onClose={onClose} title={data?.user?.name || "Learner"}>
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      <ErrorBox error={err} />
      {data && (
        <div className="space-y-4">
          <Card title="Skills">
            {data.mastery.length === 0 ? <p className="text-sm text-muted">No answers yet.</p> : (
              <ul className="space-y-2">
                {data.mastery.map((m: any) => (
                  <li key={m.skill} className="text-sm">
                    <div className="flex justify-between gap-2"><span>{m.label}</span><span className="text-xs text-muted">{pct(m.chance)} · {m.n} answers</span></div>
                    <Bar value={m.chance} tone={m.chance >= 0.7 ? "good" : "brand"} />
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Certificates">
            <ul className="space-y-2 text-sm">
              {data.ladders.map((lad: any) => lad.levels.filter((l: any) => l.cert).map((l: any) => (
                <li key={`${lad.path}${l.level}`} className="flex flex-wrap items-center gap-2">
                  <b>{lad.title} {l.level}</b>
                  <Badge tone={l.status === "active" ? "good" : "warn"}>{l.status_label}</Badge>
                  {l.cert.signed_off_by && <span className="text-xs text-muted">signed off by {l.cert.signed_off_by}</span>}
                  {l.status === "active" && !l.cert.signed_off_by && <Button size="sm" onClick={() => act(l.cert.id, "signoff")}>Sign off</Button>}
                  {l.status === "active" && <Button size="sm" tone="danger" onClick={() => act(l.cert.id, "revoke")}>Revoke</Button>}
                </li>
              )))}
              {!data.ladders.some((lad: any) => lad.levels.some((l: any) => l.cert)) && <li className="text-muted">None yet.</li>}
            </ul>
          </Card>
          <Card title="Assign practice" subtitle="Drafts a short note to the learner for approval; nothing is sent without it.">
            <div className="space-y-2">
              <Field label="What">
                <Select value={kind} onChange={(e) => { setKind(e.target.value); setRef(""); }} options={[{ value: "lesson", label: "A lesson" }, { value: "path", label: "A whole path" }, { value: "scenario", label: "A role-play scenario" }]} />
              </Field>
              {kind === "lesson" && <Field label="Lesson"><Select value={ref} onChange={(e) => setRef(e.target.value)} options={[{ value: "", label: lessonOpts.length ? "Pick a lesson" : "No published lessons" }, ...lessonOpts]} /></Field>}
              {kind === "path" && <Field label="Path"><Select value={ref} onChange={(e) => setRef(e.target.value)} options={[{ value: "", label: "Pick a path" }, { value: "compliance_core", label: "Compliance core" }, { value: "growth_operator", label: "Growth operator" }, { value: "setter", label: "Setter" }, { value: "closer", label: "Closer" }, { value: "coach", label: "Coach" }]} /></Field>}
              {kind === "scenario" && <Field label="Scenario name"><Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="For example: price objection on a call" /></Field>}
              <Field label="Due in (days)"><Input type="number" min={1} max={120} value={due} onChange={(e) => setDue(e.target.value)} /></Field>
              <Field label="Note (optional)"><TextArea rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
              <Button tone="primary" onClick={assign} loading={busy} disabled={!ref}>Assign</Button>
            </div>
          </Card>
          <Card title="Recent answers">
            {data.recent.length === 0 ? <p className="text-sm text-muted">No answers yet.</p> : (
              <ul className="space-y-1 text-xs">
                {data.recent.map((r: any, i: number) => (
                  <li key={i} className="flex gap-2"><span className={r.correct ? "text-good" : "text-bad"}>{r.correct ? "Right" : "Wrong"}</span><span className="min-w-0 flex-1 truncate">{r.prompt || "Exam question"}</span></li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </Drawer>
  );
}

// ---------------------------------------------------------------- sign-off queue
function SignoffRow({ r, onDone }: { r: any; onDone: () => void }) {
  const { toast } = useApp();
  const [score, setScore] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const n = Number(score);
  const valid = score.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= 100;
  const sign = async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await post(`/academy/team/roleplays/${r.id}/signoff`, { score: n, note });
      toast(res.rules_score === null || res.rules_score === undefined
        ? "Signed off."
        : `Signed off. The rules score was ${Math.round(res.rules_score)}: ${res.agreement ? "you agree within 10 points" : "more than 10 points apart"}.`,
        res.agreement === false ? "info" : "good");
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="rounded-lg border border-line p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <b>{r.name}</b>
        <Badge>{String(r.scenario || "Role-play").replace(/_/g, " ")}</Badge>
        {r.compliance_flags > 0 && <Badge tone="bad">{r.compliance_flags} compliance flag{r.compliance_flags > 1 ? "s" : ""}</Badge>}
      </div>
      {Array.isArray(r.criteria) && r.criteria.length > 0 && (
        <p className="mt-1 break-words text-xs text-muted">Scored on: {r.criteria.map((c: any) => c.label).filter(Boolean).join(", ")}</p>
      )}
      {Array.isArray(r.transcript) && r.transcript.length > 0 && (
        <div className="scroll-thin mt-2 max-h-48 space-y-1 overflow-auto rounded bg-panel-2 p-2 text-xs">
          {r.transcript.map((t: any, i: number) => <p key={i} className="break-words"><b>{t.speaker || t.role || "?"}:</b> {t.text || t.content || ""}</p>)}
        </div>
      )}
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Field label="Your score (0-100)"><Input type="number" min={0} max={100} value={score} placeholder="0-100" onChange={(e) => setScore(e.target.value)} className="w-24" /></Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} className="w-56 max-w-full" /></Field>
        <Button tone="primary" onClick={sign} loading={busy} disabled={!valid}>Sign off</Button>
      </div>
      <ErrorBox error={err} />
    </li>
  );
}

// ---------------------------------------------------------------- managers (owner): which coach looks after whom
function Managers() {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/academy/team/managers");
  const [manager, setManager] = useState("");
  const [member, setMember] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async (key: string, fn: () => Promise<any>, msg: string) => {
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
  const link = () => run("link", () => post("/academy/team/managers", { manager_id: Number(manager), member_id: Number(member) }),
    "Linked. That coach now sees this person's training.");
  const unlink = (l: any) => run(`u${l.manager_id}-${l.member_id}`, () => del(`/academy/team/managers/${l.manager_id}/${l.member_id}`),
    "Unlinked.");
  const clean = (n?: string | null) => (n || "").replace(" (DEMO)", "");
  return (
    <Card title="Who coaches whom" subtitle="Students follow their coach automatically. Link setters, closers (or other coaches) to a coach here so that coach can see and support their training.">
      {loading && <Loading rows={3} />}
      <ErrorBox error={error} retry={reload} />
      <ErrorBox error={err} />
      {data && (
        <div className="space-y-3">
          {data.managers.length === 0 ? (
            <p className="text-sm text-muted">There are no coach accounts in this workspace yet. Once a coach has an account, link people to them here.</p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
              <Field label="Coach">
                <Select value={manager} onChange={(e) => setManager(e.target.value)}
                  options={[{ value: "", label: "Pick a coach" }, ...data.managers.map((m: any) => ({ value: String(m.id), label: clean(m.name) }))]} />
              </Field>
              <Field label="Looks after">
                <Select value={member} onChange={(e) => setMember(e.target.value)}
                  options={[{ value: "", label: "Pick a person" }, ...data.members.filter((m: any) => String(m.id) !== manager)
                    .map((m: any) => ({ value: String(m.id), label: `${clean(m.name)} (${m.role})` }))]} />
              </Field>
              <Button tone="primary" onClick={link} loading={busy === "link"} disabled={!manager || !member}>Link</Button>
            </div>
          )}
          {data.links.length === 0 ? <Empty title="No links yet" hint="Without a link, only you see a setter's or closer's training." /> : (
            <ul className="divide-y divide-line rounded-lg border border-line">
              {data.links.map((l: any) => (
                <li key={`${l.manager_id}-${l.member_id}`} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                  <span className="min-w-0 break-words"><b>{clean(l.manager_name)}</b> looks after {clean(l.member_name)} <span className="text-xs capitalize text-muted">({l.member_role})</span></span>
                  <Button size="sm" tone="ghost" onClick={() => unlink(l)} loading={busy === `u${l.manager_id}-${l.member_id}`}>Unlink</Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}

export function TeamTab() {
  const { me } = useApp();
  const isOwner = me.user.role === "owner";
  const [sub, setSub] = useState("people");
  const { data, error, loading, reload } = useApi<any>("/academy/team");
  const mis = useApi<any>(sub === "misconceptions" ? "/academy/team/misconceptions" : null, [sub]);
  const sign = useApi<any>(sub === "signoffs" ? "/academy/team/signoffs" : null, [sub]);
  const imp = useApi<any>(sub === "impact" ? "/academy/team/impact" : null, [sub]);
  const [open, setOpen] = useState<number | null>(null);
  const atRisk = (data?.people || []).filter((p: any) => p.at_risk.length).length;
  return (
    <div className="space-y-4" data-testid="academy-team">
      {data && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Learners" value={data.people.length} hint={data.scope === "everyone" ? "Everyone in the workspace" : "People you coach"} />
          <Stat label="At risk" value={atRisk} tone={atRisk ? "warn" : undefined} />
          <Stat label="Certified (any level)" value={data.people.filter((p: any) => p.level).length} />
          <Stat label="Average mastery" value={pct(data.people.filter((p: any) => p.mastery !== null).reduce((a: number, p: any, _: number, arr: any[]) => a + p.mastery / arr.length, 0) || null)} />
        </div>
      )}
      <Tabs value={sub} onChange={setSub} tabs={[{ key: "people", label: "People" }, { key: "heatmap", label: "Skill heatmap" }, { key: "misconceptions", label: "Misconceptions" }, { key: "signoffs", label: "Role-play sign-off" }, { key: "impact", label: "Training impact" }, ...(isOwner ? [{ key: "managers", label: "Coaches" }] : [])]} />
      {sub === "managers" && isOwner && <Managers />}
      {loading && <Loading rows={5} />}
      <ErrorBox error={error} retry={reload} />
      {sub === "people" && data && (
        <Card pad={false}><div className="p-3">
          <Table rows={data.people} empty="No learners yet" onRow={(p) => setOpen(p.id)} cols={[
            { key: "name", label: "Learner", render: (p) => <span className="flex flex-wrap items-center gap-1.5">{p.name.replace(" (DEMO)", "")} {p.demo && <DemoBadge />}</span> },
            { key: "role", label: "Role", render: (p) => <span className="capitalize">{p.role}</span> },
            { key: "level", label: "Level", render: (p) => p.level ? <Badge tone="good">{p.level.path_title} {p.level.level}</Badge> : <span className="text-xs text-muted">None yet</span> },
            { key: "last", label: "Last practice", render: (p) => <span className="text-xs">{ago(p.last_active)}</span> },
            { key: "mastery", label: "Mastery", render: (p) => <span className="flex items-center gap-2"><span className="tabular-nums">{pct(p.mastery)}</span><Spark values={p.spark} /></span> },
            { key: "risk", label: "Flags", render: (p) => p.at_risk.length ? <span className="flex flex-wrap gap-1">{p.at_risk.map((r: string) => <Badge key={r} tone="warn">{r}</Badge>)}</span> : <Badge tone="good">On track</Badge> },
          ]} />
        </div></Card>
      )}
      {sub === "heatmap" && data && (
        <Card title="Chance of a right answer, by skill" subtitle="Green is 70% or more. Grey means not practised yet.">
          {data.people.length === 0 || data.skills.length === 0 ? <Empty title="Nothing to show yet" hint="It fills in as learners answer questions." /> : (
            <div className="scroll-thin overflow-x-auto">
              <table className="text-xs">
                <thead><tr><th className="px-2 py-1 text-left font-medium text-muted">Learner</th>{data.skills.map((s: any) => <th key={s.key} className="px-1 py-1 font-medium text-muted">{s.label}</th>)}</tr></thead>
                <tbody>
                  {data.people.map((p: any) => (
                    <tr key={p.id}>
                      <td className="whitespace-nowrap px-2 py-1">{p.name.replace(" (DEMO)", "")}</td>
                      {data.skills.map((s: any) => <td key={s.key} className="px-1 py-1"><span className={cx("block rounded px-2 py-1 text-center tabular-nums", heat(p.skills[s.key]))}>{p.skills[s.key] === undefined ? "–" : pct(p.skills[s.key])}</span></td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
      {sub === "misconceptions" && (
        <Card title="Questions your learners miss most" subtitle="With the wrong answer they pick most often and the line of the course that answers it.">
          {mis.loading && <Loading rows={4} />}
          <ErrorBox error={mis.error} retry={mis.reload} />
          {mis.data && (mis.data.items.length === 0 ? <Empty title="No misconceptions yet" hint="This needs a few answers per question." /> : (
            <ul className="space-y-3">
              {mis.data.items.map((m: any) => (
                <li key={m.id} className="rounded-lg border border-line p-3 text-sm">
                  <p className="break-words font-medium">{m.prompt}</p>
                  <p className="mt-1 text-xs text-muted">{Math.round((m.p_value || 0) * 100)}% right over {m.attempts} answers{m.top_wrong ? ` · most common wrong answer: “${m.top_wrong}”` : ""}{m.answer && !Array.isArray(m.answer) ? (m.kind === "spot" ? ` · the rule-breaking line: “${m.answer}”` : ` · course answer: “${m.answer}”`) : ""}</p>
                  {m.citation && <Citations items={[m.citation]} />}
                </li>
              ))}
            </ul>
          ))}
        </Card>
      )}
      {sub === "signoffs" && (
        <Card title="Role-plays waiting for a sign-off" subtitle="Read the transcript and give your own score first; the rules score is shown once you sign off. Agreeing within 10 points counts towards your own coach certificate.">
          {sign.loading && <Loading rows={4} />}
          <ErrorBox error={sign.error} retry={sign.reload} />
          {sign.data && (sign.data.items.length === 0 ? <Empty title="Nothing to sign off" hint="Finished role-plays from your learners appear here." /> : (
            <ul className="space-y-3">{sign.data.items.map((r: any) => <SignoffRow key={r.id} r={r} onDone={() => { sign.reload(); reload(); }} />)}</ul>
          ))}
        </Card>
      )}
      {sub === "impact" && (
        <Card title="Training next to results" subtitle="Correlations only, with how many people they cover. They never show that training caused a result.">
          {imp.loading && <Loading rows={3} />}
          <ErrorBox error={imp.error} retry={imp.reload} />
          {imp.data && (
            <ul className="space-y-2 text-sm">
              {imp.data.metrics.map((m: any) => (
                <li key={m.key} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2">
                  <span>{m.label}</span>
                  <span className="text-xs text-muted">{m.r === null ? m.note : `r = ${m.r} (n = ${m.n}). ${m.note}`}{m.r === null ? ` (n = ${m.n})` : ""}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
      <LearnerDrawer userId={open} onClose={() => setOpen(null)} />
    </div>
  );
}

// ---------------------------------------------------------------- admin
function DraftLesson({ L, onDone, canEdit }: { L: any; onDone: () => void; canEdit: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [showItems, setShowItems] = useState(false);
  const decide = async (decision: string) => {
    setBusy(decision);
    setErr(null);
    try {
      await post(`/academy/admin/lessons/${L.id}/decide`, { decision });
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <li className="rounded-lg border border-line p-3" data-testid="draft-lesson">
      <div className="flex flex-wrap items-center gap-2">
        <b className="break-words">{L.title}</b>
        <Badge>{L.skill_label}</Badge>
        <Badge tone="neutral">{L.gen_mode === "rules" ? "Built by rules" : "AI explainer, checked"}</Badge>
        {L.gen_version > 1 && <Badge tone="warn">Updated version {L.gen_version}</Badge>}
      </div>
      <div className="mt-2 grid gap-3 md:grid-cols-2">
        <div>
          <p className="mb-1 text-xs font-semibold text-muted">Key points</p>
          <ul className="space-y-1 text-sm">
            {L.key_points.map((k: any, i: number) => <li key={i} className="break-words">• {k.text} <span className="text-xs text-muted">[{k.cite}]</span></li>)}
            {L.mistakes.map((k: any, i: number) => <li key={`m${i}`} className="break-words text-amber-900">⚠ {k.text} <span className="text-xs text-muted">[{k.cite}]</span></li>)}
          </ul>
          <Citations items={L.citations} />
          <p className="mt-2 flex items-center gap-1 text-xs text-good"><CheckCircle2 className="size-3.5" /> Every point cited to an exact line; every line passed the guardrails.</p>
          {L.checks?.ai && <p className="text-xs text-muted">AI: {L.checks.ai}</p>}
        </div>
        <div>
          <p className="mb-1 text-xs font-semibold text-muted">Source passage ({L.source.file}, lines {L.source.lines})</p>
          <div className="scroll-thin max-h-48 overflow-auto rounded bg-panel-2 p-2 font-mono text-[11px] leading-relaxed">
            {L.source.passage.map((l: any) => <div key={l.n} className="flex gap-2"><span className="w-7 shrink-0 text-right text-muted">{l.n}</span><span className="min-w-0 break-words">{l.text}</span></div>)}
          </div>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" tone="ghost" onClick={() => setShowItems(!showItems)}>{showItems ? "Hide" : "Show"} {L.items.length} questions</Button>
        {canEdit && L.status === "draft" && (
          <>
            <Button size="sm" tone="good" onClick={() => decide("publish")} loading={busy === "publish"}>Publish</Button>
            <Button size="sm" tone="danger" onClick={() => decide("reject")} loading={busy === "reject"}>Reject</Button>
          </>
        )}
        {canEdit && (L.status === "published" || L.status === "stale") && <Button size="sm" tone="danger" onClick={() => decide("retire")} loading={busy === "retire"}>Retire</Button>}
      </div>
      {showItems && (
        <ul className="mt-2 space-y-1 text-xs">
          {L.items.map((it: any) => (
            <li key={it.id} className="rounded bg-panel-2 px-2 py-1">
              <Badge tone="brand">{it.kind_label}</Badge> <Badge tone={it.pool === "exam" ? "warn" : "neutral"}>{it.pool === "exam" ? "Exam pool" : "Practice"}</Badge>{" "}
              <span className="break-words">{it.prompt}</span>
              {it.answer !== null && it.answer !== undefined && <span className="text-muted"> → {Array.isArray(it.answer) ? it.answer.join(" → ") : String(it.answer)}</span>}
            </li>
          ))}
        </ul>
      )}
      <ErrorBox error={err} />
    </li>
  );
}

const THRESH_LABEL: Record<string, string> = {
  mastery_overall: "Overall chance of a right answer (0-1)", mastery_skill: "Per-skill chance (0-1)", roleplays: "Role-plays needed",
  roleplay_score: "Role-play score needed", write_items: "Write-it answers needed", write_score: "Write-it score needed",
  exam_items: "Exam questions", exam_pass: "Exam pass mark (0-1)", exam_minutes: "Exam minutes", cooldown_hours: "Hours before a retake",
  setter_dms: "Setter: approved DMs", setter_hold_rate: "Setter: max share held (0-1)", closer_calls: "Closer: reviewed calls",
  closer_call_score: "Closer: average call score", operator_clients: "Growth operator: clients signed", coach_graded: "Coach: role-plays graded",
  grade_tolerance: "Grading tolerance (points)", calibration_items: "Lead: graded role-plays", calibration_agreement: "Lead: agreement needed (0-1)",
};

function Thresholds({ paths, onSaved }: { paths: any[]; onSaved: () => void }) {
  const { toast } = useApp();
  const [key, setKey] = useState(paths[0]?.key || "");
  const p = paths.find((x) => x.key === key);
  const [vals, setVals] = useState<Record<string, Record<string, string>>>({});
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async () => {
    setErr(null);
    try {
      const levels: Record<string, Record<string, number>> = {};
      for (const [lv, kv] of Object.entries(vals)) for (const [k, v] of Object.entries(kv)) (levels[lv] ||= {})[k] = Number(v);
      await put(`/academy/admin/paths/${key}`, { levels });
      setVals({});
      toast("Saved. Certificates already awarded keep the rules they were awarded under.", "good");
      onSaved();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  if (!p) return null;
  return (
    <Card title="Certification thresholds" subtitle="Per path. Already-awarded certificates keep a snapshot of the rules they were awarded under.">
      <Select value={key} onChange={(e) => { setKey(e.target.value); setVals({}); }} options={paths.map((x) => ({ value: x.key, label: x.title }))} />
      <div className="mt-3 space-y-3">
        {Object.entries(p.thresholds).map(([lv, kv]: any) => (
          <div key={lv}>
            <p className="mb-1 text-xs font-semibold text-muted">{lv}</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {Object.entries(kv).map(([k, v]: any) => (
                <Field key={k} label={THRESH_LABEL[k] || k}>
                  <Input type="number" step="any" value={vals[lv]?.[k] ?? String(v)} onChange={(e) => setVals((s) => ({ ...s, [lv]: { ...(s[lv] || {}), [k]: e.target.value } }))} />
                </Field>
              ))}
            </div>
          </div>
        ))}
      </div>
      <ErrorBox error={err} />
      <Button className="mt-3" tone="primary" onClick={save} disabled={!Object.keys(vals).length}>Save thresholds</Button>
    </Card>
  );
}

export function AdminTab() {
  const { me, toast } = useApp();
  const owner = me.user.role === "owner";
  const [sub, setSub] = useState("build");
  const builds = useApi<any>("/academy/admin/builds");
  const running = (builds.data?.builds || []).some((b: any) => b.status === "running");
  useEffect(() => {
    // a build runs in the background: refresh the list until it finishes
    if (!running) return;
    const t = setInterval(() => builds.reload(), 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);
  const [status, setStatus] = useState("draft");
  const drafts = useApi<any>(sub === "drafts" || sub === "skipped" ? `/academy/admin/drafts?status=${status}` : null, [sub, status]);
  const cov = useApi<any>(sub === "coverage" ? "/academy/admin/coverage" : null, [sub]);
  const stats = useApi<any>(sub === "stats" ? "/academy/admin/item-stats" : null, [sub]);
  const sets = useApi<any>(sub === "settings" ? "/academy/admin/settings" : null, [sub]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [run, setRun] = useState<number | null>(null);
  const call = async (what: string, fn: () => Promise<any>) => {
    setBusy(what);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const startBuild = () => call("build", async () => {
    const r = await post("/academy/build");
    setRun(r.run_id);
    toast(r.status === "done" ? `Build finished: ${r.result?.created ?? 0} new drafts to review.` : "Build started. New lessons arrive as drafts for you to review.", "info");
    setTimeout(() => builds.reload(), 1500);
  });
  const checkSources = () => call("check", async () => {
    const r = await post("/academy/admin/check-sources");
    toast(r.up_to_date ? "Every published lesson matches its source." : `${r.changed.length} lessons changed and ${r.removed.length} lost their source. Build again to draft updates.`, r.up_to_date ? "good" : "info");
  });
  const publishAll = (include_exam: boolean) => call(include_exam ? "all_exam" : "all", async () => {
    const r = await post("/academy/admin/publish-all", { include_exam });
    toast(`${r.lessons_published} lessons published${include_exam ? `, ${r.exam_items_published} exam questions approved` : ""}.`, "good");
    drafts.reload();
  });
  const itemDecide = (id: number, decision: string) => call(`i${id}`, async () => {
    await post(`/academy/admin/items/${id}/decide`, { decision });
    stats.reload();
  });
  const material = builds.data?.material;
  return (
    <div className="space-y-4" data-testid="academy-admin">
      <Tabs value={sub} onChange={setSub} tabs={[{ key: "build", label: "Build" }, { key: "drafts", label: "Review drafts" }, { key: "skipped", label: "Skipped lines" }, { key: "coverage", label: "Coverage" }, { key: "stats", label: "Question stats" }, { key: "settings", label: "Settings" }]} />
      <ErrorBox error={err} />
      {sub === "build" && (
        <Card title="Build the Academy" subtitle="Turns your material into cited lessons and questions. AI is optional; with it off every lesson is built by rules.">
          {builds.loading && !builds.data && <Loading rows={3} />}
          <ErrorBox error={builds.error} retry={builds.reload} />
          {material && !material.ok && (
            <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" /> <span>{material.reason}</span>
            </div>
          )}
          {material?.demo_material && <p className="mb-3 text-xs text-muted">This is the pitch demo: lessons come from fictional demo material and are badged “Demo material”.</p>}
          {owner && (
            <div className="flex flex-wrap gap-2">
              <Button tone="primary" onClick={startBuild} loading={busy === "build"} disabled={material && !material.ok}><Hammer className="size-4" /> Build now</Button>
              <Button onClick={checkSources} loading={busy === "check"} disabled={material && !material.ok}>Check for changed sources</Button>
            </div>
          )}
          {run && <p className="mt-2 text-xs"><TraceLink runId={run} /></p>}
          {builds.data && (
            <ul className="mt-4 space-y-2 text-sm">
              {builds.data.builds.length === 0 && <li className="text-muted">No builds yet.</li>}
              {builds.data.builds.map((b: any) => (
                <li key={b.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2">
                  <b>Build {b.id}</b>
                  <Badge tone={b.status === "done" ? "good" : b.status === "error" ? "bad" : "info"}>{b.status === "done" ? "Finished" : b.status === "error" ? "Failed" : "Running"}</Badge>
                  <span className="text-xs text-muted">{new Date(b.created * 1000).toLocaleString()}</span>
                  {b.stats?.lessons_built !== undefined && <span className="text-xs text-muted">{b.stats.lessons_built} lessons, {b.stats.items_built} questions, {b.stats.skipped} unsafe lines skipped, {b.stats.created} new drafts</span>}
                  <TraceLink runId={b.run_id} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
      {sub === "drafts" && (
        <Card title="Review lessons" subtitle="Each lesson sits next to the passage it came from. Publish what's right; reject what isn't."
          actions={<Select aria-label="Show" value={status} onChange={(e) => setStatus(e.target.value)} options={[{ value: "draft", label: "Drafts" }, { value: "published", label: "Published" }, { value: "stale", label: "Source changed" }, { value: "rejected", label: "Rejected" }]} />}>
          {drafts.loading && <Loading rows={5} />}
          <ErrorBox error={drafts.error} retry={drafts.reload} />
          {drafts.data && (
            <>
              {owner && status === "draft" && (
                <div className="mb-3 flex flex-wrap gap-2">
                  <Button tone="good" onClick={() => publishAll(false)} loading={busy === "all"} disabled={!drafts.data.lessons.length}>Publish every draft that passed its checks</Button>
                  <Button onClick={() => publishAll(true)} loading={busy === "all_exam"} disabled={!drafts.data.lessons.length && !drafts.data.exam_items_awaiting_approval}>Also approve exam questions ({drafts.data.exam_items_awaiting_approval} waiting)</Button>
                </div>
              )}
              {drafts.data.stale.length > 0 && status === "draft" && <p className="mb-2 text-xs text-amber-800">{drafts.data.stale.length} published lessons have changed sources; their updated drafts are below.</p>}
              {drafts.data.lessons.length === 0 ? <Empty title={status === "draft" ? "No drafts waiting" : "Nothing here"} hint={status === "draft" ? "Run a build to create drafts from your material." : undefined} /> : (
                <ul className="space-y-3">{drafts.data.lessons.map((L: any) => <DraftLesson key={L.id} L={L} canEdit={owner} onDone={drafts.reload} />)}</ul>
              )}
            </>
          )}
        </Card>
      )}
      {sub === "skipped" && (
        <Card title="Lines the Academy did not use" subtitle="Fix the source if a line should be taught; these never become a lesson point, a quiz answer or a model reply.">
          {drafts.loading && <Loading rows={4} />}
          <ErrorBox error={drafts.error} retry={drafts.reload} />
          {drafts.data && (
            <div className="space-y-4">
              <div>
                <p className="mb-1 text-xs font-semibold text-muted">Unsafe sentences (guardrails)</p>
                {drafts.data.unsafe.length === 0 ? <p className="text-sm text-muted">None in the last build.</p> : (
                  <ul className="space-y-2">{drafts.data.unsafe.map((s: any, i: number) => (
                    <li key={i} className="rounded-lg border border-red-200 bg-red-50/50 p-2 text-sm"><b>{s.label}</b> <span className="break-words text-muted">“{s.text}”</span><Citations items={[s.citation]} /></li>
                  ))}</ul>
                )}
              </div>
              <div>
                <p className="mb-1 text-xs font-semibold text-muted">Not used by the content policy</p>
                {drafts.data.not_used.length === 0 ? <p className="text-sm text-muted">None.</p> : (
                  <ul className="space-y-1 text-sm">{drafts.data.not_used.map((s: any, i: number) => <li key={i} className="break-words">{s.label} <span className="text-xs text-muted">({s.file}, line {s.lines})</span></li>)}</ul>
                )}
              </div>
              <div>
                <p className="mb-1 text-xs font-semibold text-muted">Too noisy or too thin to teach from</p>
                {[...drafts.data.too_noisy, ...drafts.data.too_thin].length === 0 ? <p className="text-sm text-muted">None.</p> : (
                  <ul className="space-y-1 text-sm">{[...drafts.data.too_noisy, ...drafts.data.too_thin].map((s: any, i: number) => <li key={i} className="break-words">{s.title} <span className="text-xs text-muted">— {s.label}</span></li>)}</ul>
                )}
              </div>
            </div>
          )}
        </Card>
      )}
      {sub === "coverage" && (
        <div className="space-y-4">
          <Card title="Material → lessons" pad={false}>
            <div className="p-3">
              {cov.loading && <Loading rows={5} />}
              <ErrorBox error={cov.error} retry={cov.reload} />
              {cov.data && <Table rows={cov.data.documents} empty="No material indexed" cols={[
                { key: "title", label: "Material", render: (d) => <span className="break-words">{d.title}</span> },
                { key: "lessons", label: "Lessons", render: (d) => `${d.published} published of ${d.lessons}` },
                { key: "questions", label: "Questions" },
                { key: "covered", label: "", render: (d) => d.quality === "garbled" ? <Badge tone="warn">Too noisy</Badge> : d.covered ? <Badge tone="good">Covered</Badge> : <Badge>No lesson</Badge> },
              ]} />}
            </div>
          </Card>
          <Card title="Gaps in the material" subtitle="What students asked the coach that the material didn't cover, grouped by topic.">
            {cov.data && (cov.data.gaps.length === 0 ? <Empty title="No gaps recorded" hint="Uncovered student questions show up here." /> : (
              <ul className="space-y-2 text-sm">{cov.data.gaps.map((g: any) => <li key={g.topic}><b>{g.count}×</b> {g.examples.map((e: string) => `“${e}”`).join(" · ")}</li>)}</ul>
            ))}
          </Card>
        </div>
      )}
      {sub === "stats" && (
        <Card title="Question statistics" subtitle={`Share right (p) and how well a question separates stronger from weaker learners (r). Flags appear after ${stats.data?.min_attempts ?? 30} answers or 3 reports.`}>
          {stats.loading && <Loading rows={5} />}
          <ErrorBox error={stats.error} retry={stats.reload} />
          {stats.data && <Table rows={stats.data.items} empty="No questions yet" cols={[
            { key: "prompt", label: "Question", render: (r) => <span className="line-clamp-2 break-words">{r.prompt}</span> },
            { key: "kind", label: "Type", render: (r) => r.kind_label },
            { key: "attempts", label: "Answers" },
            { key: "p", label: "p", render: (r) => r.p_value ?? "–" },
            { key: "r", label: "r", render: (r) => r.discrimination ?? "–" },
            { key: "flags", label: "Flags", render: (r) => r.flags.length ? <span className="flex flex-wrap gap-1">{r.flags.map((f: string) => <Badge key={f} tone="warn">{f}</Badge>)}</span> : <span className="text-xs text-muted">–</span> },
            { key: "act", label: "", render: (r) => owner ? (
              <span className="flex flex-wrap gap-1">
                {r.status === "published" && <Button size="sm" tone="danger" onClick={() => itemDecide(r.id, "retire")} loading={busy === `i${r.id}`}>Retire</Button>}
                {r.status === "draft" && <Button size="sm" tone="good" onClick={() => itemDecide(r.id, "publish")} loading={busy === `i${r.id}`}>Approve</Button>}
              </span>) : null },
          ]} />}
        </Card>
      )}
      {sub === "settings" && (
        <div className="space-y-4">
          <Card title="Own outreach needs the compliance certificate" subtitle="When on, coaches, setters, closers and students must hold Compliance core before they can approve their own prospect messages, emails or proposals.">
            {sets.loading && <Loading rows={2} />}
            <ErrorBox error={sets.error} retry={sets.reload} />
            {sets.data && (
              <div className="flex flex-wrap items-center gap-3">
                <Badge tone={sets.data.require_cert_for_own_outreach ? "good" : "neutral"}><ShieldCheck className="size-3" /> {sets.data.require_cert_for_own_outreach ? "On" : "Off"}</Badge>
                {owner && <Button onClick={() => call("setting", async () => { await put("/academy/admin/settings", { require_cert_for_own_outreach: !sets.data.require_cert_for_own_outreach }); sets.reload(); })} loading={busy === "setting"}>
                  {sets.data.require_cert_for_own_outreach ? "Turn off" : "Turn on"}
                </Button>}
              </div>
            )}
          </Card>
          <Card title="Setters need their certificate to approve the team's messages" subtitle="When on, a setter must hold Setter L1 before approving DMs, follow-ups and reminders drafted for other people. Their own items follow the switch above.">
            {sets.data && (
              <div className="flex flex-wrap items-center gap-3">
                <Badge tone={sets.data.require_setter_cert_for_decisions ? "good" : "neutral"}><ShieldCheck className="size-3" /> {sets.data.require_setter_cert_for_decisions ? "On" : "Off"}</Badge>
                {owner && <Button onClick={() => call("setter_setting", async () => { await put("/academy/admin/settings", { require_setter_cert_for_decisions: !sets.data.require_setter_cert_for_decisions }); sets.reload(); })} loading={busy === "setter_setting"}>
                  {sets.data.require_setter_cert_for_decisions ? "Turn off" : "Turn on"}
                </Button>}
              </div>
            )}
          </Card>
          {sets.data && owner && <Thresholds paths={sets.data.paths} onSaved={sets.reload} />}
        </div>
      )}
    </div>
  );
}
