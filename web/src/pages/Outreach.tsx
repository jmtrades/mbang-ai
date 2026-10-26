import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, post } from "../api";
import { Badge, Button, Card, DemoBadge, Empty, ErrorBox, Field, Input, Loading, PageHeader, Select, Stat, Table, Tabs, useApi, useApp } from "../components/ui";

export default function Outreach() {
  const { go, me } = useApp();
  const [tab, setTab] = useState("loop");
  const { data, error, loading, reload } = useApi<any>("/outreach");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [goal, setGoal] = useState({ goal: "30000", price: "3000", senders: "1" });
  const [calc, setCalc] = useState<any>(null);
  const act = async (what: string, fn: () => Promise<any>) => {
    setBusy(what);
    setErr(null);
    try {
      const r = await fn();
      reload();
      return r;
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const openers = async () => {
    const r = await act("openers", () => post("/outreach/openers", { limit: 10 }));
    if (r?.queued?.length) go(`/queue?ids=${r.queued.join(",")}`);
  };
  return (
    <div>
      <PageHeader title="Outreach loop" subtitle="Practice prospects (DEMO): opener → value-only follow-ups → stage tracking. Every message goes to the approval queue; approved items go to a mock Instagram transport." badge={<DemoBadge />}
        right={<><Button tone="primary" onClick={openers} loading={busy === "openers"}>Queue openers</Button><Button onClick={() => act("fu", () => post("/outreach/followups"))} loading={busy === "fu"}>Queue follow-ups</Button></>} />
      <ErrorBox error={err || error} retry={reload} />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "loop", label: "Prospects" }, { key: "lab", label: "Opener lab" }, { key: "funnel", label: "Weekly funnel" }, { key: "league", label: "Setter league" }, { key: "goal", label: "Goal calculator" }]} />
      {loading && <Loading rows={6} />}
      {data && tab === "loop" && (
        data.prospects.length === 0 ? <Empty title="No practice prospects here" hint={me.is_demo ? "Reset the demo to reseed." : "Practice prospects live in the pitch demo workspace."} /> : (
          <Card pad={false}><div className="p-3"><Table rows={data.prospects} cols={[
            { key: "name", label: "Prospect", render: (p) => <span className="flex items-center gap-1.5">{p.name.replace(" (DEMO)", "")} <DemoBadge /></span> },
            { key: "opener_move", label: "Opener move" }, { key: "sender", label: "Sender" },
            { key: "stage", label: "Stage", render: (p) => <Badge>{p.stage}</Badge> },
            { key: "act", label: "", render: (p) => p.stage === "sent" ? <Button size="sm" onClick={() => act("r" + p.id, () => post(`/outreach/prospects/${p.id}/reply`, { text: "practice reply" }))}>Record reply</Button> : p.stage === "replied" ? (
              <Select value="" onChange={(e) => act("s" + p.id, () => post(`/outreach/prospects/${p.id}/stage`, { stage: e.target.value }))} options={[{ value: "", label: "Advance…" }, ...data.stages.slice(3).map((s: string) => ({ value: s, label: s }))]} aria-label="Advance stage" />
            ) : null },
          ]} /></div></Card>
        )
      )}
      {data && tab === "lab" && (
        <Card title="DM opener lab" subtitle="Reply rate by opening move with a 95% (Wilson) interval">
          <Table rows={data.lab} empty="Approve some openers and record replies first" cols={[
            { key: "move", label: "Move" }, { key: "sent", label: "Sent" }, { key: "replies", label: "Replies" },
            { key: "rate", label: "Reply rate", render: (r) => `${(r.rate * 100).toFixed(1)}%` },
            { key: "ci95", label: "95% interval", render: (r) => `${(r.ci95[0] * 100).toFixed(1)}% – ${(r.ci95[1] * 100).toFixed(1)}%` },
          ]} />
        </Card>
      )}
      {data && tab === "funnel" && (
        <Card title="Weekly funnel">
          {data.funnel.length === 0 ? <p className="text-sm text-muted">No activity yet.</p> : (
            <div className="h-72"><ResponsiveContainer><BarChart data={data.funnel}><CartesianGrid strokeDasharray="3 3" stroke="#e4e3dd" /><XAxis dataKey="week" /><YAxis allowDecimals={false} /><Tooltip />
              {["sent", "replied", "quality_conversation", "link_sent", "booked", "showed", "closed"].map((s, i) => <Bar key={s} dataKey={s} fill={["#5b6cff", "#7c8bff", "#16a34a", "#22c55e", "#b45309", "#f59e0b", "#7c3aed"][i]} />)}
            </BarChart></ResponsiveContainer></div>
          )}
        </Card>
      )}
      {data && tab === "league" && (
        <Card title="Setter league" subtitle={`Cap: ${data.cap} new conversations per sender per day`}>
          <Table rows={data.league} empty="No sends yet" cols={[{ key: "sender", label: "Sender" }, { key: "sent", label: "Sent" }, { key: "replies", label: "Replies" }, { key: "booked", label: "Booked" }, { key: "reply_rate", label: "Reply rate", render: (r) => `${(r.reply_rate * 100).toFixed(1)}%` }, { key: "today", label: "Today", render: (r) => `${r.today}/${r.cap}` }]} />
        </Card>
      )}
      {tab === "goal" && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="Revenue goal → DMs a day">
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Monthly goal"><Input value={goal.goal} onChange={(e) => setGoal({ ...goal, goal: e.target.value })} /></Field>
              <Field label="Offer price"><Input value={goal.price} onChange={(e) => setGoal({ ...goal, price: e.target.value })} /></Field>
              <Field label="Senders"><Input value={goal.senders} onChange={(e) => setGoal({ ...goal, senders: e.target.value })} /></Field>
            </div>
            <Button tone="primary" className="mt-3" onClick={async () => setCalc(await act("goal", () => post("/outreach/goal", { goal: Number(goal.goal), price: Number(goal.price), senders: Number(goal.senders) })))} loading={busy === "goal"}>Calculate</Button>
          </Card>
          {calc && (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Sales needed / month" value={calc.sales_needed} />
              <Stat label="DMs / day needed" value={calc.dms_per_day} tone={calc.capped ? "warn" : undefined} />
              <Stat label="Senders at the cap" value={calc.senders_needed} hint={`cap ${calc.per_sender_cap}/day each`} />
              <Stat label="DMs / day (capped)" value={calc.dms_per_day_capped} />
              <p className="col-span-2 text-xs text-muted">{calc.note}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
