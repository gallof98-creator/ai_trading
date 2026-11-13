"use client";
import dynamic from "next/dynamic";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, Meter, Msg, Notice, num, Panel, pct, post, Table, TIMEFRAMES, useAction, useApi, when } from "@/components/ui";
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

type Provider = { name: string; label: string; markets: string[]; listing_markets: string[]; pricing: string; signup_url: string; seconds_per_request: number;
  needs_secret: boolean; symbols_per_request: number; usage: { requests_today: number; requests_this_month: number; symbols_this_month: number };
  configured: boolean; key_hint: string | null; source: string | null; error?: string };

function DataProvider({ user, onChange }: { user: User; onChange: () => void }) {
  const providers = useApi<Provider[]>("/api/market/providers");
  const markets = useApi("/api/markets");
  const stats = useApi("/api/instruments/stats", 15_000);
  const job = useApi("/api/market/download", 2_000);
  const [name, setName] = useState("alpaca");
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [market, setMarket] = useState("US");
  const [etfs, setEtfs] = useState(false);
  const [years, setYears] = useState(10);
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [importing, setImporting] = useState(false);
  const act = useAction();
  const p = (providers.data ?? []).find((x) => x.name === name);
  const running = job.data && ["pending", "running"].includes(job.data.status);
  const isAdmin = user.role === "admin";
  const refresh = () => { providers.reload(); stats.reload(); job.reload(); onChange(); };
  const pick = (n: string) => { setName(n); setKey(""); setSecret(""); setTest(null); act.setMsg(null); const np = (providers.data ?? []).find((x) => x.name === n); if (np && !np.markets.includes(market)) setMarket(np.markets[0]); };
  const m = stats.data?.by_market?.[market];
  const missing = m ? m.instruments - m.with_data : 0;
  const eta = p ? Math.round((Math.ceil(missing / p.symbols_per_request) * Math.max(p.seconds_per_request, 1)) / 60) : 0;
  const start = (onlyMissing: boolean, mode = "full") => act.run(() => post("/api/market/download", { provider: name, market, years, only_missing: onlyMissing, mode }), mode === "update" ? "Update started." : "Download started.").then(() => job.reload());

  return (
    <Panel title="Data providers (official APIs, your own key)" right={
      <div className="flex flex-wrap gap-1">{(providers.data ?? []).map((x) => (
        <button key={x.name} onClick={() => pick(x.name)} className={`rounded border px-2 py-1 font-mono text-xs ${x.name === name ? "border-accent text-accent" : "border-line text-dim hover:text-ink"}`}>
          {x.label}{x.configured ? " ✓" : ""}
        </button>))}
      </div>}>
      {!p ? <p className="text-sm text-dim">Loading…</p> : (
        <div className="space-y-4">
          <div className="space-y-1 text-xs text-dim">
            <p><span className="text-ink">{p.label}</span> · prices for: {p.markets.join(", ")} · symbol lists for: {p.listing_markets.join(", ") || "none"} · {p.configured ? <Badge kind="up">key {p.key_hint} ({p.source})</Badge> : <Badge>no key</Badge>}</p>
            <p>{p.pricing} Get a key at <span className="font-mono text-ink">{p.signup_url.replace("https://", "")}</span>. Keys are stored encrypted and never shown again.</p>
            {p.error && <p className="text-down">{p.error}</p>}
          </div>
          {isAdmin && (
            <div className="flex flex-wrap items-end gap-3">
              <Field label={p.needs_secret ? `${p.label} API key ID` : `${p.label} API key`}><input type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={p.configured ? "replace stored key" : ""} /></Field>
              {p.needs_secret && <Field label={`${p.label} secret key`}><input type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} /></Field>}
              <Button kind="ghost" disabled={act.busy || key.trim().length < 4 || (p.needs_secret && secret.trim().length < 4)} onClick={() => act.run(() => post(`/api/market/providers/${name}/key`, { api_key: key, api_secret: secret }, "PUT"), "Key stored.").then((ok) => { if (ok) { setKey(""); setSecret(""); setTest(null); refresh(); } })}>Save key</Button>
              {p.configured && p.source === "stored" && <Button kind="ghost" disabled={act.busy} onClick={() => window.confirm(`Remove the stored ${p.label} key?`) && act.run(() => post(`/api/market/providers/${name}/key`, undefined, "DELETE"), "Key removed.").then(refresh)}>Remove key</Button>}
            </div>
          )}
          {p.configured && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" disabled={act.busy} onClick={() => act.run(async () => setTest(await post(`/api/market/providers/${name}/test`)), "Test done.")}>Test connection</Button>
                {test && <span className={`text-sm ${test.ok ? "text-up" : "text-down"}`}>{test.message}</span>}
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="space-y-3 rounded border border-line p-3">
                  <h3 className="text-sm font-semibold">1 · Import the list of listed companies</h3>
                  {p.listing_markets.length === 0 ? <p className="text-xs text-dim">{p.label} has no symbol list here. Import the US list with Alpha Vantage (one request), or add tickers by hand in the panel below.</p> : (
                    <div className="flex flex-wrap items-end gap-3">
                      <Field label="Market"><select aria-label="Listing market" value={p.listing_markets.includes(market) ? market : p.listing_markets[0]} onChange={(e) => setMarket(e.target.value)}>
                        {(markets.data ?? []).filter((x: { code: string }) => p.listing_markets.includes(x.code)).map((x: { code: string; label: string }) => <option key={x.code} value={x.code}>{x.label}</option>)}</select></Field>
                      <label className="flex items-center gap-2 pb-2 text-xs text-dim"><input type="checkbox" className="!w-auto" checked={etfs} onChange={(e) => setEtfs(e.target.checked)} /> include ETFs</label>
                      <Button kind="ghost" disabled={act.busy || importing} onClick={async () => {
                        setImporting(true);
                        try {
                          const r = await post(`/api/market/providers/${name}/symbols`, { market: p.listing_markets.includes(market) ? market : p.listing_markets[0], types: etfs ? ["stock", "etf"] : ["stock"] });
                          act.setMsg({ ok: true, text: `${r.created} instruments added, ${r.existing} already present (${r.listed} listed by the provider).` });
                          refresh();
                        } catch (e) {
                          act.setMsg({ ok: false, text: e instanceof Error ? e.message : "Request failed" });
                        } finally {
                          setImporting(false);
                        }
                      }}>{importing ? "Importing…" : "Import symbol list"}</Button>
                    </div>
                  )}
                  <p className="text-xs text-dim">Universe for {market}: {m ? `${m.instruments} instruments, ${m.with_data} with stored prices` : "none yet"}.</p>
                </div>
                <div className="space-y-3 rounded border border-line p-3">
                  <h3 className="text-sm font-semibold">2 · Download price history</h3>
                  <div className="flex flex-wrap items-end gap-3">
                    <Field label="Market"><select aria-label="Download market" value={p.markets.includes(market) ? market : p.markets[0]} onChange={(e) => setMarket(e.target.value)}>
                      {(markets.data ?? []).filter((x: { code: string }) => p.markets.includes(x.code)).map((x: { code: string; label: string }) => <option key={x.code} value={x.code}>{x.label}</option>)}</select></Field>
                    <Field label="Years of history"><input type="number" min="1" max="40" value={years} onChange={(e) => setYears(Number(e.target.value))} /></Field>
                    <Button disabled={act.busy || running || !p.markets.includes(market)} onClick={() => start(true)}>Download missing</Button>
                    <Button kind="ghost" disabled={act.busy || running || !p.markets.includes(market)} title="Fetch only the latest days for instruments that already have prices" onClick={() => start(true, "update")}>Update prices</Button>
                    <Button kind="ghost" disabled={act.busy || running || !p.markets.includes(market)} onClick={() => window.confirm(`Download again and replace the stored history of every ${market} instrument?`) && start(false)}>Refresh all</Button>
                    {running && <Button kind="ghost" onClick={() => post("/api/market/download/cancel").then(job.reload)}>Cancel</Button>}
                  </div>
                  <p className="text-xs text-dim">{p.symbols_per_request > 1 ? `Up to ${p.symbols_per_request} instruments per request` : "One request per instrument"}, one request every {p.seconds_per_request >= 1 ? `${p.seconds_per_request} seconds` : "fraction of a second"} to stay within the plan, in the background. Used so far: {p.usage.requests_today} requests today, {p.usage.symbols_this_month} symbols this month.
                    {missing > 0 && p.markets.includes(market) ? ` ${missing} instruments without prices: about ${eta >= 120 ? `${Math.round(eta / 60)} hours` : `${Math.max(eta, 1)} minutes`} if the plan's allowance lasts.` : ""} When the allowance runs out the download stops and "Download missing" resumes it later.</p>
                </div>
              </div>
            </>
          )}
          {job.data && job.data.status !== "idle" && (
            <div className="space-y-2 rounded border border-line p-3">
              <Meter label={`${job.data.mode === "update" ? "Update" : "Download"} via ${job.data.provider}: ${job.data.status}${job.data.current ? ` · ${job.data.current}` : ""}`} value={job.data.done} limit={job.data.total} format={(v) => String(v)} />
              <p className="font-mono text-xs"><span className="text-up">{job.data.stored} stored</span> · {num(job.data.bars, 0)} bars{job.data.readjusted ? ` · ${job.data.readjusted} re-downloaded after a split or dividend` : ""} · <span className={job.data.failed_count ? "text-down" : "text-dim"}>{job.data.failed_count} failed</span>{job.data.message ? ` · ${job.data.message}` : ""}</p>
              {job.data.failed.length > 0 && <Table max="max-h-40" head={["Instrument", "Reason"]} rows={job.data.failed.map((f: { instrument_id: string; reason: string }) => [f.instrument_id, <span key="r" className="whitespace-normal text-dim">{f.reason}</span>])} />}
            </div>
          )}
          <Msg msg={act.msg} />
        </div>
      )}
    </Panel>
  );
}

