import { AiModeBadge, Badge, Card, Citations, FlagList, TraceLink, cx } from "./ui";

export function ScoreRing({ score }: { score: number }) {
  const tone = score >= 70 ? "text-good" : score >= 50 ? "text-warn" : "text-bad";
  return (
    <div className="flex items-baseline gap-1">
      <span className={cx("text-4xl font-semibold tabular-nums", tone)} data-testid="score">{score}</span>
      <span className="text-sm text-muted">/100</span>
    </div>
  );
}

export function ReviewResult({ r }: { r: any }) {
  return (
    <div className="space-y-4" data-testid="review-result">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-xs text-muted">Asset type</div>
            <div className="font-semibold">{r.asset_type}</div>
          </div>
          <ScoreRing score={r.score} />
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={r.passes ? "good" : "warn"}>{r.passes ? "Passes rubric" : "Needs work"}</Badge>
            <AiModeBadge mode={r.mode} />
            <TraceLink runId={r.run_id} />
          </div>
        </div>
        <p className="mt-2 text-xs text-muted">{r.label}</p>
        <div className="mt-3"><FlagList flags={r.flags} /></div>
      </Card>
      <Card title="Line-level fixes">
        {r.fixes.length === 0 ? <p className="text-sm text-muted">No fixes needed.</p> : (
          <ol className="space-y-3">
            {r.fixes.map((f: any, i: number) => (
              <li key={i} className="rounded-lg border border-line p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                  {f.line_no ? <Badge>line {f.line_no}</Badge> : null}
                  <span className="font-medium text-ink">{f.problem}</span>
                  {f.severity === "block" && <Badge tone="bad">blocks sending</Badge>}
                </div>
                {f.line && <blockquote className="border-l-2 border-line pl-2 text-sm italic text-muted">“{f.line}”</blockquote>}
                <p className="mt-1 text-sm"><b>Fix:</b> {f.fix}</p>
                {f.suggestion && <p className="mt-1 rounded bg-green-50 px-2 py-1 text-sm text-green-900"><b>Try:</b> {f.suggestion}</p>}
                {f.citations?.length > 0 && <Citations items={r.citations.filter((c: any) => f.citations.includes(c.id))} />}
              </li>
            ))}
          </ol>
        )}
      </Card>
      <Card title="Rubric">
        <div className="space-y-2">
          {r.criteria.map((c: any) => (
            <div key={c.key} className="flex items-center gap-3 text-sm">
              <div className="w-56 shrink-0 truncate" title={c.label}>{c.label}</div>
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-black/5">
                <div className={cx("h-full rounded-full", c.score >= 8 ? "bg-good" : c.score >= 5 ? "bg-amber-500" : "bg-bad")} style={{ width: `${c.score * 10}%` }} />
              </div>
              <div className="w-10 text-right tabular-nums">{c.score}</div>
              <div className="w-16 text-right text-xs text-muted">×{c.weight}</div>
            </div>
          ))}
        </div>
        <Citations items={r.citations} />
      </Card>
    </div>
  );
}
