import { ArrowRight, Sunrise } from "lucide-react";
import { Button, Card, CopyButton, Empty, ErrorBox, Loading, PageHeader, Reveal, useApi, useApp } from "../components/ui";

export default function MorningBrief() {
  const { me, go } = useApp();
  const { data, error, loading, reload } = useApi<any>("/brief");
  return (
    <div>
      <PageHeader title="Morning brief" subtitle={data ? `${data.date} — today's few decisions, ranked by rules. Read-only.` : "Today's few decisions, ranked by rules. Read-only."}
        right={data && <CopyButton text={data.text} label="Copy as text" />} />
      <ErrorBox error={error} retry={reload} />
      {loading && <Loading rows={5} />}
      {data && data.items.length === 0 && <Empty icon={<Sunrise />} title="Nothing needs you this morning" hint="No held drafts, no high-risk students, no bottleneck, no missing logins." action={me.tabs.includes("command") ? <Button size="sm" onClick={() => go("/command")}>Open the command centre</Button> : <Button size="sm" onClick={() => go("/")}>Back to Day One</Button>} />}
      {data && data.items.length > 0 && (
        <ol className="space-y-3" data-testid="brief">
          {data.items.map((it: any, i: number) => (
            <Reveal as="li" key={it.rank} i={i}>
              <a href={it.href} className="focus-ring card card-hover group flex items-start gap-4 p-4 hover:border-brand-line sm:p-5">
                <div className={i === 0 ? "grid size-8 shrink-0 place-items-center rounded-full bg-brand text-[13px] font-semibold tabular-nums text-[var(--brand-ink)]" : "grid size-8 shrink-0 place-items-center rounded-full bg-brand-soft text-[13px] font-semibold tabular-nums text-brand ring-1 ring-brand-line"}>{it.rank}</div>
                <div className="min-w-0 flex-1"><div className="text-[15px] font-semibold text-ink">{it.title}</div><div className="mt-0.5 text-[13.5px] text-muted">{it.why}</div></div>
                <ArrowRight className="mt-1.5 size-4 text-faint transition-[transform,color] group-hover:translate-x-0.5 group-hover:text-brand" />
              </a>
            </Reveal>
          ))}
        </ol>
      )}
      {data && <Card className="mt-4" title="As text"><pre className="prose-pre text-sm">{data.text}</pre></Card>}
    </div>
  );
}
