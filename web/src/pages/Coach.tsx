import { useEffect, useRef, useState } from "react";
import { ArrowRight, Mic, MicOff, MessageCircle, Paperclip, Plus, Send, Volume2, VolumeX } from "lucide-react";
import { ApiError, api, get, post } from "../api";
import { AiModeBadge, Badge, Button, Card, Citations, Empty, ErrorBox, FlagList, Loading, PageHeader, Table, TextArea, TraceLink, cx, useApi, useApp } from "../components/ui";

type Msg = { role: string; content: string; meta?: any };

const INTENT_LABEL: Record<string, string> = { question: "Coach", metrics: "Diagnosis", asset: "Review", ads_csv: "Ads audit", goal: "Mission" };

function StartMission({ draft }: { draft: any }) {
  const { go, toast } = useApp();
  const [busy, setBusy] = useState(false);
  const [startedId, setStartedId] = useState<number | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/missions", { goal: draft.goal, template: draft.template, answers: draft.answers || {} });
      setStartedId(r.id);
      toast("Mission started", "good");
      go(`/missions?id=${r.id}`);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-2 space-y-1">
      {startedId ? (
        <Button size="sm" onClick={() => go(`/missions?id=${startedId}`)}>Open the mission <ArrowRight className="size-3.5" /></Button>
      ) : (
        <Button size="sm" tone="primary" loading={busy} onClick={start}>Start this mission <ArrowRight className="size-3.5" /></Button>
      )}
      <ErrorBox error={err} />
    </div>
  );
}

// Browser speech: recognition may use the browser's online speech service; read-aloud is local.
const SR: any = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

function useVoice(onText: (t: string) => void) {
  const [listening, setListening] = useState(false);
  const rec = useRef<any>(null);
  const supported = !!SR;
  const toggle = () => {
    if (!supported) return;
    if (listening) {
      rec.current?.stop();
      return;
    }
    const r = new SR();
    r.lang = navigator.language || "en-US";
    r.interimResults = false;
    r.onresult = (e: any) => onText(Array.from(e.results).map((x: any) => x[0].transcript).join(" "));
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    rec.current = r;
    setListening(true);
    r.start();
  };
  return { listening, toggle, supported };
}

function speak(text: string) {
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text.replace(/\[S\d+\]/g, ""));
  u.rate = 1.05;
  window.speechSynthesis.speak(u);
}

