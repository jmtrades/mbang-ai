import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, ErrorBox, Loading, PageHeader, Table, useApi } from "../components/ui";

export default function Drive() {
  const { data, error, loading, reload } = useApi<any>("/drive");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const act = async (k: string, fn: () => Promise<any>) => {
    setBusy(k);
    setErr(null);
    try {
      await fn();
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div>
      <PageHeader title="Source sync" subtitle="Local Drive inbox folder: list → diff → fetch → guardrail screen → staged for your OK. Approved files join the knowledge base (inputs/ is never modified)."
        right={<Button tone="primary" onClick={() => act("sync", () => post("/drive/sync"))} loading={busy === "sync"}>Fetch & screen</Button>} />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading />}
      {data && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title="Inbox" subtitle={<>Drop files into <code>{data.inbox}</code></>}>
            <Table rows={data.files} empty="The inbox is empty" cols={[{ key: "filename", label: "File" }, { key: "size", label: "Bytes" }, { key: "state", label: "Diff", render: (f) => <Badge tone={f.state === "unchanged" ? "neutral" : "info"}>{f.state}</Badge> }]} />
          </Card>
          <Card title="Staged for your OK">
            <Table rows={data.staged} empty="Nothing staged" cols={[
              { key: "filename", label: "File" },
              { key: "flags", label: "Screen", render: (s) => s.flags.length ? <span className="text-xs text-warn">{s.flags.map((f: any) => `L${f.line}: ${f.label}`).join("; ")}</span> : <Badge tone="good">clean</Badge> },
              { key: "status", label: "Status", render: (s) => <Badge tone={s.status === "approved" ? "good" : s.status === "rejected" ? "bad" : "warn"}>{s.status}</Badge> },
              { key: "x", label: "", render: (s) => s.status === "staged" && <span className="flex gap-1"><Button size="sm" tone="good" onClick={() => act("a" + s.id, () => post(`/drive/staged/${s.id}`, { approve: true }))}>Approve</Button><Button size="sm" tone="danger" onClick={() => act("r" + s.id, () => post(`/drive/staged/${s.id}`, { approve: false }))}>Reject</Button></span> },
            ]} />
          </Card>
        </div>
      )}
    </div>
  );
}
