import { ArrowRight } from "lucide-react";
import { Card, CopyButton, Empty, ErrorBox, Loading, PageHeader, useApi } from "../components/ui";

export default function MorningBrief() {
  const { data, error, loading, reload } = useApi<any>("/brief");
  return (
    <div>
      <PageHeader title="Morning brief" subtitle={data ? `${data.date} — today's few decisions, ranked by rules. Read-only.` : "Today's few decisions, ranked by rules. Read-only."}
        right={data && <CopyButton text={data.text} label="Copy as text" />} />
      <ErrorBox error={error} retry={reload} />
      {loading && <Loading rows={5} />}
      {data && data.items.length === 0 && <Empty title="Nothing needs you this morning" hint="No held drafts, no high-risk students, no bottleneck, no missing logins." />}
      {data && data.items.length > 0 && (
        <ol className="space-y-3" data-testid="brief">
          {data.items.map((it: any) => (
            <li key={it.rank}>
              <a href={it.href} className="focus-ring card group flex items-start gap-4 p-4 hover:border-brand">
                <div className="grid size-9 shrink-0 place-items-center rounded-full bg-side text-sm font-semibold text-white">{it.rank}</div>
                <div className="min-w-0 flex-1"><div className="font-semibold">{it.title}</div><div className="text-sm text-muted">{it.why}</div></div>
                <ArrowRight className="mt-2 size-4 text-muted group-hover:text-brand" />
              </a>
            </li>
          ))}
        </ol>
      )}
      {data && <Card className="mt-4" title="As text"><pre className="prose-pre text-sm">{data.text}</pre></Card>}
    </div>
  );
}
