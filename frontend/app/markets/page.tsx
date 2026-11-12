"use client";
import dynamic from "next/dynamic";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, Msg, Notice, num, Panel, pct, post, Table, TIMEFRAMES, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const CandleChart = dynamic(() => import("@/components/charts/CandleChart"), { ssr: false });

function Markets({ user }: { user: User }) {
  const instruments = useApi("/api/instruments", 15_000);
  const strategies = useApi("/api/strategies");
  const [iid, setIid] = useState("CRYPTO:BTC/USDT");
  const [tf, setTf] = useState("1h");
  const [strategy, setStrategy] = useState("");
  const q = `instrument_id=${encodeURIComponent(iid)}&timeframe=${tf}`;
  const chart = useApi(`/api/market/analysis?${q}${strategy ? `&strategy=${strategy}` : ""}`, 30_000);
  const signals = useApi("/api/signals?limit=30", 15_000);
  const act = useAction();
  const [csv, setCsv] = useState("");
  const [tz, setTz] = useState("UTC");
  const canWrite = user.role !== "viewer";
  const rs = chart.data?.regime_summary;

  return (
    <div className="space-y-4">
      <Panel title="Watchlist">
        <Table head={["Instrument", "Class", "Currency", "Last price", "Data", "Feed"]}
          rows={(instruments.data ?? []).map((i: { id: string; asset_class: string; quote_currency: string; last_price: number | null; data_kind: string | null; feed_status: string | null }) => [
            <button key="i" className={`underline-offset-2 hover:underline ${i.id === iid ? "text-accent" : ""}`} onClick={() => setIid(i.id)}>{i.id}</button>,
            i.asset_class, i.quote_currency, num(i.last_price, 4),
            i.data_kind ? <Badge kind={i.data_kind === "simulated" ? "accent" : "up"}>{i.data_kind}</Badge> : <span className="text-dim">no data</span>, i.feed_status ?? "—"])} />
      </Panel>
      <Panel title={`${iid} · ${tf}`} right={
        <div className="flex flex-wrap items-center gap-2">
          {chart.data && <Badge kind={chart.data.kind === "simulated" ? "accent" : "up"}>{chart.data.kind} data</Badge>}
          <select aria-label="Timeframe" value={tf} onChange={(e) => setTf(e.target.value)} className="!w-auto">{TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select>
          <select aria-label="Signals overlay" value={strategy} onChange={(e) => setStrategy(e.target.value)} className="!w-auto">
            <option value="">No signal overlay</option>
            {(strategies.data ?? []).map((s: { name: string }) => <option key={s.name}>{s.name}</option>)}
          </select>
        </div>}>
        {chart.error ? (
          <div className="space-y-4">
            <Notice>{chart.error}</Notice>
            {canWrite && (
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="space-y-2">
                  <h3 className="text-sm font-semibold">Import market data (CSV)</h3>
                  <p className="text-xs text-dim">Columns: timestamp, open, high, low, close, volume. State the timezone of the timestamps.</p>
                  <textarea aria-label="CSV" rows={5} value={csv} onChange={(e) => setCsv(e.target.value)} className="w-full rounded border border-line bg-bg p-2 font-mono text-xs" placeholder="timestamp,open,high,low,close,volume" />
                  <div className="flex items-end gap-2">
                    <Field label="Source timezone"><input value={tz} onChange={(e) => setTz(e.target.value)} /></Field>
                    <Button disabled={act.busy || !csv} onClick={() => act.run(() => post("/api/market/import-csv", { instrument_id: iid, timeframe: tf, csv, timezone: tz }), "Imported.").then((ok) => { if (ok) chart.reload(); })}>Import</Button>
                  </div>
                </div>
                <div className="space-y-2">
                  <h3 className="text-sm font-semibold">Or generate simulated data</h3>
                  <p className="text-xs text-dim">A seeded random walk for trying the platform. It is labelled SIMULATED everywhere and tells you nothing about real markets.</p>
                  <Button kind="ghost" disabled={act.busy} onClick={() => act.run(() => post("/api/market/generate-simulated", { instrument_id: iid, timeframe: tf, bars: 3000, seed: 1 }), "Generated 3,000 simulated bars.").then((ok) => { if (ok) chart.reload(); })}>Generate 3,000 simulated bars</Button>
                </div>
              </div>
            )}
            <Msg msg={act.msg} />
          </div>
        ) : chart.data ? (
          <CandleChart candles={chart.data.candles} signals={chart.data.signals}
            lines={[{ name: "EMA 20", color: "#f0b90b", values: chart.data.indicators.ema_20 }, { name: "EMA 50", color: "#4da3ff", values: chart.data.indicators.ema_50 }]} />
        ) : <p className="text-sm text-dim">Loading…</p>}
      </Panel>
      {rs && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Panel title="Market regime">
            <dl className="grid grid-cols-2 gap-y-2 font-mono text-sm">
              <dt className="text-dim">Trend</dt><dd>{rs.current.trend.replace(/_/g, " ")}</dd>
              <dt className="text-dim">Volatility</dt><dd>{rs.current.vol_level.replace(/_/g, " ")}</dd>
              <dt className="text-dim">Dynamics</dt><dd>{rs.current.vol_dynamics.replace(/_/g, " ")}</dd>
              <dt className="text-dim">ADX</dt><dd>{num(rs.indicators.adx, 1)}</dd>
              <dt className="text-dim">Vol. percentile</dt><dd>{pct(rs.indicators.vol_rank, 0)}</dd>
              <dt className="text-dim">Bars in regime</dt><dd>{rs.bars_in_current_regime}</dd>
            </dl>
          </Panel>
          <Panel title="Technical indicators (latest bar)">
            <dl className="grid grid-cols-2 gap-y-2 font-mono text-sm">
              <dt className="text-dim">Close</dt><dd>{num(chart.data.candles.at(-1)?.close, 4)}</dd>
              <dt className="text-dim">EMA 20</dt><dd>{num(chart.data.indicators.ema_20.at(-1), 4)}</dd>
              <dt className="text-dim">EMA 50</dt><dd>{num(chart.data.indicators.ema_50.at(-1), 4)}</dd>
              <dt className="text-dim">RSI 14</dt><dd>{num(chart.data.indicators.rsi_14.at(-1), 1)}</dd>
              <dt className="text-dim">ATR 14</dt><dd>{num(chart.data.indicators.atr_14.at(-1), 4)}</dd>
            </dl>
          </Panel>
          <Panel title="Unusual conditions (last 50 bars)">
            <Table empty="Nothing unusual detected." max="max-h-48" head={["Time", "Measure", "Robust z"]}
              rows={chart.data.anomalies.slice(0, 20).map((a: { ts: string; measure: string; robust_z: number }) => [when(a.ts), a.measure, num(a.robust_z, 1)])} />
          </Panel>
        </div>
      )}
      <Panel title="Recent trading signals">
        <Table empty="No signals yet. Signals appear once an active strategy receives new bars." head={["Time", "Instrument", "Strategy", "TF", "Action", "Price", "Stop distance", "Confidence"]}
          rows={(signals.data ?? []).map((s: { ts: string; instrument_id: string; strategy: string; timeframe: string; action: string; price: number; stop_distance: number; confidence: number | null }) => [
            when(s.ts), s.instrument_id, s.strategy, s.timeframe, <Badge key="a" kind={s.action === "BUY" ? "up" : "down"}>{s.action}</Badge>, num(s.price, 4), num(s.stop_distance, 4),
            s.confidence === null ? <span key="c" className="text-dim" title="Rule-based strategies have no calibrated probability">n/a</span> : pct(s.confidence, 0)])} />
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Markets user={user} />}</Shell>;
}
