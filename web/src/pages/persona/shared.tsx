// Shared pieces for the Persona screens (owner console tab, the subject's own view, the styled-answer badge).
import { useState } from "react";
import { FileText } from "lucide-react";
import { Badge, Button, Modal, cx, useApp } from "../../components/ui";

export const CLASS_LABEL: Record<string, string> = {
  opener: "Opens answers",
  closer: "Closes answers",
  tag: "Check-in question",
  transition: "Moves between points",
  signature: "Signature phrase",
};

export const DECISION_LABEL: Record<string, { text: string; tone: "good" | "bad" | "warn" | "neutral" }> = {
  approve: { text: "Approved", tone: "good" },
  reject: { text: "Rejected", tone: "neutral" },
  never_use: { text: "Never use", tone: "bad" },
  pending: { text: "Waiting for review", tone: "warn" },
};

export const EVENT_LABEL: Record<string, string> = {
  "persona.subject": "Subject chosen",
  "persona.identity": "Name or aliases changed",
  "persona.mine": "Style profile built",
  "persona.approve": "Phrase decision",
  "persona.consent": "Consent signed",
  "persona.consent_fail": "Consent attempt refused (wrong password)",
  "persona.withdraw": "Consent withdrawn",
  "persona.expired": "Consent expired",
  "persona.attribution": "Speaker marked on a document",
};

export const fmtDate = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "");
export const fmtWhen = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
export const words = (n?: number) => (n || 0).toLocaleString();

export function styleInPlainWords(s: any): string[] {
  if (!s || !s.sentence_words_mean) return [];
  const out = [`Sentences average ${Math.round(s.sentence_words_mean)} words (nine in ten under ${Math.round(s.sentence_words_p90)}).`];
  if (s.question_rate >= 0.08) out.push(`Asks a question in about ${Math.round(s.question_rate * 100)}% of sentences.`);
  if (s.you_per_100 >= 3) out.push(`Talks straight to the student: "you" about ${Math.round(s.you_per_100)} times per 100 words.`);
  if (s.we_i_ratio >= 1.2) out.push(`Says "we" and "let's" more than "I".`);
  if (s.contraction_rate >= 0.03) out.push("Uses contractions (don't, you're).");
  if (s.imperative_rate >= 0.15) out.push(`Gives direct instructions in ${Math.round(s.imperative_rate * 100)}% of sentences.`);
  if (s.certainty_hedge_ratio >= 1.5) out.push("Direct and certain rather than hedged.");
  out.push("Filler words are measured but never copied.");
  return out;
}

export function CoverageMeter({ totals }: { totals: any }) {
  const draft = totals.draft_at || 5000;
  const stable = totals.stable_at || 20000;
  const pct = Math.min(100, Math.round((100 * (totals.subject_words || 0)) / stable));
  const draftPct = Math.round((100 * draft) / stable);
  return (
    <div data-testid="coverage-meter">
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
        <span><b className="tabular-nums">{words(totals.subject_words)}</b> words said by the subject</span>
        <span className="text-xs text-muted">Draft at {words(draft)} · stable at {words(stable)}</span>
      </div>
      <div className="relative mt-2 h-3 overflow-hidden rounded-full bg-black/5" role="meter" aria-valuemin={0} aria-valuemax={stable} aria-valuenow={totals.subject_words || 0} aria-label="Attributed words">
        <div className={cx("h-full rounded-full", (totals.subject_words || 0) >= draft ? "bg-good" : "bg-warn")} style={{ width: `${pct}%` }} />
        <div className="absolute inset-y-0 w-px bg-ink/40" style={{ left: `${draftPct}%` }} />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>{totals.subject_docs || 0} documents with the subject</span>
        <span>{totals.calls || 0} group calls</span>
        <span>{words(totals.other_words)} words by other speakers (used only as contrast)</span>
        <span>{words(totals.unattributed_words)} unlabelled words (never guessed)</span>
      </div>
    </div>
  );
}

export function EvidenceChips({ items, max = 2 }: { items: any[]; max?: number }) {
  const { openSource } = useApp();
  const [open, setOpen] = useState<any | null>(null);
  const [all, setAll] = useState(false);
  if (!items?.length) return <span className="text-xs text-muted">No evidence kept</span>;
  const shown = all ? items : items.slice(0, max);
  return (
    <div className="flex flex-wrap gap-1">
      {items.length > max && !all && (
        <button className="order-last text-[11px] text-brand underline-offset-2 hover:underline" onClick={() => setAll(true)}>+{items.length - max} more</button>
      )}
      {shown.map((e: any) => (
        <button key={e.file + e.lines} onClick={() => setOpen(e)} className="focus-ring inline-flex max-w-full items-center gap-1 rounded-md border border-line bg-panel-2 px-1.5 py-0.5 text-[11px] hover:border-brand" title={e.quote} data-testid="evidence-chip">
          <FileText className="size-3 shrink-0 text-brand" />
          <span className="max-w-[9rem] truncate">{e.file.split("/").pop()}</span>
          <span className="text-muted">L{e.lines.split("-")[0]} · ×{e.count}</span>
        </button>
      ))}
      <Modal open={!!open} onClose={() => setOpen(null)} title="Where this phrase was said">
        {open && (
          <div className="space-y-3 text-sm">
            <blockquote className="rounded-lg border border-line bg-panel-2 p-3 italic">“{open.quote}”</blockquote>
            <p className="text-xs text-muted">{open.file} · line {open.lines.split("-")[0]} · seen {open.count} time{open.count === 1 ? "" : "s"} in this document</p>
            {!open.file.startsWith("demo/") && (
              <Button size="sm" onClick={() => { const e = open; setOpen(null); openSource({ id: "E", file: e.file, lines: e.lines, title: e.file }); }}>Open the full source</Button>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

export function DecisionBadge({ d }: { d: string }) {
  const x = DECISION_LABEL[d] || DECISION_LABEL.pending;
  return <Badge tone={x.tone}>{x.text}</Badge>;
}

export function NeverDoes() {
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm">
      <li>Speak as the person or claim to be them. It always says it is an AI assistant.</li>
      <li>Tell personal stories, clients or results as theirs.</li>
      <li>Use a cloned voice or their likeness. Read-aloud and the phone line use a neutral system voice.</li>
      <li>Appear in outreach, client material, calendar invites, ads or CRM actions.</li>
    </ul>
  );
}
