import { useState } from "react";
import { Check, Hammer, RotateCcw, Search, Send, Undo2 } from "lucide-react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, ErrorBox, Field, Input, Loading, PageHeader, Table, Tabs, TraceLink, cx, isDemo, DemoBadge, useApi, useApp } from "../components/ui";

const STEPS = ["Build PAUSED test", "Propose go-live", "Owner approves", "Rollback available"];

export default function AdsOps() {
  const { go, me } = useApp();
  const [tab, setTab] = useState("account");
  const acct = useApi<any>("/ads");
  const hist = useApi<any>(tab === "actions" ? "/actions" : null, [tab]);
  const [diag, setDiag] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [target, setTarget] = useState("10");
  const [flow, setFlow] = useState<{ step: number; campaign?: string; action?: any; build?: any }>({ step: 0 });
  const act = async (what: string, fn: () => Promise<any>) => {
    setBusy(what);
    setErr(null);
    try {
      const r = await fn();
      acct.reload();
      hist.reload();
      return r;
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const build = async () => {
    const r = await act("build", () => post("/ads/build-test", { name: `Thunder Dome ${new Date().toISOString().slice(5, 16)}${me.is_demo ? " (DEMO)" : ""}`, creatives: ["Hook A: stop discounting", "Hook B: referral nights", "Hook C: the Monday number"] }));
    if (r) setFlow({ step: 1, campaign: r.results[0].created[0], build: r });
  };
  const proposeLive = async () => {
    const r = await act("live", () => post("/ads/go-live", { campaign_id: flow.campaign, daily_budget: 30 }));
    if (r) setFlow({ ...flow, step: 2, action: r });
  };
  return (
    <div>
      <PageHeader title="Ads Ops" subtitle="Diagnose (read-only), propose changes (approvals only), build new tests (PAUSED, zero budget). Mock adapter by default; the real Meta client is coded and switched off."
        right={<Badge tone={acct.data?.adapter === "mock" ? "info" : "warn"}>adapter: {acct.data?.adapter || "…"}</Badge>} />
      <ErrorBox error={err || acct.error} retry={acct.reload} />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "account", label: "Practice account" }, { key: "golive", label: "Practice go-live" }, { key: "actions", label: "Actions & rollback" }]} />
      {tab === "account" && (
        <div className="space-y-4">
          <Card title={<span className="flex items-center gap-2">{acct.data?.account} {isDemo(acct.data?.account) && <DemoBadge />}</span>}
            actions={<>
              <Field label="Target CPL"><Input className="w-24" value={target} onChange={(e) => setTarget(e.target.value)} /></Field>
              <Button onClick={async () => setDiag(await act("diag", () => post("/ads/diagnose", { target_cpl: Number(target) })))} loading={busy === "diag"}><Search className="size-4" /> Diagnose</Button>
              <Button onClick={async () => { const r = await act("prop", () => post("/ads/propose", { target_cpl: Number(target) })); if (r?.proposals?.length) go(`/queue?ids=${r.proposals.map((p: any) => p.queue_id).join(",")}`); }} loading={busy === "prop"}><Send className="size-4" /> Propose changes</Button>
              <Button onClick={build} loading={busy === "build"}><Hammer className="size-4" /> Build new test</Button>
              {me.user.role === "owner" && <Button tone="danger" onClick={() => act("reset", () => post("/ads/reset"))} loading={busy === "reset"}><RotateCcw className="size-4" /> Reset practice account</Button>}
            </>}>
            {acct.loading && <Loading />}
            {acct.data && <Table rows={acct.data.tree} empty="No ads in the practice account yet. Build a test." cols={[
              { key: "name", label: "Ad", render: (a) => <span className="flex items-center gap-1.5">{a.name} {isDemo(a.name) && <DemoBadge />}</span> },
              { key: "campaign", label: "Campaign", render: (a) => a.campaign.name },
              { key: "status", label: "Status", render: (a) => <Badge tone={a.status === "ACTIVE" ? "good" : "neutral"}>{a.status}</Badge> },
              { key: "spend", label: "Spend", render: (a) => (a.metrics.spend ?? "—") },
              { key: "leads", label: "Leads", render: (a) => (a.metrics.leads ?? "—") },
            ]} />}
          </Card>
          {diag && (
            <Card title="Diagnosis (read-only — wrote nothing)" actions={<TraceLink runId={diag.run_id} />}>
              <Table rows={diag.ads} cols={[
                { key: "name", label: "Ad" }, { key: "status", label: "Status" }, { key: "spend", label: "Spend" }, { key: "leads", label: "Leads" },
                { key: "cpl", label: "CPL", render: (r) => r.cpl ?? "no leads" }, { key: "ctr", label: "CTR", render: (r) => `${(r.ctr * 100).toFixed(2)}%` },
                { key: "kill", label: "", render: (r) => r.kill ? <Badge tone="bad">kill rule</Badge> : diag.winner?.id === r.id ? <Badge tone="good">winner</Badge> : null },
              ]} />
              <Citations items={diag.citations} />
            </Card>
          )}
        </div>
      )}
      {tab === "golive" && (
        <Card title="Practice go-live (4 steps)" subtitle="Everything runs on the practice (mock) account. Activation and budget need an owner approval.">
          <ol className="mb-4 grid gap-2 md:grid-cols-4">
            {STEPS.map((s, i) => (
              <li key={s} className={cx("rounded-lg border p-3 text-sm", flow.step > i ? "border-green-300 bg-green-50" : flow.step === i ? "border-brand" : "border-line")}>
                <div className="text-xs text-muted">Step {i + 1}</div><div className="font-medium">{s}</div>
              </li>
            ))}
          </ol>
          <div className="flex flex-wrap gap-2">
            {flow.step === 0 && <Button tone="primary" onClick={build} loading={busy === "build"}><Hammer className="size-4" /> 1. Build PAUSED test</Button>}
            {flow.step === 1 && <Button tone="primary" onClick={proposeLive} loading={busy === "live"}><Send className="size-4" /> 2. Propose go-live (30/day)</Button>}
            {flow.step === 2 && (
              <>
                <Button tone="good" onClick={async () => { const r = await act("appr", () => post(`/queue/${flow.action.queue_id}/approve`)); if (r) setFlow({ ...flow, step: 3 }); }} loading={busy === "appr"}><Check className="size-4" /> 3. Approve as owner</Button>
                <Button onClick={() => go(`/queue?ids=${flow.action.queue_id}`)}>Open in queue</Button>
              </>
            )}
            {flow.step === 3 && <Button onClick={async () => { const r = await act("rb", () => post(`/actions/${flow.action.action_id}/rollback`)); if (r) setFlow({ step: 4 }); }} loading={busy === "rb"}><Undo2 className="size-4" /> 4. Roll back</Button>}
            {flow.step === 4 && <p className="text-sm text-good">Rolled back to PAUSED. History is in Actions and the Audit log.</p>}
          </div>
        </Card>
      )}
      {tab === "actions" && (
        <Card title="Actions">
          {hist.loading && <Loading />}
          {hist.data && <Table rows={hist.data.items} empty="No actions yet" cols={[
            { key: "id", label: "#" }, { key: "kind", label: "Kind" }, { key: "summary", label: "What" },
            { key: "status", label: "Status", render: (a) => <Badge tone={a.status === "done" ? "good" : a.status === "failed" || a.status === "failed_partial" ? "bad" : a.status === "rolled_back" ? "warn" : "info"}>{a.status === "failed_partial" ? "partly applied" : a.status}</Badge> },
            { key: "rb", label: "", render: (a) => me.user.role === "owner" && ["done", "failed", "failed_partial"].includes(a.status) && !a.rollback_of ? <Button size="sm" onClick={() => act("rb" + a.id, () => post(`/actions/${a.id}/rollback`))} loading={busy === "rb" + a.id}><Undo2 className="size-3.5" /> Rollback</Button> : null },
          ]} />}
        </Card>
      )}
    </div>
  );
}
