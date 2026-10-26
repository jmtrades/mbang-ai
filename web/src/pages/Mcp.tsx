import { useState } from "react";
import { ApiError, del, post } from "../api";
import { Badge, Button, Card, CopyButton, ErrorBox, Input, Loading, PageHeader, Table, useApi } from "../components/ui";

export default function Mcp() {
  const { data, error, loading, reload } = useApi<any>("/mcp/tokens");
  const [label, setLabel] = useState("");
  const [tok, setTok] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const mint = async () => {
    setErr(null);
    try {
      const r = await post("/mcp/tokens", { label });
      setTok(r.token);
      setLabel("");
      reload();
    } catch (e) {
      setErr(e as ApiError);
    }
  };
  const example = `curl -s ${location.origin}/mcp -H "Authorization: Bearer <token>" -H "content-type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ask_coach","arguments":{"question":"What are the rules for a Thunder Dome test?"}}}'`;
  return (
    <div>
      <PageHeader title="MCP server" subtitle="Read-only JSON-RPC endpoint for other AI tools. Owner-minted bearer tokens. run_skill returns a draft and never queues or sends." />
      <ErrorBox error={err || error} retry={reload} />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Mint a token">
          <div className="flex gap-2"><Input placeholder="Label (e.g. my laptop)" value={label} onChange={(e) => setLabel(e.target.value)} /><Button tone="primary" onClick={mint} disabled={!label}>Mint</Button></div>
          {tok && <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm"><b>Shown once.</b> Copy it now: <code className="break-all">{tok}</code> <CopyButton text={tok} /></div>}
          <p className="mt-3 text-xs text-muted">Endpoint: <code>POST /mcp</code> · tools: {data?.tools?.map((t: string) => <Badge key={t} className="mr-1">{t}</Badge>)}</p>
          <pre className="prose-pre mt-2 rounded-lg bg-panel-2 p-2 font-mono text-[11px]">{example}</pre>
        </Card>
        <Card title="Tokens">
          {loading && <Loading />}
          {data && <Table rows={data.items} empty="No tokens" cols={[
            { key: "label", label: "Label" }, { key: "created", label: "Created", render: (t) => new Date(t.created * 1000).toLocaleString() },
            { key: "revoked", label: "Status", render: (t) => t.revoked ? <Badge>revoked</Badge> : <Badge tone="good">active</Badge> },
            { key: "x", label: "", render: (t) => !t.revoked && <Button size="sm" tone="danger" onClick={async () => { await del(`/mcp/tokens/${t.id}`); reload(); }}>Revoke</Button> },
          ]} />}
        </Card>
      </div>
    </div>
  );
}
