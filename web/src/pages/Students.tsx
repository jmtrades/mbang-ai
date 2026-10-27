import { useState } from "react";
import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip } from "recharts";
import { ApiError, post } from "../api";
import { MoveRight, TrendingDown, TrendingUp } from "lucide-react";
import { Badge, Button, Card, CopyButton, DemoBadge, ErrorBox, Loading, PageHeader, Stat, Table, Tabs, TraceLink, cx, humanize, isDemo, useApi, useApp } from "../components/ui";

const LEVEL: Record<string, any> = { high: "bad", medium: "warn", low: "good" };

/** Axis labels short enough to sit beside a radar in a narrow card ("Progress (clients / wins)" → "Progress"). */
const shortAxis = (l: string) => {
  const s = String(l).replace(/\s*\(.*?\)\s*/g, "").trim();
  return s.length > 12 ? s.split(/\s+/)[0] : s;
};

export function RetentionStar() {
  const { data, error, loading, reload } = useApi<any>("/students/star");
  return (
    <Card title="Retention Star" subtitle={data ? `Five-point community health · ${data.students} students` : "Five-point community health"}
      actions={data && data.points.length > 0 && (
        <span className="flex items-baseline gap-1" title="Overall score out of 100">
          <span className="text-[24px] font-semibold leading-none tracking-[-0.03em] tabular-nums text-ink">{Math.round(data.overall * 100)}</span>
          <span className="text-[12px] text-muted">/ 100</span>
        </span>
      )}>
      {loading && <Loading />}
      <ErrorBox error={error} retry={reload} />
      {data && data.points.length > 0 && (
        <div className="h-56" data-testid="retention-star">
          <ResponsiveContainer>
            <RadarChart data={data.points.map((p: any) => ({ ...p, v: Math.round(p.value * 100) }))} outerRadius="70%" margin={{ top: 6, right: 8, bottom: 6, left: 8 }}>
              <PolarGrid stroke="var(--line)" />
              <PolarAngleAxis dataKey="label" tickFormatter={shortAxis} tick={{ fontSize: 11.5, fill: "var(--muted)" }} />
              <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
              <Tooltip formatter={(v: any) => [`${v} / 100`, "Score"]} />
              <Radar dataKey="v" stroke="var(--viz-1)" fill="var(--viz-1)" fillOpacity={0.22} strokeWidth={1.5} />
            </RadarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

const TREND: Record<string, { icon: typeof TrendingUp; label: string; cls: string }> = {
  rising: { icon: TrendingUp, label: "Rising", cls: "text-bad" },
  falling: { icon: TrendingDown, label: "Falling", cls: "text-good" },
  flat: { icon: MoveRight, label: "Flat", cls: "text-muted" },
};

function Trend({ t }: { t: string }) {
  const d = TREND[t];
  if (!d) return <span className="text-muted">{humanize(t) || "—"}</span>;
  const I = d.icon;
  return <span className="inline-flex items-center gap-1.5 text-[13px] text-ink-2" title={`Risk ${t}`}><I className={cx("size-3.5", d.cls)} />{d.label}</span>;
}

export default function Students() {
  const { me, go } = useApp();
  const [tab, setTab] = useState("board");
  const { data, error, loading, reload } = useApi<any>("/students");
  const digest = useApi<any>(tab === "digest" ? "/students/digest" : null, [tab]);
  const tasks = useApi<any>(tab === "tasks" ? "/coach-tasks" : null, [tab]);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const sweep = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post("/students/interventions"));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="Accountability" subtitle={me.user.role === "coach" ? "Your students, by risk." : "Every student by rule-based risk: check-in gap, outreach volume, show rate, mood and progress."}
        right={<Button tone="primary" onClick={sweep} loading={busy}>Run interventions</Button>} />
      <ErrorBox error={err} />
      {res && (
        <Card className="mb-4" title="Intervention sweep" actions={<TraceLink runId={res.run_id} />}>
          <p className="text-sm">{res.at_risk} at-risk students · {res.nudges.length} nudges queued · {res.tasks.length} coach escalations.</p>
          <Button size="sm" className="mt-2" onClick={() => go(`/queue?ids=${res.nudges.join(",")}`)}>Review nudges</Button>
        </Card>
      )}
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "board", label: "Risk board" }, { key: "star", label: "Retention Star" }, { key: "tasks", label: "Coach tasks" }, { key: "digest", label: "Owner digest" }]} />
      {tab === "board" && (
        <>
          {data && (
            <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="Students" value={data.total} demo={me.is_demo} />
              <Stat label="High risk" value={data.counts.high || 0} tone="bad" />
              <Stat label="Medium risk" value={data.counts.medium || 0} tone="warn" />
              <Stat label="Low risk" value={data.counts.low || 0} tone="good" hint={`Board computed in ${data.ms} ms`} />
            </div>
          )}
          <Card pad={false}><div className="p-3">
            {loading && <Loading rows={8} />}
            <ErrorBox error={error} retry={reload} />
            {data && <Table rows={data.students.slice(0, 300)} empty="No students yet" cols={[
              { key: "name", label: "Student", render: (s) => <span className="flex items-center gap-1.5">{s.name.replace(" (DEMO)", "")} {isDemo(s.name) && <DemoBadge />}</span> },
              { key: "level", label: "Risk", render: (s) => <Badge tone={LEVEL[s.level]} className="tabular-nums">{humanize(s.level)} · {s.risk}</Badge> },
              { key: "trend", label: "Risk trend", render: (s) => <Trend t={s.trend} /> },
              { key: "signals", label: "Signals", render: (s) => <span className="text-[12.5px] text-muted">{s.signals.map((x: any) => x.label).join(" · ") || "—"}</span> },
            ]} />}
          </div></Card>
        </>
      )}
      {tab === "star" && <RetentionStar />}
      {tab === "tasks" && (
        <Card title="Open coach tasks">{tasks.loading && <Loading />}<ErrorBox error={tasks.error} retry={tasks.reload} />
          {tasks.data && <Table rows={tasks.data.items} empty="No open tasks" cols={[{ key: "student", label: "Student" }, { key: "kind", label: "Kind", render: (t) => humanize(t.kind) }, { key: "detail", label: "Detail" }, { key: "due", label: "Due", render: (t) => new Date(t.due * 1000).toLocaleDateString() }]} />}
        </Card>
      )}
      {tab === "digest" && (
        <Card title="Owner digest" actions={digest.data && <CopyButton text={digest.data.text} />}>{digest.loading && <Loading />}<ErrorBox error={digest.error} retry={digest.reload} />
          {digest.data && (<><p className="text-sm">{digest.data.text}</p>
            <Table rows={Object.entries(digest.data.per_coach).map(([k, v]: any) => ({ coach: k, ...v }))} cols={[{ key: "coach", label: "Coach" }, { key: "students", label: "Students" }, { key: "high", label: "High" }, { key: "medium", label: "Medium" }]} /></>)}
        </Card>
      )}
    </div>
  );
}
