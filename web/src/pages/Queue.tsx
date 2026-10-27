import { useState } from "react";
import { Check, Inbox, Pencil, X } from "lucide-react";
import { ApiError, post, put } from "../api";
import { Badge, Button, DemoBadge, Empty, ErrorBox, FlagList, Loading, PageHeader, Reveal, Select, TextArea, TraceLink, cx, isDemo, useApi, useApp } from "../components/ui";

const STATUS_TONE: Record<string, any> = { pending: "info", held: "bad", needs_human: "warn", approved: "good", rejected: "neutral" };
const STATUS_LABEL: Record<string, string> = { pending: "Pending approval", held: "Held by guardrails", needs_human: "Needs a person", approved: "Approved", rejected: "Rejected" };

function Item({ it, index = 0, onChange }: { it: any; index?: number; onChange: () => void }) {
  const { toast } = useApp();
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(it.body_display);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const act = async (what: "approve" | "reject" | "save") => {
    setBusy(what);
    setErr(null);
    try {
      if (what === "save") {
        await put(`/queue/${it.id}`, { body });
        setEditing(false);
      } else {
        const r = await post(`/queue/${it.id}/${what}`, {});
        if (what === "approve") toast(`Approved — ${r.delivered}`, "good");
      }
      onChange();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const open = ["pending", "held", "needs_human"].includes(it.status);
  const kindRaw = String(it.kind || "").replace(/_/g, " ");
  const kind = kindRaw ? kindRaw[0].toUpperCase() + kindRaw.slice(1) : "";
  const channel = String(it.channel || "").replace(/_/g, " ");
  return (
    <Reveal as="li" i={index} className="card overflow-hidden" data-testid="queue-item" data-status={it.status}>
      <div className="p-4 sm:p-5">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[12px] text-muted">
              <span className="tabular-nums">#{it.id}</span>
              <span aria-hidden>·</span>
              <span>{kind}</span>
              {channel && channel !== kindRaw && <><span aria-hidden>·</span><span>{channel}</span></>}
              {it.recipient_ref && <><span aria-hidden>·</span><span className="min-w-0 truncate">To {it.recipient_ref}</span></>}
              {it.run_id ? <><span aria-hidden>·</span><TraceLink runId={it.run_id} /></> : null}
            </div>
            <h3 className="mt-1 text-[15px] font-semibold leading-snug text-ink">{it.title || "(untitled)"}</h3>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {(it.demo || isDemo(it.title) || isDemo(it.recipient_ref)) && <DemoBadge />}
            {it.meta?.client_asset && <Badge tone="info">Client asset</Badge>}
            {it.meta?.prospect_facing && <Badge tone="warn">Prospect-facing</Badge>}
            <Badge tone={STATUS_TONE[it.status]} dot>{STATUS_LABEL[it.status] || it.status}</Badge>
          </div>
        </div>
        {editing ? (
          <TextArea rows={5} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Edit draft" className="mt-3" />
        ) : (
          <p className="prose-pre mt-3 rounded-[10px] border border-line-2 bg-panel-2 px-3.5 py-3 text-[14px] leading-relaxed text-ink">{it.body_display}</p>
        )}
        <div className="mt-3"><FlagList flags={it.flags} /></div>
        {it.delivered && <p className="mt-3 text-[12px] text-muted">Delivery: {it.delivered} · by {it.decided_by}</p>}
        <ErrorBox error={err} />
      </div>
      {open && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line-2 bg-panel-2 px-4 py-3 sm:px-5">
          {editing ? (
            <>
              <Button tone="primary" size="sm" loading={busy === "save"} onClick={() => act("save")}>Save & recheck</Button>
              <Button size="sm" onClick={() => setEditing(false)}>Cancel</Button>
            </>
          ) : (
            <>
              <Button tone="good" size="sm" loading={busy === "approve"} onClick={() => act("approve")} disabled={it.status === "held"} title={it.status === "held" ? "Edit it clean first" : ""}>
                <Check className="size-3.5" /> Approve
              </Button>
              <Button size="sm" onClick={() => setEditing(true)}><Pencil className="size-3.5" /> Edit</Button>
              <Button tone="danger" size="sm" loading={busy === "reject"} onClick={() => act("reject")}><X className="size-3.5" /> Reject</Button>
              {it.status === "held" && <span className="text-[12px] text-muted">Edit it clean, then approve.</span>}
            </>
          )}
        </div>
      )}
    </Reveal>
  );
}

export default function Queue({ query }: { query: URLSearchParams }) {
  const { me, go } = useApp();
  const [status, setStatus] = useState(query.get("status") || "open");
  const ids = query.get("ids");
  const tag = query.get("tag");
  const params = new URLSearchParams();
  if (status !== "all") params.set("status", status);
  if (ids) params.set("ids", ids);
  if (tag) params.set("tag", tag);
  if (query.get("chain_id")) params.set("chain_id", query.get("chain_id")!);
  const { data, error, loading, reload } = useApi<any>(`/queue?${params.toString()}`, [status, ids, tag]);
  return (
    <div>
      <PageHeader
        title="Approval queue"
        subtitle="Everything outbound waits here. Approving marks it ready to send manually or delivers it to a mock transport — nothing reaches a real person from here."
        right={<Select value={status} onChange={(e) => setStatus(e.target.value)} options={[{ value: "open", label: "Open" }, "pending", "held", "needs_human", "approved", "rejected", { value: "all", label: "All" }]} aria-label="Filter" />}
      />
      {(ids || tag) && <p className="mb-3 text-sm text-muted">Filtered to {ids ? `${ids.split(",").length} items from a run` : `tag ${tag}`}. <a className="text-brand" href="#/queue">Show all</a></p>}
      {data?.counts && Object.keys(data.counts).length > 0 && (
        <div className="mb-5 flex flex-wrap gap-2" aria-label="Show only">
          {Object.entries(data.counts).map(([k, v]) => (
            <button key={k} type="button" onClick={() => setStatus(k)} aria-pressed={status === k}
              className={cx("focus-ring inline-flex h-8 items-center gap-2 rounded-full border px-3 text-[12.5px] font-medium transition-colors", status === k ? "border-brand-line bg-brand-soft text-ink" : "border-line bg-panel text-ink-2 shadow-xs hover:border-line-strong hover:text-ink")}>
              <span className={cx("size-1.5 rounded-full", k === "held" ? "bg-bad" : k === "needs_human" ? "bg-warn" : k === "pending" ? "bg-info" : k === "approved" ? "bg-good" : "bg-faint")} />
              {STATUS_LABEL[k] || k}
              <span className="tabular-nums text-muted">{v as number}</span>
            </button>
          ))}
        </div>
      )}
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {data && data.items.length === 0 && (
        <Empty icon={<Inbox />} title="Nothing waiting for a decision" hint="Drafts from agents, the Studio and the builder land here. Nothing is sent until someone approves it."
          action={<>
            {me.tabs.includes("studio") && <Button size="sm" onClick={() => go("/studio")}>Draft something in Studio</Button>}
            {me.tabs.includes("coach") && <Button size="sm" tone="ghost" onClick={() => go("/coach")}>Ask the coach</Button>}
            {status !== "all" && <Button size="sm" tone="ghost" onClick={() => setStatus("all")}>Show everything</Button>}
          </>} />
      )}
      <ul className="space-y-3">{data?.items.map((it: any, i: number) => <Item key={it.id} it={it} index={i} onChange={reload} />)}</ul>
    </div>
  );
}
