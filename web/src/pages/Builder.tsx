import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Bot, Building2, Play, RotateCcw, Sparkles } from "lucide-react";
import { ApiError, get, post } from "../api";
import { Badge, Button, Card, DemoBadge, ErrorBox, Field, Input, Loading, PageHeader, Run, RunSteps, TextArea, TraceLink, cx, isDemo, useApi, useApp } from "../components/ui";

const HARD_STOP_MS = 90_000;

function useRun(runId: number | null) {
  const [run, setRun] = useState<Run | null>(null);
  const [stopped, setStopped] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    setRun(null);
    setStopped(false);
    setErr(null);
    if (!runId) return;
    let alive = true;
    const t0 = Date.now();
    const tick = async () => {
      try {
        const r = await get<Run>(`/runs/${runId}`);
        if (!alive) return;
        setRun(r);
        if (r.status === "running") {
          if (Date.now() - t0 > HARD_STOP_MS) setStopped(true);
          else setTimeout(tick, 600);
        }
      } catch (e) {
        if (alive) setErr(e as ApiError);
      }
    };
    tick();
    return () => { alive = false; };
  }, [runId]);
  return { run, stopped, err };
}

function OwnForm({ onGenerate, busy }: { onGenerate: (b: any) => void; busy: boolean }) {
  const [f, setF] = useState<any>({ name: "", niche: "", offer: "", price: "", audience: "", outcome: "", mechanism: "", channels: "", faq: "" });
  const fields: [string, string][] = [["name", "Business name"], ["niche", "Niche"], ["offer", "Offer"], ["price", "Price"], ["audience", "Audience"], ["outcome", "Outcome"], ["mechanism", "Mechanism"], ["channels", "Channels"]];
  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-2">
        {fields.map(([k, l]) => <Field key={k} label={l}><Input value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} aria-label={l} /></Field>)}
      </div>
      <Field label="FAQ"><TextArea rows={2} value={f.faq} onChange={(e) => setF({ ...f, faq: e.target.value })} /></Field>
      <Button tone="primary" loading={busy} disabled={!f.name.trim()} onClick={() => onGenerate({ business: { ...f, price: Number(f.price) || 0 } })}>Generate departments</Button>
    </div>
  );
}

