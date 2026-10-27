import { useEffect, useState } from "react";
import { ApiError, put } from "../api";
import { Badge, Button, Card, DemoBadge, ErrorBox, Field, Input, Loading, PageHeader, Table, Tabs, isDemo, useApi, useApp } from "../components/ui";

export default function OwnerConsole({ query }: { query: URLSearchParams }) {
  const { toast } = useApp();
  const [tab, setTab] = useState(query.get("tab") || "personality");
  const pers = useApi<any>("/personality");
  const members = useApi<any>(tab === "members" ? "/members" : null, [tab]);
  const queries = useApi<any>(tab === "queries" ? "/queries" : null, [tab]);
  const unanswered = useApi<any>(tab === "queries" ? "/queries?unanswered=true" : null, [tab]);
  const alerts = useApi<any>("/alerts");
  const [p, setP] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { if (pers.data) setP({ ...pers.data, banned_words: pers.data.banned_words.join(", ") }); }, [pers.data]);
  const save = async () => {
    setErr(null);
    try {
      await put("/personality", { display_name: p.display_name, daily_question_limit: Number(p.daily_question_limit), banned_words: p.banned_words.split(",").map((s: string) => s.trim()).filter(Boolean), style: p.style, persona_consent_text: p.persona_consent_text || undefined });
      toast("Saved", "good");
      pers.reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Owner console" subtitle="Personality, members and payments, recent and unanswered questions, engagement alerts." />
      {alerts.data?.items?.length > 0 && (
        <div className="mb-4 space-y-2" data-testid="alerts">{alerts.data.items.map((a: any) => <a key={a.text} href={a.href} className={`focus-ring block rounded-[10px] border px-3 py-2 text-sm transition-colors ${a.level === "warn" ? "border-warn-line bg-warn-soft text-warn-ink" : "border-info-line bg-info-soft text-info-ink"}`}>{a.text}</a>)}</div>
      )}
      <ErrorBox error={err} />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "personality", label: "Personality" }, { key: "members", label: "Members & payments" }, { key: "queries", label: "Questions" }]} />
      {tab === "personality" && (pers.loading || !p ? <Loading /> : (
        <Card title="Personality">
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="Display name"><Input value={p.display_name} onChange={(e) => setP({ ...p, display_name: e.target.value })} /></Field>
            <Field label="Daily question limit per student"><Input value={p.daily_question_limit} onChange={(e) => setP({ ...p, daily_question_limit: e.target.value })} /></Field>
            <Field label="Banned words (always enforced)"><Input value={p.banned_words} onChange={(e) => setP({ ...p, banned_words: e.target.value })} placeholder="hustle, crush it" /></Field>
            <Field label="Style" hint={p.style_note}><Input value={p.style} onChange={(e) => setP({ ...p, style: e.target.value })} /></Field>
            <Field label="Persona consent (only if the assistant may speak as a named person)" hint="Recorded with your name and the time. Without it, nothing is ever written as a real person."><Input value={p.persona_consent_text || ""} onChange={(e) => setP({ ...p, persona_consent_text: e.target.value })} /></Field>
          </div>
          {p.persona_consent && <p className="mt-2 text-xs text-muted">Consent recorded {new Date(p.persona_consent.at * 1000).toLocaleString()} by {p.persona_consent.by}</p>}
          <Button className="mt-3" tone="primary" onClick={save}>Save</Button>
        </Card>
      ))}
      {tab === "members" && (
        <Card title="Members" subtitle={members.data ? <>Signed webhook: <code>{members.data.webhook}</code> · {members.data.signature}</> : ""}>
          {members.loading && <Loading />}
          {members.data && <Table rows={members.data.items} empty="No members yet" cols={[
            { key: "email", label: "Email", render: (m) => <span className="flex items-center gap-1.5">{m.email} {(m.demo || isDemo(m.plan)) && <DemoBadge />}</span> },
            { key: "status", label: "Access", render: (m) => <Badge tone={m.status === "active" ? "good" : "warn"}>{m.status}</Badge> },
            { key: "plan", label: "Plan" },
          ]} />}
        </Card>
      )}
      {tab === "queries" && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title="Unanswered (not covered)">{unanswered.data && <Table rows={unanswered.data.items} empty="None" cols={[{ key: "question", label: "Question" }, { key: "lang", label: "Lang" }]} />}</Card>
          <Card title="Recent questions">{queries.data && <Table rows={queries.data.items.slice(0, 50)} cols={[{ key: "question", label: "Question" }, { key: "covered", label: "Covered", render: (q) => <Badge tone={q.covered === "yes" ? "good" : q.covered === "partly" ? "warn" : "neutral"}>{q.covered}</Badge> }]} />}</Card>
        </div>
      )}
    </div>
  );
}
