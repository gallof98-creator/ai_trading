"use client";
import { EquityChart, PlainBars } from "@/components/charts/Charts";
import { Shell } from "@/components/Shell";
import { type Json, money, Notice, num, Panel, pct, signed, SimBadge, Stat, Table, tone, useApi, when } from "@/components/ui";

function Allocation({ title, groups }: { title: string; groups: Record<string, { gross: number; net: number; pct_of_equity: number | null }> }) {
  const data = Object.entries(groups).map(([k, g]) => ({ name: k === "null" ? "manual" : k, pct: (g.pct_of_equity ?? 0) * 100 }));
  return <Panel title={title}>{data.length ? <PlainBars layout="vertical" height={Math.max(90, data.length * 34)} data={data} xKey="name" yKey="pct" label="Gross exposure (% of equity)" /> : <p className="text-sm text-dim">No exposure.</p>}</Panel>;
}

function Portfolio() {
  const { data: ov, error } = useApi("/api/overview", 10_000);
  const equity = useApi<{ ts: string; equity: number }[]>("/api/portfolio/equity", 30_000);
  const trades = useApi("/api/trades?limit=200", 30_000);
  if (error) return <Notice kind="down">{error}</Notice>;
  if (!ov) return <p className="text-sm text-dim">Loading…</p>;
  if (!ov.configured || ov.error) return <Notice kind="accent">{ov.error ?? "No broker account yet. Create a paper account under Execution."}</Notice>;
  const ccy = ov.base_currency;
  const ids: string[] = Array.from(new Set(ov.correlations.flatMap((c: Json) => [c.a, c.b])));
  const corr = (a: string, b: string) => (a === b ? 1 : ov.correlations.find((c: Json) => (c.a === a && c.b === b) || (c.a === b && c.b === a))?.value);
  return (
    <div className="space-y-4">
      {!ov.reconciled && <Notice kind="down">Balances are shown per broker but NOT reconciled: {ov.discrepancies.join("; ")}</Notice>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <Stat label="Portfolio balance" value={money(ov.equity, ccy)} />
        <Stat label="Available capital" value={money(ov.available_capital, ccy)} />
        <Stat label="Unrealised P&L" value={signed(ov.unrealized_pnl)} cls={tone(ov.unrealized_pnl)} />
        <Stat label="Realised P&L" value={signed(ov.realized_pnl)} cls={tone(ov.realized_pnl)} />
        <Stat label="Gross / net exposure" value={`${pct(ov.gross_exposure / ov.equity, 0)} / ${pct(ov.net_exposure / ov.equity, 0)}`} />
        <Stat label="Drawdown / volatility" value={`${pct(ov.drawdown)} / ${pct(ov.volatility, 0)}`} hint="volatility: annualised, from daily equity" />
      </div>
      <Panel title="Broker balances">
        <Table head={["Account", "", "Currency", "Cash", "Equity", `Equity (${ccy})`, "Margin used", "FX rate", "Rate source / time"]}
          rows={ov.brokers.map((b: Json) => [b.broker_id, <SimBadge key="s" simulated={b.is_simulated} />, b.currency, num(b.cash), num(b.equity), num(b.equity_base), num(b.margin_used), num(b.fx_rate, 5), `${b.fx_source} · ${when(b.fx_ts)}`])} />
        <p className="mt-2 text-xs text-dim">Each balance comes from its own broker account. The total above is their sum in {ccy} at the rates shown; positions are reconciled against each broker on every update ({ov.reconciled ? "currently reconciled" : "currently NOT reconciled"}).</p>
      </Panel>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Allocation title="Exposure by market" groups={ov.exposure_by_market} />
        <Allocation title="Exposure by currency" groups={ov.exposure_by_currency} />
        <Allocation title="Exposure by broker" groups={ov.exposure_by_broker} />
        <Allocation title="Exposure by strategy" groups={ov.exposure_by_strategy} />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Performance"><EquityChart data={equity.data ?? []} /></Panel>
        <Panel title="Correlation matrix (bar returns, recent window)">
          {ids.length < 2 ? <p className="text-sm text-dim">Needs at least two instruments with 30 common bars.</p> : (
            <div className="overflow-auto">
              <table className="font-mono text-xs">
                <thead><tr><th />{ids.map((i) => <th key={i} className="px-2 py-1 font-normal text-dim">{i.split(":")[1]}</th>)}</tr></thead>
                <tbody>{ids.map((a) => (
                  <tr key={a}><th className="px-2 py-1 text-left font-normal text-dim">{a.split(":")[1]}</th>
                    {ids.map((b) => { const v = corr(a, b); return <td key={b} className="px-2 py-1 text-center" style={{ background: v === undefined ? undefined : `rgba(${v >= 0 ? "77,163,255" : "246,70,93"},${Math.abs(v) * 0.45})` }}>{v === undefined ? "—" : v.toFixed(2)}</td>; })}
                  </tr>))}</tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
      <Panel title="Closed trades">
        <Table head={["Exit", "Instrument", "Strategy", "Account", "Side", "Qty", "Entry px", "Exit px", `P&L (${ccy})`, "Exit reason"]}
          rows={(trades.data ?? []).map((t: Json) => [when(t.exit_ts), t.instrument_id, t.strategy ?? "manual", t.broker_id, t.direction > 0 ? "long" : "short", num(t.qty, 5), num(t.entry_price, 4), num(t.exit_price, 4),
            <span key="p" className={tone(t.pnl)}>{signed(t.pnl)}</span>, t.exit_reason])} />
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{() => <Portfolio />}</Shell>;
}