export default function Builder({ rest, query }: { rest: string[]; query: URLSearchParams }) {
  const { go } = useApp();
  const presets = useApi<any>("/builder/presets");
  const buildId = rest[0] ? Number(rest[0]) : null;
  const agentKey = rest[1] || null;
  const runIdQ = query.get("run") ? Number(query.get("run")) : null;
  const [picked, setPicked] = useState<string | null>(null);
  const [build, setBuild] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const { run, stopped, err: runErr } = useRun(runIdQ);
  const topRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!buildId) { setBuild(null); return; }
    get(`/builder/builds/${buildId}`).then((b) => setBuild(b.departments ? { build_id: buildId, ...b.departments } : null)).catch(setErr);
  }, [buildId]);

  const generate = async (body: any) => {
    setBusy("gen");
    setErr(null);
    try {
      const r = await post("/builder/generate", body);
      setBuild(r);
      go(`/builder/${r.build_id}`);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const runAgent = async () => {
    if (!buildId || !agentKey) return;
    setBusy("run");
    setErr(null);
    try {
      const r = await post(`/builder/builds/${buildId}/agents/${agentKey}/run`);
      go(`/builder/${buildId}/${agentKey}?run=${r.run_id}`);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const startOver = async () => {
    if (!buildId) return;
    setBusy("over");
    try {
      await post(`/builder/builds/${buildId}/start-over`);
      setBuild(null);
      setPicked(null);
      go("/builder");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };

  const agent = build && agentKey ? build.departments.flatMap((d: any) => d.agents).find((a: any) => a.key === agentKey) : null;
  const result = run?.status === "done" ? run.result : null;

  // ------------------------------------------------------------ picker
  if (!buildId) {
    return (
      <div ref={topRef}>
        <PageHeader title="Build a business's AI team" subtitle="Pick a business. Five departments with two agents each are generated from rules in under a second, then any agent can run on demo data. Nothing sends." />
        <ErrorBox error={err || presets.error} retry={presets.reload} />
        {presets.loading && <Loading rows={3} />}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" data-testid="business-picker">
          {presets.data?.presets.map((p: any) => (
            <button key={p.key} data-preset={p.key} onClick={() => setPicked(p.key)} className={cx("focus-ring card flex flex-col items-start gap-2 p-4 text-left transition", picked === p.key ? "border-brand ring-2 ring-brand/20" : "hover:border-black/20")}>
              <div className="flex w-full items-center justify-between"><Building2 className="size-5 text-brand" /><DemoBadge /></div>
              <div className="font-semibold">{p.name.replace(" (DEMO)", "")}</div>
              <div className="text-xs text-muted">{p.niche} · {p.offer.replace(" (DEMO)", "")}</div>
            </button>
          ))}
          <button data-preset="own" onClick={() => setPicked("own")} className={cx("focus-ring card flex flex-col items-start gap-2 p-4 text-left", picked === "own" ? "border-brand ring-2 ring-brand/20" : "hover:border-black/20")}>
            <Sparkles className="size-5 text-brand" />
            <div className="font-semibold">Your own business</div>
            <div className="text-xs text-muted">Fill in a short profile</div>
          </button>
        </div>
        <div className="mt-4">
          {picked && picked !== "own" && <Button tone="primary" loading={busy === "gen"} onClick={() => generate({ preset: picked })} data-testid="generate">Generate departments</Button>}
          {picked === "own" && <Card title="Your own business"><OwnForm onGenerate={generate} busy={busy === "gen"} /></Card>}
        </div>
      </div>
    );
  }

  if (!build) return <Loading rows={6} />;

  // ------------------------------------------------------------ departments
  if (!agent) {
    return (
      <div>
        <PageHeader title={build.business.name.replace(" (DEMO)", "")} badge={isDemo(build.business.name) ? <DemoBadge /> : null}
          subtitle={<span data-testid="dept-summary"><b>{build.counts.departments} departments · {build.counts.agents} agents</b> · <Badge tone="neutral">{build.generated_by}</Badge></span>}
          right={<Button tone="danger" onClick={startOver} loading={busy === "over"} data-testid="start-over"><RotateCcw className="size-4" /> Start over</Button>} />
        <ErrorBox error={err} />
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="departments">
          {build.departments.map((d: any) => (
            <Card key={d.key} title={d.name} subtitle={<>KPI: {d.kpi}</>}>
              <div className="space-y-2">
                {d.agents.map((a: any) => (
                  <button key={a.key} data-agent={a.key} onClick={() => go(`/builder/${build.build_id}/${a.key}`)} className="focus-ring flex w-full items-start gap-2 rounded-lg border border-line p-2 text-left hover:border-brand">
                    <Bot className="mt-0.5 size-4 shrink-0 text-brand" />
                    <div><div className="text-sm font-medium">{a.name}</div><div className="text-xs text-muted">{a.produces}</div></div>
                  </button>
                ))}
              </div>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------ agent
  return (
    <div>
      <PageHeader title={agent.name} subtitle={`${build.business.name} · ${build.departments.find((d: any) => d.key === agent.department)?.name}`}
        right={<>
          <Button onClick={() => go(`/builder/${build.build_id}`)}><ArrowLeft className="size-4" /> Departments</Button>
          <Button tone="danger" onClick={startOver} loading={busy === "over"} data-testid="start-over"><RotateCcw className="size-4" /> Start over</Button>
        </>} />
      <ErrorBox error={err || runErr} />
      <div className="grid gap-4 xl:grid-cols-[420px_1fr]">
        <Card title="Agent" actions={<Button tone="primary" onClick={runAgent} loading={busy === "run" || run?.status === "running"} data-testid="run-demo"><Play className="size-4" /> Run on demo data</Button>}>
          <dl className="space-y-2 text-sm" data-testid="agent-card">
            <div><dt className="text-xs font-semibold text-muted">Produces</dt><dd>{agent.produces}</dd></div>
            <div><dt className="text-xs font-semibold text-muted">Trigger</dt><dd>{agent.trigger}</dd></div>
            <div><dt className="text-xs font-semibold text-muted">KPI</dt><dd>{agent.kpi}</dd></div>
            <div><dt className="text-xs font-semibold text-muted">Guardrails</dt><dd className="flex flex-wrap gap-1">{agent.guardrails.map((g: string) => <Badge key={g} tone="warn">{g}</Badge>)}</dd></div>
            <div><dt className="text-xs font-semibold text-muted">Instructions</dt><dd className="text-muted">{agent.instructions}</dd></div>
            <div><dt className="text-xs font-semibold text-muted">DEMO inputs ({agent.inputs.length})</dt>
              <dd><ul className="mt-1 space-y-1">{agent.inputs.map((i: any, n: number) => (
                <li key={n} className="rounded-md bg-panel-2 px-2 py-1 text-xs"><span className="font-medium">{i.from}</span> <DemoBadge />: {i.text}</li>
              ))}</ul></dd></div>
          </dl>
        </Card>
        <div className="space-y-4">
          {!run && <Card><p className="text-sm text-muted">Run the agent on its DEMO inputs. You'll see each step tick: load input → rules draft → AI pass → review → queue.</p></Card>}
          {run && (
            <Card title="Run" actions={<TraceLink runId={run.id} />}>
              <RunSteps run={run} compact />
              {stopped && <p className="mt-2 text-sm text-bad">Stopped waiting after 90 s. The run is still recorded; open its trace.</p>}
            </Card>
          )}
          {result && (
            <Card title={<span data-testid="run-headline">{result.headline}</span>} actions={<Button tone="primary" size="sm" onClick={() => go(`/queue?ids=${result.queue_ids.join(",")}`)} data-testid="review-queue">Review in the queue</Button>}>
              <div className="grid gap-3 lg:grid-cols-3" data-testid="reply-cards">
                {result.cards.map((c: any) => (
                  <div key={c.queue_id} className="rounded-lg border border-line p-3 text-sm" data-testid="reply-card">
                    <div className="mb-2 flex flex-wrap items-center gap-1.5">
                      {c.escalated ? <Badge tone="bad">Escalated to a person</Badge> : <Badge tone={c.source === "AI" ? "good" : "neutral"}>{c.source}</Badge>}
                      {c.score !== null && <Badge tone={c.passes ? "good" : "warn"}>score {c.score}</Badge>}
                      <span className="text-xs text-muted">Queue #{c.queue_id}</span>
                    </div>
                    <p className="mb-1 text-xs text-muted">{c.from}: “{c.input}”</p>
                    <p className="prose-pre">{c.draft}</p>
                    {c.label && <p className="mt-2 text-[11px] italic text-muted">{c.label}</p>}
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
