import { useEffect, useState } from "react";
import { ApiError, BASE, api, post, put } from "../api";
import { Badge, Button, Card, DemoBadge, Empty, ErrorBox, Field, Input, Loading, Modal, PageHeader, Table, Tabs, TextArea, isDemo, useApi, useApp } from "../components/ui";
import { PersonaAdmin } from "./persona/PersonaAdmin";

const STATE_LABEL: Record<string, { label: string; tone: "good" | "warn" | "neutral" }> = {
  current: { label: "Current", tone: "good" },
  needs_evidence: { label: "Needs evidence", tone: "warn" },
  needs_note: { label: "Needs a typicality note", tone: "warn" },
  expired: { label: "Expired", tone: "neutral" },
};

function ProofRecords() {
  const { toast } = useApp();
  const recs = useApi<any>("/claims");
  const [note, setNote] = useState("");
  const [days, setDays] = useState("180");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [expiring, setExpiring] = useState<any>(null);
  const [reason, setReason] = useState("");
  const d = recs.data;
  const run = async (fn: () => Promise<unknown>, done: string): Promise<boolean> => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      toast(done, "good");
      recs.reload();
      return true;
    } catch (e) {
      setErr(e as ApiError);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const upload = (id: number, file: File | undefined) => {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    run(() => api(`/claims/${id}/evidence`, { form: fd }), "Evidence added");
  };
  if (recs.loading && !d) return <Loading />;
  if (recs.error) return <ErrorBox error={recs.error} retry={recs.reload} />;
  if (!d) return null;
  return (
    <div className="space-y-4">
      <Card title="Proof records" subtitle="A results figure reaches prospects only while a current proof record backs it: at least one evidence file and a plain note on what typical results look like. Without one, drafts use the no-figure line.">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {d.current
            ? <Badge tone="good">The $50k-$100k a month line is backed by a current record</Badge>
            : <Badge tone="warn">No current record: outreach uses "Let me help [Brand] grow through …"</Badge>}
        </div>
        {d.read_only_reason && <p className="mt-2 text-xs text-muted">{d.read_only_reason}</p>}
      </Card>
      <ErrorBox error={err} />
      {!d.items.length ? (
        <Empty title="No proof records yet" hint="Add one below with your evidence (results exports, invoices, screenshots) and what typical results look like." />
      ) : (
        <Card pad={false}>
          <div className="p-3">
            <Table rows={d.items} empty="No proof records yet" cols={[
              { key: "statement", label: "Figure", render: (r) => <span className="flex flex-wrap items-center gap-1.5">{r.claim_label} {(r.demo || isDemo(r.statement)) && <DemoBadge />}</span> },
              { key: "state", label: "Status", render: (r) => <Badge tone={STATE_LABEL[r.state]?.tone || "neutral"}>{STATE_LABEL[r.state]?.label || r.state}</Badge> },
              { key: "typicality_note", label: "Typical results", render: (r) => <span className="text-xs">{r.typicality_note}</span> },
              { key: "evidence", label: "Evidence", render: (r) => r.evidence.length ? (
                <ul className="space-y-0.5 text-xs">{r.evidence.map((e: any) => (
                  <li key={e.id}>{d.read_only ? e.name : <a className="underline" href={`${BASE}/claims/${r.id}/evidence/${e.id}`} target="_blank" rel="noreferrer">{e.name}</a>} <span className="text-muted">({Math.max(1, Math.round(e.size / 1024))} KB)</span></li>
                ))}</ul>) : <span className="text-xs text-muted">None yet</span> },
              { key: "expires", label: "Valid until", render: (r) => r.state === "expired" && r.expired_at ? `Expired ${new Date(r.expired_at * 1000).toLocaleDateString()}` : new Date(r.expires * 1000).toLocaleDateString() },
              { key: "actions", label: "", render: (r) => d.read_only || r.status !== "active" ? null : (
                <div className="flex flex-wrap gap-2">
                  <label className="cursor-pointer rounded-lg border border-line px-2 py-1 text-xs">
                    Add evidence
                    <input type="file" className="sr-only" aria-label={`Add evidence to record ${r.id}`} accept={d.evidence_types.join(",")} onChange={(e) => upload(r.id, e.target.files?.[0])} />
                  </label>
                  <Button size="sm" tone="ghost" onClick={() => { setExpiring(r); setReason(""); }}>Expire</Button>
                </div>
              ) },
            ]} />
          </div>
        </Card>
      )}
      {!d.read_only && (
        <Card title="Add a proof record" subtitle={`Evidence files: ${d.evidence_types.join(", ")} up to ${d.max_file_mb} MB each. Review a record at least yearly.`}>
          <div className="grid gap-3 md:grid-cols-[1fr_160px]">
            <Field label="What typical results look like" hint="Plain words, at least 20 characters. For example: most clients see far less; this range is the best quarter of our clinics.">
              <TextArea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            <Field label={`Valid for (days, up to ${d.max_days})`}><Input inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} /></Field>
          </div>
          <Button className="mt-3" tone="primary" loading={busy} disabled={note.trim().length < 20 || !Number(days)}
            onClick={() => run(() => post("/claims", { claim_key: "outreach_range", typicality_note: note, valid_days: Number(days) }), "Record added: now add the evidence").then((ok) => ok && setNote(""))}>
            Add record
          </Button>
        </Card>
      )}
      <Modal open={!!expiring} onClose={() => setExpiring(null)} title="Expire this proof record?">
        <p className="text-sm">Drafts stop using the figure straight away unless another current record backs it.</p>
        <Field label="Reason (optional)"><Input value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button tone="danger" loading={busy} onClick={() => run(() => post(`/claims/${expiring.id}/expire`, { reason }), "Record expired").then((ok) => ok && setExpiring(null))}>Expire record</Button>
          <Button onClick={() => setExpiring(null)}>Cancel</Button>
        </div>
      </Modal>
    </div>
  );
}

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
      await put("/personality", { display_name: p.display_name, daily_question_limit: Number(p.daily_question_limit), banned_words: p.banned_words.split(",").map((s: string) => s.trim()).filter(Boolean), style: p.style });
      toast("Saved", "good");
      pers.reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Owner console" subtitle="Personality and persona, proof records, members and payments, recent and unanswered questions, engagement alerts." />
      {alerts.data?.items?.length > 0 && (
        <div className="mb-4 space-y-2" data-testid="alerts">{alerts.data.items.map((a: any) => <a key={a.text} href={a.href} className={`focus-ring block rounded-[10px] border px-3 py-2 text-sm transition-colors ${a.level === "warn" ? "border-warn-line bg-warn-soft text-warn-ink" : "border-info-line bg-info-soft text-info-ink"}`}>{a.text}</a>)}</div>
      )}
      <ErrorBox error={err} />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "personality", label: "Personality" }, { key: "persona", label: "Persona" }, { key: "proof", label: "Proof records" }, { key: "members", label: "Members & payments" }, { key: "queries", label: "Questions" }]} />
      {tab === "persona" && <PersonaAdmin />}
      {tab === "proof" && <ProofRecords />}
      {tab === "personality" && (pers.loading || !p ? <Loading /> : (
        <Card title="Personality">
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="Display name"><Input value={p.display_name} onChange={(e) => setP({ ...p, display_name: e.target.value })} /></Field>
            <Field label="Daily question limit per student"><Input value={p.daily_question_limit} onChange={(e) => setP({ ...p, daily_question_limit: e.target.value })} /></Field>
            <Field label="Banned words (always enforced)"><Input value={p.banned_words} onChange={(e) => setP({ ...p, banned_words: e.target.value })} placeholder="hustle, crush it" /></Field>
            <Field label="Style" hint={p.style_note}><Input value={p.style} onChange={(e) => setP({ ...p, style: e.target.value })} /></Field>
          </div>
          {p.legacy_banner && <p className="mt-3 rounded-lg border border-warn-line bg-warn-soft px-3 py-2 text-xs text-warn-ink" data-testid="legacy-banner">{p.legacy_banner}</p>}
          <p className="mt-3 text-xs text-muted">Answering in a real person's teaching phrasing needs that person's own signed consent. Set it up in the <button className="text-brand underline-offset-2 hover:underline" onClick={() => setTab("persona")}>Persona</button> tab.</p>
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
