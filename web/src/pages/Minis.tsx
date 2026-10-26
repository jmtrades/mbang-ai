import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { ApiError, post } from "../api";
import { Button, Card, CopyButton, ErrorBox, Field, Input, Loading, PageHeader, Table, useApi } from "../components/ui";

export default function Minis() {
  const { data, error, loading, reload } = useApi<any>("/minis");
  const kb = useApi<any>("/kb");
  const [f, setF] = useState({ slug: "", title: "", question_limit: "5", scope: "" , consent_text: "I agree to share my email and get follow-up from the team." });
  const [err, setErr] = useState<ApiError | null>(null);
  const create = async () => {
    setErr(null);
    try {
      await post("/minis", { ...f, question_limit: Number(f.question_limit), scope: f.scope ? f.scope.split(",").map((s) => s.trim()) : [] });
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Minis & embed" subtitle="Lead-magnet mini assistants: scoped to chosen material, with a question limit and consent capture. Embed them on any page." />
      <ErrorBox error={err || error} retry={reload} />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="New mini">
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="Slug"><Input value={f.slug} onChange={(e) => setF({ ...f, slug: e.target.value.toLowerCase() })} placeholder="ads-helper" /></Field>
            <Field label="Title"><Input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
            <Field label="Question limit"><Input value={f.question_limit} onChange={(e) => setF({ ...f, question_limit: e.target.value })} /></Field>
            <Field label="Scope (files, comma separated; blank = all)"><Input value={f.scope} onChange={(e) => setF({ ...f, scope: e.target.value })} placeholder={kb.data?.files?.[0]?.file} /></Field>
          </div>
          <Field label="Consent text"><Input value={f.consent_text} onChange={(e) => setF({ ...f, consent_text: e.target.value })} /></Field>
          <Button className="mt-2" tone="primary" onClick={create} disabled={!f.slug || !f.title}>Create mini</Button>
        </Card>
        <Card title="Minis">
          {loading && <Loading />}
          {data && <Table rows={data.items} empty="No minis yet" cols={[
            { key: "title", label: "Mini" }, { key: "question_limit", label: "Limit" }, { key: "sessions", label: "Sessions" },
            { key: "x", label: "", render: (m) => <span className="flex gap-1"><a className="inline-flex items-center gap-1 text-sm text-brand" href={m.preview} target="_blank" rel="noreferrer"><ExternalLink className="size-3.5" /> Preview</a><CopyButton text={m.embed} label="Embed" /></span> },
          ]} />}
        </Card>
      </div>
    </div>
  );
}
