import React, { lazy } from "react";
import {
  Home, Gauge, Sunrise, MessageCircle, MessagesSquare, ClipboardCheck, PenLine, Target, Send, PhoneCall, Repeat,
  Users, UserPlus, CalendarCheck, Briefcase, Activity, Presentation, Megaphone, Sparkles, Network, Inbox, ListTree,
  ScrollText, Library, FlaskConical, ShieldCheck, SlidersHorizontal, Plug, Radio, Server, Contact, Gift, FolderSync,
  Wand2, IdCard, FolderKanban, Settings,
} from "lucide-react";

export type PageDef = { key: string; label: string; group: string; icon: React.ComponentType<{ className?: string }>; C: React.LazyExoticComponent<React.ComponentType<any>> };

const p = (f: () => Promise<{ default: React.ComponentType<any> }>) => lazy(f);

export const GROUPS = ["Overview", "Coach", "Work", "Sales", "Students", "Clients", "Studio", "Owner", "Settings"];

export const PAGES: PageDef[] = [
  { key: "home", label: "Day One", group: "Overview", icon: Home, C: p(() => import("./Home")) },
  { key: "command", label: "Command Centre", group: "Overview", icon: Gauge, C: p(() => import("./CommandCentre")) },
  { key: "brief", label: "Morning brief", group: "Overview", icon: Sunrise, C: p(() => import("./MorningBrief")) },
  { key: "coach", label: "Student Coach", group: "Coach", icon: MessageCircle, C: p(() => import("./Coach")) },
  { key: "threads", label: "Threads", group: "Coach", icon: MessagesSquare, C: p(() => import("./Threads")) },
  { key: "review", label: "Work Reviewer", group: "Work", icon: ClipboardCheck, C: p(() => import("./Reviewer")) },
  { key: "coaching", label: "Coaching mode", group: "Work", icon: PenLine, C: p(() => import("./Coaching")) },
  { key: "sales", label: "Leads & booking", group: "Sales", icon: Target, C: p(() => import("./Sales")) },
  { key: "outreach", label: "Outreach loop", group: "Sales", icon: Send, C: p(() => import("./Outreach")) },
  { key: "calls", label: "Call review", group: "Sales", icon: PhoneCall, C: p(() => import("./Calls")) },
  { key: "drills", label: "Objection drills", group: "Sales", icon: Repeat, C: p(() => import("./Drills")) },
  { key: "students", label: "Accountability", group: "Students", icon: Users, C: p(() => import("./Students")) },
  { key: "onboarding", label: "Onboarding", group: "Students", icon: UserPlus, C: p(() => import("./Onboarding")) },
  { key: "checkin", label: "My check-in", group: "Students", icon: CalendarCheck, C: p(() => import("./Checkin")) },
  { key: "clients", label: "Client fulfilment", group: "Clients", icon: Briefcase, C: p(() => import("./Clients")) },
  { key: "diagnostic", label: "Bottleneck diagnosis", group: "Clients", icon: Activity, C: p(() => import("./Diagnostic")) },
  { key: "webinar", label: "Webinar chain", group: "Clients", icon: Presentation, C: p(() => import("./Webinar")) },
  { key: "ads", label: "Ads Ops", group: "Clients", icon: Megaphone, C: p(() => import("./AdsOps")) },
  { key: "studio", label: "Studio", group: "Studio", icon: Sparkles, C: p(() => import("./Studio")) },
  { key: "builder", label: "AI departments", group: "Studio", icon: Network, C: p(() => import("./Builder")) },
  { key: "queue", label: "Approval queue", group: "Owner", icon: Inbox, C: p(() => import("./Queue")) },
  { key: "runs", label: "Runs & traces", group: "Owner", icon: ListTree, C: p(() => import("./Runs")) },
  { key: "audit", label: "Audit log", group: "Owner", icon: ScrollText, C: p(() => import("./Audit")) },
  { key: "kb", label: "Knowledge base", group: "Owner", icon: Library, C: p(() => import("./KbAdmin")) },
  { key: "evals", label: "Evals", group: "Owner", icon: FlaskConical, C: p(() => import("./Evals")) },
  { key: "golive", label: "Go-live scorecard", group: "Owner", icon: ShieldCheck, C: p(() => import("./GoLive")) },
  { key: "console", label: "Owner console", group: "Owner", icon: SlidersHorizontal, C: p(() => import("./OwnerConsole")) },
  { key: "integrations", label: "Integrations", group: "Owner", icon: Plug, C: p(() => import("./Integrations")) },
  { key: "channels", label: "Channels", group: "Owner", icon: Radio, C: p(() => import("./Channels")) },
  { key: "mcp", label: "MCP server", group: "Owner", icon: Server, C: p(() => import("./Mcp")) },
  { key: "crm", label: "CRM", group: "Owner", icon: Contact, C: p(() => import("./Crm")) },
  { key: "minis", label: "Minis & embed", group: "Owner", icon: Gift, C: p(() => import("./Minis")) },
  { key: "drive", label: "Source sync", group: "Owner", icon: FolderSync, C: p(() => import("./Drive")) },
  { key: "skills", label: "Skills & workers", group: "Owner", icon: Wand2, C: p(() => import("./Skills")) },
  { key: "profile", label: "Business profile", group: "Owner", icon: IdCard, C: p(() => import("./Profile")) },
  { key: "projects", label: "Projects & files", group: "Settings", icon: FolderKanban, C: p(() => import("./Projects")) },
  { key: "settings", label: "Settings", group: "Settings", icon: Settings, C: p(() => import("./SettingsPage")) },
];
