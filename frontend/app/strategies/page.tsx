"use client";
import { useEffect, useState } from "react";
import { ParamForm } from "@/components/ParamForm";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Msg, num, Panel, pct, post, signed, Table, TIMEFRAMES, tone, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

function Strategies({ user }: { user: User }) {
  const library = useApi("/api/strategies");
  const deployments = useApi("/api/deployments", 15_000);
  const instruments = useApi("/api/instruments");
  const brokers = useApi("/api/brokers/status");
  const [selected, setSelected] = useState<Json>(null);
  const [params, setParams] = useState<Record<string, Json>>({});
  const [target, setTarget] = useState({ instrument_id: "CRYPTO:BTC/USDT", timeframe: "1h", broker_id: "" });
  const [historyId, setHistoryId] = useState<number | null>(null);
  const history = useApi(historyId ? `/api/deployments/${historyId}/history` : null);
  const [editing, setEditing] = useState<{ id: number; schema: Json; params: Record<string, Json> } | null>(null);
  const act = useAction();
  const canWrite = user.role !== "viewer";
  const tradable = (brokers.data ?? []).filter((b: { data_only?: boolean }) => !b.data_only);

  useEffect(() => { if (!target.broker_id && tradable.length) setTarget((t) => ({ ...t, broker_id: tradable[0].id })); }, [tradable, target.broker_id]);
  const pick = (s: Json) => { setSelected(s); setParams(s.defaults); act.setMsg(null); };
  const schemaOf = (name: string) => (library.data ?? []).find((s: { name: string }) => s.name === name)?.params_schema;
  const refresh = () => deployments.reload();
  const categories: string[] = Array.from(new Set((library.data ?? []).map((s: { category: string }) => s.category)));

  return (
    <div className="space-y-4">
      <Panel title="Deployed strategies">
        <Table empty="No strategy deployed. Pick one from the library below." head={["", "Strategy", "Version", "Instrument", "TF", "Account", "Position", "Trades", "Net P&L", "Win rate", ""]}
          rows={(deployments.data ?? []).map((d: Json) => [
            <Badge key="s" kind={d.is_active ? "up" : "dim"}>{d.is_active ? "ACTIVE" : "inactive"}</Badge>, d.strategy, d.version, d.instrument_id, d.timeframe, d.broker_id,
            num(d.position, 5), d.trades, <span key="p" className={tone(d.net_profit)}>{signed(d.net_profit)}</span>, pct(d.win_rate, 1),
            <span key="a" className="flex gap-2">
              {canWrite && <button className="text-accent hover:underline" onClick={() => act.run(() => post(`/api/deployments/${d.id}`, { active: !d.is_active }, "PATCH"), d.is_active ? "Deactivated." : "Activated.").then(refresh)}>{d.is_active ? "Deactivate" : "Activate"}</button>}
              {canWrite && <button className="text-accent hover:underline" onClick={() => setEditing({ id: d.id, schema: schemaOf(d.strategy), params: d.params })}>Parameters</button>}
              <button className="text-accent hover:underline" onClick={() => setHistoryId(historyId === d.id ? null : d.id)}>History</button>
              {canWrite && <button className="text-down hover:underline" onClick={() => window.confirm(`Delete the ${d.strategy} deployment on ${d.instrument_id}?`) && act.run(() => post(`/api/deployments/${d.id}`, undefined, "DELETE"), "Deleted.").then(refresh)}>Delete</button>}
            </span>])} />
        <div className="mt-2"><Msg msg={act.msg} /></div>
        {editing?.schema && (
          <div className="mt-4 space-y-3 rounded border border-line p-3">
            <h3 className="text-sm font-semibold">Parameters of deployment #{editing.id}</h3>
            <ParamForm schema={editing.schema} value={editing.params} onChange={(p) => setEditing({ ...editing, params: p })} />
            <div className="flex gap-2">
              <Button disabled={act.busy} onClick={() => act.run(() => post(`/api/deployments/${editing.id}`, { params: editing.params }, "PATCH"), "Parameters saved (recorded in the version history).").then((ok) => { if (ok) { setEditing(null); refresh(); } })}>Save parameters</Button>
              <Button kind="ghost" onClick={() => setEditing(null)}>Cancel</Button>
            </div>
          </div>
        )}
        {historyId && (
          <div className="mt-4"><h3 className="mb-2 text-sm font-semibold">Version history of deployment #{historyId}</h3>
            <Table max="max-h-60" head={["Time", "By", "Change", "Detail"]} rows={(history.data ?? []).map((h: Json) => [when(h.ts), h.actor, h.action.replace("deployment.", ""),
              <span key="d" className="whitespace-pre-wrap break-all text-dim">{JSON.stringify(h.detail.after ?? h.detail.params ?? {})}</span>])} />
          </div>
        )}
      </Panel>
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Strategy library">
          <div className="max-h-[32rem] space-y-4 overflow-auto">
            {categories.map((c) => (
              <div key={c}>
                <h3 className="mb-1 text-[11px] uppercase tracking-wider text-dim">{c.replace(/_/g, " ")}</h3>
                <ul className="space-y-1">
                  {(library.data ?? []).filter((s: Json) => s.category === c).map((s: Json) => (
                    <li key={s.name}>
                      <button onClick={() => pick(s)} className={`w-full rounded border px-3 py-2 text-left ${selected?.name === s.name ? "border-accent" : "border-line hover:border-dim"}`}>
                        <span className="font-mono text-sm">{s.name}</span> <span className="font-mono text-[11px] text-dim">v{s.version}</span>
                        <p className="mt-0.5 text-xs text-dim">{s.description}</p>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title={selected ? `Configure ${selected.name}` : "Configure"}>
          {!selected ? <p className="text-sm text-dim">Select a strategy to configure and deploy it.</p> : (
            <div className="space-y-4">
              <ParamForm schema={selected.params_schema} value={params} onChange={setParams} />
              <div className="grid gap-3 sm:grid-cols-3">
                <Field label="Instrument"><select value={target.instrument_id} onChange={(e) => setTarget({ ...target, instrument_id: e.target.value })}>{(instruments.data ?? []).map((i: Json) => <option key={i.id}>{i.id}</option>)}</select></Field>
                <Field label="Timeframe"><select value={target.timeframe} onChange={(e) => setTarget({ ...target, timeframe: e.target.value })}>{TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select></Field>
                <Field label="Account"><select value={target.broker_id} onChange={(e) => setTarget({ ...target, broker_id: e.target.value })}>{tradable.map((b: Json) => <option key={b.id}>{b.id}</option>)}</select></Field>
              </div>
              {!tradable.length && <p className="text-xs text-warn">Create a paper account under Execution first.</p>}
              <p className="text-xs text-dim">A new deployment is created inactive. It trades only after you activate it, and every order still passes the risk engine.</p>
              {canWrite && <Button disabled={act.busy || !target.broker_id} onClick={() => act.run(() => post("/api/deployments", { strategy: selected.name, params, ...target }), "Deployment created (inactive).").then(refresh)}>Create deployment</Button>}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Strategies user={user} />}</Shell>;
}
