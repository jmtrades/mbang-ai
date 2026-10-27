import { m } from "motion/react";
import { ArrowRight, ArrowUpRight, BookOpen, Inbox, MessageCircle, Rocket, ShieldCheck, Target, Users, Workflow, BadgeCheck, Bot, Command } from "lucide-react";
import { Badge, Button, DemoBadge, ErrorBox, Kbd, Skeleton, cx, modKey, tidyCount, useApi, useApp } from "../components/ui";

const ICONS: Record<string, any> = { answers: BookOpen, drafts: Inbox, leads: Target, students: Users, runs: Workflow, blocked: ShieldCheck };
const CAPTION: Record<string, string> = { answers: "Open the coach", drafts: "Review approvals", leads: "See the pipeline", students: "Open accountability", runs: "Inspect traces", blocked: "Read the audit log" };

type Step = { key: string; title: string; why: string; href?: string };

// What a role should do first when there is no ranked brief (or the role has none).
const DEFAULT_STEPS: Record<string, Step[]> = {
  owner: [
    { key: "queue", title: "Clear your approvals", why: "Nothing reaches a person until you approve it." },
    { key: "brief", title: "Read the morning brief", why: "Today's few decisions, ranked." },
    { key: "students", title: "Check who is slipping", why: "Students at risk, with the reason and a drafted nudge." },
    { key: "kb", title: "Add your material", why: "Every answer is grounded in what you upload." },
  ],
  coach: [
    { key: "students", title: "See who is slipping", why: "Risk signals from check-ins, mood and activity." },
    { key: "queue", title: "Approve nudges and notes", why: "Drafted for you; you decide what goes out." },
    { key: "review", title: "Review student work", why: "Line-by-line fixes with lesson citations." },
    { key: "coach", title: "Answer a question", why: "Cited answers from the programme's material." },
  ],
  setter: [
    { key: "prospects", title: "Pick today's prospects", why: "Score and shortlist who to message." },
    { key: "outreach", title: "Send today's openers", why: "Five thoughtful conversations per sender, drafted for you." },
    { key: "queue", title: "Approve your drafts", why: "Every DM waits for a person before it is sent." },
    { key: "drills", title: "Drill one objection", why: "Ten minutes of practice before the first reply." },
  ],
  closer: [
    { key: "sales", title: "Prepare for booked calls", why: "Pre-call briefs and the show-up ladder." },
    { key: "calls", title: "Review your last call", why: "Talk ratio, objections and the next best line." },
    { key: "queue", title: "Approve follow-ups", why: "Nothing is sent without a person." },
    { key: "drills", title: "Drill one objection", why: "Practise the one that cost you the last deal." },
  ],
  student: [
    { key: "missions", title: "Start today's mission", why: "One clear task that moves you toward your first client." },
    { key: "coach", title: "Ask the coach", why: "Paste numbers, a script or a question and get a cited answer." },
    { key: "checkin", title: "Log your weekly check-in", why: "Two minutes. Your coach sees it and replies." },
    { key: "drills", title: "Practise an objection", why: "Role-play the objection you hear most." },
  ],
};

// The hero's one-line promise, in the reader's own terms (a student has no students; a setter's work is DMs).
const LEAD: Record<string, string> = {
  owner: "Your programme's AI. It's trained on your material, it speaks in your brand, and it does the work for you and your students. Nothing goes out without your approval.",
  coach: "It watches every student's check-ins and activity, drafts the nudge, the review and the reply, and cites the lesson behind each one. You approve what goes out.",
  setter: "It shortlists today's prospects, drafts openers in the programme's voice and scores every reply. Every DM waits for a person before it is sent.",
  closer: "It preps every booked call, reviews your recordings line by line and drafts the follow-ups. Nothing is sent without a person.",
  student: "Your programme's coach, on call. It answers from the course with the lesson it came from, reviews your work and sets today's mission toward your first client.",
};

const PRINCIPLES = [
  { icon: BadgeCheck, t: "Grounded", d: "Every answer cites your own material, or says the material doesn't cover it." },
  { icon: ShieldCheck, t: "Agentic, but gated", d: "Agents plan, draft, review and queue. You see each step and approve every outgoing item." },
  { icon: Bot, t: "Works without AI", d: "With no key or budget, rules, templates, retrieval and labelled replays keep everything running." },
];

const EASE = [0.2, 0.8, 0.2, 1] as const;

