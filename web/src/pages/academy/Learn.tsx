// Academy learner screens: Today, the lesson reader, Paths, Practice (adaptive + objection drills) and Certificates.
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, BookOpen, CheckCircle2, Circle, Clock, GraduationCap, Lock, Repeat, Timer } from "lucide-react";
import { ApiError, get, post } from "../../api";
import { Badge, Button, Card, Citation, Citations, Empty, ErrorBox, Loading, Select, Stat, TextArea, cx, useApi, useApp } from "../../components/ui";
import { PublicItem, QuestionCard } from "./Question";

export const pct = (v?: number | null) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);
const fmtDate = (t?: number | null) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "–");

export function DemoMaterialBadge() {
  return <Badge tone="demo" title="Built from the fictional demo material, not real course content">Demo material</Badge>;
}

export function Bar({ value, tone = "brand" }: { value: number; tone?: "brand" | "good" | "warn" }) {
  const v = Math.max(0, Math.min(1, value || 0));
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-black/5" role="progressbar" aria-valuenow={Math.round(v * 100)} aria-valuemin={0} aria-valuemax={100}>
      <div className={cx("h-full rounded-full", tone === "good" ? "bg-good" : tone === "warn" ? "bg-warn" : "bg-brand")} style={{ width: `${v * 100}%` }} />
    </div>
  );
}

