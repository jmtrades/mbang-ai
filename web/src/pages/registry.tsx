import React, { lazy } from "react";
import {
  Home, Gauge, Sunrise, MessageCircle, MessagesSquare, ClipboardCheck, PenLine, Target, Send, PhoneCall, Repeat,
  Users, UserPlus, CalendarCheck, Briefcase, Activity, Presentation, Megaphone, Sparkles, Network, Inbox, ListTree,
  ScrollText, Library, FlaskConical, ShieldCheck, SlidersHorizontal, Plug, Radio, Server, Contact, Gift, FolderSync,
  Wand2, IdCard, FolderKanban, Settings, Rocket, GraduationCap, BookOpenCheck, UserSearch, UsersRound, ListChecks,
} from "lucide-react";

export type PageDef = {
  key: string;
  label: string;
  group: string;
  icon: React.ComponentType<{ className?: string; strokeWidth?: number | string }>;
  C: React.LazyExoticComponent<React.ComponentType<any>>;
  /** Extra words the command palette matches on. */
  keywords?: string[];
  /** Short label for the phone tab bar. */
  short?: string;
};

const p = (f: () => Promise<{ default: React.ComponentType<any> }>) => lazy(f);

/** Every group, in the default (owner) order. Each role sees its own order, see ROLE_GROUPS. */
export const GROUPS = ["Today", "Students", "Clients", "Pipeline", "Learn", "Build", "Quality", "Data & connections", "Workspace"];

