import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AiModeBadge, Badge, Card, DemoBadge, ErrorBox, Loading, PageHeader, Stat, Table, isDemo, useApi, useApp } from "../components/ui";
import { BurstPanel } from "./Sales";
import { RetentionStar } from "./Students";

export default function CommandCentre() {
  const { openTrace, go, me } = useApp();
  const { data, error, loading, reload } = useApi<any>("/command-centre");
  const q = data?.kpis.queue || {};
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
          <div className="grid gap-4 xl:grid-cols-3">
            <Card title="Agent activity (7 days)">
              {Object.keys(data.agents_7d).length === 0 ? <p className="text-sm text-muted">No runs yet.</p> : (
                <div className="h-56"><ResponsiveContainer><BarChart data={Object.entries(data.agents_7d).map(([k, v]) => ({ agent: k, runs: v }))} layout="vertical" margin={{ left: 30 }}>
                  <XAxis type="number" allowDecimals={false} /><YAxis type="category" dataKey="agent" width={90} tick={{ fontSize: 11 }} /><Tooltip /><Bar dataKey="runs" fill="var(--viz-1)" radius={4} maxBarSize={22} />
                </BarChart></ResponsiveContainer></div>
              )}
            </Card>
            <Card title="Student risk" actions={<a className="text-xs text-brand" href="#/students">All students</a>}>
              <Table rows={data.at_risk} empty="No students" cols={[
                { key: "name", label: "Student", render: (s) => <span className="flex items-center gap-1">{s.name.replace(" (DEMO)", "")} {isDemo(s.name) && <DemoBadge />}</span> },
                { key: "level", label: "Risk", render: (s) => <Badge tone={s.level === "high" ? "bad" : s.level === "medium" ? "warn" : "good"}>{s.risk}</Badge> },
              ]} />
            </Card>
            <Card title="Recent runs" actions={<a className="text-xs text-brand" href="#/runs">All runs</a>}>
              <Table rows={data.recent_runs} onRow={(r) => openTrace(r.id)} cols={[
                { key: "agent", label: "Agent", render: (r) => <Badge>{r.agent}</Badge> }, { key: "title", label: "Run", className: "max-w-[200px] truncate" },
                { key: "ai_mode", label: "", render: (r) => <AiModeBadge mode={r.ai_mode} /> },
              ]} />
            </Card>
          </div>
          <div className="grid gap-4 xl:grid-cols-3">
            <Card title="Approval queue" actions={<a className="text-xs text-brand" href="#/queue">Open queue</a>}>
              <div className="flex flex-wrap gap-2">{Object.entries(q).map(([k, v]) => <Badge key={k} tone={k === "held" ? "bad" : k === "pending" ? "info" : k === "approved" ? "good" : "neutral"}>{k}: {v as number}</Badge>)}</div>
              <button className="mt-3 text-sm text-brand" onClick={() => go("/queue?status=held")}>Review held drafts →</button>
            </Card>
            <Card title="Quality & evals" actions={<a className="text-xs text-brand" href="#/evals">Evals</a>}>
              {Object.keys(data.evals).length === 0 ? <p className="text-sm text-muted">No eval runs yet.</p> : (
                <ul className="space-y-1 text-sm">{Object.entries(data.evals).map(([k, v]: any) => <li key={k} className="flex justify-between"><span>{k}</span><code className="text-xs">{Object.entries(v).filter(([kk]) => typeof v[kk] === "number").map(([kk, vv]) => `${kk} ${vv}`).join(" · ")}</code></li>)}</ul>
              )}
            </Card>
            <RetentionStar />
          </div>
          <BurstPanel />
        </div>
      )}
    </div>
  );
}
