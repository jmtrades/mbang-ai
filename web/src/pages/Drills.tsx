import { useState } from "react";
import { ApiError, post } from "../api";
import { Badge, Button, Card, Citations, Empty, ErrorBox, Loading, PageHeader, useApi } from "../components/ui";

export default function Drills() {
  const { data, error, loading, reload } = useApi<any>("/drills");
  const [show, setShow] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const due = (data?.cards || []).filter((c: any) => c.is_due);
  const card = due[0];
  const grade = async (g: number) => {
    try {
      await post(`/drills/${card.card_id}/grade`, { grade: g });
      setShow(false);
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Objection drills" subtitle="Spaced repetition (SM-2) on the objections that come up on real calls, answered from the course." />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading />}
      {data && !card && <Empty title="Nothing due" hint={`${data.cards.length} cards in your bank. Come back when the next one is due.`} />}
      {card && (
        <Card title={<span>Objection: “{card.objection}” <Badge>{due.length} due</Badge></span>}>
          {!show ? <Button tone="primary" onClick={() => setShow(true)}>Show the course answer</Button> : (
            <div className="space-y-3">
              <p className="rounded-lg bg-panel-2 p-3 text-sm">{card.answer}</p>
              {card.citation && <Citations items={[card.citation]} />}
              <div className="flex flex-wrap gap-2">
                {[["Forgot", 1], ["Hard", 3], ["Good", 4], ["Easy", 5]].map(([l, g]) => <Button key={l as string} onClick={() => grade(g as number)}>{l}</Button>)}
              </div>
            </div>
          )}
        </Card>
      )}
      {data && (
        <Card className="mt-4" title="All cards">
          <ul className="space-y-1 text-sm">{data.cards.map((c: any) => <li key={c.card_id} className="flex justify-between"><span>{c.objection}</span><span className="text-xs text-muted">{c.is_due ? "due now" : `next in ${c.interval_days}d`} · reps {c.reps}</span></li>)}</ul>
        </Card>
      )}
    </div>
  );
}
