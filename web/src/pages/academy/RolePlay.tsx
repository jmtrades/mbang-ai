// Role-play simulator panel, rendered inside the Academy page's "Role-play" tab (no <h1> here: the page owns it).
// Practise against a fictional persona driven by rules; finish with a cited, turn-by-turn scorecard.
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Check, Lightbulb, Mic, MicOff, RotateCw, Send, Target, Volume2, VolumeX, X } from "lucide-react";
import { ApiError, get, post, put } from "../../api";
import {
  Badge, Button, Card, Citations, DemoBadge, Empty, ErrorBox, Field, FlagList, Input, Loading, Modal, Select, Table, Tabs,
  TextArea, TraceLink, cx, useApi, useApp,
} from "../../components/ui";

type Persona = { name: string; niche: string; business: string; channel: string; temperament: string; public: string[]; note: string };
type Scenario = {
  key: string; title: string; kind: string; skill: string; skill_label: string; difficulty: number; difficulty_label: string;
  summary: string; learner_role: string; objective: string; persona: Persona; earlier: string[]; win_label: string;
  max_words: number; patience: number; recommended: boolean; source: string; version: number;
};
type Turn = {
  idx: number; speaker: "learner" | "persona"; text: string; source?: string; note?: string; flags?: any[]; acts?: string[];
  color?: "green" | "amber" | "red"; credit?: string[]; notes?: string[];
};
type Session = {
  id: number; user: string; status: "active" | "complete" | "abandoned"; scenario: Scenario; mode: string; mode_label: string;
  exam: boolean; hints: boolean; seed: number; created: number; demo: boolean; turns: Turn[]; score: any; run_id: number;
  can_play: boolean; is_learner: boolean; can_signoff: boolean; meters?: { trust: string; trust_value: number; patience: number; patience_max: number };
  hint?: string | null; reveals?: { key: string; said: string; revealed: boolean }[];
};

const TEMPER: Record<string, string> = { friendly: "Friendly", analytical: "Analytical", guarded: "Guarded", busy: "Busy" };
const OUTCOME: Record<string, string> = { won: "Reached the right outcome", lost: "Didn't reach the outcome", ended: "You ended the session" };
const REASON: Record<string, string> = {
  compliance: "Ended on a compliance problem", patience: "They ran out of patience", trust: "They lost trust",
  ignored: "They didn't reply", unconvinced: "They weren't convinced", ended: "You ended it", won: "",
};
const STATUS: Record<string, string> = { active: "In progress", complete: "Finished", abandoned: "Abandoned" };
const fmtDate = (t: number) => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// Browser dictation (the browser's own speech service) and a neutral system voice for the fictional persona.
const SR: any = typeof window !== "undefined" ? (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition : null;
function useDictation(onText: (t: string) => void) {
  const [on, setOn] = useState(false);
  const rec = useRef<any>(null);
  const toggle = () => {
    if (!SR) return;
    if (on) {
      rec.current?.stop();
      return;
    }
    const r = new SR();
    r.lang = navigator.language || "en-GB";
    r.interimResults = false;
    r.onresult = (e: any) => onText(Array.from(e.results).map((x: any) => x[0].transcript).join(" "));
    r.onend = () => setOn(false);
    r.onerror = () => setOn(false);
    rec.current = r;
    setOn(true);
    r.start();
  };
  return { on, toggle, supported: !!SR };
}
function speak(text: string) {
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text.replace(/^\(|\)$/g, ""));
  u.rate = 1.0;
  window.speechSynthesis.speak(u);
}

export function RolePlayPanel() {
  const { me } = useApp();
  const role = me.user.role;
  const [tab, setTab] = useState("practice");
  const [openId, setOpenId] = useState<number | null>(null);
  const tabs = [
    { key: "practice", label: "Practise" },
    { key: "history", label: "My sessions" },
    ...(role === "coach" || role === "owner" ? [{ key: "team", label: role === "owner" ? "All sessions" : "My students" }] : []),
    ...(role === "owner" ? [{ key: "scenarios", label: "Scenarios" }, { key: "rubric", label: "Scoring rules" }] : []),
  ];
  const open = (id: number) => {
    setOpenId(id);
    setTab("practice");
  };
  return (
    <div className="min-w-0" data-testid="roleplay-panel">
      <Tabs tabs={tabs} value={tab} onChange={(k) => { setTab(k); if (k !== "practice") setOpenId(null); }} />
      {tab === "practice" && (openId ? <SessionView id={openId} onExit={() => setOpenId(null)} onOpen={open} /> : <Picker onOpen={open} />)}
      {tab === "history" && <SessionList mine onOpen={open} />}
      {tab === "team" && <SessionList onOpen={open} team />}
      {tab === "scenarios" && <ScenarioEditor />}
      {tab === "rubric" && <RubricEditor />}
    </div>
  );
}

