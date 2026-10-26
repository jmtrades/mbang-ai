import { useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, post } from "../api";
import { Badge, Button, Card, ErrorBox, Loading, PageHeader, Stat, useApi } from "../components/ui";

export default function Evals() {
  const { data, error, loading, reload } = useApi<any>("/evals");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const run = async (kind: string) => {
    setBusy(kind);
    setErr(null);
    try {
      await post("/evals/run", { kind });
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const runs = (data?.runs || []).slice().reverse();
  const latest = (k: string) => (data?.runs || []).find((r: any) => r.kind === k)?.metrics;
  const series = runs.map((r: any, i: number) => ({
    i: i + 1,
    "hit@5": r.kind === "retrieval" ? r.metrics["hit@5"] : undefined,
    "hit@10": r.kind === "retrieval" ? r.metrics["hit@10"] : undefined,
    honesty: r.kind === "honesty" ? r.metrics.honesty : undefined,
    reviewer: r.kind === "reviewer" ? r.metrics.flaw_detection : undefined,
  }));
  const corpus = data?.runs?.[0]?.corpus;
  return (
    <div>
      <PageHeader title="Evals" subtitle="Retrieval hit@k on the gold set, out-of-scope honesty, and reviewer flaw detection. Every run is stored so you can see the trend."
        badge={corpus && <Badge tone={corpus === "fixture" ? "warn" : "good"}>{corpus === "fixture" ? "fixture corpus" : "your corpus"}</Badge>}
        right={<>
          <Button onClick={() => run("retrieval")} loading={busy === "retrieval"}>Run retrieval</Button>
          <Button onClick={() => run("honesty")} loading={busy === "honesty"}>Run honesty</Button>
          <Button onClick={() => run("reviewer")} loading={busy === "reviewer"}>Run reviewer</Button>
        </>} />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading rows={4} />}
      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="hit@5 (target ≥ 0.90)" value={latest("retrieval")?.["hit@5"] ?? "—"} />
        <Stat label="hit@10 (target ≥ 0.95)" value={latest("retrieval")?.["hit@10"] ?? "—"} />
        <Stat label="Out-of-scope honesty (≥ 0.9)" value={latest("honesty")?.honesty ?? "—"} />
        <Stat label="Reviewer flaw detection (≥ 0.8)" value={latest("reviewer")?.flaw_detection ?? "—"} />
      </div>
      <Card title="Trend">
        {series.length === 0 ? <p className="text-sm text-muted">Run an eval to start the trend.</p> : (
          <div className="h-64">
            <ResponsiveContainer>
              <LineChart data={series}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e4e3dd" />
                <XAxis dataKey="i" />
                <YAxis domain={[0, 1]} />
                <Tooltip />
                <Legend />
                <Line dataKey="hit@5" stroke="#5b6cff" connectNulls />
                <Line dataKey="hit@10" stroke="#16a34a" connectNulls />
                <Line dataKey="honesty" stroke="#b45309" connectNulls />
                <Line dataKey="reviewer" stroke="#7c3aed" connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}
