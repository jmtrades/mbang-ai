import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { ApiError, get, post } from "../api";
import { Badge, Button, Card, Citations, ErrorBox, Field, Loading, PageHeader, Tabs, TextArea, TraceLink, useApi, useApp } from "../components/ui";
import { BriefForm } from "../components/BriefForm";
import Events from "./webinar/Events";

function ChainView({ chainId, onRerun }: { chainId: string; onRerun: (r: any) => void }) {
  const { go } = useApp();
  const [st, setSt] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>("offer");
  const load = () => get(`/webinar/chain/${chainId}`).then(setSt).catch(setErr);
  useEffect(() => { load(); }, [chainId]);
  const rerun = async (step: string) => {
    setBusy(step);
    try {
      const r = await post(`/webinar/chain/${chainId}/rerun`, { step });
      onRerun(r);
      await load();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  if (!st) return <Loading rows={6} />;
  return (
    <Card title={`Chain ${chainId}`} actions={<Button size="sm" onClick={() => go(`/queue?chain_id=${chainId}&status=all`)}>Open in queue</Button>}>
      <ErrorBox error={err} />
      <ol className="space-y-2">
        {st.steps.map((s: any, i: number) => (
          <li key={s.step} className="rounded-lg border border-line">
            <div className="flex flex-wrap items-center gap-2 px-3 py-2">
              <span className="text-xs text-muted">{i + 1}</span>
              <button className="font-medium" onClick={() => setOpen(open === s.step ? null : s.step)}>{s.label}</button>
              <Badge>v{s.version}</Badge>
              {s.deps.length > 0 && <span className="text-[11px] text-muted">← {s.deps.join(", ")}</span>}
              <span className="ml-auto text-xs text-muted">Queue #{s.queue_id}</span>
              <Button size="sm" tone="ghost" loading={busy === s.step} onClick={() => rerun(s.step)} title="Re-run this step and the steps that depend on it"><RefreshCw className="size-3.5" /></Button>
            </div>
            {open === s.step && s.output && (
              <div className="border-t border-line px-3 py-2">
                {s.output.data?.html ? (
                  <iframe title={s.label} sandbox="" srcDoc={s.output.data.html} className="h-72 w-full rounded border border-line bg-white" />
                ) : (
                  <pre className="prose-pre text-sm">{s.output.body}</pre>
                )}
                <Citations items={s.output.citations} />
              </div>
            )}
          </li>
        ))}
      </ol>
    </Card>
  );
}

export default function Webinar() {
  const [tab, setTab] = useState("events");
  const chains = useApi<any>("/webinar/chains");
  const [b, setB] = useState<Record<string, string>>({});
  const [chainId, setChainId] = useState<string | null>(null);
  const [last, setLast] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [script, setScript] = useState("");
  const [reh, setReh] = useState<any>(null);
  useEffect(() => {
    if (!chainId && chains.data?.items?.length) setChainId(chains.data.items[0].chain_id);
  }, [chains.data]);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/webinar/chain", { brief: b });
      setLast(r);
      setChainId(r.chain_id);
      chains.reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const rehearse = async () => {
    setBusy(true);
    try {
      setReh(await post("/webinar/rehearsal", { script }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="Webinars" subtitle="Run real events: a registration page, reminder and replay emails approved once and sent to each registrant on time, attendance, and follow-ups for the people who stayed. The chain drafts the content." />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "events", label: "Events" }, { key: "chain", label: "Content chain" }, { key: "rehearsal", label: "Rehearsal scorer" }]} />
      {tab === "events" && <Events />}
      <ErrorBox error={err} />
      {tab === "chain" && (
        <div className="grid gap-4 xl:grid-cols-[420px_1fr]">
          <Card title="Brief">
            <BriefForm value={b} onChange={setB} />
            <Button tone="primary" className="mt-3" onClick={run} loading={busy}>Run the chain</Button>
            {last && <p className="mt-2 flex items-center gap-2 text-xs text-muted">{last.queue_ids.length} queue items · <TraceLink runId={last.run_id} /></p>}
            {chains.data?.items?.length > 0 && (
              <div className="mt-4"><div className="mb-1 text-xs font-semibold text-muted">Previous chains</div>
                {chains.data.items.map((c: any) => <button key={c.chain_id} className="mr-2 text-xs text-brand underline" onClick={() => setChainId(c.chain_id)}>{c.chain_id}</button>)}
              </div>
            )}
          </Card>
          <div>{chainId ? <ChainView key={chainId} chainId={chainId} onRerun={setLast} /> : <Card><p className="text-sm text-muted">Run a chain to see every linked output.</p></Card>}</div>
        </div>
      )}
      {tab === "rehearsal" && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title="Rehearsal script">
            <Field label="Paste the script with segment timings, e.g. 'Segment 1: hook (5 min)'"><TextArea rows={12} value={script} onChange={(e) => setScript(e.target.value)} /></Field>
            <Button tone="primary" className="mt-3" onClick={rehearse} loading={busy} disabled={script.length < 2}>Score rehearsal</Button>
          </Card>
          {reh && (
            <Card title={`Rehearsal score: ${reh.score}/100`}>
              <ul className="space-y-1 text-sm">{reh.fixes.map((f: any) => <li key={f.key}><b>{f.label}:</b> {f.fix}</li>)}</ul>
              <Citations items={reh.citations} />
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
