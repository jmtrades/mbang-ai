import { ClipboardCheck } from "lucide-react";
import { useState } from "react";
import { ApiError, post } from "../api";
import { Button, Card, Empty, ErrorBox, Field, PageHeader, Select, TextArea, useApi } from "../components/ui";
import { ReviewResult } from "../components/Review";

const SAMPLE = "In today's fast-paced world, unlock your clinic's potential with our game-changer system.\nClick here! Book now! Download the guide!";

export default function Reviewer() {
  const types = useApi<any>("/review/types");
  const [text, setText] = useState("");
  const [at, setAt] = useState("auto");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await post("/review", { text, asset_type: at === "auto" ? null : at }));
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="Work reviewer" subtitle="Ads, funnels, VSLs, Looms, DM scripts, sales calls, bios and posts — scored against the programme rubric, each rule tied to an SOP." />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Paste the work">
          <div className="space-y-3">
            <Field label="Asset type">
              <Select value={at} onChange={(e) => setAt(e.target.value)} options={["auto", ...(types.data?.types || [])]} aria-label="Asset type" />
            </Field>
            <TextArea rows={12} value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste an ad, VSL script, DM, bio, call transcript…" aria-label="Work to review" />
            <div className="flex flex-wrap gap-2">
              <Button tone="primary" onClick={run} loading={busy} disabled={text.trim().length < 2}>Review</Button>
              <Button onClick={() => setText(SAMPLE)}>Load a sample</Button>
            </div>
            <ErrorBox error={err} retry={run} />
          </div>
        </Card>
        <div>{res ? <ReviewResult r={res} /> : <Empty icon={<ClipboardCheck />} title="Nothing reviewed yet" hint="Paste an ad, a script, a DM or a funnel. You get a score, line-level fixes and the SOP each fix comes from." />}</div>
      </div>
    </div>
  );
}
