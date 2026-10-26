import { useState } from "react";
import { ApiError, put } from "../api";
import { Badge, Button, Card, ErrorBox, Input, Loading, PageHeader, StatusBadge, useApi, useApp } from "../components/ui";

export default function Integrations() {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/integrations");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [err, setErr] = useState<ApiError | null>(null);
  const save = async (fields: string[]) => {
    const body: Record<string, string> = {};
    fields.forEach((f) => { if (vals[f]) body[f] = vals[f]; });
    if (!Object.keys(body).length) return;
    setErr(null);
    try {
      await put("/vault", { fields: body });
      setVals({});
      toast("Saved to the encrypted vault", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div>
      <PageHeader title="Integrations" subtitle="Every key goes into the per-tenant encrypted vault (AES-256-GCM). The app only ever shows whether a field is set. Live switches change only on the server." />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading rows={6} />}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {data?.items.map((it: any) => (
          <Card key={it.key} title={it.name} actions={<><StatusBadge s={it.status} />{"live" in it && <Badge tone={it.live ? "warn" : "neutral"}>live switch {it.live ? "ON" : "off"}</Badge>}</>}>
            {Object.keys(it.fields).length === 0 ? <p className="text-xs text-muted">Server environment: {it.env?.join(", ")}</p> : (
              <div className="space-y-2">
                {Object.entries(it.fields).map(([f, st]: any) => (
                  <div key={f} className="flex items-center gap-2">
                    <Input type="password" autoComplete="off" placeholder={f} value={vals[f] || ""} onChange={(e) => setVals({ ...vals, [f]: e.target.value })} aria-label={f} />
                    <Badge tone={st === "set" ? "good" : "neutral"}>{st}</Badge>
                  </div>
                ))}
                <Button size="sm" onClick={() => save(Object.keys(it.fields))}>Save</Button>
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
