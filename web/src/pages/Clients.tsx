import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, CopyButton, DemoBadge, ErrorBox, Loading, PageHeader, Table, TraceLink, isDemo, useApi, useApp } from "../components/ui";

export default function Clients() {
  const { go } = useApp();
  const { data, error, loading, reload } = useApi<any>("/clients");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const fulfil = async (id: number) => {
    setBusy(id);
    setErr(null);
    try {
      setRes(await post(`/clients/${id}/fulfil`));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div>
      <PageHeader title="Client fulfilment" subtitle="Money model, bottleneck against SOP thresholds, cited launch plan, reviewed ad hooks, VSL outline and client report — all queued and guardrail-checked." />
      <Card pad={false} className="mb-4"><div className="p-3">
        {loading && <Loading />}
        <ErrorBox error={error || err} retry={reload} />
        {data && <Table rows={data.items} empty="No clients yet" cols={[
          { key: "name", label: "Client", render: (c) => <span className="flex items-center gap-1.5">{c.name.replace(" (DEMO)", "")} {isDemo(c.name) && <DemoBadge />}</span> },
          { key: "niche", label: "Niche" },
          { key: "price", label: "Offer price", render: (c) => Number(c.price).toLocaleString() },
          { key: "go", label: "", render: (c) => <Button size="sm" tone="primary" loading={busy === c.id} onClick={() => fulfil(c.id)}>Run fulfilment</Button> },
        ]} />}
      </div></Card>
      {res && (
        <div className="grid gap-4 xl:grid-cols-2" data-testid="fulfilment">
          <Card title={`Money model — ${res.client.name}`} actions={<TraceLink runId={res.run_id} />}>
            <Table rows={res.money_model.scenarios} cols={[{ key: "name", label: "Scenario" }, { key: "leads", label: "Leads" }, { key: "sales", label: "Sales" }, { key: "revenue", label: "Revenue", render: (s) => Number(s.revenue).toLocaleString() }, { key: "assumptions", label: "Assumption" }]} />
            <ul className="mt-2 list-disc pl-5 text-xs text-muted">{res.money_model.labels.map((l: string) => <li key={l}>{l}</li>)}</ul>
          </Card>
          <Card title="Bottleneck">
            {res.analysis.bottleneck ? <p className="text-sm"><b>{res.analysis.bottleneck.label}</b>: {res.analysis.bottleneck.kind === "rate" ? `${(res.analysis.bottleneck.value * 100).toFixed(1)}% vs ${(res.analysis.bottleneck.benchmark * 100).toFixed(0)}%` : res.analysis.bottleneck.value} · ≈ {Number(res.analysis.bottleneck.monthly_cost).toLocaleString()}/month</p> : <p className="text-sm">No bottleneck with enough data.</p>}
          </Card>
          <Card title="Launch plan">
            <ol className="space-y-2 text-sm">{res.launch_plan.map((p: any) => <li key={p.week}><b>{p.week}:</b> {p.step}{p.citation && <Citations items={[p.citation]} />}</li>)}</ol>
          </Card>
          <Card title="Ad hooks (reviewed)" subtitle={res.hooks.regenerated_once ? "Weak hooks were regenerated once" : "All hooks passed first time"}>
            <ul className="space-y-1 text-sm">{res.hooks.hooks.map((h: any) => <li key={h.hook} className="flex items-center justify-between gap-2"><span>{h.hook}</span><Badge tone={h.score >= 60 ? "good" : "warn"}>{h.score}</Badge></li>)}</ul>
          </Card>
          <Card title="Client report" actions={<CopyButton text={res.report} />}>
            <pre className="prose-pre text-sm">{res.report}</pre>
            <Button size="sm" className="mt-2" onClick={() => go(`/queue?ids=${res.queue_ids.join(",")}`)}>Review {res.queue_ids.length} queued items</Button>
          </Card>
          <Card title="VSL outline"><ol className="list-decimal pl-5 text-sm">{res.vsl_outline.map((v: string) => <li key={v}>{v}</li>)}</ol></Card>
        </div>
      )}
    </div>
  );
}
