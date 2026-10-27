import { useState } from "react";
import { ApiError, post, put } from "../api";
import { Badge, Button, Card, ErrorBox, Field, Input, Loading, PageHeader, Select, Stat, Table, Tabs, useApi, useApp } from "../components/ui";

export default function KbAdmin() {
  const { openSource } = useApp();
  const [tab, setTab] = useState("files");
  const { data, error, loading, reload } = useApi<any>("/kb");
  const prov = useApi<any>(tab === "provenance" ? "/provenance" : null, [tab]);
  const [sel, setSel] = useState<any>(null);
  const [ex, setEx] = useState({ file: "", lines: "" });
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async (patch: any) => {
    setErr(null);
    try {
      await put("/kb", { file: sel.file, ...patch });
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const excluded = (data?.files || []).flatMap((f: any) => f.excluded);
  const ro = !!data?.read_only;
  return (
    <div>
      <PageHeader title="Knowledge base" subtitle={data ? `Scope: ${data.scope} · ${data.status.docs} files · ${data.status.chunks} passages · embeddings: ${data.status.embeddings}` : ""} />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading rows={6} />}
      {data?.read_only_reason && <p className="mb-3 rounded-lg border border-line bg-white px-3 py-2 text-sm text-muted" data-testid="kb-read-only">{data.read_only_reason}</p>}
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "files", label: "Files" }, { key: "notused", label: `Not used (${excluded.length})` }, { key: "provenance", label: "Provenance" }]} />
      {data && tab === "files" && (
        <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
          <Card pad={false}><div className="p-3"><Table rows={data.files} onRow={setSel} cols={[
            { key: "title", label: "File", render: (f) => <div><div className="font-medium">{f.title}</div><div className="text-xs text-muted">{f.file}</div></div> },
            { key: "kind", label: "Kind" }, { key: "quality", label: "Quality", render: (f) => <Badge tone={f.quality === "ok" ? "good" : "warn"}>{f.quality}</Badge> },
            { key: "licence", label: "Licence", render: (f) => <Badge>{f.licence}</Badge> },
            { key: "provenance", label: "Provenance", render: (f) => <Badge tone={f.provenance === "owner_confirmed" ? "good" : "neutral"}>{f.provenance}</Badge> },
            { key: "priority", label: "Priority" },
          ]} /></div></Card>
          <Card title={sel ? sel.title : "Select a file"}>
            {sel && (
              <div className="space-y-2">
                <Button size="sm" onClick={() => openSource({ id: "S", file: sel.file, lines: "1-1", title: sel.title })}>Open source</Button>
                <Field label="Licence"><Select disabled={ro} value={sel.licence} onChange={(e) => { setSel({ ...sel, licence: e.target.value }); save({ licence: e.target.value }); }} options={["course", "own", "third_party_public", "unknown"]} /></Field>
                <Field label="Provenance"><Select disabled={ro} value={sel.provenance} onChange={(e) => { setSel({ ...sel, provenance: e.target.value }); save({ provenance: e.target.value }); }} options={["source_unconfirmed", "owner_confirmed"]} /></Field>
                <Field label="Priority (0.1 – 3)"><Input disabled={ro} value={sel.priority} onChange={(e) => setSel({ ...sel, priority: e.target.value })} onBlur={() => !ro && save({ priority: Number(sel.priority) })} /></Field>
                <Field label="Note"><Input disabled={ro} value={sel.note} onChange={(e) => setSel({ ...sel, note: e.target.value })} onBlur={() => !ro && save({ note: sel.note })} /></Field>
              </div>
            )}
          </Card>
        </div>
      )}
      {data && tab === "notused" && (
        <div className="space-y-4">
          <Card title="Passages not used" subtitle="Dropped at index time by the content policy or an owner exclusion.">
            <Table rows={excluded} empty="Nothing excluded" cols={[{ key: "file", label: "File" }, { key: "lines", label: "Lines" }, { key: "label", label: "Why" }]} />
          </Card>
          {!ro && (
            <Card title="Add an exclusion (by file and line range)" subtitle="Applies to this workspace only.">
              <div className="flex flex-wrap gap-2"><Input className="max-w-xs" placeholder="sops/thunderdome.txt" aria-label="File" value={ex.file} onChange={(e) => setEx({ ...ex, file: e.target.value })} /><Input className="w-32" placeholder="10-12" aria-label="Lines" value={ex.lines} onChange={(e) => setEx({ ...ex, lines: e.target.value })} />
                <Button onClick={async () => { try { await post("/kb/exclusions", ex); setEx({ file: "", lines: "" }); reload(); } catch (e) { setErr(e as ApiError); } }} disabled={!ex.file || !ex.lines}>Exclude</Button></div>
            </Card>
          )}
        </div>
      )}
      {tab === "provenance" && prov.data && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">{Object.entries(prov.data.by_provenance).map(([k, v]) => <Stat key={k} label={k} value={v as number} />)}</div>
          <Card><p className="text-sm text-muted">{prov.data.unconfirmed_note}</p></Card>
        </div>
      )}
    </div>
  );
}
