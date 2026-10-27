import { AiModeBadge, Badge, Card, ErrorBox, Loading, PageHeader, Table, ago, humanize, runTitle, useApi, useApp } from "../components/ui";

export default function Runs() {
  const { openTrace } = useApp();
  const { data, error, loading, reload } = useApi<any>("/runs?limit=200");
  return (
    <div>
      <PageHeader title="Runs & traces" subtitle="Every agent run: its plan, each tool call (input, output, ms, status) and the result. Click a run to open its trace." />
      <Card pad={false}>
        <div className="p-3">
          {loading && <Loading rows={6} />}
          <ErrorBox error={error} retry={reload} />
          {data && (
            <Table rows={data.runs} onRow={(r) => openTrace(r.id)} empty="No runs yet" cols={[
              { key: "id", label: "#", className: "tabular-nums text-muted" },
              { key: "agent", label: "Agent", render: (r) => <span className="text-muted">{humanize(r.agent)}</span> },
              { key: "title", label: "Title", className: "max-w-[520px] truncate text-ink", render: (r) => runTitle(r) || r.title },
              { key: "ai_mode", label: "Mode", render: (r) => <AiModeBadge mode={r.ai_mode} /> },
              { key: "status", label: "Status", render: (r) => <Badge tone={r.status === "done" ? "good" : r.status === "error" ? "bad" : "info"} dot>{humanize(r.status)}</Badge> },
              { key: "ms", label: "Time", className: "text-right tabular-nums text-muted", render: (r) => (r.ms != null ? `${r.ms} ms` : "—") },
              { key: "created", label: "When", className: "text-muted", render: (r) => <span title={new Date(r.created * 1000).toLocaleString()}>{ago(r.created)}</span> },
            ]} />
          )}
        </div>
      </Card>
    </div>
  );
}
