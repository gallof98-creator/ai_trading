"use client";
import Link from "next/link";
import { EquityChart } from "@/components/charts/Charts";
import { ReturnTiles } from "@/components/Returns";
import { Shell } from "@/components/Shell";
import { Badge, Button, money, Notice, num, Panel, pct, post, signed, SimBadge, Stat, Table, tone, useApi, when } from "@/components/ui";

type Fresh = { instrument_id: string; symbol: string; group: string; feed: string; last_update: string | null; last_update_kind: string; age_seconds: number | null;
  state: string; official_bars: boolean; followed_live: boolean; series: { timeframe: string; last_bar_close: string; source: string; feed: string; bars: number }[] };

const age = (s: number | null) => (s === null ? "—" : s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`);
const STATE: Record<string, "up" | "warn" | "down" | "dim"> = { current: "up", late: "warn", stale: "down", "market closed": "dim", "no data": "down" };

/** When data last arrived for each monitored instrument, and from which feed. */
function DataFreshness() {
  const { data } = useApi("/api/live/freshness", 15_000);
  if (!data || !data.instruments?.length) return null;
  const groups = Object.entries(data.groups ?? {}) as [string, { state: string; last_success: string | null; last_error: string; feed: string }][];
  return (
    <Panel title="Market data: last update per instrument" right={<span className="text-xs text-dim">US market {data.us_market_open ? "open" : "closed"}</span>}>
      <div className="mb-2 flex flex-wrap gap-2 text-xs">
        {groups.map(([name, g]) => <span key={name} title={g.last_error || undefined}>
          <Badge kind={g.state === "ok" ? "up" : g.state === "market closed" ? "dim" : "warn"}>{name}: {g.state}</Badge> <span className="text-dim">official bars, last refresh {when(g.last_success)}</span></span>)}
      </div>
      <Table max="max-h-72" head={["Instrument", "Status", "Last update", "Age", "Feed", "Newest bars stored"]}
        rows={(data.instruments as Fresh[]).map((r) => [
          r.symbol, <Badge key="s" kind={STATE[r.state] ?? "dim"}>{r.state}</Badge>,
          <span key="t" className="font-mono text-xs" title={r.last_update_kind}>{when(r.last_update)}</span>, age(r.age_seconds),
          <span key="f" className="text-xs">{r.feed}{r.official_bars ? "" : r.followed_live ? " (rebuilt from ticks right now)" : ""}</span>,
          <span key="b" className="font-mono text-[11px] text-dim">{r.series.map((x) => `${x.timeframe} ${when(x.last_bar_close).slice(5)}`).join(" · ")}</span>])} />
      <p className="mt-2 text-xs text-dim">IEX is one US exchange: prices are real time, volume is only what traded there. Consolidated-tape data is 15 minutes delayed on the free plan and is used for history only.</p>
    </Panel>
  );
}

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
        <ReturnTiles />
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
      <DataFreshness />
    </div>
  );
}

export default function Page() {
  return <Shell>{() => <Overview />}</Shell>;
}
