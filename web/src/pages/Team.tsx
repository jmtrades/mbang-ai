import { useEffect, useState } from "react";
import { Download, KeyRound, Link2, Mail, Search, Trash2, Upload, UserCheck, UserPlus, UserX, UsersRound, X } from "lucide-react";
import { ApiError, BASE, del, post } from "../api";
import {
  Badge, Button, Card, CopyButton, DemoBadge, Drawer, Empty, ErrorBox, Field, FlagList, Input, Loading, Modal, PageHeader, Select,
  Stat, Table, Tabs, TextArea, TraceLink, useApi, useApp,
} from "../components/ui";

type User = {
  id: number; username: string; display: string; role: string; role_label: string; active: boolean; email?: string | null;
  coach_id?: number | null; coach?: string | null; student_id?: number | null; sessions: number; students?: number | null;
  managers: { id: number; display: string }[]; manages: { id: number; display: string }[]; created: number; demo: boolean;
};
type Invite = {
  id: number; kind: "invite" | "reset"; email: string | null; role: string; role_label: string; display: string | null; state: string;
  created: number; expires: number; queue_id: number | null; student_id: number | null; demo: boolean;
};
type Person = { id: number; display: string; role?: string };
type Overview = {
  users: User[]; invites: Invite[]; reset_requests: { id: number; user_id: number; display: string; role: string; created: number }[];
  assignments: { manager_id: number; member_id: number; manager: string; member: string; manager_role: string; member_role: string }[];
  coaches: Person[]; managers: Person[]; members: Person[]; email_live: boolean;
  counts: { people: number; switched_off: number; open_invites: number; students: number };
  roles: { value: string; label: string }[]; invite_days: number; reset_hours: number;
};
type LinkResult = {
  link: string; full_link: string | null; queue_item: { id: number; status: string; flags: any[] } | null; run_id?: number;
  invite: Invite; no_email?: boolean; label?: string;
};

const STATE: Record<string, { label: string; tone: "good" | "warn" | "neutral" | "bad" | "info" }> = {
  open: { label: "Waiting", tone: "info" }, used: { label: "Used", tone: "good" }, expired: { label: "Expired", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};
const ACCOUNT: Record<string, { label: string; tone: "good" | "warn" | "neutral" | "info" }> = {
  active: { label: "Has an account", tone: "good" }, invited: { label: "Invited", tone: "info" },
  none: { label: "No account yet", tone: "neutral" }, "switched off": { label: "Switched off", tone: "warn" },
};
const day = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "");