function Extra({ meta }: { meta: any }) {
  const { go } = useApp();
  if (meta.intent === "goal" && meta.mission_draft) return <StartMission draft={meta.mission_draft} />;
  if (meta.intent === "metrics" && meta.bottleneck) {
    const b = meta.bottleneck;
    return (
      <div className="mt-2 rounded-lg bg-white p-2 text-xs">
        <div className="font-semibold">Bottleneck: {b.label} — ≈ {Number(b.monthly_cost).toLocaleString()} / month</div>
        <div className="text-muted">Read from your message: {Object.entries(meta.metrics).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ")}</div>
        {meta.next_best_action && <Button size="sm" className="mt-2" onClick={() => go(`/studio?line=${meta.next_best_action.line}&brief=${encodeURIComponent(meta.next_best_action.brief)}`)}>Next best action <ArrowRight className="size-3.5" /></Button>}
      </div>
    );
  }
  if (meta.intent === "asset" && meta.review) {
    const r = meta.review;
    return (
      <div className="mt-2 space-y-1 rounded-lg bg-white p-2 text-xs">
        <div className="flex items-center gap-2"><Badge>{r.asset_type}</Badge><Badge tone={r.score >= 70 ? "good" : "warn"}>{r.score}/100</Badge><span className="text-muted">{r.label}</span></div>
        <FlagList flags={r.flags} />
        <ol className="list-decimal space-y-1 pl-4">{r.fixes.slice(0, 6).map((f: any, i: number) => <li key={i}><b>{f.problem}:</b> {f.fix}{f.line && <span className="block italic text-muted">“{f.line.slice(0, 140)}”</span>}</li>)}</ol>
      </div>
    );
  }
  if (meta.intent === "ads_csv" && meta.ads_audit?.ads) {
    return (
      <div className="mt-2 rounded-lg bg-white p-2 text-xs">
        <Table rows={meta.ads_audit.ads} cols={[
          { key: "name", label: "Ad" }, { key: "spend", label: "Spend" }, { key: "leads", label: "Leads" },
          { key: "cpl", label: "CPL", render: (r) => r.cpl ?? "—" }, { key: "ctr", label: "CTR", render: (r) => `${(r.ctr * 100).toFixed(2)}%` },
          { key: "verdict", label: "Verdict", render: (r) => <Badge tone={r.kill ? "bad" : r.verdict === "keep" ? "good" : "neutral"}>{meta.ads_audit.winner?.name === r.name ? "winner" : r.verdict}</Badge> },
        ]} />
        <p className="mt-1 text-muted">Read-only: nothing was changed in any ad account.</p>
      </div>
    );
  }
  return null;
}

export default function Coach({ query }: { query: URLSearchParams }) {
  const [threadId, setThreadId] = useState<number | null>(query.get("thread") ? Number(query.get("thread")) : null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [voiceOut, setVoiceOut] = useState(false);
  const list = useApi<any>("/threads");
  const end = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const voice = useVoice((t) => setQ((cur) => (cur ? cur + " " : "") + t));

  useEffect(() => {
    if (!threadId) {
      setMsgs([]);
      return;
    }
    get(`/threads/${threadId}`).then((t) => setMsgs(t.messages)).catch((e) => setErr(e));
  }, [threadId]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [msgs, busy]);

  const done = (r: any, shown: string) => {
    setMsgs((m) => [...m.slice(0, -1), { role: "user", content: shown, meta: { intent: r.intent } }, { role: "assistant", content: r.answer, meta: { ...r.meta, intent: r.intent } }]);
    if (voiceOut) speak(r.answer);
    if (!threadId) {
      setThreadId(r.thread_id);
      list.reload();
    }
  };

  const send = async (text?: string) => {
    const t = (text ?? q).trim();
    if (!t) return;
    setBusy(true);
    setErr(null);
    setMsgs((m) => [...m, { role: "user", content: t }]);
    setQ("");
    try {
      done(await post("/chat", { text: t, thread_id: threadId }), t);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const upload = async (f: File) => {
    setBusy(true);
    setErr(null);
    setMsgs((m) => [...m, { role: "user", content: `[${f.name}] ${q}` }]);
    const form = new FormData();
    form.append("file", f);
    form.append("text", q);
    if (threadId) form.append("thread_id", String(threadId));
    setQ("");
    try {
      const r = await api("/chat/upload", { form });
      done(r, `[${f.name}]`);
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const examples = [
    "What are the rules for a Thunder Dome test?",
    "800 DMs, 40 replies, 20 conversations, 10 links sent, 6 booked, 2 showed, 1 closed, price $3k",
    "Review this DM:\nHey! What's your goal? How many clients do you have? Our fee is $3k a month.",
    "How do I set up TikTok Shop?",
  ];
  return (
    <div>
      <PageHeader title="Student Coach" subtitle="One chat that does the work: ask a question, paste your numbers, a script, an ad or a DM, or drop a screenshot or an Ads Manager export. Answers come only from the programme's material, with citations." />
      <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
        <Card title="Threads" actions={<Button size="sm" onClick={() => setThreadId(null)}><Plus className="size-3.5" /> New</Button>} pad={false} className="hidden lg:block">
          <div className="max-h-[60vh] overflow-auto p-2">
            {list.loading && <Loading />}
            {list.data?.items?.length === 0 && <p className="p-2 text-xs text-muted">No threads yet.</p>}
            {list.data?.items?.map((t: any) => (
              <button key={t.id} onClick={() => setThreadId(t.id)} className={cx("focus-ring block w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-panel-2", t.id === threadId && "bg-panel-2 font-medium")}>{t.title}</button>
            ))}
          </div>
        </Card>
        <Card pad={false}>
          <div className="scroll-thin max-h-[62vh] min-h-[40vh] space-y-4 overflow-auto p-4" data-testid="chat">
            {msgs.length === 0 && !busy && (
              <Empty icon={<MessageCircle className="size-6" />} title="Ask, paste or drop anything" hint="Questions get cited answers. Numbers get a bottleneck diagnosis. Scripts, ads and DMs get line fixes. Screenshots are read locally. Ads Manager exports get a kill/winner audit." action={
                <div className="mt-2 flex flex-wrap justify-center gap-2">{examples.map((e) => <Button key={e} size="sm" onClick={() => send(e)}>{e.split("\n")[0].slice(0, 60)}{e.length > 60 ? "…" : ""}</Button>)}</div>
              } />
            )}
            {msgs.map((m, i) => (
              <div key={i} className={cx("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div className={cx("max-w-[88%] rounded-2xl px-4 py-2.5 text-sm", m.role === "user" ? "bg-brand text-white" : "border border-line bg-panel-2")}>
                  <p className="prose-pre">{m.content}</p>
                  {m.role === "assistant" && m.meta && (
                    <div className="mt-2 space-y-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {m.meta.intent && <Badge tone="brand">{INTENT_LABEL[m.meta.intent] || m.meta.intent}</Badge>}
                        {m.meta.covered && <Badge tone={m.meta.covered === "yes" ? "good" : m.meta.covered === "partly" ? "warn" : "neutral"}>covered: {m.meta.covered}</Badge>}
                        {m.meta.followup && <Badge tone="info">follow-up, read in context</Badge>}
                        {(m.meta.mode || m.meta.covered) && <AiModeBadge mode={m.meta.mode === "live" ? "live" : m.meta.mode === "replay" ? "replay" : "rules"} />}
                        {m.meta.label && <span className="text-[11px] text-muted">{m.meta.label}</span>}
                        <TraceLink runId={m.meta.run_id} />
                        <button className="focus-ring rounded p-0.5 text-muted hover:text-ink" onClick={() => speak(m.content)} aria-label="Read aloud"><Volume2 className="size-3.5" /></button>
                      </div>
                      {m.meta.receipt && <p className="text-[11px] text-muted">{m.meta.receipt.summary}{m.meta.receipt.warnings?.length ? ` — ${m.meta.receipt.warnings[0]}` : ""}</p>}
                      {m.meta.note && <p className="text-[11px] italic text-muted">{m.meta.note}</p>}
                      <Extra meta={m.meta} />
                      <Citations items={m.meta.citations} />
                      {m.meta.not_used?.length > 0 && (
                        <details className="text-[11px] text-muted"><summary>{m.meta.not_used.length} passage(s) not used</summary>
                          {m.meta.not_used.map((n: any, j: number) => <div key={j}>{n.label} ({n.file} L{n.lines})</div>)}
                        </details>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {busy && <div className="w-48"><Loading rows={2} /></div>}
            <div ref={end} />
          </div>
          <div className="border-t border-line p-3">
            <ErrorBox error={err} retry={() => setErr(null)} />
            <form className="mt-1 flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); send(); }}>
              <input ref={fileRef} type="file" className="hidden" accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.txt,.md,.csv,.tsv,.srt,.vtt,.html,.json" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} aria-label="Attach a file" />
              <Button type="button" tone="ghost" onClick={() => fileRef.current?.click()} aria-label="Attach a screenshot or file" title="Attach a screenshot, PDF, transcript or Ads Manager export"><Paperclip className="size-4" /></Button>
              {voice.supported && (
                <Button type="button" tone={voice.listening ? "danger" : "ghost"} onClick={voice.toggle} aria-label={voice.listening ? "Stop dictation" : "Dictate"} title="Dictate (uses your browser's speech service)">
                  {voice.listening ? <MicOff className="size-4" /> : <Mic className="size-4" />}
                </Button>
              )}
              <Button type="button" tone="ghost" onClick={() => setVoiceOut(!voiceOut)} aria-label={voiceOut ? "Stop reading answers aloud" : "Read answers aloud"} title="Read answers aloud (on-device voice)">
                {voiceOut ? <Volume2 className="size-4 text-brand" /> : <VolumeX className="size-4" />}
              </Button>
              <TextArea rows={2} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask, paste numbers or a script, or attach a screenshot… (EN · FR · ES · PT)" aria-label="Message"
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
              <Button tone="primary" loading={busy} type="submit" aria-label="Send"><Send className="size-4" /></Button>
            </form>
          </div>
        </Card>
      </div>
    </div>
  );
}