function greeting(d = new Date()) {
  const h = d.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function NextSteps({ steps, loading, source }: { steps: Step[]; loading: boolean; source: "brief" | "role" }) {
  return (
    <div className="relative flex flex-col rounded-[16px] border border-line bg-[color-mix(in_oklab,var(--panel)_86%,transparent)] p-1.5 shadow-md backdrop-blur-sm">
      <div className="flex items-center justify-between gap-3 px-3 pb-1.5 pt-2.5">
        <h2 className="text-[13.5px] font-semibold text-ink">What to do next</h2>
        <span className="text-[12px] text-muted">{source === "brief" ? "Ranked from today's brief" : "Suggested for your role"}</span>
      </div>
      {loading ? (
        <div className="space-y-2 p-3" aria-busy="true" aria-label="Loading">
          {[0, 1, 2].map((i) => <Skeleton key={i} className="h-11" />)}
        </div>
      ) : (
        <ol className="flex flex-col">
          {steps.map((s, i) => (
            <m.li key={s.key + s.title} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.3, ease: EASE, delay: 0.12 + i * 0.05 }}>
              <a href={s.href || `#/${s.key}`} data-testid="next-step" className="focus-ring group flex items-start gap-3 rounded-[11px] px-3 py-2.5 transition-colors hover:bg-hover">
                <span className={cx("mt-px grid size-6 shrink-0 place-items-center rounded-full text-[12px] font-semibold tabular-nums ring-1", i === 0 ? "bg-brand text-[var(--brand-ink)] ring-transparent" : "bg-panel text-ink-2 ring-line")}>{i + 1}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[14px] font-medium leading-snug text-ink">{s.title}</span>
                  <span className="mt-0.5 line-clamp-2 text-[12.5px] leading-snug text-muted">{s.why}</span>
                </span>
                <ArrowRight className="mt-1 size-4 shrink-0 text-faint transition-[transform,color] group-hover:translate-x-0.5 group-hover:text-brand" />
              </a>
            </m.li>
          ))}
        </ol>
      )}
    </div>
  );
}

