import { useState } from "react";
import { Badge, Card, ErrorBox, Loading, PageHeader, Select, Table, useApi } from "../components/ui";

const KINDS = ["", "login", "queue", "guardrail", "action", "settings", "switch", "vault", "kb", "demo", "payment", "consent", "mcp"];

export default function Audit() {
  const [kind, setKind] = useState("");
  const { data, error, loading, reload } = useApi<any>(`/audit?limit=500${kind ? `&kind=${kind}` : ""}`, [kind]);
  return (
    <div>
      <PageHeader title="Audit log" subtitle="Logins, queueing, guardrail blocks, approvals, actions, rollbacks, settings, switch flips, vault saves (field names only) and KB scope changes." right={<Select value={kind} onChange={(e) => setKind(e.target.value)} options={KINDS.map((k) => ({ value: k, label: k || "All kinds" }))} aria-label="Kind" />} />
      <Card pad={false}>
        <div className="p-3">
          {loading && <Loading rows={8} />}
          <ErrorBox error={error} retry={reload} />
          {data && (
            <Table rows={data.items} empty="No audit entries" cols={[
              { key: "at", label: "When", render: (r) => new Date(r.at * 1000).toLocaleString(), className: "whitespace-nowrap" },
              { key: "actor", label: "Actor" },
              { key: "kind", label: "Kind", render: (r) => <Badge tone={r.kind.startsWith("guardrail") ? "bad" : r.kind === "switch" ? "warn" : "neutral"}>{r.kind}</Badge> },
              { key: "detail", label: "Detail", render: (r) => <code className="break-all text-xs">{r.detail}</code> },
            ]} />
          )}
        </div>
      </Card>
    </div>
  );
}
