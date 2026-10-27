import { useState } from "react";
import { Check, Inbox, Pencil, X } from "lucide-react";
import { ApiError, post, put } from "../api";
import { Badge, Button, DemoBadge, Empty, ErrorBox, FlagList, Loading, PageHeader, Reveal, Select, TextArea, TraceLink, cx, humanize, isDemo, useApi, useApp } from "../components/ui";

const STATUS_TONE: Record<string, any> = { pending: "info", held: "bad", needs_human: "warn", approved: "good", rejected: "neutral" };
const STATUS_LABEL: Record<string, string> = { pending: "Pending approval", held: "Held by guardrails", needs_human: "Needs a person", approved: "Approved", rejected: "Rejected" };
// most urgent first, whatever order the server counts arrive in
const STATUS_ORDER = ["held", "needs_human", "pending", "approved", "rejected"];
const DOT: Record<string, string> = { held: "bg-bad", needs_human: "bg-warn", pending: "bg-info", approved: "bg-good", rejected: "bg-faint" };

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
        {/* phones: badges drop below the title instead of squeezing it into a narrow column */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[12px] text-muted">
              <span className="tabular-nums">#{it.id}</span>
              <span aria-hidden>·</span>
              <span>{kind}</span>
              {channel && channel !== kindRaw && <><span aria-hidden>·</span><span>{channel}</span></>}
              {it.recipient_ref && <><span aria-hidden>·</span><span className="min-w-0 break-all">To {it.recipient_ref}</span></>}
              {it.run_id ? <><span aria-hidden>·</span><TraceLink runId={it.run_id} /></> : null}
            </div>
            <h3 className="mt-1 text-[15px] font-semibold leading-snug text-ink">{it.title || "(untitled)"}</h3>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-1.5 sm:justify-end">
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
  // The server's counts cover the whole workspace. Everyone but the owner sees only the items they can
  // decide, so their chips are counted from that same list; otherwise the chips promise work the list doesn't show.
  const owner = me.user.role === "owner";
  const mine = useApi<any>(owner || !data ? null : "/queue", [data]);
  const counts: Record<string, number> = owner
    ? data?.counts || {}
    : (mine.data?.items || []).reduce((acc: Record<string, number>, it: any) => ({ ...acc, [it.status]: (acc[it.status] || 0) + 1 }), {});
  const chips = STATUS_ORDER.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !STATUS_ORDER.includes(k) && counts[k]));
  return (
    <div>
      <PageHeader
        title="Approval queue"
        subtitle="Everything outbound waits here. Approving marks it ready to send manually or delivers it to a mock transport — nothing reaches a real person from here."
      />
      {(ids || tag) && <p className="mb-3 text-sm text-muted">Filtered to {ids ? `${ids.split(",").length} items from a run` : `tag ${tag}`}. <a className="text-brand" href="#/queue">Show all</a></p>}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        {chips.length > 0 && (
          <div className="flex flex-wrap gap-2" role="group" aria-label="Show only">
            {chips.map((k) => (
              <button key={k} type="button" onClick={() => setStatus(status === k ? "open" : k)} aria-pressed={status === k}
                className={cx("focus-ring inline-flex h-8 items-center gap-2 rounded-full border px-3 text-[12.5px] font-medium transition-colors", status === k ? "border-brand-line bg-brand-soft text-ink" : "border-line bg-panel text-ink-2 shadow-xs hover:border-line-strong hover:text-ink")}>
                <span className={cx("size-1.5 rounded-full", DOT[k] || "bg-faint")} />
                {STATUS_LABEL[k] || humanize(k)}
                <span className="tabular-nums text-muted">{counts[k]}</span>
              </button>
            ))}
          </div>
        )}
        <Select className="sm:ml-auto sm:w-48" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter"
          options={[{ value: "open", label: "All open" }, ...STATUS_ORDER.map((k) => ({ value: k, label: STATUS_LABEL[k] })), { value: "all", label: "Everything" }]} />
      </div>
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
