import { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, ErrorBox, Field, Input, Loading, PageHeader, Table, TextArea, TraceLink, useApi, useApp } from "../components/ui";

const SAMPLE = `Rep: Thanks for jumping on. What's your goal for the next 90 days?
Prospect: More patients. Right now we get about ten a month.
Rep: What have you tried so far?
Prospect: Discounts mostly.
Rep: So the gap is between ten and the thirty you want.
Prospect: Honestly it's too expensive and I need to think about it.`;

export default function Calls() {
  const { me } = useApp();
  const { data, error, loading, reload } = useApi<any>("/calls");
  const [f, setF] = useState({ title: me.is_demo ? "Discovery call (DEMO)" : "", rep: me.user.display, parties: "Rep, Prospect", transcript: "" });
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
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
      <PageHeader title="Call review" subtitle="Consent first: nothing is processed until every party's consent is recorded. Then stage scores with citations, the biggest leak, rep trends and objection drills." />
      <ErrorBox error={err || error} retry={reload} />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Add a call" actions={<Button size="sm" onClick={() => setF({ ...f, transcript: SAMPLE })}>Sample</Button>}>
          <div className="grid gap-2 sm:grid-cols-3">
            <Field label="Title"><Input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
            <Field label="Rep"><Input value={f.rep} onChange={(e) => setF({ ...f, rep: e.target.value })} /></Field>
            <Field label="Parties (comma separated)"><Input value={f.parties} onChange={(e) => setF({ ...f, parties: e.target.value })} /></Field>
          </div>
          <Field label="Transcript"><TextArea rows={8} value={f.transcript} onChange={(e) => setF({ ...f, transcript: e.target.value })} /></Field>
          <Button className="mt-2" tone="primary" loading={busy === "add"} disabled={!f.title || f.transcript.length < 10}
            onClick={() => act("add", () => post("/calls", { ...f, parties: f.parties.split(",").map((s) => s.trim()).filter(Boolean) }))}>Save call</Button>
        </Card>
        <Card title="Calls">
          {loading && <Loading />}
          {data && <Table rows={data.items} empty="No calls yet" cols={[
            { key: "title", label: "Call" }, { key: "rep", label: "Rep" },
            { key: "consent", label: "Consent", render: (c) => c.consent.complete ? <Badge tone="good">all parties</Badge> : (
              <span className="flex flex-wrap gap-1">{c.consent.missing.map((p: string) => <Button key={p} size="sm" onClick={() => act("c" + c.id + p, () => post(`/calls/${c.id}/consent`, { party: p, method: "verbal on recording" }))}><ShieldCheck className="size-3.5" /> {p}</Button>)}</span>) },
            { key: "go", label: "", render: (c) => <Button size="sm" tone="primary" disabled={!c.consent.complete} loading={busy === "r" + c.id} onClick={async () => setRes(await act("r" + c.id, () => post(`/calls/${c.id}/review`)))}>Review</Button> },
          ]} />}
          {data?.trends?.length > 0 && (<><div className="mt-4 mb-1 text-xs font-semibold text-muted">Rep trends</div>
            <Table rows={data.trends} cols={[{ key: "rep", label: "Rep" }, { key: "calls", label: "Calls" }, { key: "latest", label: "Latest" }, { key: "trend", label: "Trend" }, { key: "common_leak", label: "Common leak" }]} /></>)}
        </Card>
      </div>
      {res && (
        <Card className="mt-4" title={`Review: ${res.overall}/100 · biggest leak: ${res.biggest_leak.label}`} actions={<TraceLink runId={res.run_id} />}>
          <div className="grid gap-2 md:grid-cols-5">
            {res.stages.map((s: any) => <div key={s.key} className="rounded-lg border border-line p-2 text-sm"><div className="font-medium">{s.label}</div><div className="text-2xl">{s.score}/10</div>{s.citation && <Citations items={[s.citation]} />}</div>)}
          </div>
          {res.objections.length > 0 && <p className="mt-3 text-sm">Objections: {res.objections.map((o: any) => `${o.objection}${o.handled ? " (handled)" : " (not handled)"}`).join(" · ")}</p>}
          {res.discounted && <p className="mt-1 text-sm text-bad">The rep offered a discount to close — the course says never to.</p>}
          <p className="mt-1 text-xs text-muted">{res.drills.length} objection drill(s) added to the drill bank.</p>
        </Card>
      )}
    </div>
  );
}
