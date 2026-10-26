import { useState } from "react";
import { Badge, Card, ErrorBox, Loading, PageHeader, Select, Stat, StatusBadge, Table, useApi } from "../components/ui";

const SECTIONS: Record<string, string> = { A: "Parity", B: "Beyond parity", C: "Guardrails & safety", D: "Platform & ops", E: "Quality" };

export default function GoLive() {
  const { data, error, loading, reload } = useApi<any>("/golive");
  const [sec, setSec] = useState("all");
  const [st, setSt] = useState("all");
  const rows = (data?.rows || []).filter((r: any) => (sec === "all" || r.id[0] === sec) && (st === "all" || r.status === st));
  return (
    <div>
      <PageHeader title="Go-live scorecard" subtitle="Every acceptance row with its honest status, the test that proves it and the exact key or account it's waiting for."
        right={<><Select value={sec} onChange={(e) => setSec(e.target.value)} options={[{ value: "all", label: "All sections" }, ...Object.entries(SECTIONS).map(([k, v]) => ({ value: k, label: `${k} · ${v}` }))]} aria-label="Section" />
          <Select value={st} onChange={(e) => setSt(e.target.value)} options={[{ value: "all", label: "All statuses" }, "REAL_OK", "NEEDS_LOGIN", "NEEDS_AI_BUDGET"]} aria-label="Status" /></>} />
      <ErrorBox error={error} retry={reload} />
      {loading && <Loading rows={8} />}
      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
            {Object.entries(data.totals).map(([k, v]: any) => <Stat key={k} label={`${k} · ${SECTIONS[k]}`} value={`${v.score}/${v.max}`} hint={`${v.rows} rows`} />)}
          </div>
          <div className="mb-3 flex flex-wrap gap-2 text-xs">{Object.entries(data.labels).map(([k, v]: any) => <span key={k} className="flex items-center gap-1"><StatusBadge s={k} /> {v}</span>)}</div>
          <Card pad={false}><div className="p-3"><Table rows={rows} cols={[
            { key: "id", label: "ID", className: "font-mono text-xs" },
            { key: "item", label: "Item" },
            { key: "score", label: "Score", render: (r) => <Badge tone={r.score === 2 ? "good" : r.score === 1 ? "warn" : "bad"}>{r.score}/2</Badge> },
            { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> },
            { key: "evidence", label: "Test", render: (r) => <div className="space-y-0.5">{r.evidence.map((e: string) => <code key={e} className="block break-all text-[10px] text-muted">{e}</code>)}</div> },
            { key: "waiting_for", label: "Waiting for", render: (r) => <span className="text-xs">{r.missing?.length ? r.missing.join(", ") : r.waiting_for || "—"}</span> },
          ]} /></div></Card>
        </>
      )}
    </div>
  );
}
