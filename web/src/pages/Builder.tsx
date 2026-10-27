import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Bot, Building2, Check, Network, Play, RotateCcw, Sparkles } from "lucide-react";
import { ApiError, get, post } from "../api";
import { Badge, Button, Card, DemoBadge, ErrorBox, Field, Input, Loading, PageHeader, Run, RunSteps, TextArea, TraceLink, cx, humanize, isDemo, useApi, useApp } from "../components/ui";

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
    const pickedPreset = presets.data?.presets.find((p: any) => p.key === picked);
    const pickCls = (on: boolean, own = false) => cx(
      "focus-ring card relative flex flex-col items-start gap-2 p-4 text-left transition-[border-color,box-shadow,background-color]",
      on ? "border-brand bg-brand-soft shadow-[0_0_0_3px_var(--brand-ring)]" : own ? "border-dashed border-line-strong bg-panel-2/60 shadow-none hover:border-brand-line hover:bg-panel" : "hover:border-line-strong hover:shadow-md",
    );
    const Tick = ({ on }: { on: boolean }) => (
      <span aria-hidden className={cx("grid size-[18px] shrink-0 place-items-center rounded-full border transition-colors", on ? "border-transparent bg-brand text-[var(--brand-ink)]" : "border-line-strong bg-panel")}>
        {on && <Check className="size-3" strokeWidth={3} />}
      </span>
    );
    return (
      <div ref={topRef}>
        <PageHeader title="Build a business's AI team" subtitle="Pick a business. Five departments with two agents each are generated from rules in under a second, then any agent can run on demo data. Nothing sends." />
        <ErrorBox error={err || presets.error} retry={presets.reload} />
        {presets.loading && <Loading rows={3} />}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" data-testid="business-picker">
          {presets.data?.presets.map((p: any) => (
            <button key={p.key} type="button" data-preset={p.key} aria-pressed={picked === p.key} onClick={() => setPicked(p.key)} className={pickCls(picked === p.key)}>
              <div className="flex w-full items-center gap-2">
                <span className="grid size-8 place-items-center rounded-[9px] bg-brand-soft text-brand ring-1 ring-brand-line"><Building2 className="size-4" /></span>
                <span className="ml-auto"><DemoBadge /></span>
                <Tick on={picked === p.key} />
              </div>
              <div className="mt-1 font-semibold text-ink">{p.name.replace(" (DEMO)", "")}</div>
              <div className="text-[12.5px] leading-snug text-muted">{humanize(p.niche)} · {p.offer.replace(" (DEMO)", "")}</div>
            </button>
          ))}
          <button type="button" data-preset="own" aria-pressed={picked === "own"} onClick={() => setPicked("own")} className={pickCls(picked === "own", true)}>
            <div className="flex w-full items-center gap-2">
              <span className="grid size-8 place-items-center rounded-[9px] bg-panel text-brand ring-1 ring-line"><Sparkles className="size-4" /></span>
              <span className="ml-auto" />
              <Tick on={picked === "own"} />
            </div>
            <div className="mt-1 font-semibold text-ink">Your own business</div>
            <div className="text-[12.5px] leading-snug text-muted">Fill in a short profile</div>
          </button>
        </div>

        {picked === "own" ? (
          <Card title="Your own business" subtitle="Only the name is required. Everything else sharpens the drafts." className="mt-4"><OwnForm onGenerate={generate} busy={busy === "gen"} /></Card>
        ) : (
          <div className="mt-4 flex flex-col gap-3 rounded-[var(--radius-lg)] border border-line bg-panel-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <p className="text-[13.5px] text-muted">
              {pickedPreset
                ? <><span className="font-medium text-ink">{pickedPreset.name.replace(" (DEMO)", "")}</span> is ready: 5 departments and 10 agents, generated from rules.</>
                : "Pick a business above to generate its departments."}
            </p>
            <Button tone="primary" loading={busy === "gen"} disabled={!picked} onClick={() => picked && generate({ preset: picked })} data-testid="generate">
              Generate departments <ArrowRight className="size-4" />
            </Button>
          </div>
        )}

        <ol className="mt-10 grid gap-x-8 gap-y-5 border-t border-line pt-8 md:grid-cols-3" aria-label="How it works">
          {[
            { icon: Building2, t: "Pick a business", d: "A ready-made example, or your own in a short profile." },
            { icon: Network, t: "Get five departments", d: "Ads, webinars, sales follow-up, content and support. Two agents each, with a KPI, a trigger and guardrails." },
            { icon: Play, t: "Run any agent", d: "It drafts on fictional inputs and shows every step. Drafts wait in the approval queue; nothing sends." },
          ].map(({ icon: I, t, d }, i) => (
            <li key={t} className="flex gap-3">
              <span className="grid size-8 shrink-0 place-items-center rounded-[9px] border border-line bg-panel text-muted shadow-xs"><I className="size-4" /></span>
              <div>
                <div className="text-[14px] font-semibold text-ink"><span className="mr-1.5 tabular-nums text-muted">{i + 1}.</span>{t}</div>
                <p className="mt-0.5 text-[13px] leading-relaxed text-muted">{d}</p>
              </div>
            </li>
          ))}
        </ol>
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
          <dl className="space-y-4 text-[13.5px]" data-testid="agent-card">
            <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-1">
              <div><dt className="mb-1 text-[12px] font-medium text-muted">Produces</dt><dd className="text-ink">{agent.produces}</dd></div>
              <div><dt className="mb-1 text-[12px] font-medium text-muted">Trigger</dt><dd className="text-ink">{agent.trigger}</dd></div>
              <div><dt className="mb-1 text-[12px] font-medium text-muted">KPI</dt><dd className="text-ink">{agent.kpi}</dd></div>
            </div>
            <div><dt className="mb-1.5 text-[12px] font-medium text-muted">Guardrails</dt><dd className="flex flex-wrap gap-1.5">{agent.guardrails.map((g: string) => <Badge key={g} tone="warn">{g}</Badge>)}</dd></div>
            <div><dt className="mb-1 text-[12px] font-medium text-muted">Instructions</dt><dd className="leading-relaxed text-ink-2">{agent.instructions}</dd></div>
            <div><dt className="mb-1.5 text-[12px] font-medium text-muted">DEMO inputs ({agent.inputs.length})</dt>
              <dd><ul className="space-y-1.5">{agent.inputs.map((i: any, n: number) => (
                <li key={n} className="rounded-[10px] border border-line-2 bg-panel-2 px-3 py-2">
                  <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-ink">{String(i.from).replace(" (DEMO)", "")} <DemoBadge /></div>
                  <p className="mt-0.5 text-[13px] leading-snug text-ink-2">{i.text}</p>
                </li>
              ))}</ul></dd></div>
          </dl>
        </Card>
        <div className="space-y-4">
          {!run && (
            <Card title="What a run does" subtitle="Runs on the fictional inputs. Every step is traced; every draft waits for approval.">
              <ol className="space-y-1.5">
                {["Load the inputs", "Draft from rules and the playbook", "AI pass (labelled; skipped when AI is off)", "Review against the guardrails", "Queue each draft for approval"].map((t, i) => (
                  <li key={t} className="flex items-center gap-2.5 rounded-[10px] border border-line bg-panel-2 px-3 py-2 text-[13.5px] text-muted">
                    <span className="grid size-5 shrink-0 place-items-center rounded-full border border-line-strong text-[11px] font-semibold tabular-nums">{i + 1}</span>{t}
                  </li>
                ))}
              </ol>
            </Card>
          )}
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
