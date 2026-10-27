import { useState } from "react";
import { Archive, Link2, MessagesSquare, Pencil } from "lucide-react";
import { ApiError, api, post } from "../api";
import { Badge, Button, Card, Citations, CopyButton, Empty, ErrorBox, Input, Loading, PageHeader, cx, useApi, useApp } from "../components/ui";

export default function Threads({ rest }: { rest: string[] }) {
  const { go } = useApp();
  const [q, setQ] = useState("");
  const [archived, setArchived] = useState(false);
  const list = useApi<any>(`/threads?archived=${archived}${q ? `&q=${encodeURIComponent(q)}` : ""}`, [q, archived]);
  const shared = rest[0] === "shared" ? rest[1] : null;
  const [sel, setSel] = useState<number | null>(null);
  const detail = useApi<any>(shared ? `/threads/shared/${shared}` : sel ? `/threads/${sel}` : null, [sel, shared]);
  const [title, setTitle] = useState("");
  const [link, setLink] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const patch = async (body: any) => {
    try {
      await api(`/threads/${sel}`, { method: "PATCH", body });
      list.reload();
      detail.reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Threads" subtitle="Your coach conversations: auto-titled, searchable, archivable, shareable inside the workspace." right={<Button tone="primary" onClick={() => go("/coach")}>New question</Button>} />
      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Card pad={false}>
          <div className="space-y-2 p-3">
            <Input placeholder="Search threads…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search threads" />
            <label className="flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} /> Show archived</label>
          </div>
          <div className="max-h-[60vh] overflow-auto px-2 pb-2">
            {list.loading && <Loading />}
            <ErrorBox error={list.error} retry={list.reload} />
            {list.data?.items.length === 0 && <Empty icon={<MessagesSquare />} title="No threads yet" hint="Every conversation with the coach is saved here." action={<Button size="sm" tone="primary" onClick={() => { location.hash = "#/coach"; }}>Ask the coach</Button>} />}
            {list.data?.items.map((t: any) => (
              <button key={t.id} onClick={() => { setSel(t.id); setTitle(t.title); setLink(null); }} className={cx("focus-ring block w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-panel-2", sel === t.id && "bg-panel-2 font-medium")}>{t.title}</button>
            ))}
          </div>
        </Card>
        <Card title={detail.data?.title || "Pick a thread"} actions={sel && !shared ? <>
          <Input className="w-48" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Rename" />
          <Button size="sm" onClick={() => patch({ title })}><Pencil className="size-3.5" /> Rename</Button>
          <Button size="sm" onClick={() => patch({ archived: !detail.data?.archived })}><Archive className="size-3.5" /> {detail.data?.archived ? "Unarchive" : "Archive"}</Button>
          <Button size="sm" onClick={async () => { const r = await post(`/threads/${sel}/share`); setLink(`${location.origin}${location.pathname}${r.link}`); }}><Link2 className="size-3.5" /> Share</Button>
        </> : null}>
          <ErrorBox error={err || detail.error} />
          {link && <div className="mb-3 flex items-center gap-2 text-xs"><Badge tone="info">internal link</Badge><code className="truncate">{link}</code><CopyButton text={link} /></div>}
          {detail.loading && <Loading />}
          {detail.data?.messages?.map((m: any) => (
            <div key={m.id} className={cx("mb-3 rounded-lg p-3 text-sm", m.role === "user" ? "bg-panel-2" : "border border-line")}>
              <div className="mb-1 text-xs font-semibold text-muted">{m.role === "user" ? "You" : "Coach"}</div>
              <p className="prose-pre">{m.content}</p>
              <Citations items={m.meta?.citations} />
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
