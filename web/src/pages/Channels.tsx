import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, ErrorBox, Field, PageHeader, TextArea, useApp } from "../components/ui";

const CHANNELS = [
  { key: "telegram", name: "Telegram", how: "Students link with a code (/link CODE), ask with /ask, check in with /checkin 40 2 2 4. In groups the bot only answers mentions.", hook: "hooks/telegram", sig: "X-Telegram-Bot-Api-Secret-Token" },
  { key: "slack", name: "Slack", how: "DMs and @mentions. Every request's signature (v0 HMAC) and timestamp are checked.", hook: "hooks/slack", sig: "X-Slack-Signature" },
  { key: "sms", name: "SMS", how: "Opt-in first: nothing is answered until the number replies START. STOP opts out. Practice mode accepts fictional +1-555-01xx numbers only.", hook: "hooks/sms", sig: "X-Signature" },
  { key: "phone", name: "Phone", how: "Same opt-in rules as SMS (mock transcript in, drafted reply out).", hook: "hooks/phone", sig: "X-Signature" },
  { key: "email", name: "Email (inbound)", how: "Parses the email, strips quoted history, drafts a cited reply. Drafts only.", hook: "hooks/email", sig: "X-Signature" },
];

export default function Channels() {
  const { me } = useApp();
  const [code, setCode] = useState<string | null>(null);
  const [voice, setVoice] = useState<any>(null);
  const [vtext, setVtext] = useState("What are the rules for a Thunder Dome test?");
  const [deck, setDeck] = useState<any>(null);
  const [note, setNote] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const guard = async (fn: () => Promise<any>) => {
    setErr(null);
    try {
      return await fn();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Channels" subtitle="Mock channels. Every reply is drafted into the approval queue; approved replies go to a mock transport. Webhooks are signature-checked." />
      <ErrorBox error={err} />
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {CHANNELS.map((c) => (
          <Card key={c.key} title={c.name} actions={<Badge tone="info">mock</Badge>}>
            <p className="text-sm">{c.how}</p>
            <p className="mt-2 text-xs text-muted">Webhook: <code>/t/{me.tenant.id}/{c.hook}</code> · header <code>{c.sig}</code></p>
            {c.key === "telegram" && <Button size="sm" className="mt-2" onClick={async () => { const r = await guard(() => post("/channels/link-code")); if (r) setCode(r.code); }}>Get my link code</Button>}
            {c.key === "telegram" && code && <p className="mt-1 text-sm">Send <code>/link {code}</code> to the bot.</p>}
          </Card>
        ))}
      </div>
      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <Card title="Web voice (mock)" subtitle="Consent first, monthly minutes quota, transcript and recap. Audio isn't processed here; the transcript is typed.">
          {!voice?.session_id ? (
            <Button tone="primary" onClick={async () => setVoice(await guard(() => post("/voice/start", { consent: true })))}>I consent — start a session</Button>
          ) : (
            <div className="space-y-2">
              <p className="text-xs text-muted">Session #{voice.session_id} · {voice.used_minutes}/{voice.quota_minutes} min used</p>
              <TextArea rows={2} value={vtext} onChange={(e) => setVtext(e.target.value)} />
              <div className="flex gap-2">
                <Button onClick={() => guard(() => post(`/voice/${voice.session_id}/chunk`, { text: vtext, seconds: 20 }))}>Add to transcript (20 s)</Button>
                <Button tone="primary" onClick={async () => { const r = await guard(() => post(`/voice/${voice.session_id}/end`)); if (r) setVoice({ ...voice, recap: r.recap }); }}>End & recap</Button>
              </div>
              {voice.recap && voice.recap.items.map((i: any) => <div key={i.q} className="rounded-lg bg-panel-2 p-2 text-sm"><b>{i.q}</b><p>{i.answer}</p><Citations items={i.citations} /></div>)}
            </div>
          )}
        </Card>
        <Card title="Voice note → cited deck outline">
          <Field label="Voice-note transcript"><TextArea rows={4} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Paste the transcript of your voice note…" /></Field>
          <Button className="mt-2" tone="primary" disabled={note.length < 10} onClick={async () => setDeck(await guard(() => post("/voice-note/deck", { transcript: note })))}>Build deck outline</Button>
          {deck && <ol className="mt-3 space-y-2">{deck.slides.map((s: any) => <li key={s.n} className="rounded-lg border border-line p-2 text-sm"><b>{s.n}. {s.title}</b><ul className="list-disc pl-5 text-xs">{s.bullets.map((b: string) => <li key={b}>{b}</li>)}</ul>{s.citation && <Citations items={[s.citation]} />}</li>)}</ol>}
        </Card>
      </div>
    </div>
  );
}
