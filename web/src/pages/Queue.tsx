import { useState } from "react";
import { Check, Pencil, X } from "lucide-react";
import { ApiError, post, put } from "../api";
import { Badge, Button, Card, DemoBadge, Empty, ErrorBox, FlagList, Loading, PageHeader, Select, TextArea, TraceLink, isDemo, useApi, useApp } from "../components/ui";

const STATUS_TONE: Record<string, any> = { pending: "info", held: "bad", needs_human: "warn", approved: "good", rejected: "neutral" };
const STATUS_LABEL: Record<string, string> = { pending: "Pending approval", held: "Held by guardrails", needs_human: "Needs a person", approved: "Approved", rejected: "Rejected" };

function Item({ it, onChange }: { it: any; onChange: () => void }) {
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
  return (
    <li className="card p-4" data-testid="queue-item" data-status={it.status}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">#{it.id}</span>
        <Badge>{it.kind}</Badge>
        <Badge tone="neutral">{it.channel}</Badge>
        <Badge tone={STATUS_TONE[it.status]}>{STATUS_LABEL[it.status] || it.status}</Badge>
        {(it.demo || isDemo(it.title) || isDemo(it.recipient_ref)) && <DemoBadge />}
        {it.meta?.client_asset && <Badge tone="info">client asset</Badge>}
        {it.meta?.prospect_facing && <Badge tone="warn">prospect-facing</Badge>}
        <TraceLink runId={it.run_id} />
      </div>
      <div className="mb-1 font-medium">{it.title || "(untitled)"}</div>
      {it.recipient_ref && <div className="mb-2 text-xs text-muted">To: {it.recipient_ref}</div>}
      {editing ? (
        <TextArea rows={5} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Edit draft" />
      ) : (
        <p className="prose-pre rounded-lg bg-panel-2 p-3 text-sm">{it.body_display}</p>
      )}
      <div className="mt-2"><FlagList flags={it.flags} /></div>
      {it.delivered && <p className="mt-2 text-xs text-muted">Delivery: {it.delivered} · by {it.decided_by}</p>}
      <ErrorBox error={err} />
      {open && (
        <div className="mt-3 flex flex-wrap gap-2">
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
            </>
          )}
        </div>
      )}
    </li>
  );
}

export default function Queue({ query }: { query: URLSearchParams }) {
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
      {data?.counts && (
        <div className="mb-4 flex flex-wrap gap-2 text-xs">
          {Object.entries(data.counts).map(([k, v]) => <Badge key={k} tone={STATUS_TONE[k]}>{STATUS_LABEL[k] || k}: {v as number}</Badge>)}
        </div>
      )}
      {loading && <Loading rows={6} />}
      <ErrorBox error={error} retry={reload} />
      {data && data.items.length === 0 && <Empty title="Nothing waiting" hint="Drafts from agents, the Studio and the builder land here for approval." />}
      <ul className="space-y-3">{data?.items.map((it: any) => <Item key={it.id} it={it} onChange={reload} />)}</ul>
    </div>
  );
}
