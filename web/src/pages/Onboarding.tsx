import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, ErrorBox, Field, Input, PageHeader, Select, TextArea, TraceLink, useApp } from "../components/ui";

const SKILLS = [["offer", "Offer & USP"], ["dms", "DM setting"], ["sales", "Closing calls"], ["ads", "Ads testing"], ["webinars", "Webinars"], ["show_rate", "Show rate"]];

export default function Onboarding() {
  const { me, go } = useApp();
  const [f, setF] = useState<any>({ name: me.is_demo ? "Freya Nakamura (DEMO)" : "", email: me.is_demo ? "freya.nakamura@example.invalid" : "", niche: "", usp: "", clients: 0, goals: "", skills: { offer: 2, dms: 3, sales: 3, ads: 1, webinars: 2, show_rate: 3 } });
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post("/onboarding", { ...f, clients: Number(f.clients) || 0 }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="Onboarding" subtitle="Intake → gap analysis → cited lessons per gap → 30/60/90-day plan → coach assignment → milestones → welcome and kickoff drafted for approval." />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Intake">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
            <Field label="Email"><Input value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
            <Field label="Niche"><Input value={f.niche} onChange={(e) => setF({ ...f, niche: e.target.value })} placeholder="e.g. physio clinics" /></Field>
            <Field label="Clients today"><Input inputMode="numeric" value={f.clients} onChange={(e) => setF({ ...f, clients: e.target.value })} /></Field>
            <div className="sm:col-span-2"><Field label="USP (if written)"><Input value={f.usp} onChange={(e) => setF({ ...f, usp: e.target.value })} /></Field></div>
            <div className="sm:col-span-2"><Field label="Goals"><TextArea rows={2} value={f.goals} onChange={(e) => setF({ ...f, goals: e.target.value })} /></Field></div>
            {SKILLS.map(([k, label]) => (
              <Field key={k} label={`${label} (1-5)`}><Select value={String(f.skills[k])} onChange={(e) => setF({ ...f, skills: { ...f.skills, [k]: Number(e.target.value) } })} options={["1", "2", "3", "4", "5"]} /></Field>
            ))}
          </div>
          <Button tone="primary" className="mt-3" onClick={run} loading={busy} disabled={!f.name}>Onboard</Button>
          <ErrorBox error={err} retry={run} />
        </Card>
        <div className="space-y-4">
          {!res && <Card><p className="text-sm text-muted">The gap analysis and plan appear here.</p></Card>}
          {res && (
            <>
              <Card title="Gaps & lessons" actions={<TraceLink runId={res.run_id} />}>
                {res.gaps.map((g: any) => (
                  <div key={g.key} className="mb-3">
                    <div className="flex items-center gap-2 font-medium">{g.label} <Badge tone="warn">{g.why}</Badge></div>
                    <Citations items={g.lessons} />
                  </div>
                ))}
              </Card>
              <Card title="30 / 60 / 90">
                <div className="grid gap-3 md:grid-cols-3">
                  {["30", "60", "90"].map((d) => (
                    <div key={d}><div className="mb-1 text-xs font-semibold text-muted">Day {d}</div><ul className="list-disc space-y-1 pl-4 text-sm">{res.plan[d].map((x: string) => <li key={x}>{x}</li>)}</ul></div>
                  ))}
                </div>
                <p className="mt-3 text-sm">Coach: <b>{res.coach?.display || "unassigned"}</b> · {res.tasks.length} milestone tasks</p>
                <Button size="sm" className="mt-2" onClick={() => go(`/queue?ids=${res.queue_ids.join(",")}`)}>Review welcome & kickoff</Button>
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