// ---------------------------------------------------------------- lesson reader
export function LessonReader({ id, onBack, onRolePlay }: { id: number; onBack: () => void; onRolePlay: () => void }) {
  const { go, toast, me } = useApp();
  const { data, error, loading, reload } = useApi<any>(`/academy/lessons/${id}`, [id]);
  const [busy, setBusy] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const cites: Record<string, Citation> = useMemo(() => Object.fromEntries((data?.citations || []).map((c: any) => [c.id, c])), [data]);
  const complete = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post(`/academy/lessons/${id}/complete`);
      toast(r.new_review_cards ? `Done. ${r.new_review_cards} questions will come back for review from tomorrow.` : "Marked as done.", "good");
      if (r.awarded?.length) toast("New certificate earned.", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const Point = ({ k }: { k: any }) => (
    <li className="rounded-lg border border-line bg-panel-2 px-3 py-2">
      <p className="break-words text-sm leading-relaxed">{k.text}</p>
      {cites[k.cite] && <Citations items={[cites[k.cite]]} />}
    </li>
  );
  return (
    <div className="space-y-4" data-testid="lesson-reader">
      <Button tone="ghost" size="sm" onClick={onBack}><ArrowLeft className="size-4" /> Back</Button>
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {data && (
        <>
          <Card
            title={<span className="break-words">{data.title}</span>}
            subtitle={data.gen_label}
            actions={
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge>{data.skill_label}</Badge>
                <Badge tone="neutral"><Clock className="size-3" /> {data.est_minutes} min</Badge>
                {data.demo_material && <DemoMaterialBadge />}
                {data.status === "draft" && <Badge tone="warn">Draft: learners can't see it yet</Badge>}
                {data.stale && <Badge tone="warn">The source changed; an update is waiting for approval</Badge>}
                {data.done && <Badge tone="good">Done</Badge>}
              </div>
            }
          >
            {hidden ? (
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted">
                <span>The lesson is hidden while you answer from memory.</span>
                <Button size="sm" onClick={() => setHidden(false)}>Show the lesson again</Button>
              </div>
            ) : (
            <>
            {data.framing?.opener && <p className="mb-3 text-sm italic text-ink-2" data-testid="lesson-framing">{data.framing.opener}</p>}
            {data.body && <p className="mb-4 whitespace-pre-wrap break-words text-sm leading-relaxed">{data.body}</p>}
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Key points</h4>
            <ul className="space-y-2">{data.key_points.map((k: any, i: number) => <Point key={i} k={k} />)}</ul>
            {data.example.length > 0 && (
              <>
                <h4 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Example</h4>
                <ul className="space-y-2">{data.example.map((k: any, i: number) => <Point key={i} k={k} />)}</ul>
              </>
            )}
            {data.mistakes.length > 0 && (
              <>
                <h4 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Common mistakes</h4>
                <ul className="space-y-2">{data.mistakes.map((k: any, i: number) => <Point key={i} k={k} />)}</ul>
              </>
            )}
            {data.framing && (
              <div className="mt-4 space-y-1">
                {data.framing.closer && <p className="text-sm italic text-ink-2">{data.framing.closer}</p>}
                <p className="text-[11.5px] text-muted">{data.framing.badge}. <a className="underline" href={data.framing.disclosure} target="_blank" rel="noreferrer">About this</a></p>
              </div>
            )}
            </>
            )}
          </Card>
          {data.status === "draft" && data.checks.length + data.write_tasks.length > 0 && (
            <Card title="Questions in this draft" subtitle="Learners can answer them once the lesson is published.">
              <ul className="space-y-1 text-sm">
                {[...data.checks, ...data.write_tasks].map((it: PublicItem) => (
                  <li key={it.id} className="break-words"><Badge tone="brand">{it.kind_label}</Badge> {it.prompt}</li>
                ))}
              </ul>
            </Card>
          )}
          {data.status !== "draft" && data.checks.length > 0 && (
            <Card title="Check yourself" subtitle="Answer from memory first. Each answer shows the line of the course it comes from."
              actions={!hidden && <Button size="sm" onClick={() => setHidden(true)}>Hide the lesson while I answer</Button>}>
              <div className="space-y-3">{data.checks.map((it: PublicItem) => <QuestionCard key={it.id} item={it} />)}</div>
            </Card>
          )}
          {data.status !== "draft" && data.write_tasks.length > 0 && (
            <Card title="Write it" subtitle="Draft it the way you would send it; the rubric and the guardrails score it.">
              <div className="space-y-3">{data.write_tasks.map((it: PublicItem) => <QuestionCard key={it.id} item={it} />)}</div>
            </Card>
          )}
          <Card title="Practise it">
            <div className="flex flex-wrap gap-2">
              <Button onClick={onRolePlay}>Role-play this skill</Button>
              {data.practise.coaching_kind && me.tabs.includes("coaching") && <Button onClick={() => go(`/coaching?kind=${data.practise.coaching_kind}`)}>Draft one in Coaching mode</Button>}
              {data.status !== "draft" && (
                <Button tone="primary" onClick={complete} loading={busy} disabled={data.done}>
                  {data.done ? "Done" : "Mark as done and schedule reviews"}
                </Button>
              )}
            </div>
            <ErrorBox error={err} />
          </Card>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- review session (today)
function DrillReview({ card, onDone }: { card: any; onDone: () => void }) {
  const [mine, setMine] = useState("");
  const [shown, setShown] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const reveal = async () => {
    try {
      setShown(await get(`/academy/drills/${card.card_id}/answer`));
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const grade = async (g: number) => {
    try {
      await post(`/academy/review/${card.card_id}/grade`, { grade: g });
      onDone();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div className="rounded-xl border border-line bg-panel p-3 sm:p-4" data-testid="drill-review">
      <Badge tone="brand">Objection drill</Badge>
      <p className="my-2 text-sm font-medium">A prospect says: “{card.drill?.objection || card.objection}”. What do you say?</p>
      <TextArea rows={3} aria-label="Your reply" placeholder="Type your reply from memory first" value={mine} onChange={(e) => setMine(e.target.value)} disabled={!!shown} />
      {!shown ? (
        <Button className="mt-2" onClick={reveal} disabled={mine.trim().length < 3}>Show the course answer</Button>
      ) : (
        <div className="mt-2 space-y-2">
          <p className="rounded-lg bg-panel-2 p-3 text-sm">{shown.answer}</p>
          {shown.citation && <Citations items={[shown.citation]} />}
          <p className="text-xs text-muted">How close was yours?</p>
          <div className="flex flex-wrap gap-2">
            {([["Missed it", 1], ["Partly", 3], ["Got it", 4], ["Easy", 5]] as [string, number][]).map(([l, g]) => (
              <Button key={l} size="sm" onClick={() => grade(g)}>{l}</Button>
            ))}
          </div>
        </div>
      )}
      <ErrorBox error={err} />
    </div>
  );
}

export function ReviewSession({ cards, onFinish }: { cards: any[]; onFinish: () => void }) {
  const [i, setI] = useState(0);
  const [answered, setAnswered] = useState(false);
  const sessionId = useMemo(() => `s${Date.now()}`, []);
  const card = cards[i];
  const next = () => {
    setAnswered(false);
    if (i + 1 >= cards.length) onFinish();
    else setI(i + 1);
  };
  if (!card) return <Empty title="Nothing due right now" hint="Questions come back just before you'd forget them." />;
  return (
    <div className="space-y-3" data-testid="review-session">
      <div className="flex items-center justify-between gap-2 text-xs text-muted">
        <span>Review {i + 1} of {cards.length}</span>
        <div className="w-32"><Bar value={(i + (answered ? 1 : 0)) / cards.length} /></div>
      </div>
      {card.item_kind === "quiz" ? (
        <>
          <QuestionCard item={card.item} context="review" sessionId={sessionId} onAnswered={() => setAnswered(true)} />
          <Button tone="primary" onClick={next} disabled={!answered}>{i + 1 >= cards.length ? "Finish" : "Next"}</Button>
        </>
      ) : (
        <DrillReview card={card} onDone={next} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- today
export function TodayTab({ data, reload, openLesson, setTab }: { data: any; reload: () => void; openLesson: (id: number) => void; setTab: (t: string) => void }) {
  const [reviewing, setReviewing] = useState(false);
  const held = (data.certificates || []).reduce((n: number, c: any) => n + c.held.length, 0);
  if (reviewing) return <ReviewSession cards={data.session.reviews} onFinish={() => { setReviewing(false); reload(); }} />;
  return (
    <div className="space-y-4" data-testid="academy-today">
      {data.exam_open && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          You have a certification exam open. The Student Coach, lessons and practice answers are paused until you submit it.{" "}
          <button className="font-semibold underline" onClick={() => setTab("certs")}>Go to the exam</button>
        </div>
      )}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Stat label="Reviews due today" value={data.reviews_due} hint="Up to 20, mixed across skills" />
        <Stat label="Next lesson" value={data.next_lesson ? `${data.next_lesson.est_minutes} min` : "All done"} hint={data.next_lesson?.title || "Every lesson in your paths is done"} />
        <Stat label="Certificates held" value={held} hint="Internal training credentials" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Today's reviews" subtitle="Short recall questions that come back just before you'd forget them.">
          {data.reviews_due > 0 ? (
            <Button tone="primary" onClick={() => setReviewing(true)} disabled={data.exam_open}><Repeat className="size-4" /> Start {data.session.review_count} reviews</Button>
          ) : (
            <p className="text-sm text-muted">Nothing due. Finish a lesson and its questions start coming back from tomorrow.</p>
          )}
          {data.suggested_roleplay && (
            <p className="mt-3 text-sm">
              Then one role-play on your weakest skill, <b>{data.suggested_roleplay.label}</b>.{" "}
              <button className="text-brand underline-offset-2 hover:underline" onClick={() => setTab("roleplay")}>Open role-play</button>
            </p>
          )}
        </Card>
        <Card title="Next lesson">
          {data.next_lesson ? (
            <div className="space-y-2">
              <p className="break-words font-medium">{data.next_lesson.title}</p>
              <p className="text-xs text-muted">{data.next_lesson.path_title} · {data.next_lesson.skill_label} · about {data.next_lesson.est_minutes} min</p>
              <Button tone="primary" onClick={() => openLesson(data.next_lesson.id)} disabled={data.exam_open}><BookOpen className="size-4" /> Read it</Button>
              {data.exam_open && <p className="text-xs text-muted">Lessons and reviews open again once you submit your exam.</p>}
            </div>
          ) : (
            <p className="text-sm text-muted">You've finished every published lesson in your paths. Practise to keep them fresh.</p>
          )}
        </Card>
      </div>
      {data.assignments?.length > 0 && (
        <Card title="Assigned to you">
          <ul className="space-y-1.5 text-sm">
            {data.assignments.map((a: any) => (
              <li key={a.id} className="flex flex-wrap items-center justify-between gap-2">
                <span className="break-words">
                  {a.kind === "lesson" ? "Lesson" : a.kind === "path" ? "Path" : "Role-play"}{a.note ? `: ${a.note}` : ""}
                  {a.assigned_by_name && <span className="text-muted"> · from {a.assigned_by_name}</span>}
                </span>
                {a.kind === "lesson" && <Button size="sm" onClick={() => openLesson(Number(a.ref))}>Open</Button>}
                {a.due && <span className="text-xs text-muted">due {fmtDate(a.due)}</span>}
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card title="Certificate progress" subtitle="What's left for the next level, read from your real activity.">
        <ul className="space-y-3">
          {(data.certificates || []).map((c: any) => (
            <li key={c.path} className="text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <GraduationCap className="size-4 text-brand" />
                <b>{c.title}</b>
                {c.held.map((l: string) => <Badge key={l} tone="good">{l}</Badge>)}
              </div>
              {c.next ? (
                <p className="mt-1 text-xs text-muted">Next: {c.next.level} {c.next.name}. Still needed: {c.next.missing.join("; ") || "nothing — claim it in Certificates"}</p>
              ) : (
                <p className="mt-1 text-xs text-muted">Every level held.</p>
              )}
            </li>
          ))}
        </ul>
        <Button size="sm" className="mt-3" onClick={() => setTab("certs")}>See every criterion</Button>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- paths
export function PathsTab({ openLesson }: { openLesson: (id: number) => void }) {
  const list = useApi<any>("/academy/paths");
  const [open, setOpen] = useState<string | null>(null);
  const detail = useApi<any>(open ? `/academy/paths/${open}` : null, [open]);
  if (open) {
    return (
      <div className="space-y-4" data-testid="path-detail">
        <Button tone="ghost" size="sm" onClick={() => setOpen(null)}><ArrowLeft className="size-4" /> All paths</Button>
        {detail.loading && <Loading rows={6} />}
        <ErrorBox error={detail.error} retry={detail.reload} />
        {detail.data && (
          <>
            <Card title={detail.data.title} subtitle={detail.data.blurb}>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Stat label="Lessons done" value={`${detail.data.done} / ${detail.data.lessons}`} />
                <Stat label="Chance of a right answer" value={pct(detail.data.mastery)} hint="Across this path's skills" />
                <Stat label="Level" value={detail.data.level || "None yet"} hint={detail.data.level_name || "Finish L1 Foundations first"} />
              </div>
            </Card>
            <Card title="Lessons" pad={false}>
              {detail.data.lesson_list.length === 0 ? (
                <div className="p-4"><Empty title="No published lessons for this path yet" hint="The owner publishes lessons after reviewing them." /></div>
              ) : (
                <ol className="divide-y divide-line">
                  {detail.data.lesson_list.map((L: any) => (
                    <li key={L.id}>
                      <button className="focus-ring flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-panel-2" onClick={() => openLesson(L.id)}>
                        {L.done ? <CheckCircle2 className="size-4 shrink-0 text-good" /> : <Circle className="size-4 shrink-0 text-muted" />}
                        <span className="min-w-0 flex-1 break-words text-sm">{L.title}</span>
                        <span className="shrink-0 text-xs text-muted">{L.skill_label} · {L.est_minutes} min</span>
                      </button>
                    </li>
                  ))}
                </ol>
              )}
            </Card>
            <Card title="Skills">
              {detail.data.skills.length === 0 ? <p className="text-sm text-muted">No questions published for this path yet.</p> : (
                <ul className="space-y-2">
                  {detail.data.skills.map((s: any) => (
                    <li key={s.key} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 text-sm">
                      <span className="truncate">{s.label}</span>
                      <span className="text-xs tabular-nums text-muted">{s.chance === null ? "not practised" : pct(s.chance)}</span>
                      <div className="col-span-2"><Bar value={s.chance ?? 0} tone={(s.chance ?? 0) >= 0.7 ? "good" : "brand"} /></div>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </>
        )}
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {list.loading && <Loading rows={4} />}
      <ErrorBox error={list.error} retry={list.reload} />
      {list.data && (
        <div className="grid gap-3 md:grid-cols-2">
          {list.data.paths.map((p: any) => (
            <button key={p.key} className="card focus-ring p-4 text-left hover:border-brand" onClick={() => setOpen(p.key)} data-testid="path-card">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">{p.title}</span>
                {p.level ? <Badge tone="good">{p.level} {p.level_name}</Badge> : <Badge>Not certified yet</Badge>}
              </div>
              <p className="mt-1 text-xs text-muted">{p.blurb}</p>
              <div className="mt-3"><Bar value={p.lessons ? p.done / p.lessons : 0} /></div>
              <p className="mt-1 text-xs text-muted">{p.done} of {p.lessons} lessons · chance of a right answer {pct(p.mastery)} · about {p.minutes} min in total</p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- practice
export function PracticeTab() {
  const [skill, setSkill] = useState("");
  const [seen, setSeen] = useState<number[]>([]);
  const [round, setRound] = useState(0);
  const { data, error, loading, reload } = useApi<any>(`/academy/practice?n=1${skill ? `&skill=${skill}` : ""}`, [skill, round]);
  const drills = useApi<any>("/academy/drills");
  const [drill, setDrill] = useState(false);
  const [answered, setAnswered] = useState(false);
  const item: PublicItem | undefined = (data?.items || []).find((x: any) => !seen.includes(x.id)) || data?.items?.[0];
  const dueDrills = (drills.data?.cards || []).filter((c: any) => c.is_due);
  useEffect(() => setAnswered(false), [item?.id]);
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Card title="Adaptive practice" subtitle="Questions picked so you get about 7 in 10 right: hard enough to stretch, not so hard you guess."
        actions={data && (
          <Select aria-label="Skill" value={skill} onChange={(e) => { setSkill(e.target.value); setSeen([]); }}
            options={[{ value: "", label: "All skills" }, ...(data.skills || []).map((s: any) => ({ value: s.key, label: s.label }))]} />
        )}>
        {loading && <Loading rows={4} />}
        <ErrorBox error={error} retry={reload} />
        {data && !item && <Empty title="No practice questions yet" hint="Questions appear once the owner publishes lessons." />}
        {item && (
          <div className="space-y-3">
            <QuestionCard item={item} context="practice" onAnswered={() => setAnswered(true)} />
            <Button tone="primary" disabled={!answered} onClick={() => { setSeen([...seen, item.id]); setRound(round + 1); }}>Next question</Button>
          </div>
        )}
      </Card>
      <div className="space-y-4">
        <Card title="Your skills">
          {data?.mastery?.length ? (
            <ul className="space-y-2">
              {data.mastery.map((m: any) => (
                <li key={m.skill} className="text-sm">
                  <div className="flex justify-between gap-2"><span className="truncate">{m.label}</span><span className="text-xs tabular-nums text-muted">{pct(m.chance)}</span></div>
                  <Bar value={m.chance} tone={m.chance >= 0.7 ? "good" : "brand"} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">Answer a few questions to see your chance of getting a typical question right, per skill.</p>
          )}
        </Card>
        <Card title="Objection drills" subtitle="Say it from memory, then check the course's answer.">
          {drills.loading && <Loading rows={2} />}
          <ErrorBox error={drills.error} retry={drills.reload} />
          {drills.data && !drills.data.cards.length && <p className="text-sm text-muted">No drills yet. They're built from the course when the Academy is built.</p>}
          {drills.data && drills.data.cards.length > 0 && (drill && dueDrills[0] ? (
            <DrillReview card={dueDrills[0]} onDone={() => { setDrill(false); drills.reload(); }} />
          ) : (
            <div className="space-y-2">
              <p className="text-sm">{dueDrills.length} due of {drills.data.cards.length}.</p>
              <Button onClick={() => setDrill(true)} disabled={!dueDrills.length}>Drill the next one</Button>
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- certificates + exam
function ExamView({ exam, onDone }: { exam: any; onDone: () => void }) {
  const [answers, setAnswers] = useState<Record<string, any>>({});
  const [left, setLeft] = useState(Math.max(0, Math.round(exam.deadline - Date.now() / 1000)));
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    const t = setInterval(() => setLeft(Math.max(0, Math.round(exam.deadline - Date.now() / 1000))), 1000);
    return () => clearInterval(t);
  }, [exam.deadline]);
  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post(`/academy/exams/${exam.id}/submit`, { answers }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  if (res) {
    return (
      <Card title="Exam result">
        <p className="text-lg font-semibold">{Math.round(res.score * 100)}% · {res.right} of {res.total}</p>
        <p className="text-sm">{res.passed ? "Passed." : res.status === "late" ? "Submitted after the time limit, so it doesn't count." : `Not passed yet; the pass mark is ${Math.round(res.pass_mark * 100)}%.`}</p>
        {!res.passed && res.retake_after && <p className="text-xs text-muted">You can retake it after {new Date(res.retake_after * 1000).toLocaleString()}.</p>}
        <Button className="mt-3" onClick={onDone}>Back to certificates</Button>
      </Card>
    );
  }
  const answeredN = Object.values(answers).filter((v) => v !== undefined && v !== "").length;
  return (
    <div className="space-y-3" data-testid="exam-view">
      <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-sm shadow-sm">
        <span className="flex items-center gap-1.5"><Timer className="size-4" /> {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</span>
        <span className="text-muted">{answeredN} of {exam.items.length} answered</span>
        <Button tone="primary" size="sm" onClick={submit} loading={busy}>Submit the exam</Button>
      </div>
      <p className="text-xs text-muted">Answers are checked only when you submit. The Student Coach, lessons and practice answers are paused until then.</p>
      {exam.items.map((it: PublicItem, i: number) => (
        <div key={it.id}>
          <p className="mb-1 text-xs font-semibold text-muted">Question {i + 1}</p>
          <QuestionCard item={it} examMode examValue={answers[String(it.id)]} onExamChange={(v) => setAnswers((a) => ({ ...a, [String(it.id)]: v }))} />
        </div>
      ))}
      <ErrorBox error={err} />
      <Button tone="primary" onClick={submit} loading={busy}>Submit the exam</Button>
    </div>
  );
}

const LEVEL_TONE: Record<string, any> = { active: "good", expired: "warn", revoked: "bad", ready: "info", in_progress: "neutral" };

export function CertificatesTab({ setTab, examOpen = false }: { setTab: (t: string) => void; examOpen?: boolean }) {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/academy/certs/me");
  const [exam, setExam] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const check = async () => {
    setBusy("check");
    setErr(null);
    try {
      const r = await post("/academy/certs/check");
      toast(r.awarded.length ? `${r.awarded.length} new certificate${r.awarded.length > 1 ? "s" : ""}` : "Nothing new yet. The missing criteria are listed below.", r.awarded.length ? "good" : "info");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const startExam = async (path: string) => {
    setBusy(path);
    setErr(null);
    try {
      setExam(await post("/academy/exams", { path, level: "L3" }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  if (exam) return <ExamView exam={exam} onDone={() => { setExam(null); reload(); }} />;
  const LINK: Record<string, string> = { paths: "paths", practice: "practice", roleplay: "roleplay", certificates: "certs" };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">{data?.note || "Internal training credentials."} Certificates last 180 days.</p>
        <Button onClick={check} loading={busy === "check"}>Check for new certificates</Button>
      </div>
      <ErrorBox error={err} />
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {data?.ladders.map((lad: any) => (
        <Card key={lad.path} title={lad.title} subtitle={lad.blurb}>
          <ol className="space-y-3">
            {lad.levels.map((lv: any) => (
              <li key={lv.level} className="rounded-lg border border-line p-3" data-testid="cert-level" data-status={lv.status}>
                <div className="flex flex-wrap items-center gap-2">
                  {lv.status === "active" ? <CheckCircle2 className="size-4 text-good" /> : lv.missing.some((m: any) => m.key === "previous") ? <Lock className="size-4 text-muted" /> : <Circle className="size-4 text-muted" />}
                  <b>{lv.level} · {lv.name}</b>
                  <Badge tone={LEVEL_TONE[lv.status]}>{lv.status_label}</Badge>
                  {lv.cert?.expires && lv.status === "active" && <span className="text-xs text-muted">until {fmtDate(lv.cert.expires)}</span>}
                  {lv.cert?.signed_off_by && <span className="text-xs text-muted">signed off by {lv.cert.signed_off_by}</span>}
                </div>
                {lv.cert?.reason && lv.status !== "active" && <p className="mt-1 text-xs text-amber-800">{lv.cert.reason}</p>}
                {lv.cert?.text && lv.status === "active" && <p className="mt-1 break-words text-xs text-muted">{lv.cert.text}</p>}
                {lv.status !== "active" && (
                  <ul className="mt-2 space-y-1">
                    {lv.criteria.map((c: any) => (
                      <li key={c.key} className="flex flex-wrap items-start gap-2 text-sm">
                        {c.met ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-good" /> : <Circle className="mt-0.5 size-4 shrink-0 text-muted" />}
                        <span className="min-w-0 flex-1 break-words">
                          {c.label}
                          <span className="text-xs text-muted"> — {c.progress}</span>
                          {c.detail && <span className="block text-xs text-muted">{c.detail}</span>}
                        </span>
                        {!c.met && c.key === "exam" && !lv.missing.some((m: any) => m.key === "previous") && (
                          <Button size="sm" tone="primary" loading={busy === lad.path} onClick={() => startExam(lad.path)}>{examOpen ? "Back to your open exam" : "Start the timed exam"}</Button>
                        )}
                        {!c.met && c.link && LINK[c.link] && c.key !== "exam" && (
                          <Button size="sm" tone="ghost" onClick={() => setTab(LINK[c.link])}>Go</Button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ol>
        </Card>
      ))}
    </div>
  );
}
