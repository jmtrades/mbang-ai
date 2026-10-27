import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, DemoBadge, ErrorBox, Input, Loading, PageHeader, Select, Table, isDemo, useApi, useApp } from "../components/ui";

export default function Crm() {
  const { toast, go } = useApp();
  const { data, error, loading, reload } = useApi<any>("/crm");
  const [name, setName] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const move = async (id: number, stage: string) => {
    setErr(null);
    try {
      const r = await post(`/crm/contacts/${id}/move`, { stage });
      if (r.status === "needs_approval") {
        toast("Terminal move queued for owner approval");
        go(`/queue?ids=${r.queue_id}`);
      }
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="CRM" badge={<Badge tone="neutral">Mock</Badge>} subtitle="A GoHighLevel-style pipeline. Ordinary moves apply directly; closed-won and closed-lost need the owner's approval and can be rolled back." />
      <ErrorBox error={err || error} retry={reload} />
      <div className="mb-3 flex gap-2"><Input className="max-w-xs" placeholder="New contact name" value={name} onChange={(e) => setName(e.target.value)} />
        <Button onClick={async () => { await post("/crm/contacts", { name }); setName(""); reload(); }} disabled={!name}>Add contact</Button></div>
      <Card pad={false}><div className="p-3">
        {loading && <Loading />}
        {data && <Table rows={data.contacts} empty="No contacts" cols={[
          { key: "name", label: "Contact", render: (c) => <span className="flex items-center gap-1.5">{c.name.replace(" (DEMO)", "")} {isDemo(c.name) && <DemoBadge />}</span> },
          { key: "email", label: "Email" },
          { key: "stage", label: "Stage", render: (c) => <Badge tone={c.stage.startsWith("closed") ? "good" : "neutral"}>{c.stage}</Badge> },
          { key: "move", label: "Move to", render: (c) => <Select value="" onChange={(e) => e.target.value && move(c.id, e.target.value)} options={[{ value: "", label: "…" }, ...data.stages.filter((s: string) => s !== c.stage).map((s: string) => ({ value: s, label: data.terminal.includes(s) ? `${s} (needs approval)` : s }))]} aria-label="Move stage" /> },
        ]} />}
      </div></Card>
    </div>
  );
}
