import { useState } from "react";
import { Link2 } from "lucide-react";
import { ApiError, post } from "../../api";
import { Button, Card, CopyButton, ErrorBox, useApp } from "../../components/ui";

// Everyone can tie their own Telegram chat, Slack account or phone number to their sign-in. Until they do,
// messages from there are treated as coming from a stranger (a holding reply for a person, never course answers).
export function LinkChats() {
  const { toast } = useApp();
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const get = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/channels/link-code");
      setCode(r.code);
      toast("Link code ready. It works for an hour.", "good");
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={<span className="inline-flex items-center gap-1.5"><Link2 className="size-4" />Link your chat apps</span>}
      subtitle="Ask the Coach from Telegram, Slack or text message. Link each one first so we know it's you; until then, messages from there wait for a person.">
      <ErrorBox error={err} />
      {!code ? (
        <Button size="sm" loading={busy} onClick={get}>Get a link code</Button>
      ) : (
        <div className="space-y-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span>Your code:</span>
            <code className="rounded bg-panel-2 px-2 py-0.5 font-semibold tracking-wider" data-testid="link-code">{code}</code>
            <CopyButton text={code} />
          </div>
          <ul className="list-disc space-y-1 pl-5 text-muted">
            <li>Telegram: send <code>/link {code}</code> to the bot.</li>
            <li>Slack: send the app a direct message <code>link {code}</code>.</li>
            <li>Text message: text <code>LINK {code}</code> to the programme's number (reply START first to get texts back).</li>
          </ul>
          <p className="text-xs text-muted">Each code works once, for an hour. Answers there are still drafted for approval before they're sent.</p>
          <Button size="sm" loading={busy} onClick={get}>Get a new code</Button>
        </div>
      )}
    </Card>
  );
}
