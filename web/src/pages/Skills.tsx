import { useState } from "react";
import { Play } from "lucide-react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, ErrorBox, Field, Input, Loading, PageHeader, Select, Table, TraceLink, useApi } from "../components/ui";

export default function Skills() {
  const { data, error, loading, reload } = useApi<any>("/skills");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [w, setW] = useState({ name: "", skill: "dm_opener", trigger: "daily", schedule: "09:00" });
  const act = async (k: string, fn: () => Promise<any>) => {
    setBusy(k);
    setErr(null);
    try {
      const r = await fn();
      reload();
      return r;
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div>
      <PageHeader title="Skills & workers" subtitle="A library of skills that read your business profile. Workers run skills on a schedule or when sources change — drafts only, everything to the queue." />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading rows={6} />}
      {res && (
        <Card className="mb-4" title={`Result: ${res.skill}`} actions={<TraceLink runId={res.run_id} />}>
          {res.status === "needs_login" ? <p className="text-sm text-warn">{res.message}</p> : <pre className="prose-pre text-sm">{res.draft}</pre>}
          {res.queue_id && <p className="mt-1 text-xs text-muted">Queued #{res.queue_id} ({res.queue_status})</p>}
        </Card>
      )}
      {data && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title={`Skills library (${data.skills.length})`}>
            <ul className="space-y-2">
              {data.skills.map((s: any) => (
                <li key={s.key} className="flex items-start justify-between gap-2 rounded-lg border border-line p-2 text-sm">
                  <div><div className="font-medium">{s.name} {s.missing.length > 0 && <Badge tone="warn">NEEDS_LOGIN</Badge>}</div><div className="text-xs text-muted">{s.description}</div></div>
                  <Button size="sm" onClick={async () => setRes(await act(s.key, () => post(`/skills/${s.key}/run`, { inputs: {} })))} loading={busy === s.key}><Play className="size-3.5" /> Run</Button>
                </li>
              ))}
            </ul>
          </Card>
          <div className="space-y-4">
            <Card title="Add a worker">
              <div className="grid gap-2 sm:grid-cols-2">
                <Field label="Name"><Input value={w.name} onChange={(e) => setW({ ...w, name: e.target.value })} /></Field>
                <Field label="Skill"><Select value={w.skill} onChange={(e) => setW({ ...w, skill: e.target.value })} options={data.skills.map((s: any) => ({ value: s.key, label: s.name }))} /></Field>
                <Field label="Trigger"><Select value={w.trigger} onChange={(e) => setW({ ...w, trigger: e.target.value })} options={data.triggers} /></Field>
                <Field label="Time (daily/weekly)"><Input value={w.schedule} onChange={(e) => setW({ ...w, schedule: e.target.value })} /></Field>
              </div>
              <Button className="mt-2" tone="primary" disabled={!w.name} loading={busy === "add"} onClick={() => act("add", () => post("/workers", w))}>Add worker</Button>
            </Card>
            <Card title="Workers">
              <Table rows={data.workers} empty="No workers yet" cols={[
                { key: "name", label: "Worker" }, { key: "skill", label: "Skill" }, { key: "trigger", label: "Trigger" },
                { key: "last_status", label: "Last run", render: (r) => r.missing.length ? <Badge tone="warn">needs login: {r.missing.join(", ")}</Badge> : (r.last_status || "—") },
                { key: "run", label: "", render: (r) => <Button size="sm" onClick={async () => setRes(await act("w" + r.id, () => post(`/workers/${r.id}/run`)))} loading={busy === "w" + r.id}>Run now</Button> },
              ]} />
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