export const PAGES: PageDef[] = [
  { key: "home", label: "Day One", short: "Today", group: "Today", icon: Home, keywords: ["home", "start", "overview", "dashboard"], C: p(() => import("./Home")) },
  { key: "missions", label: "Missions", group: "Today", icon: Rocket, keywords: ["tasks", "today", "first client", "goals"], C: p(() => import("./Missions")) },
  { key: "command", label: "Command centre", short: "Command", group: "Today", icon: Gauge, keywords: ["kpi", "metrics", "dashboard", "overview"], C: p(() => import("./CommandCentre")) },
  { key: "brief", label: "Morning brief", short: "Brief", group: "Today", icon: Sunrise, keywords: ["decisions", "today", "digest"], C: p(() => import("./MorningBrief")) },
  { key: "queue", label: "Approvals", group: "Today", icon: Inbox, keywords: ["approval queue", "drafts", "review", "outbound", "send"], C: p(() => import("./Queue")) },

  { key: "coach", label: "Coach", group: "Learn", icon: MessageCircle, keywords: ["ask", "chat", "question", "student coach", "ai"], C: p(() => import("./Coach")) },
  { key: "threads", label: "Threads", group: "Learn", icon: MessagesSquare, keywords: ["conversations", "history", "chats"], C: p(() => import("./Threads")) },
  { key: "academy", label: "Academy", group: "Learn", icon: GraduationCap, keywords: ["course", "lessons", "training", "modules"], C: p(() => import("./Academy")) },
  { key: "playbooks", label: "Playbooks", group: "Learn", icon: BookOpenCheck, keywords: ["2026", "sops", "strategy", "plays"], C: p(() => import("./Playbooks")) },
  { key: "review", label: "Work reviewer", short: "Review", group: "Learn", icon: ClipboardCheck, keywords: ["feedback", "score", "ad", "script", "vsl", "funnel"], C: p(() => import("./Reviewer")) },
  { key: "coaching", label: "Coaching mode", group: "Learn", icon: PenLine, keywords: ["socratic", "practice", "guided"], C: p(() => import("./Coaching")) },
  { key: "drills", label: "Objection drills", short: "Drills", group: "Learn", icon: Repeat, keywords: ["roleplay", "practice", "objections", "sales training"], C: p(() => import("./Drills")) },

  { key: "prospects", label: "Prospects", group: "Pipeline", icon: UserSearch, keywords: ["leads", "clients", "find", "list", "growth operator"], C: p(() => import("./Prospects")) },
  { key: "sales", label: "Leads & booking", short: "Leads", group: "Pipeline", icon: Target, keywords: ["qualify", "book", "calls", "setter"], C: p(() => import("./Sales")) },
  { key: "outreach", label: "Outreach", group: "Pipeline", icon: Send, keywords: ["dm", "messages", "outreach loop", "openers", "follow up"], C: p(() => import("./Outreach")) },
  { key: "calls", label: "Call review", short: "Calls", group: "Pipeline", icon: PhoneCall, keywords: ["closer", "recording", "transcript", "sales call"], C: p(() => import("./Calls")) },

  { key: "students", label: "Accountability", short: "Students", group: "Students", icon: Users, keywords: ["students", "risk", "retention", "check-ins"], C: p(() => import("./Students")) },
  { key: "onboarding", label: "Onboarding", group: "Students", icon: UserPlus, keywords: ["welcome", "new students", "kickoff"], C: p(() => import("./Onboarding")) },
  { key: "checkin", label: "Weekly check-in", short: "Check-in", group: "Students", icon: CalendarCheck, keywords: ["my check-in", "progress", "mood", "week"], C: p(() => import("./Checkin")) },

  { key: "clients", label: "Client fulfilment", short: "Clients", group: "Clients", icon: Briefcase, keywords: ["delivery", "accounts", "reports"], C: p(() => import("./Clients")) },
  { key: "diagnostic", label: "Bottleneck diagnosis", group: "Clients", icon: Activity, keywords: ["funnel", "numbers", "metrics", "diagnose"], C: p(() => import("./Diagnostic")) },
  { key: "webinar", label: "Webinar chain", group: "Clients", icon: Presentation, keywords: ["webinar ads", "registration", "funnel", "launch"], C: p(() => import("./Webinar")) },
  { key: "ads", label: "Ads ops", group: "Clients", icon: Megaphone, keywords: ["meta", "facebook", "campaigns", "creative", "thunder dome"], C: p(() => import("./AdsOps")) },

  { key: "studio", label: "Studio", group: "Build", icon: Sparkles, keywords: ["create", "assets", "copy", "ads", "vsl"], C: p(() => import("./Studio")) },
  { key: "builder", label: "AI departments", group: "Build", icon: Network, keywords: ["agents", "team", "builder", "automation"], C: p(() => import("./Builder")) },
  { key: "skills", label: "Skills & workers", group: "Build", icon: Wand2, keywords: ["automations", "background jobs"], C: p(() => import("./Skills")) },
  { key: "minis", label: "Minis & embed", group: "Build", icon: Gift, keywords: ["widget", "lead magnet", "embed"], C: p(() => import("./Minis")) },

  { key: "runs", label: "Runs & traces", group: "Quality", icon: ListTree, keywords: ["agent runs", "logs", "trace", "steps"], C: p(() => import("./Runs")) },
  { key: "evals", label: "Evals", group: "Quality", icon: FlaskConical, keywords: ["quality", "accuracy", "tests", "honesty"], C: p(() => import("./Evals")) },
  { key: "golive", label: "Go-live scorecard", group: "Quality", icon: ShieldCheck, keywords: ["launch", "readiness", "checklist"], C: p(() => import("./GoLive")) },
  { key: "audit", label: "Audit log", group: "Quality", icon: ScrollText, keywords: ["history", "events", "who did what", "security"], C: p(() => import("./Audit")) },

  { key: "kb", label: "Knowledge base", group: "Data & connections", icon: Library, keywords: ["material", "documents", "sources", "train", "corpus"], C: p(() => import("./KbAdmin")) },
  { key: "drive", label: "Source sync", group: "Data & connections", icon: FolderSync, keywords: ["google drive", "import", "files"], C: p(() => import("./Drive")) },
  { key: "integrations", label: "Integrations", group: "Data & connections", icon: Plug, keywords: ["connect", "apps", "api keys", "calendar"], C: p(() => import("./Integrations")) },
  { key: "channels", label: "Channels", group: "Data & connections", icon: Radio, keywords: ["telegram", "slack", "sms", "email", "phone"], C: p(() => import("./Channels")) },
  { key: "crm", label: "CRM", group: "Data & connections", icon: Contact, keywords: ["contacts", "deals", "pipeline"], C: p(() => import("./Crm")) },
  { key: "mcp", label: "MCP server", group: "Data & connections", icon: Server, keywords: ["tokens", "tools", "api"], C: p(() => import("./Mcp")) },

  { key: "console", label: "Owner console", group: "Workspace", icon: SlidersHorizontal, keywords: ["alerts", "admin", "controls"], C: p(() => import("./OwnerConsole")) },
  { key: "setup", label: "Workspace setup", group: "Workspace", icon: ListChecks, keywords: ["onboarding", "configure", "getting started"], C: p(() => import("./Setup")) },
  { key: "team", label: "Team & invites", group: "Workspace", icon: UsersRound, keywords: ["members", "roles", "invite", "coaches", "setters", "closers"], C: p(() => import("./Team")) },
  { key: "profile", label: "Business profile", group: "Workspace", icon: IdCard, keywords: ["offer", "niche", "brand voice"], C: p(() => import("./Profile")) },
  { key: "projects", label: "Projects & files", group: "Workspace", icon: FolderKanban, keywords: ["uploads", "documents", "assets"], C: p(() => import("./Projects")) },
  { key: "settings", label: "Settings", group: "Workspace", icon: Settings, keywords: ["preferences", "branding", "theme", "appearance", "dark mode"], C: p(() => import("./SettingsPage")) },
];

