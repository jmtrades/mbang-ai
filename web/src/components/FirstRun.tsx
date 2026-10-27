import { useState } from "react";
import { ArrowRight, CircleCheck, Circle, ListChecks } from "lucide-react";
import { ApiError, post } from "../api";
import { Button, Card, ErrorBox, useApi, useApp } from "./ui";

type Item = { key: string; title: string; why: string; done: boolean; auto: boolean; manual: boolean; href: string; action: string; detail: string };
type FR = { role: string; items: Item[]; done: number; total: number; complete: boolean; dismissed: boolean; label: string };

// First-run checklist for whoever is signed in (owner, student or team member). Progress is saved on the server;
// most steps tick themselves from the person's own records.
export default function FirstRun() {
  const { toast } = useApp();
  const { data, error, reload, setData } = useApi<FR>("/setup/first-run");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  if (error) return <div className="mb-4"><ErrorBox error={error} retry={reload} /></div>;
  if (!data || data.dismissed || data.complete || !data.items.length) return null;

  const mark = async (key: string, done = true) => {
    setBusy(key);
    setErr(null);
    try {
      const d = await post<FR>(`/setup/first-run/${key}`, { done });
      setData(d);
      if (key === "_dismissed") toast("Checklist hidden", "info");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const pct = Math.round((data.done / Math.max(1, data.total)) * 100);
  return (
    <Card
      className="mb-5"
      title={<span className="flex items-center gap-2"><ListChecks className="size-4 text-brand" /> Get started</span>}
      subtitle={`${data.done} of ${data.total} done. Steps tick themselves as you go.`}
      actions={<Button size="sm" tone="ghost" onClick={() => mark("_dismissed")} loading={busy === "_dismissed"}>Hide</Button>}
    >
      <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-black/5" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Checklist progress">
        <div className="h-full rounded-full bg-brand transition-all" style={{ width: `${pct}%` }} />
      </div>
      <ErrorBox error={err} />
      <ol className="divide-y divide-line" data-testid="first-run">
        {data.items.map((it) => (
          <li key={it.key} className="flex flex-wrap items-start gap-3 py-2.5 sm:flex-nowrap">
            {it.done ? <CircleCheck className="mt-0.5 size-5 shrink-0 text-good" aria-label="Done" /> : <Circle className="mt-0.5 size-5 shrink-0 text-muted" aria-label="To do" />}
            <div className="min-w-0 flex-1">
              <div className={it.done ? "text-sm font-medium text-muted line-through decoration-1" : "text-sm font-medium"}>{it.title}</div>
              <div className="text-xs text-muted">{it.detail || it.why}</div>
            </div>
            {!it.done && (
              <div className="flex shrink-0 items-center gap-2">
                {it.manual && <Button size="sm" onClick={() => mark(it.key)} loading={busy === it.key}>Mark done</Button>}
                <a href={it.href} className="focus-ring inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-medium text-brand hover:bg-black/5">{it.action} <ArrowRight className="size-3.5" /></a>
              </div>
            )}
          </li>
        ))}
      </ol>
      <p className="mt-2 text-[11px] text-muted">{data.label}</p>
    </Card>
  );
}
