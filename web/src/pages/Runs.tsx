import { AiModeBadge, Badge, Card, ErrorBox, Loading, PageHeader, Table, useApi, useApp } from "../components/ui";

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
              { key: "id", label: "#" },
              { key: "agent", label: "Agent", render: (r) => <Badge>{r.agent}</Badge> },
              { key: "title", label: "Title", className: "max-w-[420px] truncate" },
              { key: "ai_mode", label: "Mode", render: (r) => <AiModeBadge mode={r.ai_mode} /> },
              { key: "status", label: "Status", render: (r) => <Badge tone={r.status === "done" ? "good" : r.status === "error" ? "bad" : "info"}>{r.status}</Badge> },
              { key: "ms", label: "ms", className: "tabular-nums" },
              { key: "created", label: "When", render: (r) => new Date(r.created * 1000).toLocaleString() },
            ]} />
          )}
        </div>
      </Card>
    </div>
  );
}
