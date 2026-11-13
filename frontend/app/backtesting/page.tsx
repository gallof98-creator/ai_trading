"use client";
import { useState } from "react";
import { DrawdownChart, EquityChart, PlainBars, SignedBars } from "@/components/charts/Charts";
import { ParamForm } from "@/components/ParamForm";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Msg, Notice, num, Panel, parseFx, pct, post, signed, Stat, Table, TIMEFRAMES, tone, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

function Results({ r }: { r: Json }) {
  const m = r.metrics;
  return (
    <div className="space-y-4">
      <Notice kind="accent">{r.disclaimer}</Notice>
      {r.warnings.map((w: string) => <Notice key={w}>{w}</Notice>)}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <Stat label="Net profit" value={signed(m.net_profit)} cls={tone(m.net_profit)} />
        <Stat label="Gross profit" value={num(m.gross_profit)} />
        <Stat label="Total return" value={pct(m.total_return)} cls={tone(m.total_return)} />
        <Stat label="Annualised return" value={pct(m.annualized_return)} />
        <Stat label="Max drawdown" value={pct(m.max_drawdown)} />
        <Stat label="Trades" value={m.number_of_trades} />
        <Stat label="Win rate" value={pct(m.win_rate, 1)} />
        <Stat label="Profit factor" value={num(m.profit_factor)} />
        <Stat label="Sharpe" value={num(m.sharpe_ratio)} />
        <Stat label="Sortino" value={num(m.sortino_ratio)} />
        <Stat label="Calmar" value={num(m.calmar_ratio)} />
        <Stat label="Recovery factor" value={num(m.recovery_factor)} />
        <Stat label="Expectancy / trade" value={signed(m.expectancy)} cls={tone(m.expectancy)} />
        <Stat label="Average win" value={num(m.average_win)} />
        <Stat label="Average loss" value={num(m.average_loss)} />
        <Stat label="Time in market" value={pct(m.exposure_time, 1)} />
        <Stat label="Orders / rejected" value={`${r.orders_total} / ${r.orders_rejected}`} />
        <Stat label="Costs" value={num((r.costs.commission ?? 0) + (r.costs.spread_slippage ?? 0) + (r.costs.financing ?? 0))} hint="commission + spread/slippage + financing" />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Equity curve"><EquityChart data={r.equity_curve} /></Panel>
        <Panel title="Drawdown"><DrawdownChart data={r.drawdown_curve} height={240} /></Panel>
        <Panel title="Monthly returns"><SignedBars data={r.monthly_returns.map((x: Json) => ({ month: x.month, value: x.return * 100 }))} xKey="month" yKey="value" unit="%" label="Return" /></Panel>
        <Panel title="Trade P&L distribution"><PlainBars data={r.trade_distribution.map((b: Json) => ({ bucket: num((b.from + b.to) / 2, 0), count: b.count }))} xKey="bucket" yKey="count" label="Trades" /></Panel>
      </div>
      <Panel title="Trades (most recent 500)">
        <Table head={["Entry", "Exit", "Instrument", "Strategy", "Side", "Qty", "Entry px", "Exit px", "P&L", "Exit reason"]}
          rows={[...r.trades].reverse().map((t: Json) => [when(t.entry_ts), when(t.exit_ts), t.instrument_id, t.strategy, t.direction > 0 ? "long" : "short", num(t.qty, 5),
            num(t.entry_price, 4), num(t.exit_price, 4), <span key="p" className={tone(t.pnl)}>{signed(t.pnl)}</span>, t.exit_reason])} />
      </Panel>
      <p className="font-mono text-[11px] text-dim">Configuration hash {r.config_hash}. Running the same configuration on the same data reproduces these numbers exactly.</p>
    </div>
  );
}

