import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { ApiError, api, get, post } from "../api";
import { Badge, Button, ErrorBox, Input, useApp } from "../components/ui";

export type Funnel = {
  funnel_id: number; title: string; live: boolean; page_url: string; apply_url: string; thanks_url: string;
  confirmed_url: string; precall_url: string;
  script_url: string; video_url: string; booking_url: string; applications: number; words?: string;
  words_note?: string | null; minutes?: number;
  copy: { script?: { section: string; text: string }[]; followups?: string[]; prep?: string[] };
  pack: { id: number; status: string } | null;
};

export function FunnelCard({ funnel }: { funnel: Funnel }) {
  const { go, toast } = useApp();
  const [s, setS] = useState<Funnel>(funnel);
  const [video, setVideo] = useState(funnel.video_url);
  const [booking, setBooking] = useState(funnel.booking_url);
  const [precall, setPrecall] = useState(funnel.precall_url || "");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async (what: string, fn: () => Promise<Funnel>, done: string) => {
    setBusy(what);
    setErr(null);
    try {
      setS(await fn());
      toast(done, "good");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const approve = () => run("approve", async () => { await post(`/queue/${s.pack!.id}/approve`); return get<Funnel>(`/funnels/${s.funnel_id}`); }, "Funnel is live");
  const save = () => run("save", () => api<Funnel>(`/funnels/${s.funnel_id}`, { method: "PATCH", body: { video_url: video, booking_url: booking, precall_url: precall } }), "Links saved");
  return (
    <div className="mt-2 rounded-[12px] border border-line bg-panel p-3" data-testid="funnel-card">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-ink">{s.title}</span>
        <Badge tone={s.live ? "good" : "warn"}>{s.live ? "Live" : "Waiting for your approval"}</Badge>
      </div>
      <ul className="mt-2 space-y-0.5 text-[12.5px] text-ink-2">
        <li>VSL script: about {s.minutes ?? 1} minutes, ready to read on camera{s.words === "ai" ? ", written by the AI" : ""}</li>
        {s.words !== "ai" && <li className="text-muted">Template script{s.words_note ? ` (${s.words_note})` : ""}</li>}
        <li>VSL page, application page, thank-you page, booking confirmation page: {s.live ? "live" : "ready to publish"}</li>
        <li>Follow-ups for people who apply but don't book: {s.copy.followups?.length ?? 0} drafted</li>
        {s.live && <li>Applications so far: {s.applications}</li>}
      </ul>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <Input placeholder="Video link (YouTube, Loom, Vimeo)" value={video} onChange={(e) => setVideo(e.target.value)} aria-label="Video link" />
        <Input placeholder="Booking link (Calendly, GHL…)" value={booking} onChange={(e) => setBooking(e.target.value)} aria-label="Booking link" />
        <Input placeholder="Pre-call video link (shown after they book)" value={precall} onChange={(e) => setPrecall(e.target.value)} aria-label="Pre-call video link" />
      </div>
      {s.live && <p className="mt-1.5 text-[12px] text-muted">In your booking tool, set the after-booking redirect to: <span className="select-all text-ink-2">{location.origin + s.confirmed_url}</span></p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {!s.live && s.pack && <Button size="sm" tone="primary" onClick={approve} disabled={!!busy}>{busy === "approve" ? "Publishing…" : "Approve and launch"} <ArrowRight className="size-3.5" /></Button>}
        <Button size="sm" tone="secondary" onClick={save} disabled={!!busy}>{busy === "save" ? "Saving…" : "Save links"}</Button>
        <a href={s.script_url} download><Button size="sm" tone="secondary">Download VSL script</Button></a>
        {s.live && <a href={s.page_url} target="_blank" rel="noreferrer"><Button size="sm" tone="secondary">Open VSL page</Button></a>}
        {s.live && <a href={s.apply_url} target="_blank" rel="noreferrer"><Button size="sm" tone="ghost">Application page</Button></a>}
        {s.live && <a href={s.thanks_url} target="_blank" rel="noreferrer"><Button size="sm" tone="ghost">Thank-you page</Button></a>}
        {s.live && <a href={s.confirmed_url} target="_blank" rel="noreferrer"><Button size="sm" tone="ghost">Confirmation page</Button></a>}
        <Button size="sm" tone="ghost" onClick={() => go("/queue")}>See in Approvals</Button>
      </div>
      {err && <ErrorBox error={err} />}
      {(s.copy.script?.length ?? 0) > 0 && (
        <details className="mt-2 text-[12.5px]">
          <summary className="cursor-pointer text-muted">Read the VSL script</summary>
          {s.copy.script!.map((p) => (
            <div key={p.section} className="mt-2"><div className="font-medium text-ink-2">{p.section}</div>
              <p className="whitespace-pre-wrap text-[12.5px] text-muted">{p.text}</p></div>
          ))}
        </details>
      )}
    </div>
  );
}
