// One Academy question: answer it, get the course's answer with its citation, and see what changed.
import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Check, Flag, X } from "lucide-react";
import { ApiError, post } from "../../api";
import { Badge, Button, Citations, ErrorBox, FlagList, Input, TextArea, cx, useApp } from "../../components/ui";

export type PublicItem = {
  id: number;
  kind: string;
  kind_label: string;
  prompt: string;
  options?: string[] | null;
  skill?: string;
  skill_label?: string;
  asset?: string;
  predicted_chance?: number;
};

const pct = (v?: number | null) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);

function OrderInput({ options, value, onChange, disabled }: { options: string[]; value: string[]; onChange: (v: string[]) => void; disabled?: boolean }) {
  const move = (i: number, d: number) => {
    const j = i + d;
    if (j < 0 || j >= value.length) return;
    const next = [...value];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  return (
    <ol className="space-y-1.5" aria-label="Put these in order">
      {value.map((o, i) => (
        <li key={o} className="flex items-center gap-2 rounded-lg border border-line bg-panel-2 px-2 py-1.5 text-sm">
          <span className="w-5 shrink-0 text-center text-xs font-semibold text-muted">{i + 1}</span>
          <span className="min-w-0 flex-1 break-words">{o}</span>
          <Button size="sm" tone="ghost" disabled={disabled || i === 0} onClick={() => move(i, -1)} aria-label={`Move “${o}” up`}>
            <ArrowUp className="size-3.5" />
          </Button>
          <Button size="sm" tone="ghost" disabled={disabled || i === value.length - 1} onClick={() => move(i, 1)} aria-label={`Move “${o}” down`}>
            <ArrowDown className="size-3.5" />
          </Button>
        </li>
      ))}
      {!options.length && <li className="text-sm text-muted">No options.</li>}
    </ol>
  );
}

export function QuestionCard({ item, context = "quiz", onAnswered, sessionId, examMode, examValue, onExamChange }: {
  item: PublicItem;
  context?: "quiz" | "review" | "practice";
  onAnswered?: (res: any) => void;
  sessionId?: string;
  examMode?: boolean;
  examValue?: string | string[];
  onExamChange?: (v: string | string[]) => void;
}) {
  const { toast } = useApp();
  const started = useRef(Date.now());
  const [text, setText] = useState("");
  const [order, setOrder] = useState<string[]>(item.options || []);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [reporting, setReporting] = useState(false);
  const [reason, setReason] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  useEffect(() => {
    started.current = Date.now();
    setText("");
    setOrder(item.options || []);
    setRes(null);
    setErr(null);
    setPicked(null);
  }, [item.id]);
  useEffect(() => {
    if (examMode && item.kind === "order" && examValue === undefined && onExamChange) onExamChange(item.options || []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [examMode, item.id]);

  const submit = async (answer: string | string[]) => {
    if (examMode) {
      onExamChange?.(answer);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await post(`/academy/items/${item.id}/answer`, { answer, ms: Date.now() - started.current, context, session_id: sessionId });
      setRes(r);
      onAnswered?.(r);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const report = async () => {
    try {
      await post(`/academy/items/${item.id}/report`, { reason });
      setReporting(false);
      toast("Thanks. The owner will look at this question.", "good");
    } catch (e) {
      setErr(e as ApiError);
    }
  };

  const choice = ["mc_numeric", "mc_term", "scenario", "tf", "spot"].includes(item.kind);
  const done = !!res;
  return (
    <div className="rounded-xl border border-line bg-panel p-3 sm:p-4" data-testid="academy-question" data-kind={item.kind}>
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <Badge tone="brand">{item.kind_label}</Badge>
        {item.skill_label && <Badge>{item.skill_label}</Badge>}
        {item.predicted_chance !== undefined && <span className="text-[11px] text-muted">Your chance on this one: about {pct(item.predicted_chance)}</span>}
      </div>
      <p className="mb-3 break-words text-sm font-medium leading-relaxed">{item.prompt}</p>

      {choice && (
        <div className="grid gap-1.5">
          {(item.options || []).map((o) => {
            const isPicked = examMode ? examValue === o : picked === o;
            const right = done && res.answer === o;
            const wrong = done && picked === o && !res.correct;
            return (
              <button
                key={o}
                disabled={busy || (done && !examMode)}
                aria-pressed={isPicked}
                onClick={() => {
                  setPicked(o);
                  submit(o);
                }}
                className={cx(
                  "focus-ring w-full rounded-lg border px-3 py-2 text-left text-sm break-words transition",
                  right ? "border-green-300 bg-green-50" : wrong ? "border-red-300 bg-red-50" : isPicked ? "border-brand bg-[color-mix(in_oklab,var(--brand)_8%,white)]" : "border-line bg-panel-2 hover:border-brand",
                )}
                data-testid="academy-option"
              >
                {o}
              </button>
            );
          })}
        </div>
      )}

      {item.kind === "cloze" && (
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) submit(text.trim());
          }}
        >
          <Input
            aria-label="Your answer"
            placeholder="Type the missing words"
            value={examMode ? (examValue as string) || "" : text}
            onChange={(e) => (examMode ? onExamChange?.(e.target.value) : setText(e.target.value))}
            disabled={done && !examMode}
            className="min-w-0 flex-1"
          />
          {!examMode && <Button tone="primary" type="submit" loading={busy} disabled={done || !text.trim()}>Check</Button>}
        </form>
      )}

      {item.kind === "order" && (
        <div className="space-y-2">
          <OrderInput options={item.options || []} value={examMode ? ((examValue as string[]) || item.options || []) : order}
            onChange={(v) => (examMode ? onExamChange?.(v) : setOrder(v))} disabled={done && !examMode} />
          {!examMode && <Button tone="primary" onClick={() => submit(order)} loading={busy} disabled={done}>Check the order</Button>}
        </div>
      )}

      {item.kind === "write" && (
        <div className="space-y-2">
          <TextArea aria-label="Your draft" rows={4} placeholder="Write it as you would send it" value={text} onChange={(e) => setText(e.target.value)} disabled={done} />
          <Button tone="primary" onClick={() => submit(text)} loading={busy} disabled={done || text.trim().split(/\s+/).length < 3}>Score my draft</Button>
          <p className="text-[11px] text-muted">Scored by the programme rubric and the guardrails. Nothing is sent anywhere.</p>
        </div>
      )}

      <ErrorBox error={err} />

      {res && (
        <div className={cx("mt-3 space-y-2 rounded-lg border p-3 text-sm", res.correct ? "border-green-200 bg-green-50/60" : "border-amber-200 bg-amber-50/60")} data-testid="academy-result" data-correct={res.correct ? "1" : "0"}>
          <p className="flex items-center gap-1.5 font-semibold">
            {res.correct ? <Check className="size-4 text-good" /> : <X className="size-4 text-bad" />}
            {item.kind === "write" ? `Score ${Math.round(res.score)} / 100${res.correct ? " — passes" : " — not yet"}` : res.correct ? "Right" : "Not quite"}
          </p>
          {!res.correct && res.answer && item.kind !== "write" && (
            <p>{item.kind === "spot" ? "The line that breaks the rules: " : "The course answer: "}<b>{Array.isArray(res.answer) ? res.answer.join(" → ") : res.answer}</b></p>
          )}
          {res.explanation && <p className="break-words text-muted">{res.explanation}</p>}
          {res.citation && <Citations items={[res.citation]} />}
          {res.feedback?.criteria?.length > 0 && (
            <ul className="space-y-1">
              {res.feedback.criteria.filter((c: any) => c.fix).map((c: any) => (
                <li key={c.label} className="rounded-md bg-white/70 px-2 py-1 text-xs">
                  <b>{c.label}</b> ({c.score}/10): {c.fix}{" "}
                  {c.citations?.length > 0 && <span className="text-muted">[{c.citations.join(", ")}]</span>}
                </li>
              ))}
            </ul>
          )}
          {res.feedback?.citations?.length > 0 && <Citations items={res.feedback.citations} />}
          {res.feedback && <FlagList flags={res.feedback.flags} />}
          <p className="text-xs text-muted">
            {res.mastery && res.mastery.counted !== false && <>Chance on a typical {item.skill_label || "question"} question now: {pct(res.mastery.chance)}. </>}
            {res.mastery?.counted === false && res.mastery.note && <>{res.mastery.note} </>}
            {res.review && <>Next review in {res.review.interval_days} day{res.review.interval_days === 1 ? "" : "s"}. </>}
            {res.awarded?.length > 0 && <b className="text-good">New certificate earned. </b>}
          </p>
        </div>
      )}

      {!examMode && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {!reporting ? (
            <button className="focus-ring inline-flex items-center gap-1 text-[11px] text-muted hover:text-ink" onClick={() => setReporting(true)}>
              <Flag className="size-3" /> Report a problem with this question
            </button>
          ) : (
            <div className="flex w-full flex-wrap gap-2">
              <Input aria-label="What's wrong with it?" placeholder="What's wrong with it? (optional)" value={reason} onChange={(e) => setReason(e.target.value)} className="min-w-0 flex-1" />
              <Button size="sm" onClick={report}>Send report</Button>
              <Button size="sm" tone="ghost" onClick={() => setReporting(false)}>Cancel</Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
