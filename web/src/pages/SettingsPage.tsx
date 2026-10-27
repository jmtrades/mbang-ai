import { useEffect, useState } from "react";
import { ApiError, post, put } from "../api";
import { Badge, Button, Card, ErrorBox, Field, Input, Loading, PageHeader, useApi, useApp } from "../components/ui";
import MemoryPanel from "../components/MemoryPanel";

export default function SettingsPage() {
  const { me, toast, reloadMe } = useApp();
  const { data, error, loading, reload } = useApi<any>("/settings");
  const [brand, setBrand] = useState<any>(null);
  const [banned, setBanned] = useState("");
  const [blocked, setBlocked] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const owner = me.user.role === "owner";
  useEffect(() => {
    if (!data) return;
    setBrand(data.brand || {});
    setBanned((data.banned_names || []).join(", "));
    setBlocked((data.blocked_attendees || []).join(", "));
  }, [data]);
  const save = async (key: string, value: any) => {
    setErr(null);
    try {
      await put("/settings", { key, value });
      toast("Saved", "good");
      reload();
      if (key === "brand") reloadMe();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  return (
    <div>
      <PageHeader title="Settings" subtitle={owner ? "Branding, banned names and the never-invite list. Live switches and AI budgets are server-only." : "Your workspace."} />
      <ErrorBox error={err || error} retry={reload} />
      {loading && <Loading />}
      {owner && brand && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title="White-label branding">
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Brand name"><Input value={brand.name || ""} onChange={(e) => setBrand({ ...brand, name: e.target.value })} /></Field>
              <Field label="Owner display name"><Input value={brand.owner_display || ""} onChange={(e) => setBrand({ ...brand, owner_display: e.target.value })} /></Field>
              <Field label="Primary colour"><Input type="color" value={brand.primary || "#5b6cff"} onChange={(e) => setBrand({ ...brand, primary: e.target.value })} /></Field>
              <Field label="Accent colour"><Input type="color" value={brand.accent || "#16a34a"} onChange={(e) => setBrand({ ...brand, accent: e.target.value })} /></Field>
            </div>
            <Button className="mt-2" tone="primary" onClick={() => save("brand", brand)}>Save branding</Button>
          </Card>
          <Card title="Banned names" subtitle="Scrubbed from every output and the UI; any queue item containing one is blocked.">
            <Input value={banned} onChange={(e) => setBanned(e.target.value)} placeholder="comma separated" />
            <Button className="mt-2" onClick={() => save("banned_names", list(banned))}>Save</Button>
          </Card>
          <Card title="Never-invite list" subtitle="Checked when an invite or message is proposed and again at send time (Gmail addresses canonicalised).">
            <Input value={blocked} onChange={(e) => setBlocked(e.target.value)} placeholder="comma separated emails" />
            <Button className="mt-2" onClick={() => save("blocked_attendees", list(blocked))}>Save</Button>
          </Card>
          <Card title="Server-only switches">
            <ul className="space-y-1 text-sm">{Object.entries(data.server_only).map(([k, v]: any) => <li key={k} className="flex justify-between"><code>{k}</code><Badge tone={v && v !== 0 ? "warn" : "neutral"}>{String(v)}</Badge></li>)}</ul>
            <p className="mt-2 text-xs text-muted">Change with <code>scripts/tenant_admin.py … --owner-yes "&lt;recorded reason&gt;"</code> on the server. The API refuses these with 403.</p>
          </Card>
          {me.is_demo && (
            <Card title="Pitch demo">
              <p className="text-sm text-muted">Backs up the demo database first, then restores the clean story. Refused on the real workspace.</p>
              <Button className="mt-2" tone="danger" onClick={async () => { try { await post("/demo/reset"); toast("Demo reset", "good"); location.reload(); } catch (e) { setErr(e as ApiError); } }}>Reset demo to clean story</Button>
            </Card>
          )}
        </div>
      )}
      {!owner && <Card><p className="text-sm">Signed in as <b>{me.user.display}</b> ({me.user.role}) in {me.tenant.name}.</p></Card>}
      {!owner && <div className="mt-4"><MemoryPanel /></div>}
    </div>
  );
}
