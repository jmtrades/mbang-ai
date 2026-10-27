// Owner console → Persona: who the persona is, how much of their own words the system has, the mined phrases, a
// neutral-vs-styled preview, the consent on file and its audit trail. The owner sets things up; only the subject
// (on their own login, from the Business profile page) approves phrases and signs.
import { useEffect, useState } from "react";
import { ExternalLink, ShieldCheck } from "lucide-react";
import { ApiError, get, post, put } from "../../api";
import { Badge, Button, Card, DemoBadge, Empty, ErrorBox, Field, Input, Loading, Modal, Select, Table, TraceLink, useApi, useApp } from "../../components/ui";
import { CLASS_LABEL, CoverageMeter, DecisionBadge, EVENT_LABEL, EvidenceChips, NeverDoes, fmtDate, fmtWhen, styleInPlainWords, words } from "./shared";

const STATUS_TONE: Record<string, "good" | "warn" | "neutral" | "bad" | "info"> = {
  active: "good", draft: "warn", awaiting_subject: "info", withdrawn: "neutral", expired: "neutral", neutral: "neutral",
};

export function PersonaAdmin() {
  const { toast, go } = useApp();
  const { data, error, loading, reload } = useApi<any>("/persona");
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pick, setPick] = useState<string>("");
  const [aliases, setAliases] = useState("");
  const [lastRun, setLastRun] = useState<number | null>(null);
  const [preview, setPreview] = useState<any>(null);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [deleteProfile, setDeleteProfile] = useState(false);
  const [allDocs, setAllDocs] = useState(false);

  useEffect(() => {
    if (!data) return;
    setPick(String(data.identity.subject_user_id || data.candidates[0]?.id || ""));
    setAliases((data.identity.aliases || []).join(", "));
  }, [data]);

  const act = async (key: string, fn: () => Promise<any>, ok: string) => {
    setBusy(key);
    setErr(null);
    try {
      const r = await fn();
      toast(ok, "good");
      reload();
      return r;
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

  if (loading && !data) return <Loading rows={6} />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  if (!data) return null;
  const ident = data.identity;
  const totals = data.coverage.totals;
  const prof = data.profile;

  return (
    <div className="space-y-4" data-testid="persona-admin">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={STATUS_TONE[data.status.key] || "neutral"}>{data.status.label}</Badge>
        <span className="text-sm text-muted">{data.status.detail}</span>
        {data.is_demo && <DemoBadge />}
        <a className="ml-auto inline-flex items-center gap-1 text-xs text-brand hover:underline" href={data.disclosure_url} target="_blank" rel="noreferrer">
          Public disclosure page <ExternalLink className="size-3" />
        </a>
      </div>
      {data.legacy.present && (
        <div className="rounded-lg border border-warn-line bg-warn-soft px-3 py-2 text-sm text-warn-ink" data-testid="legacy-banner">{data.legacy.banner}</div>
      )}
      <ErrorBox error={err} />

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Whose teaching phrasing" subtitle="The person must have their own owner login: only they can approve phrases and sign consent.">
          {data.candidates.length === 0 ? (
            <Empty title="No owner logins yet" hint="Invite the person with an owner role first (Team & invites)." action={<Button size="sm" onClick={() => go("/team")}>Open Team & invites</Button>} />
          ) : (
            <div className="space-y-3">
              {ident.subject && <p className="text-sm">Subject: <b>{ident.name}</b> <span className="text-muted">(signs in as {ident.subject.username})</span></p>}
              <div className="grid gap-2 sm:grid-cols-2">
                <Field label="Person">
                  <Select value={pick} onChange={(e) => setPick(e.target.value)} options={data.candidates.map((u: any) => ({ value: String(u.id), label: `${u.display} (${u.username})` }))} />
                </Field>
                <Field label="Other names in transcripts" hint={data.active ? "Locked while consent is in force: they decide whose words count as the subject's." : "Comma separated, e.g. a first name used in captions."}>
                  <Input value={aliases} disabled={!!data.active} onChange={(e) => setAliases(e.target.value)} placeholder="First name, initials" />
                </Field>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button tone="primary" loading={busy === "subject"} disabled={!pick} onClick={() => act("subject", () => post("/persona/subject", { user_id: Number(pick), aliases: list(aliases) }), "Subject saved")}>
                  {ident.subject_user_id && String(ident.subject_user_id) !== pick ? "Change subject" : "Save subject"}
                </Button>
                {ident.subject_user_id && !data.active && (
                  <Button loading={busy === "aliases"} onClick={() => act("aliases", () => put("/persona/identity", { aliases: list(aliases) }), "Names saved")}>Save other names only</Button>
                )}
              </div>
              {ident.subject_user_id && String(ident.subject_user_id) !== pick && <p className="text-xs text-warn">Changing the subject withdraws any consent on file straight away.</p>}
            </div>
          )}
        </Card>

        <Card title="Material said by the subject" subtitle="Only lines with a speaker label that matches the subject count. Nothing is guessed.">
          <CoverageMeter totals={totals} />
          {totals.fixture_refused && (
            <p className="mt-3 rounded-lg border border-warn-line bg-warn-soft px-3 py-2 text-xs text-warn-ink">The knowledge base is still the fictional sample material. Load the owner's own labelled transcripts first; nothing is learned from the sample.</p>
          )}
        </Card>
      </div>

      <Card title="Documents" subtitle="Mark a lesson recorded by the subject alone so all its lines count as theirs." actions={
        data.coverage.docs.length > 6 ? <Button size="sm" tone="ghost" onClick={() => setAllDocs(!allDocs)}>{allDocs ? "Show fewer" : `Show all ${data.coverage.docs.length}`}</Button> : null
      }>
        <Table rows={data.coverage.docs.slice(0, allDocs ? 300 : 6)} empty="No transcripts or uploads yet" cols={[
          { key: "title", label: "Document", render: (d) => <span className="flex flex-wrap items-center gap-1.5">{d.title}{d.demo && <DemoBadge />}</span> },
          { key: "subject_words", label: "Subject", className: "tabular-nums", render: (d) => words(d.subject_words) },
          { key: "other_words", label: "Others", className: "tabular-nums", render: (d) => words(d.other_words) },
          { key: "unattributed_words", label: "Unlabelled", className: "tabular-nums", render: (d) => words(d.unattributed_words) },
          { key: "mark", label: "", render: (d) => d.demo || !ident.subject_user_id || d.speakers.length > 0 ? null : (
            <Button size="sm" loading={busy === "mark:" + d.file} onClick={() => act("mark:" + d.file, () => put("/persona/attribution", { file: d.file, speaker: d.marked_speaker ? null : ident.name }), d.marked_speaker ? "Mark removed" : "Marked as the subject's")}>
              {d.marked_speaker ? "Unmark" : "All the subject's"}
            </Button>
          ) },
        ]} />
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Style profile" subtitle="Built offline from the subject's own lines, against other speakers and the written SOPs." actions={
          <Button tone="primary" size="sm" loading={busy === "mine"} disabled={!ident.subject_user_id} onClick={async () => {
            const r = await act("mine", () => post("/persona/mine", {}), "Profile built");
            if (r?.run_id) setLastRun(r.run_id);
          }}>{prof ? "Rebuild profile" : "Build profile"}</Button>
        }>
          {lastRun && <p className="mb-2 text-xs text-muted">Run <TraceLink runId={lastRun} /></p>}
          {!prof ? (
            <Empty title="No profile yet" hint={`Needs ${words(totals.draft_at)} words said by the subject. A new version always needs fresh consent.`} />
          ) : (
            <div className="space-y-2 text-sm">
              <p className="flex flex-wrap items-center gap-2">
                <b>Version {prof.version}</b>
                <Badge tone={prof.maturity === "stable" ? "good" : "warn"}>{prof.maturity === "stable" ? "Stable" : "Draft"}</Badge>
                <span className="text-muted">{words(prof.words)} words · built {fmtDate(prof.created)}</span>
                {prof.demo && <DemoBadge />}
              </p>
              <ul className="list-disc space-y-0.5 pl-5">{styleInPlainWords(prof.stylometrics).map((s) => <li key={s}>{s}</li>)}</ul>
              {prof.lexicon?.length > 0 && <p className="text-xs text-muted">Word choices: {prof.lexicon.map((l: any) => `"${l.prefer}" over "${l.over}"`).join(", ")} (used only in template text, never inside quoted material).</p>}
              {prof.report?.note && <p className="text-xs text-muted">{prof.report.note}</p>}
            </div>
          )}
        </Card>

        <Card title="Consent" subtitle="Signed by the subject on their own login, scoped, expiring and withdrawable." actions={
          data.active ? <Button tone="danger" size="sm" onClick={() => setWithdrawOpen(true)}>Withdraw now</Button> : prof ? <Button size="sm" tone="danger" onClick={() => setWithdrawOpen(true)}>Delete or reset</Button> : null
        }>
          {data.active ? (
            <div className="space-y-2 text-sm">
              <p className="flex flex-wrap items-center gap-2"><ShieldCheck className="size-4 text-good" /> Consent v{data.active.version}, signed {fmtDate(data.active.at)}, expires {fmtDate(data.active.expires)}</p>
              <ul className="list-disc pl-5 text-xs">{data.active.scopes.map((s: string) => <li key={s}>{data.scopes.find((x: any) => x.key === s)?.label || s}</li>)}</ul>
            </div>
          ) : (
            <p className="text-sm text-muted">{ident.subject ? `Waiting for ${ident.name} to review the phrases and sign from their own Business profile page.` : "Choose the subject first."}</p>
          )}
          <div className="mt-3 border-t border-line pt-3">
            <p className="mb-1 text-xs font-medium text-muted">What it never does</p>
            <NeverDoes />
          </div>
        </Card>
      </div>

      <Card title="Phrases" subtitle={data.is_subject ? "You are the subject: approve the ones that are yours." : "Only the subject can approve. You can reject a phrase or mark it never to be used."}>
        <Table rows={data.phrases} empty="No phrases yet. Build a profile first." cols={[
          { key: "display", label: "Phrase", render: (p) => <span className="font-medium">“{p.display || p.phrase}”</span> },
          { key: "cls", label: "Used as", render: (p) => <span className="whitespace-nowrap text-xs">{CLASS_LABEL[p.cls] || p.cls}</span> },
          { key: "count", label: "Seen", className: "tabular-nums", render: (p) => <span className="whitespace-nowrap text-xs">{p.count}× in {p.docs} docs</span> },
          { key: "evidence", label: "Evidence", render: (p) => <EvidenceChips items={p.evidence} /> },
          { key: "decision", label: "Decision", render: (p) => (
            <div className="flex flex-wrap items-center gap-1">
              <DecisionBadge d={p.decision} />
              {data.is_subject && p.decision !== "approve" && <Button size="sm" onClick={() => act("p" + p.id, () => put(`/persona/phrases/${p.id}`, { decision: "approve" }), "Approved")}>Approve</Button>}
              {p.decision !== "reject" && <Button size="sm" tone="ghost" onClick={() => act("p" + p.id, () => put(`/persona/phrases/${p.id}`, { decision: "reject" }), "Rejected")}>Reject</Button>}
              {p.decision !== "never_use" && <Button size="sm" tone="ghost" onClick={() => act("p" + p.id, () => put(`/persona/phrases/${p.id}`, { decision: "never_use" }), "Marked never to use")}>Never use</Button>}
            </div>
          ) },
        ]} />
      </Card>

      <Card title="Neutral vs styled preview" subtitle="Offline and private: five sample questions, framed with the approved phrases. Nobody else sees this." actions={
        <Button size="sm" loading={busy === "preview"} disabled={!prof} onClick={async () => {
          setBusy("preview");
          setErr(null);
          try { setPreview(await get("/persona/preview")); } catch (e) { setErr(e as ApiError); } finally { setBusy(null); }
        }}>Run preview</Button>
      }>
        {!preview ? <Empty title="No preview yet" hint={prof ? "Run it to compare the neutral answer with the framed one, with the checks." : "Build a profile first."} /> : (
          <div className="space-y-3">
            {preview.items.map((it: any) => (
              <div key={it.question} className="rounded-lg border border-line p-3">
                <p className="text-sm font-medium">{it.question}</p>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  <div><p className="mb-1 text-[11px] font-semibold text-muted">Neutral</p><p className="prose-pre rounded bg-panel-2 p-2 text-xs">{it.neutral}</p></div>
                  <div><p className="mb-1 text-[11px] font-semibold text-muted">Styled</p><p className="prose-pre rounded bg-panel-2 p-2 text-xs">{it.styled}</p></div>
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  <Badge tone={it.checks.cited_lines_unchanged ? "good" : "bad"}>{it.checks.cited_lines_unchanged ? "Quoted lines unchanged" : "Quoted lines changed"}</Badge>
                  <Badge tone={it.checks.same_sources ? "good" : "info"}>{it.checks.same_sources ? "Same sources" : "Subject's own passages ranked higher"}</Badge>
                  <Badge tone={it.checks.guardrails_clean ? "good" : "bad"}>{it.checks.guardrails_clean ? "Guardrails clean" : "Guardrails would block"}</Badge>
                  {!it.styled_applied && <Badge>No approved framing applied</Badge>}
                </div>
              </div>
            ))}
            <p className="text-xs text-muted">{preview.note}</p>
          </div>
        )}
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Consent history">
          <Table rows={data.consents} empty="No consent signed yet" cols={[
            { key: "profile_version", label: "Version", render: (c) => `v${c.profile_version}` },
            { key: "at", label: "Signed", render: (c) => fmtDate(c.at) },
            { key: "expires", label: "Expires", render: (c) => fmtDate(c.expires) },
            { key: "state", label: "State", render: (c) => c.withdrawn_at ? <Badge>{c.withdrawn_by === "expired" ? "Expired" : c.withdrawn_by === "superseded" ? "Replaced" : "Withdrawn"} {fmtDate(c.withdrawn_at)}</Badge> : <Badge tone="good">In force</Badge> },
            { key: "demo", label: "", render: (c) => (c.demo ? <DemoBadge /> : null) },
          ]} />
        </Card>
        <Card title="Audit timeline">
          {data.timeline.length === 0 ? <Empty title="Nothing yet" /> : (
            <ol className="space-y-1.5 text-sm">
              {data.timeline.slice(0, 25).map((a: any) => (
                <li key={a.id} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-xs tabular-nums text-muted">{fmtWhen(a.at)}</span>
                  <span>{EVENT_LABEL[a.kind] || "Persona change"}</span>
                  <span className="text-xs text-muted">by {a.actor}</span>
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>

      <Modal open={withdrawOpen} onClose={() => setWithdrawOpen(false)} title={data.active ? "Withdraw consent now" : "Delete or reset the persona"}>
        <div className="space-y-3 text-sm">
          <p>{data.active ? "Styling stops in this request: answers go neutral, styled AI replays are deleted, and past messages keep their text with a note that the consent has ended." : "This deletes the mined profile and phrases. Consent records and the audit log stay."}</p>
          <label className="flex items-center gap-2"><input type="checkbox" checked={deleteProfile || !data.active} disabled={!data.active} onChange={(e) => setDeleteProfile(e.target.checked)} /> Also delete the mined profile and phrases</label>
          <div className="flex flex-wrap gap-2">
            <Button tone="danger" loading={busy === "withdraw"} onClick={async () => {
              await act("withdraw", () => post("/persona/withdraw", { delete_profile: deleteProfile || !data.active }), data.active ? "Consent withdrawn" : "Profile deleted");
              setWithdrawOpen(false);
            }}>{data.active ? "Withdraw now" : "Delete profile"}</Button>
            <Button onClick={() => setWithdrawOpen(false)}>Cancel</Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