export const PAGE_BY_KEY: Record<string, PageDef> = Object.fromEntries(PAGES.map((pg) => [pg.key, pg]));

type Role = "owner" | "coach" | "setter" | "closer" | "student";

/** Group order per role: the work a role does most sits at the top. */
export const ROLE_GROUPS: Record<Role, string[]> = {
  owner: GROUPS,
  coach: ["Today", "Students", "Learn", "Pipeline", "Build", "Quality", "Workspace"],
  setter: ["Today", "Pipeline", "Learn", "Workspace"],
  closer: ["Today", "Pipeline", "Learn", "Workspace"],
  student: ["Today", "Learn", "Pipeline", "Workspace"],
};

/** Most-used first, per role. Pages a role can open but that are not listed keep registry order after these. */
const ROLE_PRIORITY: Record<Role, string[]> = {
  owner: ["home", "command", "queue", "brief", "missions", "students", "onboarding", "clients", "diagnostic", "ads", "webinar",
    "prospects", "sales", "outreach", "calls", "coach", "threads", "academy", "playbooks", "review", "coaching", "drills",
    "studio", "builder", "skills", "minis", "runs", "evals", "golive", "audit", "kb", "drive", "integrations", "channels", "crm", "mcp",
    "console", "setup", "team", "profile", "projects", "settings"],
  coach: ["home", "brief", "queue", "missions", "students", "onboarding", "coach", "threads", "review", "coaching", "academy",
    "playbooks", "drills", "calls"],
  setter: ["home", "missions", "queue", "prospects", "outreach", "sales", "coach", "drills", "academy", "playbooks", "threads"],
  closer: ["home", "missions", "queue", "sales", "calls", "prospects", "coach", "drills", "academy", "playbooks", "threads"],
  student: ["home", "missions", "checkin", "queue", "coach", "academy", "playbooks", "review", "coaching", "drills", "threads", "prospects"],
};

/** A page can sit in a different group for one role (a student's own check-in belongs with today's work). */
const ROLE_GROUP_OVERRIDE: Partial<Record<Role, Record<string, string>>> = {
  student: { checkin: "Today" },
};

/** The four pages on the phone tab bar, per role (a fifth "More" button opens the full menu). */
export const ROLE_TABBAR: Record<Role, string[]> = {
  owner: ["home", "queue", "command", "coach"],
  coach: ["home", "students", "queue", "coach"],
  setter: ["home", "prospects", "outreach", "coach"],
  closer: ["home", "sales", "calls", "coach"],
  student: ["home", "missions", "coach", "checkin"],
};

const asRole = (role: string): Role => (role in ROLE_GROUPS ? (role as Role) : "student");

export function groupOf(page: PageDef, role: string): string {
  return ROLE_GROUP_OVERRIDE[asRole(role)]?.[page.key] || page.group;
}

/** Sidebar sections for a role: groups in the role's order, pages most-used first. */
export function navFor(role: string, allowed: string[]): { group: string; items: PageDef[] }[] {
  const r = asRole(role);
  const prio = ROLE_PRIORITY[r];
  const rank = (k: string) => {
    const i = prio.indexOf(k);
    return i === -1 ? 1000 + PAGES.findIndex((pg) => pg.key === k) : i;
  };
  const pages = PAGES.filter((pg) => allowed.includes(pg.key));
  const order = [...ROLE_GROUPS[r], ...GROUPS.filter((g) => !ROLE_GROUPS[r].includes(g))];
  return order
    .map((group) => ({ group, items: pages.filter((pg) => groupOf(pg, r) === group).sort((a, b) => rank(a.key) - rank(b.key)) }))
    .filter((s) => s.items.length > 0);
}

export function tabbarFor(role: string, allowed: string[]): PageDef[] {
  return ROLE_TABBAR[asRole(role)].filter((k) => allowed.includes(k)).map((k) => PAGE_BY_KEY[k]);
}