// ---------------------------------------------------------------- picker
function Picker({ onOpen }: { onOpen: (id: number) => void }) {
  const { me, toast } = useApp();
  const { data, error, loading, reload } = useApi<{ items: Scenario[]; skills: Record<string, string> }>("/academy/scenarios");
  const [skill, setSkill] = useState("all");
  const [exam, setExam] = useState(false);
  const [hints, setHints] = useState(true);
  const [aiMode, setAiMode] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const items = (data?.items || []).filter((s) => skill === "all" || s.skill === skill);
  const start = async (key: string) => {
    setBusy(key);
    setErr(null);
    try {
      const v = await post<Session>("/academy/roleplays", { scenario_key: key, exam, hints: hints && !exam, mode: aiMode && !exam ? "ai" : "rules" });
      onOpen(v.id);
    } catch (e) {
      setErr(e as ApiError);
      toast("Couldn't start the role-play", "bad");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4">
      <Card title="Practise a conversation" subtitle="A fictional prospect, client or student reacts to what you actually do. Scores follow the SOP, turn by turn, and can't be gamed with keywords.">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Skill">
            <Select value={skill} onChange={(e) => setSkill(e.target.value)} options={[{ value: "all", label: "All skills" }, ...Object.entries(data?.skills || {}).map(([value, label]) => ({ value, label }))]} />
          </Field>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={hints && !exam} disabled={exam} onChange={(e) => setHints(e.target.checked)} /> Live hints</label>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={exam} onChange={(e) => setExam(e.target.checked)} /> Exam mode (no hints, rules only)</label>
          <label className="flex items-center gap-2 text-sm" title={me.ai.enabled ? "" : me.ai.label}>
            <input type="checkbox" checked={aiMode && !exam} disabled={exam} onChange={(e) => setAiMode(e.target.checked)} /> AI phrasing
          </label>
        </div>
        <p className="mt-2 text-xs text-muted">
          {aiMode && !exam
            ? me.ai.enabled ? "The rules decide what happens; the AI only words the other person's lines. Scores are always rules-based." : `${me.ai.label}: lines will come from the rules-mode template bank (or a saved AI replay).`
            : "Rules mode: the other person's lines come from a fixed template bank, so every session can be replayed exactly."}
        </p>
      </Card>
      <ErrorBox error={err || error} retry={err ? undefined : reload} />
      {loading && <Loading rows={4} />}
      {data && !items.length && <Empty title="No scenarios for this skill" hint="Pick another skill or ask the owner to add one." />}
      <div className="grid gap-3 md:grid-cols-2">
        {items.map((s) => (
          <Card key={s.key} title={<span className="flex flex-wrap items-center gap-1.5">{s.title}</span>} subtitle={`${s.skill_label} · ${s.kind === "dm" ? "Direct messages" : "Live conversation"}`}
            actions={<Badge tone={s.difficulty === 1 ? "good" : s.difficulty === 2 ? "info" : "warn"}>{s.difficulty_label}</Badge>}>
            <p className="text-sm">{s.summary}</p>
            <p className="mt-2 text-xs text-muted">With {s.persona.name}, {s.persona.niche} · {TEMPER[s.persona.temperament] || s.persona.temperament}</p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button tone="primary" size="sm" loading={busy === s.key} onClick={() => start(s.key)} data-testid={`start-${s.key}`}>
                <Target className="size-3.5" /> {exam ? "Start exam" : "Start"}
              </Button>
              {s.recommended && <Badge tone="brand">Recommended for you</Badge>}
              {s.source !== "builtin" && <Badge>Edited by the owner</Badge>}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- session (chat + scorecard)
function SessionView({ id, onExit, onOpen }: { id: number; onExit: () => void; onOpen: (id: number) => void }) {
  const { toast } = useApp();
  const { data, error, loading, reload, setData } = useApi<Session>(`/academy/roleplays/${id}`, [id]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [voice, setVoice] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const dict = useDictation((t) => setText((cur) => (cur ? cur + " " : "") + t));
  useEffect(() => {
    if (data?.status === "active" && data.turns.length > 1) end.current?.scrollIntoView({ block: "nearest" });
  }, [data?.turns.length]);

  const send = async () => {
    const t = text.trim();
    if (!t || !data) return;
    setBusy(true);
    setErr(null);
    try {
      const v = await post<Session>(`/academy/roleplays/${id}/turn`, { text: t });
      setData(v);
      setText("");
      const last = v.turns[v.turns.length - 1];
      if (voice && last?.speaker === "persona") speak(last.text);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const act = async (path: string, msg: string) => {
    setBusy(true);
    setErr(null);
    try {
      setData(await post<Session>(`/academy/roleplays/${id}/${path}`));
      toast(msg, "good");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  // "Practise again" repeats the same kind of attempt; a drill is always practice (hints on, never an exam).
  const again = async (key: string, practice = false) => {
    setBusy(true);
    try {
      const exam = !practice && !!data?.exam;
      const v = await post<Session>("/academy/roleplays", { scenario_key: key, exam, hints: exam ? false : practice || !data || data.hints || data.exam, mode: data?.mode === "ai" && !exam ? "ai" : "rules" });
      onOpen(v.id);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  if (loading && !data) return <Loading rows={6} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  const sc = data.scenario;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" tone="ghost" onClick={onExit}><ArrowLeft className="size-4" /> All scenarios</Button>
        <span className="min-w-0 truncate text-sm font-semibold">{sc.title}</span>
        <Badge tone={data.status === "active" ? "info" : data.status === "complete" ? "good" : "neutral"}>{STATUS[data.status]}</Badge>
        {data.exam && <Badge tone="warn">Exam</Badge>}
        {data.demo && <DemoBadge />}
        {!data.is_learner && <Badge>{data.user}</Badge>}
      </div>
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 space-y-3">
          <Card pad={false} title="Conversation" subtitle={data.mode_label}
            actions={"speechSynthesis" in window ? (
              <Button size="sm" tone="ghost" onClick={() => setVoice(!voice)} aria-label={voice ? "Stop reading their lines aloud" : "Read their lines aloud"} title="Read their lines aloud (a neutral system voice)">
                {voice ? <Volume2 className="size-4" /> : <VolumeX className="size-4" />}
              </Button>
            ) : undefined}>
            <ol className="scroll-thin max-h-[60vh] space-y-2 overflow-y-auto p-3" aria-live="polite" data-testid="roleplay-chat">
              {data.turns.map((t) => <Bubble key={t.idx} t={t} persona={sc.persona.name} showAnnot={data.status === "complete"} />)}
              {!data.turns.length && <li className="text-sm text-muted">You go first: {sc.kind === "dm" ? "send your opening message." : "open the conversation."}</li>}
              <div ref={end} />
            </ol>
            {data.can_play && (
              <div className="space-y-2 border-t border-line p-3">
                {data.hint && (
                  <p className="flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900" data-testid="roleplay-hint">
                    <Lightbulb className="mt-0.5 size-3.5 shrink-0" /> <span>{data.hint}</span>
                  </p>
                )}
                <TextArea rows={3} value={text} maxLength={1500} placeholder={sc.kind === "dm" ? "Write your message…" : "Say your line…"} aria-label="Your line"
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
                <div className="flex flex-wrap items-center gap-2">
                  <Button tone="primary" onClick={send} loading={busy} disabled={!text.trim()} data-testid="roleplay-send"><Send className="size-4" /> Send</Button>
                  {dict.supported && (
                    <Button onClick={dict.toggle} aria-label={dict.on ? "Stop dictation" : "Dictate"}>{dict.on ? <MicOff className="size-4" /> : <Mic className="size-4" />} {dict.on ? "Stop" : "Dictate"}</Button>
                  )}
                  <span className="text-xs text-muted">{text.trim() ? `${text.trim().split(/\s+/).length} words` : ""}</span>
                  <span className="flex-1" />
                  <Button size="sm" onClick={() => act("end", "Session scored")} disabled={busy}>End and score</Button>
                  <Button size="sm" tone="ghost" onClick={() => act("abandon", "Session abandoned")} disabled={busy}>Abandon</Button>
                </div>
                <ErrorBox error={err} />
              </div>
            )}
            {data.status === "abandoned" && (
              <div className="flex flex-wrap gap-2 border-t border-line p-3">
                <p className="w-full text-sm text-muted">This session was abandoned, so it has no score.</p>
                <Button tone="primary" size="sm" onClick={() => again(sc.key)} loading={busy}><RotateCw className="size-3.5" /> Practise again</Button>
              </div>
            )}
          </Card>
        </div>
        <PersonaCard s={data} />
      </div>
      {data.status === "complete" && data.score && (
        <Scorecard s={data} busy={busy} onAgain={() => again(sc.key)} onDrill={async (crit: string) => {
          try {
            const r = await post<any>(`/academy/roleplays/${id}/drill`, { criterion: crit });
            if (r.card) toast(`Added "${r.card.objection}" to your objection drills`, "good");
            await again(r.scenario_key, true);
          } catch (e) {
            setErr(e as ApiError);
          }
        }} onSigned={(v: Session) => setData(v)} />
      )}
      {data.status === "complete" && <ErrorBox error={err} />}
    </div>
  );
}

function Bubble({ t, persona, showAnnot }: { t: Turn; persona: string; showAnnot: boolean }) {
  const mine = t.speaker === "learner";
  const ring = showAnnot && mine ? (t.color === "green" ? "border-l-4 border-l-green-600" : t.color === "red" ? "border-l-4 border-l-red-600" : "border-l-4 border-l-amber-500") : "";
  const sceneTest = (t.flags || []).filter((f: any) => f.scenario_test);
  return (
    <li className={cx("flex", mine ? "justify-end" : "justify-start")}>
      <div className={cx("max-w-[88%] min-w-0 rounded-xl px-3 py-2 text-sm break-words", mine ? "bg-[color-mix(in_oklab,var(--brand)_10%,white)]" : "border border-line bg-panel-2", ring)}>
        <div className="mb-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
          <span className="font-semibold">{mine ? "You" : persona}</span>
          {!mine && t.source && t.source !== "Template line" && <Badge tone="info" title={t.note}>{t.source}</Badge>}
          {!mine && sceneTest.length > 0 && <Badge tone="warn" title="The scenario tests how you respond to this">Test of your response</Badge>}
          {showAnnot && mine && <Badge tone={t.color === "green" ? "good" : t.color === "red" ? "bad" : "warn"}>{t.color === "green" ? "Earned credit" : t.color === "red" ? "Problem" : "No credit"}</Badge>}
        </div>
        <p className="whitespace-pre-wrap">{t.text}</p>
        {mine && !!t.acts?.length && (
          <div className="mt-1 flex flex-wrap gap-1">{t.acts.map((a) => <span key={a} className="rounded bg-white/70 px-1.5 py-0.5 text-[10px] text-muted">{a}</span>)}</div>
        )}
        {mine && !!t.flags?.length && <div className="mt-1"><FlagList flags={t.flags} /></div>}
        {showAnnot && mine && (!!t.credit?.length || !!t.notes?.length) && (
          <ul className="mt-1 space-y-0.5 text-[11px]">
            {(t.credit || []).map((c) => <li key={c} className="flex items-start gap-1 text-green-800"><Check className="mt-0.5 size-3 shrink-0" />{c}</li>)}
            {(t.notes || []).map((n) => <li key={n} className="flex items-start gap-1 text-amber-900"><X className="mt-0.5 size-3 shrink-0" />{n}</li>)}
          </ul>
        )}
      </div>
    </li>
  );
}

function PersonaCard({ s }: { s: Session }) {
  const sc = s.scenario;
  const p = sc.persona;
  const m = s.meters;
  return (
    <aside className="min-w-0 space-y-3">
      <Card title={p.name} subtitle={`${p.business} · ${p.channel}`}>
        <div className="space-y-2 text-sm">
          <div className="flex flex-wrap gap-1.5">
            <Badge>{p.niche}</Badge>
            <Badge tone="info">{TEMPER[p.temperament] || p.temperament}</Badge>
          </div>
          {!!p.public.length && <ul className="list-disc space-y-0.5 pl-4 text-xs">{p.public.map((x) => <li key={x}>{x}</li>)}</ul>}
          <p className="text-[11px] text-muted">{p.note}</p>
        </div>
      </Card>
      <Card title="Your brief">
        <div className="space-y-2 text-xs">
          <p>{sc.learner_role}</p>
          <p><b>Aim:</b> {sc.objective}</p>
          <p><b>A win looks like:</b> {sc.win_label}.</p>
          {!!sc.earlier.length && (
            <div><b>What they told you earlier:</b><ul className="mt-1 list-disc space-y-0.5 pl-4">{sc.earlier.map((x) => <li key={x}>“{x}”</li>)}</ul></div>
          )}
          {sc.kind === "dm" && <p className="text-muted">Keep each message under {sc.max_words} words, with one question.</p>}
        </div>
      </Card>
      {m && s.status === "active" && (
        <Card title="How it's going">
          <div className="space-y-2 text-xs">
            <div>
              <div className="flex justify-between"><span>Trust</span><span className="font-semibold">{m.trust}</span></div>
              <div className="mt-1 h-1.5 rounded bg-black/5"><div className={cx("h-1.5 rounded", m.trust_value < 35 ? "bg-amber-500" : m.trust_value < 65 ? "bg-sky-500" : "bg-green-600")} style={{ width: `${m.trust_value}%` }} /></div>
            </div>
            <div className="flex justify-between"><span>Patience left</span><span className="font-semibold tabular-nums">{m.patience} of {m.patience_max} turns</span></div>
          </div>
        </Card>
      )}
    </aside>
  );
}

function Scorecard({ s, busy, onAgain, onDrill, onSigned }: { s: Session; busy: boolean; onAgain: () => void; onDrill: (crit: string) => void; onSigned: (v: Session) => void }) {
  const sc = s.score;
  const { toast } = useApp();
  const [note, setNote] = useState("");
  const [signing, setSigning] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const focus: any[] = (sc.focus || []).map((k: string) => sc.criteria.find((c: any) => c.key === k)).filter(Boolean);
  // Playbook citations aren't line-numbered files: show "step 2" in the title instead of an L-range.
  const where = (l: string) => l.replace(/^steps:/, "step ").replace(/^do:/, "do ").replace(/^dont:/, "don't ").replace(/^guidance$/, "guidance");
  const cites = (c: any) => {
    if (!c.cite) return [];
    if (c.cite.kind === "external") return [{ ...c.cite, lines: undefined, title: `${c.cite.title} (${where(c.cite.lines || "guidance")})` }];
    if (c.cite.problem) return [{ ...c.cite, kind: "external", lines: undefined, title: `${c.cite.file} (not in your knowledge base yet)` }];
    return [c.cite];
  };
  const signoff = async () => {
    setSigning(true);
    setErr(null);
    try {
      onSigned(await post<Session>(`/academy/roleplays/${s.id}/signoff`, { note }));
      toast("Signed off", "good");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setSigning(false);
    }
  };
  return (
    <div className="space-y-4" data-testid="roleplay-scorecard">
      <Card title="Scorecard" subtitle="Scored by fixed rules, turn by turn: the same lines always get the same score."
        actions={<><TraceLink runId={s.run_id} />{s.is_learner && <Button tone="primary" size="sm" onClick={onAgain} loading={busy}><RotateCw className="size-3.5" /> {s.exam ? "Retake the exam" : "Practise again"}</Button>}</>}>
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <div className={cx("text-4xl font-semibold tabular-nums", sc.passed ? "text-good" : sc.total >= 50 ? "text-warn" : "text-bad")}>{sc.total}<span className="text-base text-muted">/100</span></div>
            <div className="text-xs text-muted">Pass: {sc.pass_mark}+ and the right outcome</div>
          </div>
          <div className="space-y-1 text-sm">
            <div className="flex flex-wrap gap-1.5">
              <Badge tone={sc.passed ? "good" : "warn"}>{sc.passed ? "Passed" : "Not yet"}</Badge>
              <Badge tone={sc.outcome === "won" ? "good" : "neutral"}>{OUTCOME[sc.outcome] || "Finished"}</Badge>
              {sc.exam && <Badge tone="warn">Exam attempt</Badge>}
              {sc.signoff ? <Badge tone="good">Signed off</Badge> : <Badge>Awaiting sign-off</Badge>}
            </div>
            {REASON[sc.end_reason] && <p className="text-xs text-muted">{REASON[sc.end_reason]}.</p>}
          </div>
        </div>
        {sc.gate === "compliance" && (
          <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-900">Compliance gate: something you said is never allowed (a guarantee, an income promise, invented proof, pressure or steering into debt), so this session scores 0.</p>
        )}
        {!!sc.integrity_flags?.length && (
          <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900">Crammed turns ({sc.integrity_flags.length}): stage keywords, or answers to several different objections, packed into one line. Those turns earn no credit and cost points.</p>
        )}
        {!!focus.length && (
          <div className="mt-4">
            <h4 className="mb-1 text-sm font-semibold">Work on these next</h4>
            <ol className="space-y-2">
              {focus.map((c: any) => (
                <li key={c.key} className="rounded-lg border border-line bg-panel-2 p-2 text-sm">
                  <div className="font-medium">{c.label}</div>
                  {c.tip && <p className="text-xs">{c.tip}</p>}
                  {c.model_line && <p className="mt-1 text-xs italic">Try: “{c.model_line}”</p>}
                </li>
              ))}
            </ol>
          </div>
        )}
      </Card>

      <Card title="Every criterion, with the rule it comes from" pad={false}>
        <ul className="divide-y divide-line">
          {sc.criteria.map((c: any) => (
            <li key={c.key} className="space-y-1 p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{c.label}</span>
                <span className="tabular-nums text-xs font-semibold">{c.points} / {c.max}</span>
              </div>
              <div className="h-1.5 rounded bg-black/5"><div className={cx("h-1.5 rounded", c.points >= c.max ? "bg-green-600" : c.points > 0 ? "bg-amber-500" : "bg-red-500")} style={{ width: `${c.max ? (100 * c.points) / c.max : 0}%` }} /></div>
              <p className="text-xs text-muted break-words">{c.evidence}</p>
              {c.tip && <p className="text-xs">{c.tip}</p>}
              {c.model_line && <p className="text-xs italic">A stronger line: “{c.model_line}”</p>}
              {!!c.model_flags?.length && <FlagList flags={c.model_flags} />}
              {c.cite?.snippet && <p className="text-[11px] text-muted">Rule: “{c.cite.snippet}”</p>}
              <div className="min-w-0 max-w-full overflow-hidden"><Citations items={cites(c)} /></div>
              <div className="flex flex-wrap items-center gap-2">
                {c.rule_status !== "confirmed" && <Badge tone="warn" title="The owner hasn't confirmed this rule against their SOP yet">Default rule</Badge>}
                {c.cite?.problem && <Badge tone="warn" title={c.cite.problem}>Source needs checking</Badge>}
                {s.is_learner && c.points < c.max && c.drill_scenario && (
                  <Button size="sm" onClick={() => onDrill(c.key)} disabled={busy}><Target className="size-3.5" /> Drill this</Button>
                )}
              </div>
            </li>
          ))}
        </ul>
        {!!sc.penalties?.length && (
          <div className="border-t border-line p-3 text-sm">
            <div className="mb-1 font-medium">Deductions</div>
            <ul className="space-y-1 text-xs">{sc.penalties.map((p: any) => <li key={p.key}>−{p.points}: {p.label} ({p.evidence})</li>)}</ul>
          </div>
        )}
      </Card>

      {!!s.reveals?.length && (
        <Card title="What they would have told you" subtitle="Facts you only learn by asking. Use their words in the gap.">
          <ul className="space-y-1 text-sm">
            {s.reveals.map((r) => (
              <li key={r.key} className="flex items-start gap-2">
                {r.revealed ? <Check className="mt-0.5 size-4 shrink-0 text-good" /> : <X className="mt-0.5 size-4 shrink-0 text-muted" />}
                <span className={r.revealed ? "" : "text-muted"}>“{r.said}”</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Coach sign-off" subtitle="Certification uses the rules score plus a person's sign-off.">
        {sc.signoff ? (
          <p className="text-sm">Signed off by <b>{sc.signoff.by}</b> on {fmtDate(sc.signoff.at)}{sc.signoff.note ? `: “${sc.signoff.note}”` : "."}</p>
        ) : s.can_signoff ? (
          <div className="space-y-2">
            <Field label="Note for the learner (optional)"><Input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="What they did well, what to drill next" /></Field>
            <Button tone="good" onClick={signoff} loading={signing}><Check className="size-4" /> Sign off</Button>
            <ErrorBox error={err} />
          </div>
        ) : (
          <p className="text-sm text-muted">{s.is_learner ? "Your coach or the owner can sign this off." : "Not signed off yet."}</p>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- session lists
function SessionList({ mine, team, onOpen }: { mine?: boolean; team?: boolean; onOpen: (id: number) => void }) {
  const { me } = useApp();
  const [pending, setPending] = useState(false);
  const path = mine ? `/academy/roleplays?user_id=${me.user.id}` : `/academy/roleplays${pending ? "?pending_signoff=true" : ""}`;
  const { data, error, loading, reload } = useApi<{ items: any[] }>(path, [path]);
  return (
    <Card title={mine ? "Your role-plays" : me.user.role === "owner" ? "Everyone's role-plays" : "Your students' role-plays"}
      actions={team ? <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={pending} onChange={(e) => setPending(e.target.checked)} /> Awaiting sign-off only</label> : undefined}>
      <ErrorBox error={error} retry={reload} />
      {loading && <Loading rows={4} />}
      {data && (
        <Table
          rows={data.items}
          empty={mine ? "No role-plays yet. Pick a scenario on the Practise tab." : "No role-plays to show yet"}
          onRow={(r) => onOpen(r.id)}
          cols={[
            ...(mine ? [] : [{ key: "user", label: "Learner" }]),
            { key: "title", label: "Scenario", render: (r: any) => <span className="flex flex-wrap items-center gap-1">{r.title}{r.demo && <DemoBadge />}{r.exam && <Badge tone="warn">Exam</Badge>}</span> },
            { key: "status", label: "Status", render: (r: any) => STATUS[r.status] || r.status },
            { key: "total", label: "Score", render: (r: any) => (r.total === null || r.total === undefined ? "—" : <span className={cx("font-semibold tabular-nums", r.passed ? "text-good" : "")}>{r.total}</span>) },
            { key: "signoff", label: "Sign-off", render: (r: any) => (r.signoff ? <Badge tone="good">Signed</Badge> : r.status === "complete" ? <Badge>Waiting</Badge> : "—") },
            { key: "created", label: "When", render: (r: any) => <span className="whitespace-nowrap text-xs text-muted">{fmtDate(r.created)}</span> },
          ]}
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- owner: scenarios
function ScenarioEditor() {
  const { toast } = useApp();
  const list = useApi<{ items: Scenario[] }>("/academy/scenarios");
  const [key, setKey] = useState<string>("");
  const [newKey, setNewKey] = useState("");
  const detail = useApi<any>(key ? `/academy/scenarios/${key}` : null, [key]);
  const [json, setJson] = useState("");
  const [lines, setLines] = useState("");
  const [seed, setSeed] = useState("1");
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | string | null>(null);
  useEffect(() => {
    if (detail.data?.data) setJson(JSON.stringify(detail.data.data, null, 2));
    setResult(null);
  }, [detail.data]);
  const parsed = () => {
    try {
      return JSON.parse(json);
    } catch {
      setErr("That isn't valid JSON yet. Check for a missing comma or quote.");
      return null;
    }
  };
  const save = async (k: string) => {
    const data = parsed();
    if (!data) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await put<any>(`/academy/scenarios/${k}`, { data: { ...data, key: k } });
      toast(`Saved (version ${r.version})`, "good");
      list.reload();
      setKey(k);
      detail.reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    setBusy(true);
    setErr(null);
    try {
      await post(`/academy/scenarios/${key}/reset`);
      toast("Restored the built-in version", "good");
      detail.reload();
      list.reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const testRun = async () => {
    const data = parsed();
    const ls = lines.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!data || !ls.length) {
      if (!ls.length) setErr("Add at least one line to play, one per line.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      setResult(await post<any>(`/academy/scenarios/${key}/test-run`, { lines: ls, seed: Number(seed) || 1, data }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-4">
      <Card title="Scenarios" subtitle="Each scenario is data: a public persona card, hidden facts, an objection queue, meters and a win condition. Saves are checked against the guardrails.">
        <ErrorBox error={list.error} retry={list.reload} />
        {list.loading && <Loading rows={3} />}
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
          <Field label="Edit a scenario">
            <Select value={key} onChange={(e) => setKey(e.target.value)} options={[{ value: "", label: "Choose…" }, ...(list.data?.items || []).map((s) => ({ value: s.key, label: `${s.title} (${s.difficulty_label})` }))]} />
          </Field>
          <div className="flex items-end gap-2">
            <Input placeholder="new_scenario_key" value={newKey} onChange={(e) => setNewKey(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))} aria-label="New scenario key" />
            <Button disabled={!newKey || newKey.length < 3 || !json} onClick={() => save(newKey)} loading={busy} title="Saves the JSON below under this new key">Save as new</Button>
          </div>
        </div>
      </Card>
      {key && (
        <Card title={detail.data?.title || key} subtitle={detail.data ? `Version ${detail.data.version} · ${detail.data.source === "builtin" ? "built-in default" : detail.data.source === "edited" ? "edited by you" : "your scenario"}` : undefined}
          actions={detail.data?.builtin && detail.data?.source !== "builtin" ? <Button size="sm" onClick={reset} disabled={busy}>Restore built-in</Button> : undefined}>
          {detail.loading && <Loading rows={6} />}
          <ErrorBox error={detail.error} retry={detail.reload} />
          {detail.data && (
            <div className="space-y-3">
              <TextArea rows={16} value={json} onChange={(e) => setJson(e.target.value)} className="font-mono text-xs" aria-label="Scenario JSON" spellCheck={false} />
              <Button tone="primary" onClick={() => save(key)} loading={busy}>Save scenario</Button>
              <div className="rounded-lg border border-line p-3">
                <div className="mb-2 text-sm font-semibold">Test run</div>
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_120px]">
                  <TextArea rows={4} value={lines} onChange={(e) => setLines(e.target.value)} placeholder="One learner line per row" aria-label="Lines to play" />
                  <Field label="Variation" hint="Same number, same replies"><Input value={seed} inputMode="numeric" onChange={(e) => setSeed(e.target.value.replace(/\D/g, ""))} /></Field>
                </div>
                <Button className="mt-2" onClick={testRun} loading={busy}>Run the draft</Button>
                {result && (
                  <div className="mt-3 space-y-2 text-sm">
                    <div className="flex flex-wrap items-center gap-2"><b>{result.score.total}/100</b><Badge>{OUTCOME[result.score.outcome] || "Finished"}</Badge><TraceLink runId={result.run_id} /></div>
                    <ol className="space-y-1 text-xs">{result.transcript.map((t: any) => <li key={t.idx} className="break-words"><b>{t.speaker === "learner" ? "You" : "Them"}:</b> {t.text}</li>)}</ol>
                  </div>
                )}
              </div>
            </div>
          )}
        </Card>
      )}
      <ErrorBox error={err} />
    </div>
  );
}

// ---------------------------------------------------------------- owner: rubric rules
function RubricEditor() {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/academy/roleplays/rubric");
  const [edit, setEdit] = useState<any>(null);
  const [form, setForm] = useState<any>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | string | null>(null);
  const openEdit = (r: any) => {
    setEdit(r);
    setErr(null);
    setForm({ weight: String(r.weight), cite_file: r.cite_file || "", cite_lines: r.cite_lines || "", must_contain: (r.must_contain || []).join(", "), params: JSON.stringify(r.params || {}), confirmed: r.status === "confirmed" });
  };
  const save = async () => {
    let params: any;
    try {
      params = JSON.parse(form.params || "{}");
    } catch {
      setErr("Parameters must be valid JSON, for example {\"max_words\": 60}.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await put<any>(`/academy/roleplays/rubric/${edit.rule_key}`, {
        weight: Number(form.weight), cite_file: form.cite_file, cite_lines: form.cite_lines, params, confirmed: !!form.confirmed,
        must_contain: String(form.must_contain).split(",").map((x: string) => x.trim()).filter(Boolean),
      });
      toast(r.check.ok ? "Saved. The citation checks out." : "Saved, but the citation needs a look", r.check.ok ? "good" : "info");
      setEdit(null);
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const recheck = async () => {
    try {
      const r = await post<any>("/academy/roleplays/rubric/validate");
      toast(r.flagged.length ? `${r.flagged.length} rule(s) need checking against your SOP` : "Every citation checks out", r.flagged.length ? "info" : "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const statusBadge = (s: string) => (s === "confirmed" ? <Badge tone="good">Confirmed</Badge> : s === "edited" ? <Badge tone="info">Edited</Badge> : <Badge tone="warn">Default</Badge>);
  return (
    <div className="space-y-4">
      <Card title="Scoring rules" subtitle="Every role-play criterion comes from one of these rules and cites the SOP line behind it. Defaults need confirming against your own SOPs; certification only counts confirmed rules."
        actions={<Button size="sm" onClick={recheck}><RotateCw className="size-3.5" /> Re-check citations</Button>}>
        <ErrorBox error={error} retry={reload} />
        {loading && <Loading rows={5} />}
        {data && (
          <>
            <p className="mb-2 text-xs text-muted">{data.confirmed} of {data.items.length} confirmed · {data.flagged} need checking</p>
            <ul className="divide-y divide-line">
              {data.items.map((r: any) => (
                <li key={r.rule_key} className="space-y-1 py-2.5 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="min-w-0 font-medium">{r.label}</span>
                    <span className="flex flex-wrap items-center gap-1.5">
                      <span className="text-xs tabular-nums text-muted">{r.kind === "gate" ? "Gate" : r.kind === "penalty" ? `−${r.weight}` : `${r.weight} pts`}</span>
                      {statusBadge(r.status)}
                      {r.ok ? <Badge tone="good">Citation checks out</Badge> : <Badge tone="warn">Needs checking</Badge>}
                    </span>
                  </div>
                  {r.quote && <p className="break-words text-[11px] text-muted">“{r.quote.slice(0, 220)}” <span className="break-words">({r.source_title || r.cite_file}{r.cite_lines ? `, ${r.cite_lines}` : ""})</span></p>}
                  {!r.ok && <p className="text-xs text-warn">{r.issues.join("; ")}</p>}
                  {data.can_edit && <Button size="sm" onClick={() => openEdit(r)}>Edit</Button>}
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
      <Modal open={!!edit} onClose={() => setEdit(null)} title={edit ? `Edit: ${edit.label}` : ""}>
        {edit && (
          <div className="space-y-3">
            <Field label="Points (weight)"><Input value={form.weight} inputMode="decimal" onChange={(e) => setForm({ ...form, weight: e.target.value })} /></Field>
            <Field label="Cited file" hint="A path in your knowledge base, or playbook:<domain>/<entry>"><Input value={form.cite_file} onChange={(e) => setForm({ ...form, cite_file: e.target.value })} /></Field>
            <Field label="Cited lines" hint="For example 4, 10-12, steps:2 or guidance"><Input value={form.cite_lines} onChange={(e) => setForm({ ...form, cite_lines: e.target.value })} /></Field>
            <Field label="The cited lines must contain" hint="Comma-separated, for example 60, one question"><Input value={form.must_contain} onChange={(e) => setForm({ ...form, must_contain: e.target.value })} /></Field>
            <Field label="Parameters (JSON)"><TextArea rows={3} className="font-mono text-xs" value={form.params} onChange={(e) => setForm({ ...form, params: e.target.value })} /></Field>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.confirmed} onChange={(e) => setForm({ ...form, confirmed: e.target.checked })} /> I've checked this rule against my SOP</label>
            <ErrorBox error={err} />
            <div className="flex gap-2"><Button tone="primary" onClick={save} loading={busy}>Save rule</Button><Button tone="ghost" onClick={() => setEdit(null)}>Cancel</Button></div>
          </div>
        )}
      </Modal>
      {!edit && <ErrorBox error={err} />}
    </div>
  );
}

export default RolePlayPanel;
