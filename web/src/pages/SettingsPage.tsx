import { useEffect, useState, type ReactNode } from "react";
import { ApiError, post, put } from "../api";
import { Lock, Monitor, Moon, RotateCcw, Sun } from "lucide-react";
import { Avatar, Badge, Button, Card, ErrorBox, Field, Input, Loading, PageHeader, Segmented, cx, humanize, useApi, useApp } from "../components/ui";
import { type ThemePref, useTheme } from "../theme";

const HEX = /^#[0-9a-f]{6}$/i;

/** Dark text on light brand colours, white on the rest (same rule the app uses for primary buttons). */
function inkOn(hex: string): string {
  if (!HEX.test(hex)) return "#ffffff";
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => { const c = v / 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.42 ? "#0b0b0d" : "#ffffff";
}

/** Swatch + hex text, so the colour is visible and can be pasted exactly (a bare native picker shows neither). */
function ColorField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="space-y-1.5">
      <span className="block text-[12.5px] font-medium text-ink-2">{label}</span>
      <div className="flex items-center gap-2">
        <input type="color" aria-label={`${label} picker`} value={HEX.test(value) ? value : "#000000"} onChange={(e) => onChange(e.target.value)}
          className="size-9 shrink-0 cursor-pointer appearance-none rounded-[9px] border border-line bg-panel p-1 shadow-xs [&::-moz-color-swatch]:rounded-[6px] [&::-moz-color-swatch]:border-none [&::-webkit-color-swatch]:rounded-[6px] [&::-webkit-color-swatch]:border-none [&::-webkit-color-swatch-wrapper]:p-0" />
        <Input aria-label={label} value={value} maxLength={7} spellCheck={false} onChange={(e) => onChange(e.target.value.startsWith("#") ? e.target.value : `#${e.target.value}`)}
          className={cx("font-mono text-[13px] uppercase", value && !HEX.test(value) && "border-bad-line")} />
      </div>
    </div>
  );
}