function Markets({ user }: { user: User }) {
  const [search, setSearch] = useState("");
  const instruments = useApi(`/api/instruments?limit=200&q=${encodeURIComponent(search)}`, 15_000);
  const stats = useApi("/api/instruments/stats", 15_000);
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
  const refreshAll = () => { instruments.reload(); stats.reload(); chart.reload(); };
  const rs = chart.data?.regime_summary;

  return (
    <div className="space-y-4">
      <Panel title={`Watchlist · ${stats.data?.total ?? "…"} instruments, ${stats.data?.with_data ?? "…"} with stored prices`}
        right={<input aria-label="Search instruments" placeholder="Search ticker…" value={search} onChange={(e) => setSearch(e.target.value)} className="!w-44" />}>
        <Table max="max-h-72" head={["Instrument", "Class", "Currency", "Stored data", "Last price", "Feed", ""]}
          rows={(instruments.data ?? []).map((i: { id: string; asset_class: string; quote_currency: string; last_price: number | null; feed_status: string | null; removable: boolean; series: { timeframe: string; bars: number; kind: string }[] }) => [
            <button key="i" className={`underline-offset-2 hover:underline ${i.id === iid ? "text-accent" : ""}`} onClick={() => { setIid(i.id); if (i.series.length && !i.series.some((c) => c.timeframe === tf)) setTf(i.series[0].timeframe); }}>{i.id}</button>,
            i.asset_class, i.quote_currency,
            i.series.length ? i.series.map((c) => <span key={c.timeframe} className="mr-2"><Badge kind={c.kind === "simulated" ? "accent" : "up"}>{c.timeframe} · {c.bars} {c.kind}</Badge></span>) : <span className="text-dim">none</span>,
            num(i.last_price, 4), i.feed_status ?? "—",
            canWrite && i.removable ? <button key="x" className="text-down hover:underline" onClick={() => window.confirm(`Remove ${i.id} and its stored prices?`) && act.run(() => post(`/api/instruments/${encodeURIComponent(i.id)}`, undefined, "DELETE"), "Removed.").then(refreshAll)}>Remove</button> : ""])} />
        {(instruments.data?.length ?? 0) >= 200 && <p className="mt-2 text-xs text-dim">Showing the first 200 matches (instruments with stored prices first). Use the search box to find others.</p>}
      </Panel>
      {canWrite && <DataProvider user={user} onChange={refreshAll} />}
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
