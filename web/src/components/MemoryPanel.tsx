import { useEffect, useState } from "react";
import { Brain, Download, Plus, Trash2, X } from "lucide-react";
import { ApiError, BASE, del, put } from "../api";
import { Badge, Button, Card, ErrorBox, Field, Input, Loading, Modal, Select, TextArea, useApi, useApp } from "./ui";

type Num = { label: string; value: string; source?: string };
type Dec = { text: string; at?: number; source?: string };
type Profile = { offer: string; niche: string; goal: string; notes: string; price: number | null; currency: string };
type Suggestion = { key: string; kind: "profile" | "number"; field?: string; label?: string; value: any; source: string; why: string };
type Mem = { profile: Profile; numbers: Num[]; decisions: Dec[]; updated: number | null; empty: boolean; label: string; suggestions?: Suggestion[] };

const CURRENCIES = ["USD", "GBP", "EUR", "CAD", "AUD"];
const SOURCE_LABEL: Record<string, string> = { you: "You", demo: "Demo", "found:checkin": "Check-in", "found:client": "Client record", "found:student_record": "Student record", "found:prospects": "Prospects" };

function fmtDate(ts?: number | null) {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
}

export default function MemoryPanel() {
  const { me, toast } = useApp();
  const { data, error, loading, reload } = useApi<Mem>("/memory");
  const [p, setP] = useState<Profile | null>(null);
  const [nums, setNums] = useState<Num[]>([]);
  const [decs, setDecs] = useState<Dec[]>([]);
  const [newDec, setNewDec] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!data) return;
    setP({ ...data.profile });
    setNums(data.numbers.map((n) => ({ label: n.label, value: n.value, source: n.source })));
    setDecs(data.decisions.map((d) => ({ text: d.text, at: d.at, source: d.source })));
    setDirty(false);
  }, [data]);

  const change = <K extends keyof Profile>(k: K, v: Profile[K]) => { setP((cur) => (cur ? { ...cur, [k]: v } : cur)); setDirty(true); };
  const suggestions = (data?.suggestions || []).filter((s) =>
    s.kind === "number" ? !nums.some((n) => n.label.toLowerCase() === (s.label || "").toLowerCase()) : p && !(p as any)[s.field || ""]);

  const keep = (s: Suggestion) => {
    if (s.kind === "number") setNums((cur) => [...cur, { label: s.label || "", value: String(s.value), source: s.source }]);
    else if (s.field === "price") change("price", Number(s.value));
    else if (s.field) change(s.field as keyof Profile, String(s.value) as any);
    setDirty(true);
  };

  const save = async () => {
    if (!p) return;
    setBusy(true);
    setErr(null);
    try {
      const clean = nums.filter((n) => n.label.trim() && n.value.trim());
      await put("/memory", {
        profile: { ...p, price: p.price === null || Number.isNaN(p.price as number) ? null : p.price },
        numbers: clean.map((n) => ({ label: n.label.trim(), value: n.value.trim(), source: n.source || "you" })),
        decisions: decs.map((d) => ({ text: d.text, at: d.at ?? null, source: d.source || "you" })),
      });
      toast("Saved. The coach uses this from your next question.", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const wipe = async () => {
    setBusy(true);
    setErr(null);
    try {
      await del("/memory");
      setConfirm(false);
      toast("Deleted everything it knew about you", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };

  const readers = me.user.role === "student" ? "Only you can change it. Your coach and the workspace owner can read it." : "Only you can change it. The workspace owner can read it.";

  return (
    <Card
      title={<span className="flex items-center gap-2"><Brain className="size-4 text-brand" /> What I know about you</span>}
      subtitle={`Your offer, niche, price, numbers and decisions, so advice fits your business. ${readers}`}
      actions={
        <>
          <a href={`${BASE}/memory/export`} download className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-2.5 py-1 text-xs font-medium hover:bg-panel-2"><Download className="size-3.5" /> Export</a>
          <Button size="sm" tone="danger" onClick={() => setConfirm(true)} disabled={!data || data.empty}><Trash2 className="size-3.5" /> Delete all</Button>
        </>
      }
    >
      {loading && !data && <Loading rows={4} />}
      <ErrorBox error={err || error} retry={error ? reload : undefined} />
      {p && (
        <div className="space-y-5" data-testid="memory-panel">
          {data?.empty && !dirty && <p className="rounded-lg border border-dashed border-line px-3 py-2 text-sm text-muted">Nothing saved yet. Fill in what you're happy to share; every field is optional.</p>}
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="What you sell"><TextArea rows={2} value={p.offer} maxLength={300} onChange={(e) => change("offer", e.target.value)} placeholder="e.g. Done-with-you patient acquisition" /></Field>
            <Field label="Who you sell to"><Input value={p.niche} maxLength={200} onChange={(e) => change("niche", e.target.value)} placeholder="e.g. physio clinics in one region" /></Field>
            <Field label="Your price" hint="What one client pays you. Leave empty if you haven't set it.">
              <div className="flex gap-2">
                <div className="min-w-0 flex-1">
                  <Input type="number" min={0} step="any" inputMode="decimal" value={p.price ?? ""}
                    onChange={(e) => change("price", e.target.value === "" ? null : Number(e.target.value))} aria-label="Price" />
                </div>
                <div className="w-24 shrink-0">
                  <Select options={CURRENCIES} value={p.currency} onChange={(e) => change("currency", e.target.value)} aria-label="Currency" />
                </div>
              </div>
            </Field>
            <Field label="Your current goal"><Input value={p.goal} maxLength={300} onChange={(e) => change("goal", e.target.value)} placeholder="e.g. first client by week 6" /></Field>
          </div>
          <Field label="Anything else to remember"><TextArea rows={2} value={p.notes} maxLength={2000} onChange={(e) => change("notes", e.target.value)} /></Field>

          <section>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-sm font-semibold">Your numbers</h4>
              <Button size="sm" onClick={() => { setNums([...nums, { label: "", value: "", source: "you" }]); setDirty(true); }} disabled={nums.length >= 30}><Plus className="size-3.5" /> Add a number</Button>
            </div>
            {nums.length === 0 ? <p className="text-xs text-muted">No numbers yet. Add the ones you track, like openers sent or calls booked last week.</p> : (
              <ul className="space-y-2">
                {nums.map((n, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <div className="min-w-0 flex-1">
                      <Input aria-label="Number name" value={n.label} maxLength={60} placeholder="What it is"
                        onChange={(e) => { const c = [...nums]; c[i] = { ...n, label: e.target.value, source: "you" }; setNums(c); setDirty(true); }} />
                    </div>
                    <div className="w-24 shrink-0 sm:w-28">
                      <Input aria-label="Value" value={n.value} maxLength={40} placeholder="Value"
                        onChange={(e) => { const c = [...nums]; c[i] = { ...n, value: e.target.value, source: "you" }; setNums(c); setDirty(true); }} />
                    </div>
                    {n.source && n.source !== "you" && <Badge tone="info">{SOURCE_LABEL[n.source] || "Found"}</Badge>}
                    <Button size="sm" tone="ghost" aria-label="Remove this number" onClick={() => { setNums(nums.filter((_, j) => j !== i)); setDirty(true); }}><X className="size-3.5" /></Button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h4 className="mb-2 text-sm font-semibold">Decisions you've made</h4>
            <div className="flex gap-2">
              <div className="min-w-0 flex-1"><Input value={newDec} maxLength={300} onChange={(e) => setNewDec(e.target.value)} placeholder="e.g. Chose physio clinics over dental" aria-label="New decision" /></div>
              <Button size="sm" onClick={() => { if (newDec.trim()) { setDecs([{ text: newDec.trim(), at: Date.now() / 1000, source: "you" }, ...decs]); setNewDec(""); setDirty(true); } }} disabled={!newDec.trim() || decs.length >= 100}>Add</Button>
            </div>
            {decs.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {decs.map((d, i) => (
                  <li key={i} className="flex items-start gap-2 rounded-lg border border-line px-3 py-2 text-sm">
                    <span className="min-w-0 flex-1 break-words">{d.text}</span>
                    <span className="shrink-0 text-xs text-muted">{fmtDate(d.at)}</span>
                    <button className="focus-ring shrink-0 text-muted hover:text-bad" aria-label="Remove this decision" onClick={() => { setDecs(decs.filter((_, j) => j !== i)); setDirty(true); }}><X className="size-3.5" /></button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {suggestions.length > 0 && (
            <section className="rounded-lg border border-sky-200 bg-sky-50/70 p-3" data-testid="memory-suggestions">
              <h4 className="text-sm font-semibold text-sky-950">Found in your own records</h4>
              <p className="mb-2 text-xs text-sky-900/80">Picked out by simple rules from your check-ins, client records and prospects (AI not used). Keep the ones that are right.</p>
              <ul className="space-y-1.5">
                {suggestions.map((s) => (
                  <li key={s.key} className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="min-w-0 flex-1 break-words"><b>{s.kind === "number" ? s.label : s.field === "price" ? "Your price" : s.field === "offer" ? "What you sell" : "Who you sell to"}:</b> {String(s.value)} <span className="text-xs text-muted">({s.why})</span></span>
                    <Button size="sm" onClick={() => keep(s)}>Keep</Button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <Button tone="primary" onClick={save} loading={busy} disabled={!dirty}>Save</Button>
            <span className="text-xs text-muted">{dirty ? "Unsaved changes" : data?.updated ? `Last updated ${fmtDate(data.updated)}` : ""}</span>
          </div>
        </div>
      )}
      <Modal open={confirm} onClose={() => setConfirm(false)} title="Delete everything it knows about you?">
        <p className="text-sm">This removes your saved offer, niche, price, numbers and decisions. Your check-ins, lessons and other records are not touched. This can't be undone; export first if you want a copy.</p>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <Button onClick={() => setConfirm(false)}>Keep it</Button>
          <Button tone="danger" onClick={wipe} loading={busy}><Trash2 className="size-3.5" /> Delete all</Button>
        </div>
      </Modal>
    </Card>
  );
}
