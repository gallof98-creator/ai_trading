"use client";
import dynamic from "next/dynamic";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, Msg, Notice, num, Panel, pct, post, Table, TIMEFRAMES, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const CandleChart = dynamic(() => import("@/components/charts/CandleChart"), { ssr: false });

function Universe({ onChange }: { onChange: () => void }) {
  const markets = useApi("/api/markets");
  const [market, setMarket] = useState("US");
  const [kind, setKind] = useState("stock");
  const [tickers, setTickers] = useState("");
  const [replace, setReplace] = useState(false);
  const [report, setReport] = useState<{ imported: { file: string; instrument_id: string; bars: number; first: string; last: string; missing_bars: number }[]; failed: { file: string; reason: string }[] } | null>(null);
  const [progress, setProgress] = useState("");
  const act = useAction();

  const add = async () => {
    try {
      const r = await post("/api/instruments", { tickers: tickers.split(/[\s,;]+/).filter(Boolean), market, asset_class: kind });
      const invalid = r.invalid.length ? `, ${r.invalid.length} invalid: ${r.invalid.map((i: { ticker: string }) => i.ticker).join(", ")}` : "";
      act.setMsg({ ok: r.invalid.length === 0, text: `${r.created.length} added, ${r.existing.length} already present${invalid}.` });
      if (r.created.length) setTickers("");
      onChange();
    } catch (e) {
      act.setMsg({ ok: false, text: e instanceof Error ? e.message : "Request failed" });
    }
  };

  const upload = async (list: FileList | null) => {
    if (!list?.length) return;
    const files = Array.from(list);
    const total = { imported: [] as NonNullable<typeof report>["imported"], failed: [] as NonNullable<typeof report>["failed"] };
    setReport(null);
    for (let i = 0; i < files.length; i += 10) {  // small batches keep each request light
      setProgress(`Importing ${Math.min(i + 10, files.length)} of ${files.length} files…`);
      const batch = await Promise.all(files.slice(i, i + 10).map(async (f) => ({ name: f.name, csv: await f.text() })));
      try {
        const r = await post("/api/market/import-csv-bulk", { market, asset_class: kind, timeframe: "1d", replace, files: batch });
        total.imported.push(...r.imported); total.failed.push(...r.failed);
      } catch (e) {
        total.failed.push(...batch.map((b) => ({ file: b.name, reason: e instanceof Error ? e.message : "request failed" })));
      }
    }
    setProgress("");
    setReport(total);
    onChange();
  };

  return (
    <Panel title="Stock universe">
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Market"><select value={market} onChange={(e) => setMarket(e.target.value)}>{(markets.data ?? []).map((m: { code: string; label: string; currency: string }) => <option key={m.code} value={m.code}>{m.label} · {m.currency}</option>)}</select></Field>
            <Field label="Type"><select value={kind} onChange={(e) => setKind(e.target.value)}><option value="stock">Stocks</option><option value="etf">ETFs</option></select></Field>
          </div>
          <Field label="Add tickers (separated by spaces, commas or new lines)">
            <textarea aria-label="Tickers" rows={3} value={tickers} onChange={(e) => setTickers(e.target.value)} placeholder="MSFT NVDA AMZN GOOGL" className="w-full rounded border border-line bg-bg p-2 font-mono text-xs" />
          </Field>
          <div className="flex items-center gap-3"><Button kind="ghost" disabled={!tickers.trim()} onClick={add}>Add to universe</Button><Msg msg={act.msg} /></div>
        </div>
        <div className="space-y-3">
          <h3 className="text-sm font-semibold">Import daily price files in bulk</h3>
          <p className="text-xs text-dim">Select many CSV or TXT files at once, one per instrument, each named after its ticker (AAPL.csv, eni.mi.csv, aapl.us.txt). Unknown tickers are added to the market chosen on the left. Accepted layouts: Date/Open/High/Low/Close/Volume exports and bulk-download text files.</p>
          <label className="flex items-center gap-2 text-xs text-dim"><input type="checkbox" className="!w-auto" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> Replace data that is already stored for a ticker</label>
          <input aria-label="Price files" type="file" multiple accept=".csv,.txt" onChange={(e) => { upload(e.target.files); e.target.value = ""; }} className="!p-1 text-xs" />
          {progress && <p className="text-sm text-accent">{progress}</p>}
          {report && (
            <div className="space-y-2">
              <p className="text-sm"><span className="text-up">{report.imported.length} imported</span>{report.failed.length > 0 && <span className="text-down"> · {report.failed.length} failed</span>}</p>
              {report.failed.length > 0 && <Table max="max-h-40" head={["File", "Reason"]} rows={report.failed.map((f) => [f.file, <span key="r" className="whitespace-normal text-dim">{f.reason}</span>])} />}
              {report.imported.length > 0 && <Table max="max-h-40" head={["Instrument", "Bars", "From", "To", "Missing days"]} rows={report.imported.map((f) => [f.instrument_id, f.bars, f.first.slice(0, 10), f.last.slice(0, 10), f.missing_bars])} />}
              <p className="text-xs text-dim">"Missing days" counts weekdays without a bar; exchange holidays are included because holiday calendars are not bundled.</p>
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

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
  const coverage = useApi("/api/market/coverage", 30_000);
  const refreshAll = () => { instruments.reload(); coverage.reload(); chart.reload(); };
  const rs = chart.data?.regime_summary;

  return (
    <div className="space-y-4">
      <Panel title={`Watchlist (${instruments.data?.length ?? 0} instruments)`}>
        <Table max="max-h-72" head={["Instrument", "Class", "Currency", "Stored data", "Last price", "Feed", ""]}
          rows={(instruments.data ?? []).map((i: { id: string; asset_class: string; quote_currency: string; last_price: number | null; data_kind: string | null; feed_status: string | null; removable: boolean }) => {
            const stored = (coverage.data ?? []).filter((c: { instrument_id: string }) => c.instrument_id === i.id);
            return [
              <button key="i" className={`underline-offset-2 hover:underline ${i.id === iid ? "text-accent" : ""}`} onClick={() => { setIid(i.id); if (stored.length && !stored.some((c: { timeframe: string }) => c.timeframe === tf)) setTf(stored[0].timeframe); }}>{i.id}</button>,
              i.asset_class, i.quote_currency,
              stored.length ? stored.map((c: { timeframe: string; bars: number; kind: string }) => <span key={c.timeframe} className="mr-2"><Badge kind={c.kind === "simulated" ? "accent" : "up"}>{c.timeframe} · {c.bars} {c.kind}</Badge></span>) : <span className="text-dim">none</span>,
              num(i.last_price, 4), i.feed_status ?? "—",
              canWrite && i.removable ? <button key="x" className="text-down hover:underline" onClick={() => window.confirm(`Remove ${i.id} and its stored prices?`) && act.run(() => post(`/api/instruments/${encodeURIComponent(i.id)}`, undefined, "DELETE"), "Removed.").then(refreshAll)}>Remove</button> : ""];
          })} />
      </Panel>
      {canWrite && <Universe onChange={refreshAll} />}
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
