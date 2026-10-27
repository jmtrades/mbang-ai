import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { AnimatePresence, m } from "motion/react";
import { CornerDownLeft, Search } from "lucide-react";
import { Kbd, cx } from "./ui";

export type Command = {
  id: string;
  label: string;
  section: "Recent" | "Actions" | "Pages";
  icon: React.ComponentType<{ className?: string }>;
  hint?: string;
  keywords?: string[];
  shortcut?: string;
  run: () => void;
};

// ---------------------------------------------------------------- fuzzy match
// Contiguous substring beats scattered subsequence; word starts and early hits score higher.
// Returns null when the query is not a subsequence of the text.
export function fuzzy(query: string, text: string): { score: number; hits: number[] } | null {
  const q = query.trim().toLowerCase();
  const t = text.toLowerCase();
  if (!q) return { score: 0, hits: [] };
  const at = t.indexOf(q);
  const wordStart = (i: number) => i === 0 || /[\s\-&/(.]/.test(t[i - 1]);
  if (at !== -1) {
    const hits = Array.from({ length: q.length }, (_, k) => at + k);
    return { score: 1000 - at * 4 + (wordStart(at) ? 200 : 0) - t.length, hits };
  }
  let score = 0;
  let ti = 0;
  let run = 0;
  const hits: number[] = [];
  for (const ch of q) {
    if (ch === " ") continue;
    const j = t.indexOf(ch, ti);
    if (j === -1) return null;
    run = j === ti && hits.length ? run + 1 : 0;
    score += 10 + run * 8 + (wordStart(j) ? 18 : 0) - Math.min(j - ti, 12);
    hits.push(j);
    ti = j + 1;
  }
  return { score: score - t.length * 0.5, hits };
}

function scoreCommand(q: string, c: Command): { score: number; hits: number[] } | null {
  const best = fuzzy(q, c.label);
  let s = best ? best.score : -Infinity;
  const extra = [c.hint || "", ...(c.keywords || [])];
  for (const k of extra) {
    const r = fuzzy(q, k);
    if (r && r.score * 0.7 > s) s = r.score * 0.7;
  }
  if (s === -Infinity) return null;
  return { score: s, hits: best?.hits || [] };
}

function Highlight({ text, hits }: { text: string; hits: number[] }) {
  if (!hits.length) return <>{text}</>;
  const set = new Set(hits);
  return (
    <>
      {text.split("").map((ch, i) => (set.has(i) ? <mark key={i} className="bg-transparent font-semibold text-ink">{ch}</mark> : <React.Fragment key={i}>{ch}</React.Fragment>))}
    </>
  );
}

// ---------------------------------------------------------------- palette
export function CommandPalette({ open, onClose, commands }: { open: boolean; onClose: () => void; commands: Command[] }) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  useEffect(() => {
    if (open) {
      setQ("");
      setActive(0);
      const t = setTimeout(() => inputRef.current?.focus(), 10);
      return () => clearTimeout(t);
    }
  }, [open]);

  const results = useMemo(() => {
    if (!q.trim()) return commands.map((c) => ({ c, hits: [] as number[] }));
    const seen = new Set<string>();
    const ranked = commands
      .filter((c) => c.section !== "Recent")
      .map((c) => ({ c, r: scoreCommand(q, c) }))
      .filter((x): x is { c: Command; r: { score: number; hits: number[] } } => !!x.r)
      .sort((a, b) => b.r.score - a.r.score)
      .filter((x) => (seen.has(x.c.id) ? false : (seen.add(x.c.id), true)));
    // when something contains the query as written, scattered-letter matches are just noise
    const exact = ranked.length > 0 && ranked[0].r.score >= 300;
    return ranked.filter((x) => !exact || x.r.score >= 300).map((x) => ({ c: x.c, hits: x.r.hits }));
  }, [q, commands]);

  // group in section order, but when searching keep pure ranking (one "Results" section)
  const sections = useMemo(() => {
    if (q.trim()) return [{ name: "Results", items: results }];
    const order: Command["section"][] = ["Recent", "Actions", "Pages"];
    return order.map((name) => ({ name, items: results.filter((x) => x.c.section === name) })).filter((s) => s.items.length);
  }, [results, q]);
  const flat = sections.flatMap((s) => s.items);

  useEffect(() => {
    setActive(0);
  }, [q]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const runAt = (i: number) => {
    const item = flat[i];
    if (!item) return;
    onClose();
    // let the dialog close before navigating/toggling so focus restores cleanly
    setTimeout(() => item.c.run(), 0);
  };

  const onKey = (e: React.KeyboardEvent) => {
    const n = flat.length;
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) {
      e.preventDefault();
      if (n) setActive((a) => (a + 1) % n);
    } else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) {
      e.preventDefault();
      if (n) setActive((a) => (a - 1 + n) % n);
    } else if (e.key === "Home" && n) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && n) {
      e.preventDefault();
      setActive(n - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      runAt(active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Tab") {
      e.preventDefault();
    }
  };

  let index = -1;
  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[80] flex items-start justify-center px-3 pt-[12vh] sm:px-6" role="dialog" aria-modal="true" aria-label="Command palette" onKeyDown={onKey}>
          <m.div className="absolute inset-0 bg-[var(--overlay)] backdrop-blur-[3px]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} onClick={onClose} />
          <m.div
            initial={{ opacity: 0, scale: 0.97, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -4 }}
            transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
            className="relative flex max-h-[min(560px,72vh)] w-full max-w-[620px] flex-col overflow-hidden rounded-[16px] border border-line bg-panel shadow-xl"
          >
            <div className="flex items-center gap-2.5 border-b border-line-2 px-4">
              <Search className="size-[18px] shrink-0 text-muted" />
              <input
                ref={inputRef}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search pages and actions…"
                aria-label="Search pages and actions"
                role="combobox"
                aria-expanded="true"
                aria-controls={listId}
                aria-activedescendant={flat.length ? `${listId}-${active}` : undefined}
                autoComplete="off"
                spellCheck={false}
                className="h-[52px] min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-placeholder"
              />
              <Kbd className="hidden sm:inline-flex">Esc</Kbd>
            </div>
            <div ref={listRef} id={listId} role="listbox" aria-label="Results" className="scroll-thin flex-1 overflow-y-auto p-2">
              {flat.length === 0 && (
                <div className="px-3 py-10 text-center">
                  <p className="font-medium text-ink">No matches for “{q}”</p>
                  <p className="mt-1 text-[13px] text-muted">Try a page name, like “approvals” or “coach”.</p>
                </div>
              )}
              {sections.map((s) => (
                <div key={s.name} role="group" aria-label={s.name} className="mb-1 last:mb-0">
                  <div className="px-2.5 pb-1 pt-2 text-[11.5px] font-medium text-muted">{s.name}</div>
                  {s.items.map(({ c, hits }) => {
                    index += 1;
                    const i = index;
                    const on = i === active;
                    const Icon = c.icon;
                    return (
                      <div
                        key={`${s.name}-${c.id}`}
                        id={`${listId}-${i}`}
                        role="option"
                        aria-selected={on}
                        data-index={i}
                        onMouseMove={() => active !== i && setActive(i)}
                        onClick={() => runAt(i)}
                        className={cx("flex h-10 cursor-pointer items-center gap-3 rounded-[9px] px-2.5 text-[14px]", on ? "bg-hover text-ink" : "text-ink-2")}
                      >
                        <span className={cx("grid size-6 shrink-0 place-items-center rounded-md border", on ? "border-brand-line bg-brand-soft text-brand" : "border-line bg-panel-2 text-muted")}>
                          <Icon className="size-3.5" />
                        </span>
                        <span className="min-w-0 flex-1 truncate">
                          <Highlight text={c.label} hits={hits} />
                        </span>
                        {c.hint && <span className="hidden shrink-0 text-[12px] text-muted sm:inline">{c.hint}</span>}
                        {c.shortcut && <Kbd className="hidden sm:inline-flex">{c.shortcut}</Kbd>}
                        {on && <CornerDownLeft className="size-3.5 shrink-0 text-muted" />}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
            <div className="hidden items-center gap-4 border-t border-line-2 bg-panel-2 px-4 py-2 text-[12px] text-muted sm:flex">
              <span className="flex items-center gap-1.5"><Kbd>↑</Kbd><Kbd>↓</Kbd> to move</span>
              <span className="flex items-center gap-1.5"><Kbd>↵</Kbd> to open</span>
              <span className="flex items-center gap-1.5"><Kbd>Esc</Kbd> to close</span>
            </div>
          </m.div>
        </div>
      )}
    </AnimatePresence>
  );
}
