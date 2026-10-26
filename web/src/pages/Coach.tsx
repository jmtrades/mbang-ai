import { useEffect, useRef, useState } from "react";
import { MessageCircle, Plus, Send } from "lucide-react";
import { ApiError, get, post } from "../api";
import { AiModeBadge, Badge, Button, Card, Citations, Empty, ErrorBox, Loading, PageHeader, TextArea, TraceLink, cx, useApi } from "../components/ui";

type Msg = { role: string; content: string; meta?: any };

export default function Coach({ query }: { query: URLSearchParams }) {
  const [threadId, setThreadId] = useState<number | null>(query.get("thread") ? Number(query.get("thread")) : null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const list = useApi<any>("/threads");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!threadId) {
      setMsgs([]);
      return;
    }
    get(`/threads/${threadId}`).then((t) => setMsgs(t.messages)).catch((e) => setErr(e));
  }, [threadId]);
  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [msgs, busy]);

  const ask = async (text?: string) => {
    const question = (text ?? q).trim();
    if (question.length < 2) return;
    setBusy(true);
    setErr(null);
    setMsgs((m) => [...m, { role: "user", content: question }]);
    setQ("");
    try {
      const r = await post("/coach/ask", { question, thread_id: threadId });
      setMsgs((m) => [...m, { role: "assistant", content: r.answer, meta: r }]);
      if (!threadId) {
        setThreadId(r.thread_id);
        list.reload();
      }
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const examples = ["What are the rules for a Thunder Dome test?", "When should reminders go out before a call?", "How do I handle 'I need to think about it'?", "How do I set up TikTok Shop?"];
  return (
    <div>
      <PageHeader title="Student Coach" subtitle="Answers only from the programme's own material, with citations. When the material doesn't cover something, it says so." />
      <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
        <Card title="Threads" actions={<Button size="sm" onClick={() => setThreadId(null)}><Plus className="size-3.5" /> New</Button>} pad={false} className="hidden lg:block">
          <div className="max-h-[60vh] overflow-auto p-2">
            {list.loading && <Loading />}
            {list.data?.items?.length === 0 && <p className="p-2 text-xs text-muted">No threads yet.</p>}
            {list.data?.items?.map((t: any) => (
              <button key={t.id} onClick={() => setThreadId(t.id)} className={cx("focus-ring block w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-panel-2", t.id === threadId && "bg-panel-2 font-medium")}>
                {t.title}
              </button>
            ))}
          </div>
        </Card>
        <Card pad={false}>
          <div className="scroll-thin max-h-[62vh] min-h-[40vh] space-y-4 overflow-auto p-4" data-testid="chat">
            {msgs.length === 0 && !busy && (
              <Empty icon={<MessageCircle className="size-6" />} title="Ask the coach anything about the programme" hint="Try one of these:" action={
                <div className="mt-2 flex flex-wrap justify-center gap-2">{examples.map((e) => <Button key={e} size="sm" onClick={() => ask(e)}>{e}</Button>)}</div>
              } />
            )}
            {msgs.map((m, i) => (
              <div key={i} className={cx("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div className={cx("max-w-[85%] rounded-2xl px-4 py-2.5 text-sm", m.role === "user" ? "bg-brand text-white" : "border border-line bg-panel-2")}>
                  <p className="prose-pre">{m.content}</p>
                  {m.role === "assistant" && m.meta && (
                    <div className="mt-2 space-y-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge tone={m.meta.covered === "yes" ? "good" : m.meta.covered === "partly" ? "warn" : "neutral"}>covered: {m.meta.covered}</Badge>
                        <AiModeBadge mode={m.meta.mode === "live" ? "live" : m.meta.mode === "replay" ? "replay" : "rules"} />
                        {m.meta.label && <span className="text-[11px] text-muted">{m.meta.label}</span>}
                        <TraceLink runId={m.meta.run_id} />
                      </div>
                      {m.meta.note && <p className="text-[11px] italic text-muted">{m.meta.note}</p>}
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
            <form className="mt-1 flex gap-2" onSubmit={(e) => { e.preventDefault(); ask(); }}>
              <TextArea rows={2} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question (English, Français, Español, Português)…" aria-label="Question"
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } }} />
              <Button tone="primary" loading={busy} type="submit" aria-label="Send"><Send className="size-4" /></Button>
            </form>
          </div>
        </Card>
      </div>
    </div>
  );
}
