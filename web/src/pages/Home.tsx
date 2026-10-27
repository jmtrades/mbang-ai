import { ArrowRight, BookOpen, Inbox, Target, Users, Workflow, ShieldCheck } from "lucide-react";
import { DemoBadge, ErrorBox, Loading, PageHeader, useApi, useApp } from "../components/ui";
import FirstRun from "../components/FirstRun";

const ICONS: Record<string, any> = { answers: BookOpen, drafts: Inbox, leads: Target, students: Users, runs: Workflow, blocked: ShieldCheck };

export default function Home() {
  const { me } = useApp();
  const { data, error, loading, reload } = useApi<any>("/day-one");
  const brand = me.brand?.name || "Operator AI";
  return (
    <div>
      <PageHeader title={`Day One with ${brand}`} badge={me.is_demo ? <DemoBadge /> : null}
        subtitle="Your programme's AI. It's trained on your material, it speaks under your brand, and it does the work for you and your students. Nothing goes out without your approval." />
      <FirstRun />
      <ErrorBox error={error} retry={reload} />
      {loading && <Loading rows={4} />}
      {data && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" data-testid="outcome-cards">
          {data.cards.filter((c: any) => me.tabs.includes(c.href.replace("#/", "").split("?")[0]) || c.key === "answers").map((c: any) => {
            const Icon = ICONS[c.key] || ArrowRight;
            return (
              <a key={c.key} href={c.href} className="focus-ring card group flex items-start gap-3 p-5 transition hover:border-brand">
                <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-[color-mix(in_oklab,var(--brand)_12%,white)] text-brand"><Icon className="size-5" /></div>
                <div className="min-w-0 flex-1">
                  <div className="text-3xl font-semibold tabular-nums">{c.count}</div>
                  <div className="text-sm text-muted">{c.title}</div>
                </div>
                <ArrowRight className="size-4 text-muted transition group-hover:translate-x-0.5 group-hover:text-brand" />
              </a>
            );
          })}
        </div>
      )}
      <div className="mt-6 grid gap-3 md:grid-cols-3">
        {[
          ["Grounded", "Every answer cites your own material, or says the material doesn't cover it."],
          ["Agentic but gated", "Agents plan, draft, review and queue. You see each step and approve every outgoing item."],
          ["Works without AI", "With no key or budget, rules, templates, retrieval and labelled replays keep everything running."],
        ].map(([t, d]) => (
          <div key={t} className="rounded-xl border border-dashed border-line p-4"><div className="font-semibold">{t}</div><p className="mt-1 text-sm text-muted">{d}</p></div>
        ))}
      </div>
    </div>
  );
}
