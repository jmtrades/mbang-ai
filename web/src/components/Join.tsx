import React, { useEffect, useMemo, useState } from "react";
import { Eye, EyeOff, KeyRound, Lock, Mail, UserPlus } from "lucide-react";
import { ApiError, get, post } from "../api";
import { Badge, Button, ErrorBox, Field, Input, Loading } from "./ui";

// Shown full-screen when the address is #/join?token=… (an invite or a password-reset link) or #/join?forgot=1.
// The token never travels in a URL to the server: it is read from the page address, sent in a POST body,
// and removed from the address bar straight away.

type Check = { valid: boolean; kind: "invite" | "reset"; role: string; role_label: string; email: string; display: string; brand: string; expires: number; is_demo: boolean; username?: string };

function readHash() {
  const [, qs] = location.hash.replace(/^#/, "").split("?");
  const q = new URLSearchParams(qs || "");
  return { token: q.get("token") || "", forgot: q.has("forgot") };
}

const RULES: { test: (p: string) => boolean; text: string }[] = [
  { test: (p) => p.length >= 10, text: "At least 10 characters" },
  {
    test: (p) => p.length >= 16 || [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((r) => r.test(p)).length >= 3,
    text: "Three of: lower-case, capitals, numbers, symbols (or 16+ characters)",
  },
];

function Shell({ brand, children }: { brand: string; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-[80] overflow-y-auto bg-bg" role="dialog" aria-modal="true" aria-labelledby="join-title">
      <div className="flex min-h-full items-center justify-center p-4">
        <div className="card w-full max-w-md p-6">
          <div className="mb-5 text-sm font-semibold text-muted">{brand}</div>
          {children}
        </div>
      </div>
    </div>
  );
}

export default function Join({ onDone }: { onDone?: () => void }) {
  const initial = useMemo(readHash, []);
  const [token] = useState(initial.token);
  const [mode] = useState<"token" | "forgot" | "missing">(initial.token ? "token" : initial.forgot ? "forgot" : "missing");
  const [brand, setBrand] = useState("");
  const [check, setCheck] = useState<Check | null>(null);
  const [checkErr, setCheckErr] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(mode === "token");
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ident, setIdent] = useState("");
  const [sent, setSent] = useState<string | null>(null);

  useEffect(() => {
    get("/public").then((d: any) => setBrand(d?.brand?.name || "")).catch(() => null);
    // keep the one-time token out of the address bar and the browser history
    if (initial.token) history.replaceState(null, "", location.pathname + location.search + "#/join");
    if (mode !== "token") return;
    post<Check>("/join/check", { token })
      .then((d) => {
        setCheck(d);
        setName(d.display || "");
        if (d.brand) setBrand(d.brand);
      })
      .catch((e) => setCheckErr(e as ApiError))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const brandName = brand || "Your workspace";
  const okRules = RULES.map((r) => r.test(pw));
  const canSubmit = okRules.every(Boolean) && pw === pw2 && (check?.kind === "reset" || name.trim().length > 0);

  const accept = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) {
      setErr(pw !== pw2 ? "The two passwords don't match." : "Check the password rules below.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await post("/join/accept", { token, password: pw, ...(check?.kind === "invite" ? { display: name.trim() } : {}) });
      location.hash = "#/";
      onDone?.();
    } catch (e2) {
      setErr((e2 as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const forgot = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ident.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ message: string }>("/join/forgot", { ident: ident.trim() });
      setSent(r.message);
    } catch (e2) {
      setErr((e2 as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const back = (
    <a className="text-sm text-brand underline-offset-2 hover:underline" href="#/" onClick={() => setTimeout(() => location.reload(), 0)}>Back to sign in</a>
  );

  if (mode === "forgot") {
    return (
      <Shell brand={brandName}>
        <h1 id="join-title" className="mb-1 flex items-center gap-2 text-xl font-semibold"><KeyRound className="size-5 text-brand" /> Forgot your password?</h1>
        <p className="mb-4 text-sm text-muted">Enter your email or username. The workspace owner gets your request and sends you a one-time link to choose a new password.</p>
        {sent ? (
          <div className="space-y-4">
            <p className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-900" role="status">{sent}</p>
            {back}
          </div>
        ) : (
          <form onSubmit={forgot} className="space-y-3">
            <Field label="Email or username"><Input value={ident} onChange={(e) => setIdent(e.target.value)} autoComplete="username" maxLength={200} /></Field>
            {err && <ErrorBox error={err} />}
            <Button tone="primary" className="w-full" type="submit" loading={busy} disabled={!ident.trim()}>Ask for a reset link</Button>
            <div className="text-center">{back}</div>
          </form>
        )}
      </Shell>
    );
  }

  if (mode === "missing") {
    return (
      <Shell brand={brandName}>
        <h1 id="join-title" className="mb-2 text-xl font-semibold">Open your link again</h1>
        <p className="mb-4 text-sm text-muted">For safety the one-time code is removed from the address bar once the page opens. Open the link from your email or message again, or ask the person who invited you for a new one.</p>
        {back}
      </Shell>
    );
  }

  return (
    <Shell brand={brandName}>
      {loading && <Loading rows={4} />}
      {checkErr && (
        <div className="space-y-4">
          <h1 id="join-title" className="text-xl font-semibold">This link doesn't work</h1>
          <ErrorBox error={checkErr} />
          <p className="text-sm text-muted">Links work once and expire. Ask the person who invited you to create a new one.</p>
          {back}
        </div>
      )}
      {check && (
        <form onSubmit={accept} className="space-y-4" aria-label={check.kind === "reset" ? "Choose a new password" : "Accept invite"}>
          <div>
            <h1 id="join-title" className="flex items-center gap-2 text-xl font-semibold">
              {check.kind === "reset" ? <Lock className="size-5 text-brand" /> : <UserPlus className="size-5 text-brand" />}
              {check.kind === "reset" ? "Choose a new password" : `Join ${check.brand}`}
            </h1>
            <p className="mt-1 text-sm text-muted">
              {check.kind === "reset"
                ? "Choosing a new password signs you out on every other device."
                : <>You've been invited as {/^[aeiou]/i.test(check.role_label) ? "an" : "a"} <b>{check.role_label}</b>. Choose a password to finish.</>}
            </p>
            {check.is_demo && <Badge tone="demo" className="mt-2">Pitch demo: fictional workspace</Badge>}
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm">
            <Mail className="size-4 shrink-0 text-muted" />
            <span className="min-w-0 break-all">You'll sign in with <b>{check.kind === "reset" ? check.username || check.email : check.email}</b></span>
          </div>
          {check.kind === "invite" && (
            <Field label="Your name"><Input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoComplete="name" /></Field>
          )}
          <Field label="New password">
            <div className="relative">
              <Input type={show ? "text" : "password"} value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" maxLength={200} className="pr-10" />
              <button type="button" onClick={() => setShow(!show)} className="focus-ring absolute inset-y-0 right-0 grid w-10 place-items-center text-muted" aria-label={show ? "Hide password" : "Show password"}>
                {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
          </Field>
          <Field label="Type it again"><Input type={show ? "text" : "password"} value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" maxLength={200} /></Field>
          <ul className="space-y-1 text-xs" aria-label="Password rules">
            {RULES.map((r, i) => (
              <li key={r.text} className={okRules[i] ? "text-good" : "text-muted"}>{okRules[i] ? "✓" : "•"} {r.text}</li>
            ))}
            <li className={pw && pw === pw2 ? "text-good" : "text-muted"}>{pw && pw === pw2 ? "✓" : "•"} Both passwords match</li>
          </ul>
          {err && <ErrorBox error={err} />}
          <Button tone="primary" className="w-full" type="submit" loading={busy}>{check.kind === "reset" ? "Save new password" : "Create my account"}</Button>
          <p className="text-center text-xs text-muted">This link works once and stops working on {new Date(check.expires * 1000).toLocaleDateString()}.</p>
        </form>
      )}
    </Shell>
  );
}
