import { useEffect, useState } from "react";
import { ApiError, put } from "../api";
import { Button, Card, ErrorBox, Field, Input, Loading, PageHeader, useApi, useApp } from "../components/ui";
import MemoryPanel from "../components/MemoryPanel";

export default function Profile() {
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<any>("/profile");
  const [p, setP] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { if (data) setP(data.profile); }, [data]);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      await put("/profile", { profile: p });
      toast("Profile saved — every skill reads it", "good");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <PageHeader title="Business profile" subtitle="Eight sections every skill, worker, Studio line and the webinar chain read before drafting." right={<Button tone="primary" onClick={save} loading={busy}>Save profile</Button>} />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading rows={8} />}
      {p && data && (
        <div className="grid gap-4 md:grid-cols-2">
          {Object.entries(data.sections).map(([sec, fields]: any) => (
            <Card key={sec} title={sec[0].toUpperCase() + sec.slice(1)}>
              <div className="space-y-2">
                {fields.map((f: string) => (
                  <Field key={f} label={f.replace(/_/g, " ")}>
                    <Input value={p[sec]?.[f] || ""} onChange={(e) => setP({ ...p, [sec]: { ...p[sec], [f]: e.target.value } })} />
                  </Field>
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}
      <div className="mt-6">
        <MemoryPanel />
      </div>
    </div>
  );
}
