import { useState } from "react";
import { ApiError, post } from "../api";
import { Button, Card, ErrorBox, Field, Input, Loading, PageHeader, Select, Table, TextArea, useApi, useApp } from "../components/ui";

export default function Checkin() {
  const { toast } = useApp();
  const mine = useApi<any>("/checkin/mine");
  const [f, setF] = useState({ outreach: "", calls_booked: "", calls_showed: "", mood: "4", wins: "", blockers: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      await post("/checkin", { ...f, outreach: Number(f.outreach) || 0, calls_booked: Number(f.calls_booked) || 0, calls_showed: Number(f.calls_showed) || 0, mood: Number(f.mood) });
      toast("Check-in saved", "good");
      mine.reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="My weekly check-in" subtitle="Two minutes. Your coach sees it, and the accountability engine uses it to spot when you need help." />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="This week">
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="DMs / outreach"><Input inputMode="numeric" value={f.outreach} onChange={(e) => setF({ ...f, outreach: e.target.value })} /></Field>
            <Field label="Calls booked"><Input inputMode="numeric" value={f.calls_booked} onChange={(e) => setF({ ...f, calls_booked: e.target.value })} /></Field>
            <Field label="Calls showed"><Input inputMode="numeric" value={f.calls_showed} onChange={(e) => setF({ ...f, calls_showed: e.target.value })} /></Field>
          </div>
          <div className="mt-3 space-y-3">
            <Field label="Mood"><Select value={f.mood} onChange={(e) => setF({ ...f, mood: e.target.value })} options={[{ value: "1", label: "1 — rough" }, { value: "2", label: "2" }, { value: "3", label: "3 — okay" }, { value: "4", label: "4" }, { value: "5", label: "5 — great" }]} /></Field>
            <Field label="Wins"><TextArea rows={2} value={f.wins} onChange={(e) => setF({ ...f, wins: e.target.value })} /></Field>
            <Field label="Blockers"><TextArea rows={2} value={f.blockers} onChange={(e) => setF({ ...f, blockers: e.target.value })} /></Field>
            <Button tone="primary" onClick={submit} loading={busy}>Save check-in</Button>
            <ErrorBox error={err} retry={submit} />
          </div>
        </Card>
        <Card title="My recent check-ins">
          {mine.loading && <Loading />}
          <ErrorBox error={mine.error} retry={mine.reload} />
          {mine.data && <Table rows={mine.data.items} empty="No check-ins yet" cols={[
            { key: "at", label: "Week of", render: (c) => new Date(c.at * 1000).toLocaleDateString() },
            { key: "outreach", label: "DMs" }, { key: "calls_booked", label: "Booked" }, { key: "calls_showed", label: "Showed" }, { key: "mood", label: "Mood" },
          ]} />}
        </Card>
      </div>
    </div>
  );
}