function LinkOnce({ res, emailLive, kind }: { res: LinkResult; emailLive: boolean; kind: "invite" | "reset" }) {
  const full = res.full_link || `${location.origin}${res.link}`;
  const q = res.queue_item;
  return (
    <div className="space-y-3" data-testid="link-once">
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
        <div className="mb-1 flex items-center gap-1.5 text-sm font-semibold text-amber-950"><Link2 className="size-4" /> One-time link, shown only now</div>
        <p className="mb-2 text-xs text-amber-900">We keep only a fingerprint of it, so it can't be shown again. It works once and stops working after {kind === "reset" ? "48 hours" : "7 days"}.</p>
        <div className="flex flex-wrap items-center gap-2">
          <code className="min-w-0 flex-1 break-all rounded bg-white px-2 py-1 font-mono text-[11px]">{full}</code>
          <CopyButton text={full} label="Copy link" />
        </div>
      </div>
      {q ? (
        <div className="rounded-lg border border-line p-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Mail className="size-4 text-muted" />
            <span className="font-medium">{q.status === "held" ? "The email is held: edit it before approving" : q.status === "needs_human" ? "The email needs a person to check it" : "The email is waiting for your approval"}</span>
            <a href="#/queue" className="text-xs text-brand hover:underline">Open the approval queue</a>
            <TraceLink runId={res.run_id} />
          </div>
          {q.flags?.length ? <div className="mt-2"><FlagList flags={q.flags} /></div> : null}
          {!emailLive && <p className="mt-2 text-xs text-muted">Email isn't connected on this server yet, so approving puts it on the practice inbox and nobody receives it. Copy the link above and send it yourself.</p>}
          <p className="mt-1 text-[11px] text-muted">{res.label}</p>
        </div>
      ) : (
        <p className="text-xs text-muted">{res.no_email ? "This person has no email address on file, so no email was drafted. " : "No email was drafted. "}Send the link yourself.</p>
      )}
    </div>
  );
}

function InviteModal({ open, onClose, data, preset, onDone }: { open: boolean; onClose: () => void; data: Overview; preset?: Partial<{ email: string; display: string; role: string; student_id: number }>; onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [display, setDisplay] = useState("");
  const [role, setRole] = useState("student");
  const [coach, setCoach] = useState("");
  const [sendEmail, setSendEmail] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [res, setRes] = useState<LinkResult | null>(null);
  useEffect(() => {
    if (!open) return;
    setEmail(preset?.email || ""); setDisplay(preset?.display || ""); setRole(preset?.role || "student"); setCoach("");
    setSendEmail(true); setErr(null); setRes(null);
  }, [open, preset]);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await post<LinkResult>("/team/invites", {
        email: email.trim() || null, role, display: display.trim() || null, send_email: sendEmail, origin: location.origin,
        coach_id: role === "student" && coach ? Number(coach) : null, student_id: preset?.student_id ?? null,
      });
      setRes(r);
      onDone();
    } catch (e2) {
      setErr(e2 as ApiError);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={onClose} title={res ? "Invite created" : "Invite someone"}>
      {res ? (
        <div className="space-y-4">
          <LinkOnce res={res} emailLive={data.email_live} kind="invite" />
          <div className="flex justify-end"><Button tone="primary" onClick={onClose}>Done</Button></div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} maxLength={200} required={!preset?.student_id} placeholder={preset?.student_id ? "From the student record" : "name@company.com"} /></Field>
            <Field label="Name (optional)"><Input value={display} onChange={(e) => setDisplay(e.target.value)} maxLength={80} /></Field>
            <Field label="Role"><Select value={role} onChange={(e) => setRole(e.target.value)} options={data.roles} disabled={!!preset?.student_id} /></Field>
            {role === "student" && (
              <Field label="Coach (optional)"><Select value={coach} onChange={(e) => setCoach(e.target.value)} options={[{ value: "", label: "Assign later" }, ...data.coaches.map((c) => ({ value: String(c.id), label: c.display }))]} /></Field>
            )}
          </div>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={sendEmail} onChange={(e) => setSendEmail(e.target.checked)} />
            <span>Draft the invite email and put it in my approval queue <span className="block text-xs text-muted">Drafted from a template (AI not used). Nothing is sent until you approve it.</span></span>
          </label>
          <p className="text-xs text-muted">They choose their own password. A {ROLE_HELP[role] || role} sees only what that role allows.</p>
          <ErrorBox error={err} />
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" onClick={onClose}>Cancel</Button>
            <Button tone="primary" type="submit" loading={busy}><UserPlus className="size-4" /> Create invite</Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
const ROLE_HELP: Record<string, string> = {
  coach: "coach", setter: "appointment setter", closer: "closer", student: "student",
};

function ManageDrawer({ user, data, onClose, onChanged }: { user: User | null; data: Overview; onClose: () => void; onChanged: () => void }) {
  const { me, toast } = useApp();
  const [role, setRole] = useState("");
  const [coach, setCoach] = useState("");
  const [mgr, setMgr] = useState("");
  const [confirm, setConfirm] = useState("");
  const [resetEmail, setResetEmail] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [reset, setReset] = useState<LinkResult | null>(null);
  const [deleted, setDeleted] = useState<any>(null);
  useEffect(() => {
    if (!user) return;
    setRole(user.role); setCoach(user.coach_id ? String(user.coach_id) : ""); setMgr(""); setConfirm(""); setErr(null);
    setReset(null); setDeleted(null); setResetEmail(true);
  }, [user?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!user) return null;
  const owner = user.role === "owner";
  const self = user.id === me.user.id;
  const run = async (key: string, fn: () => Promise<any>, msg?: string) => {
    setBusy(key);
    setErr(null);
    try {
      const r = await fn();
      if (msg) toast(msg, "good");
      if (r?.warning) toast(r.warning, "info");
      onChanged();
      return r;
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  const mgrOptions = data.managers.filter((m) => m.id !== user.id && !user.managers.some((x) => x.id === m.id));
  return (
    <Drawer open={!!user} onClose={onClose} title={<span className="flex items-center gap-2">{user.display} {user.demo && <DemoBadge />}</span>}>
      {deleted ? (
        <div className="space-y-3 text-sm">
          <p className="font-medium">The account was deleted and its personal records removed.</p>
          <ul className="list-disc space-y-1 pl-5 text-muted">
            {Object.entries(deleted.removed || {}).map(([k, v]) => <li key={k}>{String(v)} {k.replace(/_/g, " ")}</li>)}
            {deleted.drafts_rejected ? <li>{deleted.drafts_rejected} open draft(s) in their name rejected</li> : null}
          </ul>
          <p className="text-xs text-muted">Kept: {deleted.kept?.join(" ")}</p>
          <Button onClick={onClose}>Close</Button>
        </div>
      ) : (
        <div className="space-y-5 text-sm">
          <div className="text-muted">
            <div className="break-all">Signs in as <b className="text-ink">{user.username}</b></div>
            <div>{user.active ? `${user.sessions} active session(s)` : "Switched off: can't sign in"} · joined {day(user.created)}</div>
          </div>
          <ErrorBox error={err} />
          {owner ? (
            <p className="rounded-lg border border-line bg-panel-2 px-3 py-2 text-xs text-muted">Owner accounts are managed on the server, not from this screen.</p>
          ) : (
            <>
              <section className="space-y-2">
                <h4 className="font-semibold">Role</h4>
                <div className="flex gap-2">
                  <div className="min-w-0 flex-1"><Select value={role} onChange={(e) => setRole(e.target.value)} options={data.roles} aria-label="Role" disabled={self} /></div>
                  <Button onClick={() => run("role", () => post(`/team/users/${user.id}/role`, { role }), "Role changed")} disabled={role === user.role || self} loading={busy === "role"}>Save</Button>
                </div>
                <p className="text-xs text-muted">Takes effect on their next click. Each role sees only its own tabs and approves only its kind of drafts.</p>
              </section>

              {user.role === "student" && user.student_id && (
                <section className="space-y-2">
                  <h4 className="font-semibold">Coach</h4>
                  <div className="flex gap-2">
                    <div className="min-w-0 flex-1"><Select value={coach} onChange={(e) => setCoach(e.target.value)} aria-label="Coach"
                      options={[{ value: "", label: "No coach yet" }, ...data.coaches.map((c) => ({ value: String(c.id), label: c.display }))]} /></div>
                    <Button onClick={() => run("coach", () => post(`/team/students/${user.student_id}/coach`, { coach_id: coach ? Number(coach) : null }), "Coach updated")}
                      disabled={coach === (user.coach_id ? String(user.coach_id) : "")} loading={busy === "coach"}>Save</Button>
                  </div>
                </section>
              )}

              {(user.role === "setter" || user.role === "closer") && (
                <section className="space-y-2">
                  <h4 className="font-semibold">Managed by</h4>
                  {user.managers.length ? (
                    <ul className="flex flex-wrap gap-1.5">
                      {user.managers.map((m) => (
                        <li key={m.id} className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-0.5 text-xs">
                          {m.display}
                          <button className="focus-ring text-muted hover:text-bad" aria-label={`Remove ${m.display}`} onClick={() => run("unassign", () => del(`/team/assignments/${m.id}/${user.id}`), "Removed")}><X className="size-3" /></button>
                        </li>
                      ))}
                    </ul>
                  ) : <p className="text-xs text-muted">No manager yet.</p>}
                  {mgrOptions.length > 0 && (
                    <div className="flex gap-2">
                      <div className="min-w-0 flex-1"><Select value={mgr} onChange={(e) => setMgr(e.target.value)} aria-label="Add a manager"
                        options={[{ value: "", label: "Add a manager…" }, ...mgrOptions.map((m) => ({ value: String(m.id), label: m.display }))]} /></div>
                      <Button onClick={() => run("assign", () => post("/team/assignments", { manager_id: Number(mgr), member_id: user.id }), "Manager added")} disabled={!mgr} loading={busy === "assign"}>Add</Button>
                    </div>
                  )}
                </section>
              )}

              <section className="space-y-2">
                <h4 className="font-semibold">Access</h4>
                {user.active ? (
                  <>
                    <Button tone="danger" disabled={self} loading={busy === "off"} onClick={() => run("off", () => post(`/team/users/${user.id}/deactivate`, {}), "Switched off. Every session has ended.")}><UserX className="size-4" /> Switch off this account</Button>
                    <p className="text-xs text-muted">They're signed out everywhere straight away. Their records stay; you can switch them back on.</p>
                  </>
                ) : (
                  <Button tone="good" loading={busy === "on"} onClick={() => run("on", () => post(`/team/users/${user.id}/reactivate`, {}), "Switched back on")}><UserCheck className="size-4" /> Switch back on</Button>
                )}
              </section>

              {user.active && (
                <section className="space-y-2">
                  <h4 className="font-semibold">Password</h4>
                  {reset ? <LinkOnce res={reset} emailLive={data.email_live} kind="reset" /> : (
                    <>
                      <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={resetEmail} onChange={(e) => setResetEmail(e.target.checked)} /> Also draft an email for my approval</label>
                      <Button loading={busy === "reset"} onClick={async () => { const r = await run("reset", () => post<LinkResult>(`/team/users/${user.id}/reset-link`, { send_email: resetEmail, origin: location.origin })); if (r) setReset(r); }}>
                        <KeyRound className="size-4" /> Create a reset link
                      </Button>
                      <p className="text-xs text-muted">A one-time link to choose a new password, valid for {data.reset_hours} hours. Older reset links stop working.</p>
                    </>
                  )}
                </section>
              )}
            </>
          )}

          <section className="space-y-2">
            <h4 className="font-semibold">Their data</h4>
            <a href={`${BASE}/team/users/${user.id}/export`} download className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-3.5 py-2 text-sm font-medium hover:bg-panel-2"><Download className="size-4" /> Export everything (JSON)</a>
            {!owner && !self && (
              <div className="space-y-2 rounded-lg border border-red-200 bg-red-50/50 p-3">
                <p className="text-xs text-red-950">Delete removes their name, email, password, memory, chats, uploads, practice and prospect lists, and rejects open drafts in their name. Approval history and the audit log are kept. This can't be undone.</p>
                <Field label={`Type ${user.username} to confirm`}><Input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" /></Field>
                <Button tone="danger" disabled={confirm !== user.username} loading={busy === "delete"}
                  onClick={async () => { const r = await run("delete", () => post(`/team/users/${user.id}/delete`, { confirm }), "Deleted"); if (r) setDeleted(r); }}>
                  <Trash2 className="size-4" /> Delete this person's data
                </Button>
              </div>
            )}
          </section>
        </div>
      )}
    </Drawer>
  );
}

function People({ data, onManage, reload }: { data: Overview; onManage: (u: User) => void; reload: () => void }) {
  const { toast } = useApp();
  const [res, setRes] = useState<LinkResult | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const resetFor = async (uid: number) => {
    setBusy(uid);
    setErr(null);
    try {
      setRes(await post<LinkResult>(`/team/users/${uid}/reset-link`, { send_email: true, origin: location.origin }));
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4">
      {data.reset_requests.length > 0 && (
        <Card title="Password reset requests" subtitle="People who used 'Forgot password'. Nothing has been sent to them yet.">
          <ErrorBox error={err} />
          <ul className="divide-y divide-line">
            {data.reset_requests.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <span className="min-w-0 flex-1"><b>{r.display}</b> asked on {day(r.created)}</span>
                <Button size="sm" onClick={() => resetFor(r.user_id)} loading={busy === r.user_id}><KeyRound className="size-3.5" /> Create reset link</Button>
                <Button size="sm" tone="ghost" onClick={async () => { try { await post(`/team/reset-requests/${r.id}/dismiss`, {}); toast("Dismissed", "info"); reload(); } catch (e) { setErr(e as ApiError); } }}>Dismiss</Button>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card pad={false}>
        <Table
          rows={data.users}
          empty="Only you so far. Invite your first coach, setter, closer or student."
          onRow={onManage}
          cols={[
            { key: "display", label: "Person", render: (u: User) => (
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 font-medium">{u.display} {u.demo && <DemoBadge />}</div>
                <div className="max-w-[16rem] truncate text-xs text-muted">{u.email || u.username}</div>
              </div>
            ) },
            { key: "role", label: "Role", render: (u: User) => <Badge tone={u.role === "owner" ? "brand" : "neutral"}>{u.role_label}</Badge> },
            { key: "active", label: "Access", render: (u: User) => u.active ? <Badge tone="good">Active</Badge> : <Badge tone="warn">Switched off</Badge> },
            { key: "team", label: "Coach / team", render: (u: User) => (
              <span className="text-xs text-muted">
                {u.role === "student" ? (u.coach ? `Coach: ${u.coach}` : "No coach yet") :
                  u.role === "coach" ? `${u.students || 0} student(s)` :
                    u.managers.length ? `Managed by ${u.managers.map((m) => m.display).join(", ")}` :
                      u.manages.length ? `Manages ${u.manages.length}` : "—"}
              </span>
            ) },
            { key: "act", label: "", render: (u: User) => <Button size="sm" onClick={(e) => { e.stopPropagation(); onManage(u); }}>Manage</Button> },
          ]}
        />
      </Card>
      <Modal open={!!res} onClose={() => setRes(null)} title="Reset link created">
        {res && <LinkOnce res={res} emailLive={data.email_live} kind="reset" />}
      </Modal>
    </div>
  );
}

function Invites({ data, reload, onReinvite }: { data: Overview; reload: () => void; onReinvite: (i: Invite) => void }) {
  const { toast } = useApp();
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const cancel = async (id: number) => {
    setBusy(id);
    setErr(null);
    try {
      await post(`/team/invites/${id}/revoke`, {});
      toast("Link cancelled. Its queued email was withdrawn.", "good");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(null);
    }
  };
  return (
    <Card pad={false}>
      <div className="px-4 pt-3"><ErrorBox error={err} /></div>
      <Table
        rows={data.invites}
        empty="No invites yet."
        cols={[
          { key: "who", label: "Who", render: (i: Invite) => (
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 font-medium">{i.display || i.email || "Removed"} {i.demo && <DemoBadge />}</div>
              <div className="max-w-[16rem] truncate text-xs text-muted">{i.email}</div>
            </div>
          ) },
          { key: "kind", label: "Link for", render: (i: Invite) => i.kind === "reset" ? "Password reset" : `Joining as ${i.role_label}` },
          { key: "state", label: "Status", render: (i: Invite) => <Badge tone={STATE[i.state]?.tone || "neutral"}>{STATE[i.state]?.label || i.state}</Badge> },
          { key: "dates", label: "Dates", render: (i: Invite) => <span className="text-xs text-muted">Created {day(i.created)}{i.state === "open" ? ` · expires ${day(i.expires)}` : ""}</span> },
          { key: "act", label: "", render: (i: Invite) => (
            <div className="flex gap-1.5">
              {i.state === "open" && <Button size="sm" tone="danger" onClick={() => cancel(i.id)} loading={busy === i.id}>Cancel link</Button>}
              {i.kind === "invite" && i.state !== "open" && i.state !== "used" && i.email && <Button size="sm" onClick={() => onReinvite(i)}>New link</Button>}
              {i.queue_id && i.state === "open" && <a className="self-center text-xs text-brand hover:underline" href="#/queue">Email</a>}
            </div>
          ) },
        ]}
      />
    </Card>
  );
}

function Students({ data, reloadAll, onInvite }: { data: Overview; reloadAll: () => void; onInvite: (s: any) => void }) {
  const { toast } = useApp();
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const list = useApi<{ items: any[]; total: number; shown: number }>(`/team/students?q=${encodeURIComponent(query)}`, [query]);
  const [text, setText] = useState("");
  const [coach, setCoach] = useState("");
  const [invite, setInvite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [result, setResult] = useState<any>(null);
  useEffect(() => { const t = setTimeout(() => setQuery(q.trim()), 300); return () => clearTimeout(t); }, [q]);
  const setStudentCoach = async (sid: number, cid: string) => {
    setErr(null);
    try {
      await post(`/team/students/${sid}/coach`, { coach_id: cid ? Number(cid) : null });
      toast("Coach updated", "good");
      list.reload();
      reloadAll();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const doImport = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post("/team/students/import", { text, coach_id: coach ? Number(coach) : null, invite, origin: location.origin });
      setResult(r);
      if (r.created.length) setText("");
      list.reload();
      reloadAll();
    } catch (e) {
      setErr(e as ApiError);
    } finally {
      setBusy(false);
    }
  };
  const coachOpts = [{ value: "", label: "No coach yet" }, ...data.coaches.map((c) => ({ value: String(c.id), label: c.display }))];
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <Card title="Student records" subtitle={list.data ? `${list.data.total.toLocaleString()} in total${list.data.shown < list.data.total ? `, showing ${list.data.shown}` : ""}` : undefined} pad={false}>
        <div className="px-4 pt-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name or email" className="pl-9" aria-label="Search students" maxLength={80} />
          </div>
          <div className="mt-2"><ErrorBox error={err || list.error} retry={list.error ? list.reload : undefined} /></div>
        </div>
        {list.loading && !list.data ? <div className="p-4"><Loading rows={4} /></div> : (
          <Table
            rows={list.data?.items || []}
            empty={query ? "No students match that search." : "No student records yet. Import your list on the right."}
            cols={[
              { key: "name", label: "Student", render: (s: any) => (
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-medium">{s.name} {s.demo && <DemoBadge />}</div>
                  <div className="max-w-[14rem] truncate text-xs text-muted">{s.email || "No email"}</div>
                </div>
              ) },
              { key: "coach", label: "Coach", render: (s: any) => (
                <div className="w-44"><Select value={s.coach_id ? String(s.coach_id) : ""} onChange={(e) => setStudentCoach(s.id, e.target.value)} options={coachOpts} aria-label={`Coach for ${s.name}`} /></div>
              ) },
              { key: "account", label: "Account", render: (s: any) => <Badge tone={ACCOUNT[s.account]?.tone || "neutral"}>{ACCOUNT[s.account]?.label || s.account}</Badge> },
              { key: "act", label: "", render: (s: any) => s.account === "none" && s.email ? <Button size="sm" onClick={() => onInvite(s)}><UserPlus className="size-3.5" /> Invite</Button> : null },
            ]}
          />
        )}
      </Card>
      <Card title={<span className="flex items-center gap-2"><Upload className="size-4 text-brand" /> Import students</span>} subtitle="One per line: name, email, niche (niche optional). A header row is fine.">
        <div className="space-y-3">
          <TextArea rows={7} value={text} onChange={(e) => setText(e.target.value)} placeholder={"Ava Example, ava@example.com, physio clinics\nNoah Sample, noah@example.com"} aria-label="Students to import" />
          <Field label="Coach for everyone in this list (optional)"><Select value={coach} onChange={(e) => setCoach(e.target.value)} options={coachOpts} /></Field>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={invite} onChange={(e) => setInvite(e.target.checked)} />
            <span>Also draft an invite email for each new student <span className="block text-xs text-muted">Up to 200 at a time, each waiting for your approval.</span></span>
          </label>
          <Button tone="primary" onClick={doImport} loading={busy} disabled={!text.trim()}>Import</Button>
          {result && (
            <div className="space-y-2 rounded-lg border border-line p-3 text-sm" data-testid="import-result">
              <p><b>{result.created.length}</b> added · <b>{result.skipped.length}</b> skipped{result.invites.length ? <> · <b>{result.invites.length}</b> invite email(s) waiting in the <a className="text-brand hover:underline" href="#/queue">approval queue</a></> : null}</p>
              {result.skipped.length > 0 && (
                <ul className="max-h-40 overflow-auto text-xs text-muted">{result.skipped.map((s: any, i: number) => <li key={i}>Line {s.line}: {s.reason}</li>)}</ul>
              )}
              {result.not_invited > 0 && <p className="text-xs text-muted">{result.not_invited} not invited yet (limit reached or on the never-invite list). Use Invite next to each one.</p>}
              <TraceLink runId={result.run_id} />
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function Assignments({ data, reload }: { data: Overview; reload: () => void }) {
  const { toast } = useApp();
  const [mgr, setMgr] = useState("");
  const [mem, setMem] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const add = async () => {
    setErr(null);
    try {
      await post("/team/assignments", { manager_id: Number(mgr), member_id: Number(mem) });
      toast("Assigned", "good");
      setMem("");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const remove = async (a: { manager_id: number; member_id: number }) => {
    setErr(null);
    try {
      await del(`/team/assignments/${a.manager_id}/${a.member_id}`);
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Who manages whom" subtitle="Setters and closers can report to a coach, a closer or you. Managers see their team's training progress.">
        <ErrorBox error={err} />
        {data.assignments.length === 0 ? <Empty title="No assignments yet" hint="Pick a manager and a setter or closer." icon={<UsersRound className="size-6" />} /> : (
          <ul className="divide-y divide-line">
            {data.assignments.map((a) => (
              <li key={`${a.manager_id}-${a.member_id}`} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <span className="min-w-0 flex-1"><b>{a.member}</b> <span className="text-muted">is managed by</span> <b>{a.manager}</b></span>
                <Button size="sm" tone="ghost" onClick={() => remove(a)}>Remove</Button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Add an assignment">
        {data.members.length === 0 ? <p className="text-sm text-muted">Invite a setter or closer first.</p> : (
          <div className="space-y-3">
            <Field label="Setter or closer"><Select value={mem} onChange={(e) => setMem(e.target.value)} options={[{ value: "", label: "Choose…" }, ...data.members.map((m) => ({ value: String(m.id), label: `${m.display} (${m.role})` }))]} /></Field>
            <Field label="Managed by"><Select value={mgr} onChange={(e) => setMgr(e.target.value)} options={[{ value: "", label: "Choose…" }, ...data.managers.filter((m) => String(m.id) !== mem).map((m) => ({ value: String(m.id), label: `${m.display} (${m.role})` }))]} /></Field>
            <Button tone="primary" onClick={add} disabled={!mgr || !mem}>Assign</Button>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function Team({ query }: { query?: URLSearchParams }) {
  const { me } = useApp();
  const { data, error, loading, reload } = useApi<Overview>("/team");
  const qtab = query?.get("tab") || "";
  const [tab, setTab] = useState(qtab || "people");
  const [manage, setManage] = useState<User | null>(null);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [preset, setPreset] = useState<any>(undefined);
  useEffect(() => { if (qtab) setTab(qtab); }, [qtab]);
  // fresh data when it exists; the last snapshot after a delete, so the drawer can show what was removed
  const managed = manage ? data?.users.find((u) => u.id === manage.id) || manage : null;
  const openInvite = (p?: any) => { setPreset(p); setInviteOpen(true); };
  return (
    <div>
      <PageHeader title="Team & invites" badge={me.is_demo ? <DemoBadge /> : null}
        subtitle="Invite people with a one-time link, set what each person can do, and handle access and data requests. Every change is in the audit log."
        right={<Button tone="primary" onClick={() => openInvite()} disabled={!data}><UserPlus className="size-4" /> Invite someone</Button>} />
      <ErrorBox error={error} retry={reload} />
      {loading && !data && <Loading rows={6} />}
      {data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Active people" value={data.counts.people} hint={data.counts.switched_off ? `${data.counts.switched_off} switched off` : "including you"} />
            <Stat label="Links waiting" value={data.counts.open_invites} hint="Invites and reset links not used yet" />
            <Stat label="Student records" value={data.counts.students.toLocaleString()} />
            <Stat label="Reset requests" value={data.reset_requests.length} tone={data.reset_requests.length ? "warn" : undefined} />
          </div>
          <Tabs value={tab} onChange={setTab} tabs={[
            { key: "people", label: "People" }, { key: "invites", label: "Invites & links" },
            { key: "students", label: "Students" }, { key: "assignments", label: "Assignments" },
          ]} />
          {tab === "people" && <People data={data} onManage={(u) => setManage(u)} reload={reload} />}
          {tab === "invites" && <Invites data={data} reload={reload} onReinvite={(i) => openInvite({ email: i.email, display: i.display, role: i.role })} />}
          {tab === "students" && <Students data={data} reloadAll={reload} onInvite={(s) => openInvite({ email: s.email, display: s.name, role: "student", student_id: s.id })} />}
          {tab === "assignments" && <Assignments data={data} reload={reload} />}
          <InviteModal open={inviteOpen} onClose={() => setInviteOpen(false)} data={data} preset={preset} onDone={reload} />
          <ManageDrawer user={managed} data={data} onClose={() => setManage(null)} onChanged={reload} />
        </>
      )}
    </div>
  );
}
