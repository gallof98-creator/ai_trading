"use client";
import Link from "next/link";
import { EquityChart } from "@/components/charts/Charts";
import { Shell } from "@/components/Shell";
import { Badge, Button, money, Notice, num, Panel, pct, post, signed, SimBadge, Stat, Table, tone, useApi, when } from "@/components/ui";

function Overview() {
  const { data: ov, error } = useApi("/api/overview", 10_000);
  const { data: equity } = useApi<{ ts: string; equity: number }[]>("/api/portfolio/equity", 30_000);
  const notes = useApi("/api/notifications?limit=12", 10_000);
  if (error) return <Notice kind="down">{error}</Notice>;
  if (!ov) return <p className="text-sm text-dim">Loading…</p>;
  if (!ov.configured) {
    return (
      <Notice kind="accent">
        No broker account yet. Set the initial paper capital in <Link className="underline" href="/settings">Settings</Link>, then create a paper
        account under <Link className="underline" href="/execution">Execution</Link>.
      </Notice>
    );
  }
  if (ov.error) return <Notice kind="down">{ov.error} — add an FX rate in <Link className="underline" href="/settings">Settings</Link>.</Notice>;
  const ccy = ov.base_currency;
  const simulatedData = Object.values(ov.data_kinds ?? {}).includes("simulated");
  return (
    <div className="space-y-4">
      {ov.risk.halted && <Notice kind="down">Trading is halted ({ov.risk.kill_switch ?? Object.keys(ov.risk.breakers).join(", ")}). New risk-increasing orders are rejected. See the <Link className="underline" href="/risk">Risk Center</Link>.</Notice>}
      {!ov.reconciled && <Notice kind="down">Positions do not reconcile with the broker: {ov.discrepancies.join("; ")}</Notice>}
      {simulatedData && <Notice kind="accent">Prices currently driving the paper engine are SIMULATED (seeded random walk), not market data. Results say nothing about real markets.</Notice>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Stat label="Portfolio value" value={money(ov.equity, ccy)} hint={ov.all_simulated ? "paper accounts" : "includes real accounts"} />
        <Stat label="Available balance" value={money(ov.available_capital, ccy)} />
        <Stat label="Daily P&L" value={signed(ov.daily_pnl)} cls={tone(ov.daily_pnl)} />
        <Stat label="Total P&L" value={signed(ov.total_pnl)} cls={tone(ov.total_pnl)} />
        <Stat label="Win rate" value={pct(ov.win_rate, 1)} hint={`${ov.number_of_trades} closed trades`} />
        <Stat label="Max drawdown" value={pct(ov.max_drawdown)} cls={ov.max_drawdown > 0.1 ? "text-warn" : ""} />
        <Stat label="Open positions" value={ov.positions.length} />
        <Stat label="Active strategies" value={ov.active_strategies} />
        <Stat label="Active brokers" value={ov.active_brokers} />
        <Stat label="Trading mode" value={ov.trading_mode} />
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Equity" className="xl:col-span-2"><EquityChart data={equity ?? []} /></Panel>
        <Panel title="Notifications" right={<Button kind="ghost" onClick={() => post("/api/notifications/read-all").then(notes.reload)}>Mark read</Button>}>
          <ul className="max-h-60 space-y-2 overflow-auto text-xs">
            {(notes.data?.items ?? []).map((n: { id: number; severity: string; title: string; ts: string; is_read: boolean }) => (
              <li key={n.id} className={n.is_read ? "text-dim" : ""}>
                <Badge kind={n.severity === "critical" ? "down" : n.severity === "warning" ? "warn" : "dim"}>{n.severity}</Badge> {n.title}
                <div className="font-mono text-[10px] text-dim">{when(n.ts)}</div>
              </li>
            ))}
            {!notes.data?.items?.length && <li className="text-dim">No notifications.</li>}
          </ul>
        </Panel>
      </div>
      <Panel title="Open positions">
        <Table empty="No open positions." head={["Instrument", "Strategy", "Account", "Qty", "Avg price", "Last", "Unrealised P&L"]}
          rows={ov.positions.map((p: { instrument_id: string; strategy: string; broker_id: string; qty: number; avg_price: number; last_price: number; unrealized_pnl: number }) => [
            p.instrument_id, p.strategy ?? "manual", <span key="b">{p.broker_id} <SimBadge simulated={ov.brokers.find((b: { broker_id: string }) => b.broker_id === p.broker_id)?.is_simulated} /></span>,
            num(p.qty, 5), num(p.avg_price, 4), num(p.last_price, 4), <span key="u" className={tone(p.unrealized_pnl)}>{signed(p.unrealized_pnl)} {ccy}</span>])} />
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{() => <Overview />}</Shell>;
}
