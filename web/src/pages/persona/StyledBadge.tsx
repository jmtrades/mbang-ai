// The badge on a coach answer that was framed in someone's teaching phrasing: who, that it is an AI and not them,
// under which consent, a link to the public disclosure page, and a per-user switch back to a neutral voice.
import { useState } from "react";
import { Sparkles } from "lucide-react";
import { put } from "../../api";
import { useApp } from "../../components/ui";

const STYLED = /styled after/i;

export const isStyledLabel = (label?: string) => !!label && STYLED.test(label);

export function NeutralVoiceNote({ me, onChange }: { me: any; onChange: () => void }) {
  const { toast } = useApp();
  if (!me?.active || !me.neutral_voice) return null;
  return (
    <span className="mt-1 block text-xs text-muted" data-testid="neutral-voice-note">
      You chose a neutral voice for your answers.{" "}
      <button className="focus-ring text-brand underline-offset-2 hover:underline" onClick={async () => {
        try { await put("/persona/me", { neutral_voice: false }); onChange(); } catch { toast("Couldn't change that. Try again.", "bad"); }
      }}>Use styled answers again</button>
    </span>
  );
}

export function StyledBadge({ label, me, onChange }: { label: string; me: any; onChange: () => void }) {
  const { toast } = useApp();
  const [busy, setBusy] = useState(false);
  const v = /consent v(\d+)/i.exec(label)?.[1];
  const ended = !!v && (!me?.active || String(me.active.version) !== v);
  const setNeutral = async (value: boolean) => {
    setBusy(true);
    try {
      await put("/persona/me", { neutral_voice: value });
      toast(value ? "New answers will use a neutral voice" : "Styled answers are back on", "good");
      onChange();
    } catch {
      toast("Couldn't change that. Try again.", "bad");
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="inline-flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-md bg-violet-50 px-1.5 py-0.5 text-[11px] text-violet-900" data-testid="styled-badge">
      <Sparkles className="size-3 shrink-0" />
      <span className="break-words">{label}{ended ? " (consent ended)" : ""}</span>
      {me?.disclosure_url && <a className="underline-offset-2 hover:underline" href={me.disclosure_url} target="_blank" rel="noreferrer">What's this?</a>}
      {me?.active && (
        <button className="focus-ring underline-offset-2 hover:underline disabled:opacity-50" disabled={busy} onClick={() => setNeutral(!me.neutral_voice)}>
          {me.neutral_voice ? "Use styled answers" : "Use neutral voice"}
        </button>
      )}
    </span>
  );
}
