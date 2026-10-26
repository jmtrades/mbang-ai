import { useState } from "react";
import { Rocket } from "lucide-react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, ErrorBox, PageHeader, TraceLink, cx, useApp } from "../components/ui";
import { BriefForm } from "../components/BriefForm";

const LINES: [string, string, string][] = [
  ["outreach", "Outreach", "Opener, value follow-up, call offer"],
  ["ads", "Ads", "Three ad copies, one CTA each"],
  ["funnels", "Funnels", "Landing page draft"],
  ["creatives", "Creatives", "Thunder Dome creative briefs"],
  ["webinars", "Webinars", "Seven-segment outline"],
  ["training", "Student training", "Objection drill"],
  ["delivery", "Client delivery", "Weekly client report email"],
];

function Drafts({ drafts }: { drafts: any[] }) {
  return (
    <ul className="space-y-2">
      {drafts.map((d) => (
        <li key={d.queue_id} className="rounded-lg border border-line p-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{d.title}</span>
            <Badge tone={d.score >= 70 ? "good" : "warn"}>{d.score}</Badge>
            <Badge tone={d.status === "pending" ? "info" : d.status === "held" ? "bad" : "warn"}>{d.status}</Badge>
            {d.ai && <Badge tone="good">AI</Badge>}
            <span className="text-xs text-muted">Queue #{d.queue_id}</span>
          </div>
          <Citations items={d.citations?.slice(0, 2)} />
        </li>
      ))}
    </ul>
  );
}

export default function Studio({ query }: { query: URLSearchParams }) {
  const { go } = useApp();
  const [line, setLine] = useState(query.get("line") || "outreach");
  const [b, setB] = useState<Record<string, string>>({});
  const [res, setRes] = useState<any>(null);
  const [kit, setKit] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const fromNba = query.get("brief");
  const run = async (kind: "line" | "kit") => {
    setBusy(kind);
    setErr(null);
    try {
      if (kind === "line") setRes(await post("/studio/run", { line, brief: b }));
      else setKit(await post("/studio/launch-kit", { brief: b }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div>
      <PageHeader title="Studio" subtitle="Seven lines of work. Each goes brief → draft → rubric + guardrail review → approval queue. Nothing sends." right={<Button tone="primary" onClick={() => run("kit")} loading={busy === "kit"}><Rocket className="size-4" /> Launch kit (all 7)</Button>} />
      {fromNba && <div className="mb-4 rounded-lg border border-brand/30 bg-[color-mix(in_oklab,var(--brand)_8%,white)] px-3 py-2 text-sm">Next best action: <b>{fromNba}</b></div>}
      <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">
        {LINES.map(([k, label, hint]) => (
          <button key={k} onClick={() => setLine(k)} data-line={k} className={cx("focus-ring card p-3 text-left transition", line === k ? "border-brand ring-2 ring-brand/20" : "hover:border-black/20")}>
            <div className="text-sm font-semibold">{label}</div>
            <div className="text-[11px] text-muted">{hint}</div>
          </button>
        ))}
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Brief" subtitle="Blank fields use your business profile, then sensible defaults.">
          <BriefForm value={b} onChange={setB} />
          <Button tone="primary" className="mt-3" onClick={() => run("line")} loading={busy === "line"}>Draft {LINES.find((l) => l[0] === line)?.[1]}</Button>
          <ErrorBox error={err} />
        </Card>
        <div className="space-y-4">
          {res && (
            <Card title={`${res.line} drafts`} actions={<><Badge tone="good">Sent: {res.sent}</Badge><TraceLink runId={res.run_id} /></>}>
              <Drafts drafts={res.drafts} />
              <Button size="sm" className="mt-3" onClick={() => go(`/queue?ids=${res.queue_ids.join(",")}`)}>Review in the queue</Button>
            </Card>
          )}
          {kit && (
            <Card title={`Launch kit ${kit.kit_id}`} actions={<><Badge tone="good">Sent: {kit.sent}</Badge><TraceLink runId={kit.run_id} /></>}>
              <div className="space-y-3">
                {Object.entries(kit.lines).map(([k, v]: any) => (<div key={k}><div className="mb-1 text-xs font-semibold uppercase text-muted">{k}</div><Drafts drafts={v.drafts} /></div>))}
              </div>
              <Button size="sm" className="mt-3" onClick={() => go(`/queue?ids=${kit.queue_ids.join(",")}`)}>Review {kit.queue_ids.length} drafts</Button>
            </Card>
          )}
          {!res && !kit && <Card><p className="text-sm text-muted">Pick a line and draft it, or run the Launch kit to queue every line from one brief.</p></Card>}
        </div>
      </div>
    </div>
  );
}
