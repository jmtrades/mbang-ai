import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, ErrorBox, Field, PageHeader, Select, TextArea } from "../components/ui";
import { ReviewResult } from "../components/Review";

const KINDS = [
  { value: "dm", label: "DM (prospect)" },
  { value: "followup", label: "Follow-up (prospect)" },
  { value: "cold_video_pitch", label: "Cold video pitch" },
  { value: "bio", label: "Profile bio" },
  { value: "caption", label: "Caption" },
  { value: "script", label: "Short script" },
  { value: "reminder", label: "Call reminder" },
  { value: "vsl", label: "My VSL (client asset)" },
  { value: "email", label: "My funnel email (client asset)" },
];

export default function Coaching({ query }: { query?: URLSearchParams }) {
  // the Academy's "Practise it" link opens Coaching mode with the matching kind (?kind=dm)
  const asked = query?.get("kind");
  const [kind, setKind] = useState(asked && KINDS.some((k) => k.value === asked) ? asked : "dm");
  const [tier, setTier] = useState("cold");
  const [text, setText] = useState("");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const prospect = ["dm", "followup", "cold_video_pitch"].includes(kind);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post("/coaching", { text, kind, lead_tier: prospect ? tier : null }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="Coaching mode" subtitle="Paste your own DM, follow-up, pitch, bio, caption, script or reminder. You get line-level fixes with citations — every suggestion passes the guardrails." />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Your work">
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="What is it?"><Select value={kind} onChange={(e) => setKind(e.target.value)} options={KINDS} aria-label="Kind" /></Field>
              {prospect && <Field label="Lead temperature" hint="Cold leads get the no-figure line"><Select value={tier} onChange={(e) => setTier(e.target.value)} options={["cold", "warm", "hot", "unqualified"]} aria-label="Lead tier" /></Field>}
            </div>
            <p className="text-xs text-muted">
              Rule applied: {prospect ? <Badge tone="warn">Outreach money rule</Badge> : ["vsl", "email"].includes(kind) ? <Badge tone="info">Client asset rule</Badge> : <Badge>Plain</Badge>}
            </p>
            <TextArea rows={10} value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste it here…" aria-label="Your work" />
            <Button tone="primary" onClick={run} loading={busy} disabled={text.trim().length < 2}>Get line fixes</Button>
            <ErrorBox error={err} retry={run} />
          </div>
        </Card>
        <div>{res ? <ReviewResult r={res} /> : <Card><p className="text-sm text-muted">Fixes appear here, each with the course passage behind it.</p></Card>}</div>
      </div>
    </div>
  );
}