/** A label/description on the left and its control on the right; stacks on phones. */
function Row({ title, hint, children, className }: { title: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cx("grid gap-3 py-4 first:pt-0 last:pb-0 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] sm:items-center sm:gap-6", className)}>
      <div className="min-w-0">
        <div className="text-[13.5px] font-medium text-ink">{title}</div>
        {hint && <p className="mt-0.5 text-[12.5px] leading-snug text-muted">{hint}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function switchValue(v: unknown): { text: string; tone: "neutral" | "good" | "warn" } {
  if (v === null || v === undefined || v === "") return { text: "Not set", tone: "neutral" };
  if (v === false) return { text: "Off", tone: "neutral" };
  if (v === true) return { text: "On", tone: "warn" };
  return { text: String(v), tone: v === 0 || v === "0" ? "neutral" : "warn" };
}

export default function SettingsPage() {
  const { me, toast, reloadMe } = useApp();
  const theme = useTheme();
  const { data, error, loading, reload } = useApi<any>("/settings");
  const [brand, setBrand] = useState<any>(null);
  const [banned, setBanned] = useState("");
  const [blocked, setBlocked] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const owner = me.user.role === "owner";
  useEffect(() => {
    if (!data) return;
    setBrand(data.brand || {});
    setBanned((data.banned_names || []).join(", "));
    setBlocked((data.blocked_attendees || []).join(", "));
  }, [data]);
  const save = async (key: string, value: any) => {
    setErr(null);
    setBusy(key);
    try {
      await put("/settings", { key, value });
      toast("Saved", "good");
      reload();
      if (key === "brand") reloadMe();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const primary = brand?.primary || "#5b6cff";
  const accent = brand?.accent || "#16a34a";
  const colorsOk = HEX.test(primary) && HEX.test(accent);
  const display = (me.user.display || me.user.role).replace(/\s*\(DEMO\)\s*/g, "");
  return (
    <div className="max-w-4xl">
      <PageHeader title="Settings" subtitle={owner ? "Branding, banned names and the never-invite list. Live switches and AI budgets are server-only." : "Your account and how the app looks on this device."} />
      <ErrorBox error={err || error} retry={reload} />
      <div className="space-y-4">
        {!owner && (
          <Card title="Account">
            <div className="flex flex-wrap items-center gap-3">
              <Avatar name={display} className="size-10 text-[13px]" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[14px] font-semibold text-ink">{display}</div>
                <div className="truncate text-[12.5px] text-muted">{me.tenant.name}</div>
              </div>
              <Badge tone="brand">{humanize(me.user.role)}</Badge>
              {me.is_demo && <Badge tone="demo" className="font-semibold tracking-[0.04em]">DEMO</Badge>}
            </div>
          </Card>
        )}

        <Card title="Appearance">
          <Row title="Theme" hint="Saved on this device. System follows your operating system's light or dark setting.">
            <div className="sm:flex sm:justify-end">
              <Segmented<ThemePref> label="Theme" value={theme.pref} onChange={theme.setPref} options={[
                { value: "system", label: "System", icon: <Monitor /> },
                { value: "light", label: "Light", icon: <Sun /> },
                { value: "dark", label: "Dark", icon: <Moon /> },
              ]} />
            </div>
          </Row>
        </Card>

        {owner && loading && <Loading />}
        {owner && brand && (
          <>
            <Card title="White-label branding" subtitle="The name and colours students, coaches and leads see everywhere: the app, emails and the embed." pad={false}>
              <div className="grid gap-4 p-4 sm:grid-cols-2 sm:p-5">
                <Field label="Brand name"><Input value={brand.name || ""} onChange={(e) => setBrand({ ...brand, name: e.target.value })} /></Field>
                <Field label="Owner display name"><Input value={brand.owner_display || ""} onChange={(e) => setBrand({ ...brand, owner_display: e.target.value })} /></Field>
                <ColorField label="Primary colour" value={primary} onChange={(v) => setBrand({ ...brand, primary: v })} />
                <ColorField label="Accent colour" value={accent} onChange={(v) => setBrand({ ...brand, accent: v })} />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line-2 bg-panel-2 px-4 py-3 sm:px-5">
                <div className="flex items-center gap-2 text-[12.5px] text-muted" aria-hidden>
                  <span className="grid size-6 place-items-center rounded-[7px] text-[11px] font-semibold shadow-xs" style={{ background: HEX.test(primary) ? primary : undefined, color: inkOn(primary) }}>{(brand.name || "O").trim()[0]?.toUpperCase()}</span>
                  <span className="h-6 rounded-[7px] px-2 text-[11.5px] font-medium leading-6 shadow-xs" style={{ background: HEX.test(primary) ? primary : undefined, color: inkOn(primary) }}>Button</span>
                  <span className="size-2.5 rounded-full" style={{ background: HEX.test(accent) ? accent : undefined }} />
                  Preview
                </div>
                <Button tone="primary" size="sm" loading={busy === "brand"} disabled={!colorsOk} onClick={() => save("brand", brand)}>Save branding</Button>
              </div>
            </Card>

            <Card title="Guardrail lists">
              <div className="divide-y divide-line-2">
                <Row title="Banned names" hint="Scrubbed from every output and the UI; any queue item containing one is blocked.">
                  <div className="flex gap-2">
                    <Input aria-label="Banned names" value={banned} onChange={(e) => setBanned(e.target.value)} placeholder="Names, comma separated" />
                    <Button loading={busy === "banned_names"} onClick={() => save("banned_names", list(banned))}>Save</Button>
                  </div>
                </Row>
                <Row title="Never-invite list" hint="Checked when an invite or message is proposed and again at send time (Gmail addresses canonicalised).">
                  <div className="flex gap-2">
                    <Input aria-label="Never-invite list" value={blocked} onChange={(e) => setBlocked(e.target.value)} placeholder="Emails, comma separated" />
                    <Button loading={busy === "blocked_attendees"} onClick={() => save("blocked_attendees", list(blocked))}>Save</Button>
                  </div>
                </Row>
              </div>
            </Card>

            <Card title={<span className="flex items-center gap-2"><Lock className="size-4 text-muted" />Server-only switches</span>} subtitle="Read-only here. The API refuses changes with 403." pad={false}>
              <ul className="divide-y divide-line-2">
                {Object.entries(data.server_only).map(([k, v]: any) => {
                  const sv = switchValue(v);
                  return (
                    <li key={k} className="flex items-center gap-3 px-4 py-2.5 sm:px-5">
                      <div className="min-w-0 flex-1">
                        <div className="text-[13.5px] text-ink">{humanize(k)}</div>
                        <code className="!border-0 !bg-transparent !p-0 text-[11.5px] text-muted">{k}</code>
                      </div>
                      <Badge tone={sv.tone} dot={sv.tone !== "neutral"}>{sv.text}</Badge>
                    </li>
                  );
                })}
              </ul>
              <p className="border-t border-line-2 px-4 py-3 text-[12.5px] leading-relaxed text-muted sm:px-5">
                Change on the server with <code>scripts/tenant_admin.py … --owner-yes "&lt;recorded reason&gt;"</code>.
              </p>
            </Card>

            {me.is_demo && (
              <Card title="Pitch demo" className="border-bad-line">
                <Row title="Start the pitch again" hint="Backs up the demo database first, then restores the clean story. Refused on the real workspace.">
                  <div className="sm:flex sm:justify-end">
                    <Button tone="danger" loading={busy === "reset"} onClick={async () => { setBusy("reset"); try { await post("/demo/reset"); toast("Demo reset", "good"); location.reload(); } catch (e) { setErr(e as ApiError); setBusy(null); } }}>
                      <RotateCcw className="size-4" /> Reset demo to clean story
                    </Button>
                  </div>
                </Row>
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  );
}
