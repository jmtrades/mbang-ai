import { useState } from "react";
import { FolderOpen, Trash2, Upload } from "lucide-react";
import { ApiError, api, del, post } from "../api";
import { Badge, Button, Card, Empty, ErrorBox, Field, Input, Loading, PageHeader, Select, Table, TextArea, useApi, useApp } from "../components/ui";

export default function Projects() {
  const { me, toast } = useApp();
  const projects = useApi<any>("/projects");
  const [pid, setPid] = useState<string>("");
  const mems = useApi<any>(`/memories${pid ? `?project_id=${pid}` : ""}`, [pid]);
  const ups = useApi<any>(`/uploads${pid ? `?project_id=${pid}` : ""}`, [pid]);
  const [name, setName] = useState("");
  const [memo, setMemo] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [receipt, setReceipt] = useState<any>(null);
  const [toKb, setToKb] = useState(false);
  const run = async (what: string, fn: () => Promise<any>) => {
    setBusy(what);
    setErr(null);
    try {
      return await fn();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const upload = async (f: File) => {
    const form = new FormData();
    form.append("file", f);
    if (pid) form.append("project_id", pid);
    if (toKb) form.append("to_kb", "true");
    const r = await run("up", () => api("/uploads", { form }));
    if (r) {
      setReceipt(r);
      ups.reload();
    }
  };
  return (
    <div>
      <PageHeader title="Projects & files" subtitle="Projects with scoped memory you can delete, and uploads (TXT, MD, CSV, JSON, PDF, DOCX, SRT/VTT, HTML, images with local OCR) with a 'what I read' receipt." />
      <ErrorBox error={err} />
      <div className="mb-4 flex flex-wrap items-end gap-2">
        <Field label="Project"><Select value={pid} onChange={(e) => setPid(e.target.value)} options={[{ value: "", label: "No project (general)" }, ...(projects.data?.items || []).map((p: any) => ({ value: String(p.id), label: p.name }))]} /></Field>
        <Input className="w-56" placeholder="New project name" value={name} onChange={(e) => setName(e.target.value)} />
        <Button onClick={async () => { if (await run("p", () => post("/projects", { name }))) { setName(""); projects.reload(); } }} loading={busy === "p"} disabled={!name}>Create project</Button>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Memory" subtitle="Notes the assistant keeps for this scope. Delete any of them at any time.">
          <div className="flex gap-2"><TextArea rows={2} value={memo} onChange={(e) => setMemo(e.target.value)} placeholder="e.g. My niche is dental clinics in Leeds" />
            <Button onClick={async () => { if (await run("m", () => post("/memories", { content: memo, project_id: pid ? Number(pid) : null }))) { setMemo(""); mems.reload(); } }} loading={busy === "m"} disabled={!memo}>Save</Button></div>
          {mems.loading && <Loading />}
          <ul className="mt-3 space-y-1">
            {mems.data?.items.map((m: any) => (
              <li key={m.id} className="flex items-center justify-between gap-2 rounded-md bg-panel-2 px-2 py-1 text-sm">{m.content}
                <Button size="sm" tone="ghost" aria-label="Delete memory" onClick={async () => { await del(`/memories/${m.id}`); mems.reload(); toast("Memory deleted"); }}><Trash2 className="size-3.5" /></Button></li>
            ))}
          </ul>
          {mems.data?.items.length === 0 && <p className="mt-2 text-xs text-muted">No memories in this scope.</p>}
        </Card>
        <Card title="Uploads" subtitle={ups.data ? `Local OCR: ${ups.data.ocr}` : ""} actions={
          <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-panel px-3 py-2 text-sm font-medium hover:bg-panel-2">
            <Upload className="size-4" /> {busy === "up" ? "Reading…" : "Upload a file"}
            <input type="file" className="hidden" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} aria-label="Upload a file" />
          </label>}>
          {me.user.role === "owner" && <label className="mb-2 flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={toKb} onChange={(e) => setToKb(e.target.checked)} /> Add to my knowledge base</label>}
          {receipt && (
            <div className="mb-3 rounded-[10px] border border-good-line bg-good-soft p-3 text-sm text-good-ink" data-testid="receipt">
              <b>What I read:</b> {receipt.summary}
              {receipt.warnings?.map((w: string) => <div key={w} className="text-xs text-warn">{w}</div>)}
              <pre className="prose-pre mt-1 max-h-24 overflow-auto text-xs text-muted">{receipt.preview}</pre>
            </div>
          )}
          {ups.data?.items.length === 0 ? <Empty icon={<FolderOpen />} title="No uploads yet" hint="Add a transcript, a PDF, a doc or a screenshot above. You get a receipt of what was read before anything uses it." /> : (
            <Table rows={ups.data?.items || []} cols={[{ key: "filename", label: "File" }, { key: "kind", label: "Kind", render: (u) => <Badge>{u.kind}</Badge> }, { key: "words", label: "Words", render: (u) => u.receipt.words }]} />
          )}
        </Card>
      </div>
    </div>
  );
}
