import type { ReactNode } from "react";
import { ArrowRight, ArrowUpRight, ChevronRight, FlaskConical, ListTree } from "lucide-react";
import { AiModeBadge, Badge, Button, Card, DemoBadge, Empty, ErrorBox, Loading, PageHeader, Stat, ago, cx, humanize, isDemo, runTitle, useApi, useApp } from "../components/ui";
import { BurstPanel } from "./Sales";
import { RetentionStar } from "./Students";

const QUEUE_ORDER = ["held", "needs_human", "pending", "approved", "rejected"];
const QUEUE_LABEL: Record<string, string> = { held: "Held by guardrails", needs_human: "Needs a person", pending: "Pending approval", approved: "Approved", rejected: "Rejected" };
const QUEUE_DOT: Record<string, string> = { held: "bg-bad", needs_human: "bg-warn", pending: "bg-info", approved: "bg-good", rejected: "bg-faint" };
const LEVEL_TONE: Record<string, any> = { high: "bad", medium: "warn", low: "good" };

const CardLink = ({ href, children }: { href: string; children: ReactNode }) => (
  <a href={href} className="focus-ring group inline-flex items-center gap-1 rounded text-[12.5px] font-medium text-brand">
    {children} <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" />
  </a>
);

export default function CommandCentre() {
  const { openTrace, go, me } = useApp();
  const { data, error, loading, reload } = useApi<any>("/command-centre");
  const q: Record<string, number> = data?.kpis.queue || {};
  const queueRows = QUEUE_ORDER.filter((k) => q[k]).concat(Object.keys(q).filter((k) => !QUEUE_ORDER.includes(k) && q[k]));
  const queueTotal = queueRows.reduce((n, k) => n + q[k], 0) || 1;
  const agents = Object.entries(data?.agents_7d || {}).map(([k, v]) => ({ agent: humanize(k), runs: v as number })).sort((a, b) => b.runs - a.runs);
  const agentMax = Math.max(1, ...agents.map((a) => a.runs));
  const evals = Object.entries(data?.evals || {});
  return (
    <div>
      <PageHeader title="Command centre" subtitle="Everything at a glance: KPIs, risk, leads, agent activity, recent runs with traces, quality and the queue." badge={me.is_demo ? <DemoBadge /> : null} />
      <ErrorBox error={error} retry={reload} />
      {loading && <Loading rows={6} />}
      {data && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <Stat label="Students" value={data.kpis.students} demo={data.is_demo} />
            <Stat label="At risk" value={data.kpis.at_risk} tone={data.kpis.at_risk ? "warn" : undefined} demo={data.is_demo} />
            <Stat label="Hot leads" value={data.kpis.leads.hot || 0} demo={data.is_demo} />
            <Stat label="Waiting approval" value={(q.pending || 0) + (q.needs_human || 0)} />
            <Stat label="Held by guardrails" value={q.held || 0} tone={q.held ? "bad" : undefined} />
            <Stat label="Agent runs today" value={data.kpis.runs_today} hint={data.ai.label} />
          </div>

          {/* ---------------------------------------------------------- what happened, who needs help */}
          <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)] 2xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <Card title="Recent runs" subtitle="Every agent run is traced step by step" actions={<CardLink href="#/runs">All runs</CardLink>} pad={false}>
              {data.recent_runs.length === 0 ? (
                <div className="p-4 sm:p-5"><Empty icon={<ListTree />} title="No runs yet" hint="Ask the coach or run an agent and its trace lands here." /></div>
              ) : (
                <ul className="divide-y divide-line-2">
                  {data.recent_runs.slice(0, 8).map((r: any) => (
                    <li key={r.id}>
                      <button type="button" onClick={() => openTrace(r.id)} className="focus-ring group flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-hover focus-visible:-outline-offset-2 sm:px-5">
                        <span className="w-24 shrink-0 truncate text-[12px] font-medium text-muted max-sm:hidden">{humanize(r.agent)}</span>
                        <span className="min-w-0 flex-1 truncate text-[13.5px] text-ink">{runTitle(r) || r.title}</span>
                        <AiModeBadge mode={r.ai_mode} />
                        <span className="hidden w-14 shrink-0 text-right text-[12px] tabular-nums text-muted sm:block">{ago(r.created)}</span>
                        <ChevronRight className="size-4 shrink-0 text-faint transition-colors group-hover:text-ink-2" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Student risk" subtitle="Highest rule-based risk first" actions={<CardLink href="#/students">All students</CardLink>} pad={false}>
              {data.at_risk.length === 0 ? (
                <div className="p-4 sm:p-5"><Empty title="No students yet" /></div>
              ) : (
                <ul className="divide-y divide-line-2">
                  {data.at_risk.slice(0, 8).map((s: any, i: number) => (
                    <li key={s.id ?? i} className="flex items-center gap-2 px-4 py-2.5 sm:px-5">
                      <span className="min-w-0 truncate text-[13.5px] text-ink">{s.name.replace(" (DEMO)", "")}</span>
                      {isDemo(s.name) && <DemoBadge />}
                      <Badge tone={LEVEL_TONE[s.level] || "neutral"} className="ml-auto tabular-nums">{humanize(s.level)} · {s.risk}</Badge>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          {/* ---------------------------------------------------------- activity, queue, health */}
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <Card title="Approval queue" subtitle="Nothing is sent until a person decides" actions={<CardLink href="#/queue">Open</CardLink>} className="flex flex-col" bodyClassName="flex flex-1 flex-col">
              {queueRows.length === 0 ? <Empty title="Queue is clear" /> : (
                <>
                  {/* share of the queue by state: one glance says whether it is mostly blocked or mostly waiting */}
                  <div className="mb-3 flex h-2 overflow-hidden rounded-full bg-[var(--neutral-soft)]" aria-hidden>
                    {queueRows.map((k) => <span key={k} className={cx("h-full first:rounded-l-full last:rounded-r-full", QUEUE_DOT[k] || "bg-faint")} style={{ width: `${(q[k] / queueTotal) * 100}%` }} />)}
                  </div>
                  <ul className="-mx-2 space-y-0.5">
                    {queueRows.map((k) => (
                      <li key={k}>
                        <a href={`#/queue?status=${k}`} className="focus-ring group flex h-9 items-center gap-2.5 rounded-[8px] px-2 text-[13.5px] transition-colors hover:bg-hover">
                          <span className={cx("size-2 shrink-0 rounded-full", QUEUE_DOT[k] || "bg-faint")} />
                          <span className="flex-1 text-ink-2 group-hover:text-ink">{QUEUE_LABEL[k] || humanize(k)}</span>
                          <span className="font-semibold tabular-nums text-ink">{q[k]}</span>
                        </a>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {q.held ? (
                <Button size="sm" className="mt-4 self-start" onClick={() => go("/queue?status=held")}>
                  Fix {q.held} held {q.held === 1 ? "draft" : "drafts"} <ArrowUpRight className="size-3.5" />
                </Button>
              ) : null}
            </Card>
            <div className="flex flex-col gap-4">
              <Card title="Agent activity" subtitle="Runs per agent, last 7 days" actions={<CardLink href="#/runs">Runs</CardLink>}>
                {agents.length === 0 ? <Empty title="No runs this week" /> : (
                  <ul className="space-y-3">
                    {agents.slice(0, 7).map((a) => (
                      <li key={a.agent} className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)_2.5rem] items-center gap-3 text-[13.5px]">
                        <span className="truncate text-ink-2">{a.agent}</span>
                        <span className="h-2 overflow-hidden rounded-full bg-[var(--neutral-soft)]">
                          <span className="block h-full rounded-full bg-[var(--viz-1)]" style={{ width: `${Math.max(4, (a.runs / agentMax) * 100)}%` }} />
                        </span>
                        <span className="text-right font-semibold tabular-nums text-ink">{a.runs}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
              <Card title="Quality & evals" subtitle="Answer accuracy and honesty checks" actions={evals.length > 0 && <CardLink href="#/evals">Evals</CardLink>} className="flex-1">
                {evals.length === 0 ? (
                  <div className="flex items-start gap-3">
                    <span className="grid size-9 shrink-0 place-items-center rounded-full bg-panel-2 text-muted ring-1 ring-line"><FlaskConical className="size-4" /></span>
                    <div className="min-w-0">
                      <p className="text-[13.5px] font-medium text-ink">No eval runs yet</p>
                      <p className="mt-0.5 text-[12.5px] leading-snug text-muted">Run the gold-question set to measure citation accuracy and honest “not covered” answers.</p>
                      {me.tabs.includes("evals") && <Button size="sm" className="mt-3" onClick={() => go("/evals")}>Open evals</Button>}
                    </div>
                  </div>
                ) : (
                  <ul className="divide-y divide-line-2">
                    {evals.map(([k, v]: any) => (
                      <li key={k} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 py-2 first:pt-0 last:pb-0">
                        <span className="text-[13.5px] font-medium text-ink">{humanize(k)}</span>
                        <span className="flex flex-wrap gap-1.5">
                          {Object.entries(v).filter(([, vv]) => typeof vv === "number").map(([kk, vv]) => (
                            <Badge key={kk} className="tabular-nums">{humanize(kk)} {String(vv)}</Badge>
                          ))}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>
            <RetentionStar />
          </div>
          <BurstPanel />
        </div>
      )}
    </div>
  );
}