function Backtesting({ user }: { user: User }) {
  const library = useApi("/api/strategies");
  const instruments = useApi("/api/instruments");
  const coverage = useApi("/api/market/coverage");
  const runs = useApi("/api/backtests");
  const [form, setForm] = useState({ strategy: "ema_crossover", instrument_id: "CRYPTO:BTC/USDT", timeframe: "1h", start: "", end: "", capital: "", fx: "USDT=0.92, USD=0.92",
    spread_bps: 2, slippage_bps: 1, participation_rate: 0.1 });
  const [params, setParams] = useState<Record<string, Json> | null>(null);
  const [result, setResult] = useState<Json>(null);
  const [grid, setGrid] = useState('{"fast": [5, 10, 20], "slow": [30, 50, 80]}');
  const [wf, setWf] = useState<Json>(null);
  const [cmpSel, setCmpSel] = useState<string[]>(["ema_crossover", "donchian_breakout", "rsi_reversion"]);
  const [cmp, setCmp] = useState<Json>(null);
  const [scan, setScan] = useState<Json>(null);
  const act = useAction();
  const canWrite = user.role !== "viewer";
  const meta = (library.data ?? []).find((s: Json) => s.name === form.strategy);
  const p = params ?? meta?.defaults ?? {};
  const common = () => ({ initial_capital: form.capital ? Number(form.capital) : null, static_fx: parseFx(form.fx), start: form.start || null, end: form.end || null });
  const set = (k: string, v: string | number) => setForm({ ...form, [k]: v });

  const run = () => act.run(async () => {
    const r = await post("/api/backtests", { label: `${form.strategy} ${form.instrument_id} ${form.timeframe}`, ...common(),
      deployments: [{ strategy: form.strategy, instrument_id: form.instrument_id, timeframe: form.timeframe, params: p }],
      execution: { spread_bps: Number(form.spread_bps), slippage_bps: Number(form.slippage_bps), participation_rate: Number(form.participation_rate) || null } });
    setResult(r); runs.reload();
  }, "Backtest finished.");

  return (
    <div className="space-y-4">
      <Panel title="New backtest">
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Strategy"><select value={form.strategy} onChange={(e) => { set("strategy", e.target.value); setParams(null); }}>{(library.data ?? []).map((s: Json) => <option key={s.name}>{s.name}</option>)}</select></Field>
            <Field label="Asset"><select value={form.instrument_id} onChange={(e) => set("instrument_id", e.target.value)}>{(instruments.data ?? []).map((i: Json) => <option key={i.id}>{i.id}</option>)}</select></Field>
            <Field label="Timeframe"><select value={form.timeframe} onChange={(e) => set("timeframe", e.target.value)}>{TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select></Field>
            <Field label="Capital (blank = paper capital)"><input type="number" min="0" value={form.capital} onChange={(e) => set("capital", e.target.value)} /></Field>
            <Field label="From (UTC, optional)"><input type="date" value={form.start} onChange={(e) => set("start", e.target.value)} /></Field>
            <Field label="To (UTC, optional)"><input type="date" value={form.end} onChange={(e) => set("end", e.target.value)} /></Field>
            <Field label="Spread (bps)"><input type="number" min="0" step="any" value={form.spread_bps} onChange={(e) => set("spread_bps", e.target.value)} /></Field>
            <Field label="Slippage (bps)"><input type="number" min="0" step="any" value={form.slippage_bps} onChange={(e) => set("slippage_bps", e.target.value)} /></Field>
            <Field label="Max share of bar volume"><input type="number" min="0" max="1" step="any" value={form.participation_rate} onChange={(e) => set("participation_rate", e.target.value)} /></Field>
            <Field label="Assumed FX to base (e.g. USD=0.92)" className="lg:col-span-2"><input value={form.fx} onChange={(e) => set("fx", e.target.value)} /></Field>
          </div>
          {meta && <ParamForm schema={meta.params_schema} value={p} onChange={setParams} />}
          <p className="text-xs text-dim">
            Stored data: {(coverage.data ?? []).length ? (coverage.data ?? []).map((c: Json) => `${c.instrument_id} ${c.timeframe} (${c.bars} ${c.kind} bars)`).join(" · ") : "none — import or generate data under Markets"}.
          </p>
          <div className="flex items-center gap-3">{canWrite && <Button disabled={act.busy} onClick={run}>{act.busy ? "Running…" : "Run backtest"}</Button>}<Msg msg={act.msg} /></div>
        </div>
      </Panel>
      {result && (
        <>
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-semibold">{result.label ?? "Result"}</h2>
            {canWrite && result.id && <Button kind="ghost" onClick={() => act.run(() => post("/api/ai/reports", { backtest_id: result.id }), "Report created — see Quant AI.")}>Create AI research report</Button>}
          </div>
          <Results r={result} />
        </>
      )}
      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Walk-forward analysis (out-of-sample testing)">
          <div className="space-y-3">
            <p className="text-xs text-dim">Parameters are chosen on each training window and judged only on the following unseen window, for the strategy, asset and timeframe selected above.</p>
            <Field label="Parameter grid (JSON)"><input value={grid} onChange={(e) => setGrid(e.target.value)} /></Field>
            {canWrite && <Button kind="ghost" disabled={act.busy} onClick={() => act.run(async () => {
              setWf(await post("/api/backtests/walk-forward", { strategy: form.strategy, instrument_id: form.instrument_id, timeframe: form.timeframe, param_grid: JSON.parse(grid), n_folds: 4, ...common() }));
            }, "Walk-forward finished.")}>Run walk-forward</Button>}
            {wf && (
              <>
                <div className="grid grid-cols-3 gap-3">
                  <Stat label="In-sample score" value={num(wf.summary.mean_in_sample_score)} />
                  <Stat label="Out-of-sample score" value={num(wf.summary.mean_out_of_sample_score)} />
                  <Stat label="Efficiency" value={num(wf.summary.walk_forward_efficiency)} hint="OOS ÷ IS; low = overfitted" />
                </div>
                <Table max="max-h-60" head={["Fold", "Test window", "Chosen parameters", "IS score", "OOS return", "OOS trades"]}
                  rows={wf.folds.map((f: Json) => [f.fold, `${f.test_start.slice(0, 10)} → ${f.test_end.slice(0, 10)}`, JSON.stringify(f.best_params ?? "none qualified"), num(f.in_sample_score),
                    f.out_of_sample ? <span className={tone(f.out_of_sample.total_return)}>{pct(f.out_of_sample.total_return)}</span> : "—", f.out_of_sample?.number_of_trades ?? "—"])} />
                <EquityChart data={wf.out_of_sample_equity} height={180} />
              </>
            )}
          </div>
        </Panel>
        <Panel title="Strategy comparison">
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {(library.data ?? []).map((s: Json) => (
                <label key={s.name} className="flex items-center gap-1 font-mono text-xs text-dim">
                  <input type="checkbox" className="!w-auto" checked={cmpSel.includes(s.name)} onChange={(e) => setCmpSel(e.target.checked ? [...cmpSel, s.name] : cmpSel.filter((x) => x !== s.name))} />{s.name}
                </label>
              ))}
            </div>
            {canWrite && <Button kind="ghost" disabled={act.busy || cmpSel.length < 2 || cmpSel.length > 12} onClick={() => act.run(async () => {
              setCmp(await post("/api/backtests/compare", { ...common(), items: cmpSel.map((s) => ({ strategy: s, instrument_id: form.instrument_id, timeframe: form.timeframe, label: s })) }));
            }, "Comparison finished.")}>Compare with default parameters</Button>}
            {cmp && (
              <>
                <Table max="max-h-72" head={["Strategy", "Return", "Max DD", "Sharpe", "Profit factor", "Win rate", "Trades"]}
                  rows={cmp.rows.map((r: Json) => [r.label, <span key="r" className={tone(r.total_return)}>{pct(r.total_return)}</span>, pct(r.max_drawdown), num(r.sharpe_ratio), num(r.profit_factor), pct(r.win_rate, 1), r.number_of_trades])} />
                {cmp.notes.map((n: string) => <p key={n} className="text-xs text-warn">{n}</p>)}
              </>
            )}
          </div>
        </Panel>
      </div>
      <Panel title="Universe scan: one strategy across every instrument with stored data">
        <div className="space-y-3">
          <p className="text-xs text-dim">Runs the strategy and parameters selected at the top independently on each instrument that has stored bars on the selected timeframe ({form.timeframe}). Use it to see whether a result holds across many stocks instead of one.</p>
          {canWrite && <Button kind="ghost" disabled={act.busy} onClick={() => act.run(async () => {
            setScan(await post("/api/backtests/scan", { strategy: form.strategy, params: p, timeframe: form.timeframe, ...common(),
              execution: { spread_bps: Number(form.spread_bps), slippage_bps: Number(form.slippage_bps), participation_rate: Number(form.participation_rate) || null } }));
          }, "Scan finished.")}>{act.busy ? "Running…" : `Scan all instruments (${form.timeframe})`}</Button>}
          {scan && (
            <>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                <Stat label="Instruments tested" value={scan.summary.instruments_tested} hint={scan.summary.instruments_skipped ? `${scan.summary.instruments_skipped} skipped` : undefined} />
                <Stat label="Profitable" value={pct(scan.summary.share_profitable, 0)} />
                <Stat label="Median return" value={pct(scan.summary.median_return)} cls={tone(scan.summary.median_return)} />
                <Stat label="Best / worst" value={`${pct(scan.summary.best_return, 0)} / ${pct(scan.summary.worst_return, 0)}`} />
                <Stat label="Median Sharpe" value={num(scan.summary.median_sharpe)} />
              </div>
              {scan.notes.map((n: string) => <p key={n} className="text-xs text-warn">{n}</p>)}
              <Table head={["Instrument", "Data", "Period", "Return", "Max DD", "Sharpe", "Profit factor", "Win rate", "Trades"]}
                rows={scan.rows.map((r: Json) => [r.instrument_id, <Badge key="k" kind={r.data_kind === "simulated" ? "accent" : "up"}>{r.data_kind}</Badge>, `${r.first.slice(0, 10)} → ${r.last.slice(0, 10)}`,
                  <span key="r" className={tone(r.total_return)}>{pct(r.total_return)}</span>, pct(r.max_drawdown), num(r.sharpe_ratio), num(r.profit_factor), pct(r.win_rate, 1), r.number_of_trades])} />
              {scan.skipped.length > 0 && <p className="text-xs text-dim">Skipped: {scan.skipped.map((k: Json) => `${k.instrument_id} (${k.reason})`).join("; ")}</p>}
            </>
          )}
        </div>
      </Panel>
      <Panel title="Previous runs">
        <Table head={["When", "Label", "Data", "Return", "Max DD", "Sharpe", "Trades", ""]}
          rows={(runs.data ?? []).map((r: Json) => [when(r.created_at), r.label, r.data_kinds.map((k: string) => <Badge key={k} kind={k === "simulated" ? "accent" : "up"}>{k}</Badge>),
            <span key="t" className={tone(r.metrics.total_return)}>{pct(r.metrics.total_return)}</span>, pct(r.metrics.max_drawdown), num(r.metrics.sharpe_ratio), r.metrics.number_of_trades,
            <button key="o" className="text-accent hover:underline" onClick={() => act.run(async () => setResult(await post(`/api/backtests/${r.id}`, undefined, "GET")), "Loaded.")}>Open</button>])} />
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Backtesting user={user} />}</Shell>;
}
