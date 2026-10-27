import { useEffect, useRef, useState } from "react";
import { ArrowRight, ArrowUp, BookOpen, Calculator, ClipboardCheck, Lightbulb, Mic, MicOff, Paperclip, Plus, Sparkles, Volume2, VolumeX } from "lucide-react";
import { ApiError, api, get, post } from "../api";
import { AiModeBadge, Badge, Button, Card, Citations, Empty, ErrorBox, FlagList, Loading, PageHeader, Table, TextArea, TraceLink, cx, useApi, useApp } from "../components/ui";

type Msg = { role: string; content: string; meta?: any };

const INTENT_LABEL: Record<string, string> = { question: "Coach", metrics: "Diagnosis", asset: "Review", ads_csv: "Ads audit" };

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
  if (meta.intent === "metrics" && meta.bottleneck) {
    const b = meta.bottleneck;
    return (
      <div className="mt-2 rounded-[10px] border border-line-2 bg-panel p-2.5 text-xs">
        <div className="font-semibold">Bottleneck: {b.label} — ≈ {Number(b.monthly_cost).toLocaleString()} / month</div>
        <div className="text-muted">Read from your message: {Object.entries(meta.metrics).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ")}</div>
        {meta.next_best_action && <Button size="sm" className="mt-2" onClick={() => go(`/studio?line=${meta.next_best_action.line}&brief=${encodeURIComponent(meta.next_best_action.brief)}`)}>Next best action <ArrowRight className="size-3.5" /></Button>}
      </div>
    );
  }
  if (meta.intent === "asset" && meta.review) {
    const r = meta.review;
    return (
      <div className="mt-2 space-y-1 rounded-[10px] border border-line-2 bg-panel p-2.5 text-xs">
        <div className="flex items-center gap-2"><Badge>{r.asset_type}</Badge><Badge tone={r.score >= 70 ? "good" : "warn"}>{r.score}/100</Badge><span className="text-muted">{r.label}</span></div>
        <FlagList flags={r.flags} />
        <ol className="list-decimal space-y-1 pl-4">{r.fixes.slice(0, 6).map((f: any, i: number) => <li key={i}><b>{f.problem}:</b> {f.fix}{f.line && <span className="block italic text-muted">“{f.line.slice(0, 140)}”</span>}</li>)}</ol>
      </div>
    );
  }
  if (meta.intent === "ads_csv" && meta.ads_audit?.ads) {
    return (
      <div className="mt-2 rounded-[10px] border border-line-2 bg-panel p-2.5 text-xs">
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
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (msgs.length || busy) end.current?.scrollIntoView({ block: "end", behavior: reduce ? "auto" : "smooth" });
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
    { icon: BookOpen, label: "Ask a question", text: "What are the rules for a Thunder Dome test?" },
    { icon: Calculator, label: "Paste your numbers", text: "800 DMs, 40 replies, 20 conversations, 10 links sent, 6 booked, 2 showed, 1 closed, price $3k" },
    { icon: ClipboardCheck, label: "Get a DM reviewed", text: "Review this DM:\nHey! What's your goal? How many clients do you have? Our fee is $3k a month." },
    { icon: Lightbulb, label: "Learn a playbook", text: "How do I set up TikTok Shop?" },
  ];
  return (
    <div>
      <PageHeader title="Coach" subtitle="One chat that does the work. Ask a question, paste your numbers, a script, an ad or a DM, or drop a screenshot or an Ads Manager export. Every answer cites the programme's own material." />
      <div className="grid gap-4 lg:grid-cols-[248px_minmax(0,1fr)]">
        <Card title="Threads" actions={<Button size="sm" onClick={() => setThreadId(null)}><Plus className="size-3.5" /> New</Button>} pad={false} className="hidden self-start lg:block">
          <div className="scroll-thin max-h-[62vh] space-y-px overflow-auto p-2">
            {list.loading && <div className="p-2"><Loading /></div>}
            {list.data?.items?.length === 0 && <p className="px-2 py-3 text-[13px] text-muted">No threads yet. Your conversations will appear here.</p>}
            {list.data?.items?.map((t: any) => (
              <button key={t.id} type="button" onClick={() => setThreadId(t.id)} aria-current={t.id === threadId ? "true" : undefined}
                className={cx("focus-ring block w-full truncate rounded-[8px] px-2.5 py-1.5 text-left text-[13.5px] transition-colors", t.id === threadId ? "bg-side-active font-medium text-ink" : "text-ink-2 hover:bg-hover hover:text-ink")}>{t.title}</button>
            ))}
          </div>
        </Card>
        <section className="card flex flex-col md:min-h-[60vh] md:overflow-hidden" aria-label="Conversation">
          <div className="scroll-thin flex-1 space-y-5 p-4 sm:p-5 md:max-h-[64vh] md:min-h-[44vh] md:overflow-auto" data-testid="chat">
            {msgs.length === 0 && !busy && (
              <div className="mx-auto flex max-w-2xl flex-col items-center py-2 text-center sm:py-10">
                <span className="brand-mark grid size-11 place-items-center rounded-[13px]"><Sparkles className="size-5" /></span>
                <h2 className="t-h2 mt-4 text-ink">Ask, paste or drop anything</h2>
                <p className="mt-1.5 max-w-lg text-[13.5px] leading-relaxed text-muted max-sm:hidden">Questions get cited answers. Numbers get a bottleneck diagnosis. Scripts, ads and DMs get line fixes. Screenshots are read locally. Ads Manager exports get a kill/winner audit.</p>
                <div className="mt-6 grid w-full gap-2 sm:grid-cols-2">
                  {examples.map(({ icon: I, label, text }) => (
                    <button key={label} type="button" onClick={() => send(text)}
                      className="focus-ring group flex items-start gap-3 rounded-[12px] border border-line bg-panel p-3 text-left shadow-xs transition-[border-color,box-shadow] hover:border-brand-line hover:shadow-md">
                      <span className="grid size-8 shrink-0 place-items-center rounded-[9px] bg-brand-soft text-brand ring-1 ring-brand-line"><I className="size-4" /></span>
                      <span className="min-w-0">
                        <span className="block text-[13px] font-semibold text-ink">{label}</span>
                        <span className="mt-0.5 line-clamp-1 sm:line-clamp-2 text-[12.5px] leading-snug text-muted">{text.replace("Review this DM:\n", "")}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {msgs.map((m, i) => (
              <div key={i} className={cx("flex gap-3", m.role === "user" ? "justify-end" : "justify-start")}>
                {m.role !== "user" && <span aria-hidden className="brand-mark mt-0.5 hidden size-7 shrink-0 place-items-center rounded-[8px] sm:grid"><Sparkles className="size-3.5" /></span>}
                <div className={cx("min-w-0 max-w-[88%] text-[14px] leading-relaxed", m.role === "user" ? "rounded-[18px] rounded-br-[6px] bg-brand px-4 py-2.5 text-[var(--brand-ink)] shadow-xs" : "rounded-[18px] rounded-tl-[6px] border border-line bg-panel-2 px-4 py-3")}>
                  <p className="prose-pre">{m.content}</p>
                  {m.role === "assistant" && m.meta && (
                    <div className="mt-2.5 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {m.meta.intent && <Badge tone="brand">{INTENT_LABEL[m.meta.intent] || m.meta.intent}</Badge>}
                        {m.meta.covered && <Badge tone={m.meta.covered === "yes" ? "good" : m.meta.covered === "partly" ? "warn" : "neutral"}>covered: {m.meta.covered}</Badge>}
                        {m.meta.followup && <Badge tone="info">follow-up, read in context</Badge>}
                        {(m.meta.mode || m.meta.covered) && <AiModeBadge mode={m.meta.mode === "live" ? "live" : m.meta.mode === "replay" ? "replay" : "rules"} />}
                        {m.meta.label && <span className="text-[11.5px] text-muted">{m.meta.label}</span>}
                        <TraceLink runId={m.meta.run_id} />
                        <button type="button" className="focus-ring rounded p-0.5 text-muted hover:text-ink" onClick={() => speak(m.content)} aria-label="Read aloud"><Volume2 className="size-3.5" /></button>
                      </div>
                      {m.meta.receipt && <p className="text-[11.5px] text-muted">{m.meta.receipt.summary}{m.meta.receipt.warnings?.length ? ` — ${m.meta.receipt.warnings[0]}` : ""}</p>}
                      {m.meta.note && <p className="text-[11.5px] italic text-muted">{m.meta.note}</p>}
                      <Extra meta={m.meta} />
                      <Citations items={m.meta.citations} />
                      {m.meta.not_used?.length > 0 && (
                        <details className="text-[11.5px] text-muted"><summary>{m.meta.not_used.length} {m.meta.not_used.length === 1 ? "passage" : "passages"} not used</summary>
                          {m.meta.not_used.map((n: any, j: number) => <div key={j}>{n.label} ({n.file} L{n.lines})</div>)}
                        </details>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {busy && (
              <div className="flex items-center gap-3" aria-busy="true" aria-label="Thinking">
                <span aria-hidden className="brand-mark hidden size-7 shrink-0 place-items-center rounded-[8px] sm:grid"><Sparkles className="size-3.5" /></span>
                <span className="inline-flex items-center gap-1 rounded-[18px] rounded-tl-[6px] border border-line bg-panel-2 px-4 py-3.5">
                  {[0, 1, 2].map((d) => <span key={d} className="size-1.5 animate-bounce rounded-full bg-muted" style={{ animationDelay: `${d * 120}ms` }} />)}
                </span>
              </div>
            )}
            <div ref={end} className="scroll-mb-44 md:scroll-mb-0" />
          </div>
          <div className="z-10 rounded-b-[var(--radius-lg)] border-t border-line-2 bg-panel p-3 max-md:sticky max-md:bottom-[calc(58px+env(safe-area-inset-bottom))] sm:p-4">
            <ErrorBox error={err} retry={() => setErr(null)} />
            <form className="rounded-[14px] border border-line bg-panel shadow-xs transition-[border-color,box-shadow] focus-within:border-brand focus-within:ring-[3px] focus-within:ring-[var(--brand-ring)]" onSubmit={(e) => { e.preventDefault(); send(); }}>
              <input ref={fileRef} type="file" className="hidden" accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.txt,.md,.csv,.tsv,.srt,.vtt,.html,.json" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} aria-label="Attach a file" />
              <textarea rows={2} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask, paste numbers or a script, or attach a screenshot…" aria-label="Message"
                className="block max-h-48 min-h-[52px] w-full resize-none bg-transparent px-3.5 pt-3 text-[14px] leading-relaxed text-ink outline-none placeholder:text-placeholder"
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
              <div className="flex items-center gap-1 px-2 pb-2">
                <Button type="button" tone="ghost" size="icon-sm" onClick={() => fileRef.current?.click()} aria-label="Attach a screenshot or file" title="Attach a screenshot, PDF, transcript or Ads Manager export"><Paperclip className="size-4" /></Button>
                {voice.supported && (
                  <Button type="button" tone={voice.listening ? "danger" : "ghost"} size="icon-sm" onClick={voice.toggle} aria-label={voice.listening ? "Stop dictation" : "Dictate"} title="Dictate (uses your browser's speech service)">
                    {voice.listening ? <MicOff className="size-4" /> : <Mic className="size-4" />}
                  </Button>
                )}
                <Button type="button" tone="ghost" size="icon-sm" onClick={() => setVoiceOut(!voiceOut)} aria-label={voiceOut ? "Stop reading answers aloud" : "Read answers aloud"} aria-pressed={voiceOut} title="Read answers aloud (on-device voice)">
                  {voiceOut ? <Volume2 className="size-4 text-brand" /> : <VolumeX className="size-4" />}
                </Button>
                <span className="ml-1 hidden truncate text-[11.5px] text-muted sm:inline">EN · FR · ES · PT · Shift+Enter for a new line</span>
                <Button tone="primary" size="icon-sm" className="ml-auto" loading={busy} type="submit" aria-label="Send" disabled={!q.trim() && !busy}>{!busy && <ArrowUp className="size-4" />}</Button>
              </div>
            </form>
          </div>
        </section>
      </div>
    </div>
  );
}
