"use client";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Msg, Notice, num, Panel, parseFx, post, SimBadge, Table, TIMEFRAMES, useAction, useApi, when } from "@/components/ui";
import type { Health, User } from "@/services/api";

const statusKind = (s: string) => (s === "filled" ? "up" : s === "rejected" ? "down" : s === "pending_approval" ? "warn" : "dim");

function Execution({ user }: { user: User }) {
  const health = useApi<Health>("/health");
  const brokers = useApi("/api/brokers/status", 10_000);
  const approvals = useApi("/api/approvals", 5_000);
  const orders = useApi("/api/orders?limit=150", 10_000);
  const fills = useApi("/api/fills?limit=100", 10_000);
  const instruments = useApi("/api/instruments");
  const settings = useApi("/api/settings");
  const log = useApi(user.role === "admin" ? "/api/audit?limit=80" : null, 20_000);
  const [account, setAccount] = useState({ id: "paper-main", label: "Main paper account" });
  const [confirm, setConfirm] = useState("");
  const [replay, setReplay] = useState({ instrument_id: "CRYPTO:BTC/USDT", timeframe: "1h", bars: 100, seed: 1, fx: "USD=0.92" });
  const act = useAction();
  const isAdmin = user.role === "admin";
  const canWrite = user.role !== "viewer";
  const paper = (brokers.data ?? []).filter((b: Json) => b.is_simulated);
  const refresh = () => { brokers.reload(); orders.reload(); fills.reload(); approvals.reload(); };

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Paper trading" right={<Badge kind="accent">SIMULATED EXECUTION</Badge>}>
          <div className="space-y-4">
            <p className="text-xs text-dim">Paper accounts use the same signal, risk and portfolio logic as live trading; only the execution venue is simulated (next-bar fills, spread, slippage, fees, partial fills). Starting capital: {settings.data?.initial_paper_capital ? `${num(settings.data.initial_paper_capital)} ${settings.data.base_currency}` : "not configured — set it in Settings first"}.</p>
            {isAdmin && (
              <div className="flex flex-wrap items-end gap-3">
                <Field label="Account id"><input value={account.id} onChange={(e) => setAccount({ ...account, id: e.target.value })} /></Field>
                <Field label="Label"><input value={account.label} onChange={(e) => setAccount({ ...account, label: e.target.value })} /></Field>
                <Button disabled={act.busy} onClick={() => act.run(() => post("/api/paper/accounts", account), "Paper account created.").then(refresh)}>Create paper account</Button>
              </div>
            )}
            {isAdmin && paper.length > 0 && (
              <div className="rounded border border-down/30 p-3">
                <h3 className="text-sm font-semibold text-down">Reset a paper account</h3>
                <p className="mt-1 text-xs text-dim">Deletes its positions, orders and trade history and restores the starting capital. Type the account id to confirm.</p>
                <div className="mt-2 flex flex-wrap items-end gap-3">
                  <Field label={`Type an account id (${paper.map((b: Json) => b.id).join(", ")})`}><input value={confirm} onChange={(e) => setConfirm(e.target.value)} /></Field>
                  <Button kind="danger" disabled={act.busy || !paper.some((b: Json) => b.id === confirm)} onClick={() => act.run(() => post(`/api/paper/accounts/${confirm}/reset`, { confirmation: confirm }), "Account reset.").then(() => { setConfirm(""); refresh(); })}>Reset account</Button>
                </div>
              </div>
            )}
            {canWrite && (
              <div className="rounded border border-line p-3">
                <h3 className="text-sm font-semibold">Simulated market replay</h3>
                <p className="mt-1 text-xs text-dim">Feeds seeded random-walk bars to the paper engine so you can watch the full pipeline without a market-data connection. Needs an active strategy on the instrument and timeframe. Not market data.</p>
                <div className="mt-2 grid gap-3 sm:grid-cols-5">
                  <Field label="Instrument"><select value={replay.instrument_id} onChange={(e) => setReplay({ ...replay, instrument_id: e.target.value })}>{(instruments.data ?? []).map((i: Json) => <option key={i.id}>{i.id}</option>)}</select></Field>
                  <Field label="Timeframe"><select value={replay.timeframe} onChange={(e) => setReplay({ ...replay, timeframe: e.target.value })}>{TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select></Field>
                  <Field label="Bars"><input type="number" min="1" max="1000" value={replay.bars} onChange={(e) => setReplay({ ...replay, bars: Number(e.target.value) })} /></Field>
                  <Field label="Seed"><input type="number" min="0" value={replay.seed} onChange={(e) => setReplay({ ...replay, seed: Number(e.target.value) })} /></Field>
                  <Field label="Assumed FX to base"><input value={replay.fx} onChange={(e) => setReplay({ ...replay, fx: e.target.value })} /></Field>
                </div>
                <div className="mt-3"><Button kind="ghost" disabled={act.busy} onClick={() => act.run(() => post("/api/paper/replay", { instrument_id: replay.instrument_id, timeframe: replay.timeframe, bars: replay.bars, seed: replay.seed, fx_assumptions: parseFx(replay.fx) }), "Replay processed.").then(refresh)}>{act.busy ? "Replaying…" : "Replay simulated bars"}</Button></div>
              </div>
            )}
            <Msg msg={act.msg} />
          </div>
        </Panel>
        <div className="min-w-0 space-y-4">
          <Panel title="Live trading status">
            {health.data?.live_trading_authorized
              ? <Notice kind="down">Live trading is AUTHORIZED in this deployment. Real-money orders {settings.data?.live_orders_require_approval === false ? "are sent automatically" : "still wait for a human approval"}.</Notice>
              : <p className="text-sm text-dim">Live trading is <span className="text-ink">disabled</span>. It can only be enabled by the operator of a separate live deployment (two explicit environment settings), never from this interface and never automatically. Without it, exchange connections use the testnet or market data only.</p>}
          </Panel>
          <Panel title="Broker connectivity">
            <Table max="max-h-64" empty="No accounts." head={["Account", "", "Status", "Trading", "Order types", "Shorting"]}
              rows={(brokers.data ?? []).map((b: Json) => [b.id, b.data_only ? <Badge key="k">DATA ONLY</Badge> : <SimBadge key="k" simulated={b.is_simulated} />,
                <Badge key="c" kind={b.connected ? "up" : "down"}>{b.connected ? "connected" : "disconnected"}</Badge>, b.capabilities?.trading_enabled ? "enabled" : "off",
                (b.capabilities?.order_types ?? []).join(", ") || "—", b.capabilities?.supports_short ? "yes" : "no"])} />
          </Panel>
        </div>
      </div>
      <Panel title={`Pending approvals (${approvals.data?.length ?? 0})`}>
        <Table empty="No orders are waiting for approval." head={["Created", "", "Order", "Strategy", "Reference price", "Stop distance", ""]}
          rows={(approvals.data ?? []).map((a: Json) => [when(a.created_at), <SimBadge key="s" simulated={a.is_simulated} />, `${a.order.side.toUpperCase()} ${num(a.order.qty, 5)} ${a.order.instrument_id}${a.order.reduce_only ? " (exit)" : ""}`,
            a.order.strategy, num(a.reference_price, 4), num(a.stop_distance, 4),
            canWrite ? <span key="b" className="flex gap-2">
              <Button onClick={() => act.run(() => post(`/api/approvals/${a.id}`, { approve: true }), "Approved; the risk engine re-checked the order.").then(refresh)}>Approve</Button>
              <Button kind="ghost" onClick={() => act.run(() => post(`/api/approvals/${a.id}`, { approve: false }), "Declined.").then(refresh)}>Decline</Button></span> : "—"])} />
      </Panel>
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Orders">
          <Table head={["Updated", "", "Order", "Type", "Status", "Filled", "Avg px", "Purpose"]}
            rows={(orders.data ?? []).map((o: Json) => [when(o.updated_at), <SimBadge key="s" simulated={o.is_simulated} />, `${o.side.toUpperCase()} ${num(o.qty, 5)} ${o.instrument_id}`, o.type,
              <Badge key="t" kind={statusKind(o.status)}>{o.status.replace(/_/g, " ")}</Badge>, num(o.filled_qty, 5), num(o.avg_fill_price, 4), <span key="r" className="max-w-xs whitespace-normal text-dim">{o.reason}</span>])} />
        </Panel>
        <Panel title="Executions">
          <Table head={["Time", "", "Fill", "Price", "Commission", "Purpose", "Strategy"]}
            rows={(fills.data ?? []).map((f: Json) => [when(f.ts), <SimBadge key="s" simulated={f.is_simulated} />, `${f.side.toUpperCase()} ${num(f.qty, 5)} ${f.instrument_id}`, num(f.price, 4), num(f.commission, 4), f.reason, f.strategy ?? "manual"])} />
        </Panel>
      </div>
      {isAdmin && (
        <Panel title="Execution and audit log">
          <Table head={["Time", "Actor", "Action", "Detail"]}
            rows={(log.data ?? []).map((l: Json) => [when(l.ts), l.actor, l.action, <span key="d" className="block max-w-2xl truncate text-dim" title={JSON.stringify(l.detail)}>{JSON.stringify(l.detail)}</span>])} />
        </Panel>
      )}
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Execution user={user} />}</Shell>;
}
