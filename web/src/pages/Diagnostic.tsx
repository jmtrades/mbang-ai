import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { ApiError, post } from "../api";
import { AiModeBadge, Badge, Button, Card, Citations, ErrorBox, Field, Input, PageHeader, Select, Table, TraceLink, cx, useApi, useApp, DemoBadge } from "../components/ui";

const FIELDS = [
  ["dms_sent", "DMs sent"], ["replies", "Replies"], ["conversations", "Quality conversations"], ["links_sent", "Links sent"],
  ["booked", "Calls booked"], ["showed", "Calls showed"], ["closed", "Closed"], ["offer_price", "Offer price"],
  ["ad_spend", "Ad spend"], ["leads", "Leads"], ["visitors", "Landing visitors"], ["registrants", "Webinar registrants"],
  ["attendees", "Webinar attendees"], ["webinar_sales", "Webinar sales"],
];
const SAMPLE: Record<string, number> = { dms_sent: 800, replies: 40, conversations: 20, links_sent: 10, booked: 6, showed: 2, closed: 1, offer_price: 3000 };

export default function Diagnostic() {
  const { go } = useApp();
  const clients = useApi<any>("/clients");
  const [m, setM] = useState<Record<string, string>>({});
  const [client, setClient] = useState("");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      const metrics: Record<string, number> = {};
      Object.entries(m).forEach(([k, v]) => { if (v !== "" && !isNaN(Number(v))) metrics[k] = Number(v); });
      setRes(await post("/diagnose", { metrics, client_id: client ? Number(client) : null }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const fmt = (s: any) => (s.kind === "rate" ? `${(s.value * 100).toFixed(1)}%` : `$${s.value}`);
  const fb = (s: any) => (s.kind === "rate" ? `${(s.benchmark * 100).toFixed(0)}%` : `$${s.benchmark}`);
  return (
    <div>
      <PageHeader title="Bottleneck diagnosis" subtitle="Pure maths first: every stage against the SOP benchmark, the money cost of not fixing it, and the one bottleneck worth fixing now." />
      <div className="grid gap-4 xl:grid-cols-[380px_1fr]">
        <Card title="Numbers" actions={<Button size="sm" onClick={() => setM(Object.fromEntries(Object.entries(SAMPLE).map(([k, v]) => [k, String(v)])))}>Sample</Button>}>
          <div className="space-y-3">
            {clients.data?.items?.length > 0 && (
              <Field label="Or use a client's numbers">
                <Select value={client} onChange={(e) => setClient(e.target.value)} options={[{ value: "", label: "— none —" }, ...clients.data.items.map((c: any) => ({ value: String(c.id), label: c.name }))]} aria-label="Client" />
              </Field>
            )}
            <div className="grid grid-cols-2 gap-2">
              {FIELDS.map(([k, label]) => (
                <Field key={k} label={label}><Input inputMode="decimal" value={m[k] ?? ""} onChange={(e) => setM({ ...m, [k]: e.target.value })} aria-label={label} /></Field>
              ))}
            </div>
            <Button tone="primary" onClick={run} loading={busy}>Diagnose</Button>
            <ErrorBox error={err} retry={run} />
          </div>
        </Card>
        <div className="space-y-4">
          {!res && <Card><p className="text-sm text-muted">Enter your pipeline numbers (or load the sample) and run the diagnosis.</p></Card>}
          {res && (
            <>
              <Card title={res.bottleneck ? `Bottleneck: ${res.bottleneck.label}` : "No bottleneck"} actions={<><AiModeBadge mode={res.mode} /><TraceLink runId={res.run_id} /></>}>
                {res.bottleneck && <p className="mb-2 text-2xl font-semibold text-bad">≈ {Number(res.bottleneck.monthly_cost).toLocaleString()} / month <span className="text-sm font-normal text-muted">cost of not fixing</span></p>}
                <p className="text-sm">{res.prescription}</p>
                {res.assumption && <p className="mt-1 text-xs text-muted">{res.assumption} <DemoBadge /></p>}
                <Citations items={res.citations} />
                {res.next_best_action && (
                  <Button tone="primary" className="mt-3" onClick={() => go(`/studio?line=${res.next_best_action.line}&brief=${encodeURIComponent(res.next_best_action.brief)}`)} data-testid="next-best-action">
                    Next best action: open Studio ({res.next_best_action.line}) <ArrowRight className="size-4" />
                  </Button>
                )}
              </Card>
              <Card title="Every stage">
                <Table rows={res.stages} cols={[
                  { key: "label", label: "Stage" },
                  { key: "value", label: "Yours", render: (s) => <span className={cx(s.below && "font-semibold text-bad")}>{fmt(s)}</span> },
                  { key: "benchmark", label: "SOP benchmark", render: (s) => fb(s) },
                  { key: "monthly_cost", label: "Cost / month", render: (s) => s.monthly_cost ? Number(s.monthly_cost).toLocaleString() : "—" },
                  { key: "realistic", label: "", render: (s) => (!s.realistic ? <Badge>low volume</Badge> : s.key === res.bottleneck?.key ? <Badge tone="bad">bottleneck</Badge> : null) },
                  { key: "citation", label: "Source", render: (s) => s.citation ? <Citations items={[s.citation]} /> : null },
                ]} />
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
