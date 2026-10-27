// The subject's own view (shown only to the person whose phrasing is used): review the mined phrases, sign consent in
// their own words with scopes, an expiry and their password, and withdraw at any time.
import { useEffect, useState } from "react";
import { ExternalLink, ShieldCheck } from "lucide-react";
import { ApiError, post, put } from "../../api";
import { Badge, Button, Card, DemoBadge, Empty, ErrorBox, Field, Input, TextArea, useApi, useApp } from "../../components/ui";
import { CLASS_LABEL, DecisionBadge, EvidenceChips, NeverDoes, fmtDate, words } from "./shared";

const isoDay = (ts: number) => new Date(ts * 1000).toISOString().slice(0, 10);

export function PersonaSubject() {
  const { toast } = useApp();
  const { data, reload } = useApi<any>("/persona/me");
  const [statement, setStatement] = useState("");
  const [scopes, setScopes] = useState<string[]>(["coach_answers"]);
  const [expires, setExpires] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [confirmWithdraw, setConfirmWithdraw] = useState(false);

  useEffect(() => {
    if (!data?.subject) return;
    setExpires(isoDay(Date.now() / 1000 + data.subject.default_days * 86400));
    if (data.active) setScopes(data.active.scopes);
  }, [data]);

  if (!data || !data.is_subject) return null;
  const s = data.subject;
  const prof = s.profile;
  const tooShort = statement.trim().length < s.min_statement;

  const decide = async (id: number, decision: string) => {
    setErr(null);
    try {
      await put(`/persona/phrases/${id}`, { decision });
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const signNow = async () => {
    setBusy("sign");
    setErr(null);
    try {
      const exp = expires ? new Date(expires + "T23:59:00").getTime() / 1000 : undefined;
      await post("/persona/consent", { statement, scopes, profile_version: prof.version, expires: exp, password });
      setPassword("");
      toast("Consent signed. You can withdraw it at any time.", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const withdrawNow = async () => {
    setBusy("withdraw");
    setErr(null);
    try {
      await post("/persona/withdraw", { reason: "withdrawn by the subject" });
      setConfirmWithdraw(false);
      toast("Withdrawn. Every answer is neutral again.", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mb-6 space-y-4" data-testid="persona-subject">
      <Card title={<span className="flex flex-wrap items-center gap-2">Your teaching phrasing <Badge tone={data.active ? "good" : "neutral"}>{data.status}</Badge>{data.is_demo && <DemoBadge />}</span>}
        subtitle="Only you can see this. The coach may open and close its answers with phrases you approve, only while your consent is in force. It is always an AI assistant, never you.">
        <ErrorBox error={err} />
        {s.reminder && <p className="mb-3 rounded-lg border border-warn-line bg-warn-soft px-3 py-2 text-sm text-warn-ink">{s.reminder}</p>}
        {data.active ? (
          <div className="space-y-3">
            <p className="flex flex-wrap items-center gap-2 text-sm"><ShieldCheck className="size-4 text-good" /> Consent v{data.active.version} signed {fmtDate(data.active.at)}, in force until {fmtDate(data.active.expires)}.</p>
            {s.last_consent?.statement && <blockquote className="rounded-lg border border-line bg-panel-2 p-3 text-sm italic">“{s.last_consent.statement}”</blockquote>}
            {!confirmWithdraw ? (
              <Button tone="danger" className="w-full py-3 text-base sm:w-auto" onClick={() => setConfirmWithdraw(true)} data-testid="withdraw-now">Withdraw now</Button>
            ) : (
              <div className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm">
                <p>Styling stops immediately for everyone. Saved styled answers are deleted. You can sign again later.</p>
                <div className="flex flex-wrap gap-2">
                  <Button tone="danger" loading={busy === "withdraw"} onClick={withdrawNow}>Yes, withdraw now</Button>
                  <Button onClick={() => setConfirmWithdraw(false)}>Keep it</Button>
                </div>
              </div>
            )}
          </div>
        ) : !prof ? (
          <Empty title="Nothing to review yet" hint={`A style profile is built once there are ${words(5000)} words you said in the labelled material.`} />
        ) : (
          <p className="text-sm text-muted">Profile v{prof.version} is ready: {s.approved} phrases approved, {s.pending} waiting for you.</p>
        )}
        <a className="mt-3 inline-flex items-center gap-1 text-xs text-brand hover:underline" href={data.disclosure_url} target="_blank" rel="noreferrer">What students and visitors are told <ExternalLink className="size-3" /></a>
      </Card>

      {prof && (
        <Card title="Your phrases" subtitle={"Approve only what you actually say and are happy to be used. Never use keeps a phrase out of every future version too." + (data.active ? " Changing a decision now pauses styling until you sign again." : "")}>
          {s.phrases.length === 0 ? <Empty title="No phrases were found" hint="Your material may be too short or too close to everyone else's wording." /> : (
            <ul className="space-y-2">
              {s.phrases.map((p: any) => (
                <li key={p.id} className="rounded-lg border border-line p-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">“{p.display || p.phrase}”</span>
                    <span className="text-xs text-muted">{CLASS_LABEL[p.cls] || p.cls} · {p.count}× in {p.docs} docs</span>
                    <DecisionBadge d={p.decision} />
                  </div>
                  <div className="mt-1.5"><EvidenceChips items={p.evidence} /></div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {p.decision !== "approve" && <Button size="sm" tone="good" onClick={() => decide(p.id, "approve")}>Approve</Button>}
                    {p.decision !== "reject" && <Button size="sm" onClick={() => decide(p.id, "reject")}>Reject</Button>}
                    {p.decision !== "never_use" && <Button size="sm" tone="ghost" onClick={() => decide(p.id, "never_use")}>Never use</Button>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {prof && !data.active && (
        <Card title={`Sign consent for profile v${prof.version}`} subtitle="Changing a phrase decision or building a new version later needs a fresh signature.">
          <div className="space-y-3">
            <Field label="In your own words: what you agree to, and what you don't" hint={`At least ${s.min_statement} characters.`}>
              <TextArea rows={4} value={statement} onChange={(e) => setStatement(e.target.value)} placeholder={s.placeholder} />
            </Field>
            <fieldset className="space-y-1.5">
              <legend className="text-xs font-medium text-muted">Where it may be used</legend>
              {s.scopes.map((sc: any) => (
                <label key={sc.key} className="flex items-start gap-2 text-sm">
                  <input type="checkbox" className="mt-1" checked={scopes.includes(sc.key)} onChange={(e) => setScopes(e.target.checked ? [...scopes, sc.key] : scopes.filter((x) => x !== sc.key))} />
                  <span>{sc.label}</span>
                </label>
              ))}
              <p className="text-xs text-muted">Not offered at all: a copy of your voice, your likeness, or outreach in your name.</p>
            </fieldset>
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Expires on" hint="Default 12 months. You'll get a reminder 30 days before."><Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} /></Field>
              <Field label="Your password" hint="Confirms it's really you."><Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
            </div>
            {data.is_demo && <p className="rounded-lg bg-violet-50 px-3 py-2 text-xs text-violet-900">Pitch demo: the sample person's password isn't shared, so signing can't be completed here. Reset the demo in Settings to bring back the sample consent.</p>}
            <Button tone="primary" loading={busy === "sign"} disabled={tooShort || !password || scopes.length === 0 || data.is_demo} onClick={signNow}>Sign consent</Button>
          </div>
        </Card>
      )}

      <Card title="What it never does"><NeverDoes /></Card>
    </div>
  );
}