export default function Home() {
  const { me, go, openPalette } = useApp();
  const has = (k: string) => me.tabs.includes(k);
  const { data, error, loading, reload } = useApi<any>("/day-one");
  const brief = useApi<any>(has("brief") ? "/brief" : null);
  const brand = me.brand?.name || "Operator AI";
  const shown = (me.user.display || "").replace(/\s*\(DEMO\)\s*/g, "").trim().split(/\s+/)[0];
  // demo users are named after their role; "Good morning, Owner" reads oddly, so only greet real names
  const first = shown && shown.toLowerCase() !== me.user.role.toLowerCase() ? shown : "";
  const today = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" }).format(new Date());

  const allowedHref = (href: string) => has(href.replace("#/", "").split("?")[0]);
  const briefSteps: Step[] = (brief.data?.items || [])
    .filter((it: any) => it.href && allowedHref(it.href))
    .slice(0, 4)
    .map((it: any) => ({ key: String(it.rank), title: tidyCount(it.title), why: tidyCount(it.why || ""), href: it.href }));
  const roleSteps = (DEFAULT_STEPS[me.user.role] || DEFAULT_STEPS.student).filter((s) => has(s.key));
  const steps = briefSteps.length ? briefSteps : roleSteps;
  // The outcome counts are workspace-wide. Everyone but the owner only decides their own drafts, so their
  // "waiting for you" card counts the same list their approval queue shows.
  const mine = useApi<any>(me.user.role !== "owner" && has("queue") ? "/queue?status=open" : null);
  const cards = (data?.cards || [])
    .filter((c: any) => allowedHref(c.href) || c.key === "answers")
    .map((c: any) => (c.key === "drafts" && mine.data ? { ...c, count: mine.data.items.length } : c));

  return (
    <div>
      {/* ------------------------------------------------------------ hero */}
      <section className="hero-surface card overflow-hidden">
        <div className="grid gap-8 p-5 sm:p-8 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] lg:gap-12 lg:p-10">
          <div className="flex min-w-0 flex-col justify-center">
            <m.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3, ease: EASE }} className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] font-medium text-muted">
              <span>{today}</span>
              <span aria-hidden className="text-faint">·</span>
              <span>{greeting()}{first ? `, ${first}` : ""}</span>
              {me.is_demo && <DemoBadge />}
            </m.div>
            <m.h1 initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, ease: EASE, delay: 0.04 }} className="t-display text-ink">
              Day One with{" "}
              <span className="bg-[linear-gradient(100deg,var(--brand-fg),color-mix(in_oklab,var(--brand-fg)_55%,var(--ink)))] bg-clip-text text-transparent">{brand}</span>
            </m.h1>
            <m.p initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, ease: EASE, delay: 0.08 }} className="t-lead mt-4 max-w-xl max-sm:text-[14.5px]">
              {LEAD[me.user.role] || LEAD.student}
            </m.p>
            <m.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, ease: EASE, delay: 0.12 }} className="mt-7 flex flex-col gap-2.5 sm:flex-row sm:flex-wrap">
              {has("missions") && (
                <Button tone="primary" size="lg" onClick={() => go("/missions")} data-testid="cta-missions">
                  <Rocket className="size-[18px]" /> Start today's mission <ArrowRight className="size-4 opacity-80" />
                </Button>
              )}
              {has("coach") && (
                <Button size="lg" onClick={() => go("/coach")} data-testid="cta-coach">
                  <MessageCircle className="size-[18px] text-brand" /> Ask the coach
                </Button>
              )}
            </m.div>
            {openPalette && (
              <button type="button" onClick={openPalette} className="focus-ring mt-5 hidden w-fit items-center gap-1.5 rounded-md text-[12.5px] text-muted transition-colors hover:text-ink sm:flex">
                <Command className="size-3.5" /> Jump to any page or action with <Kbd>{modKey === "⌘" ? "⌘K" : "Ctrl K"}</Kbd>
              </button>
            )}
          </div>
          <m.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, ease: EASE, delay: 0.1 }} className="min-w-0">
            <NextSteps steps={steps} loading={has("brief") && brief.loading} source={briefSteps.length ? "brief" : "role"} />
          </m.div>
        </div>
      </section>

      {/* ------------------------------------------------------------ outcomes */}
      <section className="mt-10" aria-labelledby="outcomes-title">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 id="outcomes-title" className="t-h2 text-ink">What {brand} has done so far</h2>
            <p className="mt-0.5 text-[13.5px] text-muted">Live counts from this workspace. Each one opens the work behind it.</p>
          </div>
          {has("command") && (
            <a href="#/command" className="focus-ring group inline-flex items-center gap-1 rounded text-[13px] font-medium text-brand">
              Command centre <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" />
            </a>
          )}
        </div>
        <ErrorBox error={error} retry={reload} />
        {(loading || (data && mine.loading)) && (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3" aria-busy="true" aria-label="Loading">
            {[0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-[128px] rounded-[12px]" />)}
          </div>
        )}
        {data && !mine.loading && (
          <div className={cx("grid grid-cols-2 gap-3", cards.length === 4 ? "lg:grid-cols-4" : cards.length <= 2 ? "lg:grid-cols-2" : "lg:grid-cols-3")} data-testid="outcome-cards">
            {cards.map((c: any, i: number) => {
              const Icon = ICONS[c.key] || ArrowRight;
              const needsYou = c.key === "drafts" && c.count > 0;
              return (
                <m.a
                  key={c.key}
                  href={c.href}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.32, ease: EASE, delay: i * 0.04 }}
                  className="focus-ring card card-hover group relative flex flex-col p-4 hover:-translate-y-px sm:p-5"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="text-[28px] font-semibold leading-none tracking-[-0.03em] tabular-nums text-ink sm:text-[32px]">{Number(c.count).toLocaleString()}</div>
                    <span className={cx("grid size-8 shrink-0 place-items-center rounded-[9px] ring-1", c.key === "blocked" ? "bg-good-soft text-good ring-good-line" : "bg-brand-soft text-brand ring-brand-line")}>
                      <Icon className="size-4" />
                    </span>
                  </div>
                  <div className="mt-2.5 text-[13px] leading-snug text-muted">{c.title}</div>
                  <div className="mt-auto flex items-center justify-between gap-2 pt-3">
                    {needsYou ? <Badge tone="warn" dot>Needs you</Badge> : <span className="truncate text-[12px] font-medium text-muted transition-colors group-hover:text-brand">{CAPTION[c.key] || "Open"}</span>}
                    <ArrowUpRight className="size-4 shrink-0 text-faint transition-[transform,color] group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-brand" />
                  </div>
                </m.a>
              );
            })}
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------ principles */}
      <section className="mt-10 grid gap-x-8 gap-y-5 border-t border-line pt-8 md:grid-cols-3" aria-label="How it works">
        {PRINCIPLES.map(({ icon: I, t, d }) => (
          <div key={t} className="flex gap-3">
            <span className="grid size-8 shrink-0 place-items-center rounded-[9px] border border-line bg-panel text-muted shadow-xs"><I className="size-4" /></span>
            <div>
              <div className="text-[14px] font-semibold text-ink">{t}</div>
              <p className="mt-0.5 text-[13px] leading-relaxed text-muted">{d}</p>
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}
