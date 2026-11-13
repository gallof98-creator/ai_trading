"use client";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Shell } from "@/components/Shell";
import { useActiveSymbol } from "@/components/symbol";
import { Badge, Button, Field, type Json, Msg, Notice, num, Panel, pct, post, Table, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const ProChart = dynamic(() => import("@/components/charts/ProChart"), { ssr: false });
const RES: [string, string][] = [["1", "1m"], ["5", "5m"], ["15", "15m"], ["30", "30m"], ["60", "1h"], ["240", "4h"], ["1D", "D"], ["1W", "W"], ["1M", "M"]];
const TOGGLES: [string, string][] = [["volume", "Volume"], ["sma", "SMA 20/50/200"], ["ema", "EMA 20"], ["bollinger", "Bollinger"], ["levels", "Support / resistance"], ["signals", "Signals"], ["rsi", "RSI"], ["macd", "MACD"]];
const TYPES = ["common_stock", "etf", "reit", "adr", "preferred_stock", "crypto", "forex", "future"];
const nice = (t: string | null | undefined) => (t ?? "—").replace(/_/g, " ");
const KIND: Record<string, "up" | "warn" | "accent" | "dim"> = { realtime: "up", delayed: "warn", historical: "warn", simulated: "accent", none: "dim" };

function Chart({ user }: { user: User }) {
  const [symbol, setSymbol] = useActiveSymbol("CRYPTO:BTC/USDT");
  const [resolution, setResolution] = useState("1D");
  const [show, setShow] = useState<Record<string, boolean>>({ volume: true, sma: true, ema: false, bollinger: false, levels: true, signals: true, rsi: true, macd: false });
  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const [type, setType] = useState("");
  const [sector, setSector] = useState("");
  const [priced, setPriced] = useState(true);
  const [list, setList] = useState("");
  const [ticket, setTicket] = useState({ broker: "", qty: "", stop: "10" });
  const act = useAction();
  const enc = encodeURIComponent(symbol);
  const info = useApi(`/api/charts/symbols?symbol=${enc}`);
  const usable: string[] = info.data?.supported_resolutions ?? [];
  const res = usable.includes(resolution) ? resolution : usable.includes("1D") ? "1D" : usable[0];
  const ready = info.data?.has_data && res;
  const bars = useApi(ready ? `/api/charts/history?symbol=${enc}&resolution=${res}&countback=1500` : null, 20_000);
  const studies = useApi(ready ? `/api/charts/studies?symbol=${enc}&resolution=${res}&bars=1500` : null, 60_000);
  const position = useApi(info.data?.instrument_id ? `/api/charts/position?symbol=${enc}` : null, 15_000);
  const search = useApi(`/api/charts/search?q=${encodeURIComponent(q)}${exchange ? `&exchange=${exchange}` : ""}${type ? `&type=${type}` : ""}${sector ? `&sector=${encodeURIComponent(sector)}` : ""}${priced ? "&has_data=true" : ""}&limit=40`);
  const coverage = useApi("/api/global/coverage");
  const lists = useApi("/api/scanner/watchlists");
  const brokers = useApi("/api/brokers/status");
  const paper = (brokers.data ?? []).filter((b: Json) => b.is_simulated);
  const broker = ticket.broker || paper[0]?.id || "";
  useEffect(() => { act.setMsg(null); }, [symbol]); // eslint-disable-line react-hooks/exhaustive-deps
  const chartBars = useMemo(() => (bars.data?.s === "ok" ? bars.data : null), [bars.data]);
  const d = info.data;
  const last = chartBars ? chartBars.c[chartBars.c.length - 1] : null;
  const prev = chartBars && chartBars.c.length > 1 ? chartBars.c[chartBars.c.length - 2] : null;
  const watch = (lists.data ?? []).find((w: Json) => w.name === list);
  const canTrade = user.role !== "viewer";
  const mine = (position.data?.positions ?? []).find((p: Json) => p.from_chart && p.broker_id === broker);
  const order = async (action: "buy" | "close") => {
    let note = "";
    const ok = await act.run(async () => {
      const r = await post("/api/charts/orders", { symbol, broker_id: broker, action, qty: action === "buy" ? Number(ticket.qty) : null, stop_pct: action === "buy" ? Number(ticket.stop) / 100 : null });
      position.reload();
      if (r.status === "rejected") throw new Error(`Refused by the risk engine: ${r.reason}`);
      note = `Order ${r.status}: ${r.side.toLowerCase()} ${r.qty}. ${r.note}`;
    });
    if (ok) act.setMsg({ ok: true, text: note });
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]">
      <div className="space-y-4">
        <Panel title="Find an instrument">
          <div className="space-y-2">
            <input className="w-full" placeholder="Ticker, name, ISIN or FIGI" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
            <div className="grid grid-cols-2 gap-2">
              <select aria-label="Exchange" value={exchange} onChange={(e) => setExchange(e.target.value)}><option value="">All exchanges</option>
                {(coverage.data?.exchanges ?? []).filter((e: Json) => e.discovered > 0).map((e: Json) => <option key={e.code} value={e.code}>{e.code} · {e.name}</option>)}</select>
              <select aria-label="Type" value={type} onChange={(e) => setType(e.target.value)}><option value="">All types</option>{TYPES.map((t) => <option key={t} value={t}>{nice(t)}</option>)}</select>
            </div>
            <input className="w-full" placeholder="Sector (where known)" value={sector} onChange={(e) => setSector(e.target.value)} aria-label="Sector" />
            <label className="flex items-center gap-1.5 text-xs text-dim"><input type="checkbox" checked={priced} onChange={(e) => setPriced(e.target.checked)} />Only instruments with stored prices</label>
            <div className="max-h-72 overflow-auto">
              {(search.data ?? []).map((r: Json) => (
                <button key={r.symbol} onClick={() => setSymbol(r.symbol)} className={`flex w-full items-baseline gap-2 border-b border-line/50 px-1 py-1.5 text-left text-xs hover:bg-line/30 ${r.symbol === symbol ? "text-accent" : ""}`}>
                  <span className="font-mono font-semibold">{r.ticker}</span><span className="text-dim">{r.exchange}</span>
                  <span className="min-w-0 flex-1 truncate text-dim">{r.description || nice(r.type)}</span>{!r.has_data && <Badge>no prices</Badge>}
                </button>))}
              {search.data?.length === 0 && <p className="py-2 text-xs text-dim">Nothing matches.{priced ? " Untick the box to include instruments without prices." : ""}</p>}
            </div>
          </div>
        </Panel>
        <Panel title="Watchlists" right={<select aria-label="Watchlist" value={list} onChange={(e) => setList(e.target.value)}><option value="">Choose…</option>{(lists.data ?? []).map((w: Json) => <option key={w.id} value={w.name}>{w.name}</option>)}</select>}>
          {watch ? <div className="max-h-56 overflow-auto">{watch.instrument_ids.map((i: string) => (
            <button key={i} onClick={() => setSymbol(i)} className={`block w-full border-b border-line/50 px-1 py-1.5 text-left font-mono text-xs hover:bg-line/30 ${i === symbol ? "text-accent" : ""}`}>{i.split(":")[1]}</button>))}</div>
            : <p className="text-xs text-dim">{(lists.data ?? []).length ? "Pick a watchlist to step through its instruments." : "No watchlists yet: create one from the scanner in Global Markets."}</p>}
        </Panel>
      </div>

      <div className="min-w-0 space-y-4">
        {info.error && <Notice kind="down">{info.error}</Notice>}
        {d && (
          <Panel title={`${d.ticker} · ${d.exchange}${d.mic ? ` (${d.mic})` : ""}`} right={
            <span className="flex flex-wrap items-center gap-2">
              <Badge kind={KIND[d.data.kind] ?? "dim"}>{d.data.kind === "none" ? "NO DATA" : d.data.kind.toUpperCase()}</Badge>
              {d.data.last_bar && <span className="text-xs text-dim">last bar {when(d.data.last_bar)}{d.data.age_days > 4 ? ` · ${num(d.data.age_days, 0)} days old` : ""}</span>}
            </span>}>
            <div className="mb-3 flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <span className="text-sm">{d.description || nice(d.type)}</span>
              {last !== null && <span className="font-mono text-lg">{num(last)} <span className="text-xs text-dim">{d.currency}</span></span>}
              {last !== null && prev ? <span className={`font-mono text-sm ${last >= prev ? "text-up" : "text-down"}`}>{last >= prev ? "+" : ""}{((last / prev - 1) * 100).toFixed(2)}%</span> : null}
              <span className="font-mono text-xs text-dim">{nice(d.type)} · ISIN {d.isin ?? "—"} · FIGI {d.figi ?? "—"}{d.sector ? ` · ${d.sector}` : ""}{d.other_matches ? ` · ${d.other_matches} other listing(s) share this identifier` : ""}</span>
            </div>
            <div className="mb-3 flex flex-wrap items-center gap-1">
              {RES.map(([r, name]) => <button key={r} disabled={!usable.includes(r)} title={usable.includes(r) ? "" : "No stored bars at this resolution"} onClick={() => setResolution(r)}
                className={`rounded border px-2 py-1 font-mono text-xs disabled:cursor-not-allowed disabled:opacity-30 ${res === r ? "border-accent text-accent" : "border-line text-dim hover:text-ink"}`}>{name}</button>)}
              <span className="mx-2 h-4 w-px bg-line" />
              {TOGGLES.map(([k, name]) => <button key={k} onClick={() => setShow({ ...show, [k]: !show[k] })} className={`rounded border px-2 py-1 text-xs ${show[k] ? "border-accent/60 text-ink" : "border-line text-dim"}`}>{name}</button>)}
            </div>
            {!d.has_data ? <Notice>{d.data.note} {d.instrument_id ? <>Download prices from <Link className="underline" href="/markets">Markets</Link>.</> : <>Open <Link className="underline" href="/global">Global Markets</Link> to add it.</>}</Notice>
              : bars.error ? <Notice kind="down">{bars.error}</Notice>
              : chartBars ? <ProChart bars={chartBars} studies={studies.data} show={show} intraday={!["1D", "1W", "1M"].includes(res)} />
              : <p className="text-sm text-dim">{bars.data?.s === "no_data" ? "No bars in this range." : "Loading…"}</p>}
            <p className="mt-2 text-xs text-dim">{d.data.note} Source: {d.data.sources.join(", ") || "—"}. {usable.length < RES.length && d.has_data ? "Greyed-out resolutions need intraday bars, which arrive only from a connected broker feed." : ""} Chart by TradingView Lightweight Charts.</p>
          </Panel>)}

        {d?.has_data && (
          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Technical picture">
              {studies.data ? <div className="space-y-2 text-sm">
                <div className="flex flex-wrap gap-1.5"><Badge kind={studies.data.trend === "up" ? "up" : studies.data.trend === "down" ? "down" : "dim"}>trend: {studies.data.trend ?? "not enough history"}</Badge>
                  <Badge>regime: {nice(studies.data.regime)}</Badge><Badge>RSI {num(studies.data.panes.rsi_14.at(-1), 0)}</Badge></div>
                <Table max="max-h-48" empty="No confirmed swing levels in the last 250 bars." head={["Level", "Price", "Distance", "Touches"]}
                  rows={[...studies.data.levels].sort((a: Json, b: Json) => b.price - a.price).map((l: Json) => [<span key="k" className={l.kind === "support" ? "text-up" : "text-down"}>{l.kind}</span>, num(l.price), pct(l.distance, 1), l.touches])} />
                <p className="text-xs text-dim">Levels are clusters of swing highs and lows, each confirmed by five later bars. Trend compares the price with its 50- and 200-bar averages. Descriptive, not a forecast.</p>
              </div> : <p className="text-sm text-dim">{studies.error ?? "Loading…"}</p>}
            </Panel>
            <Panel title="Signals on this instrument">
              <Table max="max-h-56" empty="No signal recorded. Signals appear when a strategy is deployed on this instrument (Strategies) or analysed in Quant AI." head={["When", "Action", "Strategy", "Confidence"]}
                rows={[...(studies.data?.signals ?? [])].reverse().map((s: Json) => [when(new Date(s.time * 1000).toISOString()), <Badge key="a" kind={s.action === "BUY" ? "up" : "down"}>{s.action}</Badge>, s.strategy, s.confidence == null ? "—" : pct(s.confidence, 0)])} />
              <div className="mt-3 flex flex-wrap gap-3 text-xs"><Link className="text-accent" href={`/quant-ai?symbol=${enc}`}>Analyse in Quant AI →</Link><Link className="text-accent" href={`/markets?symbol=${enc}`}>Data and quality →</Link><Link className="text-accent" href="/global">Scanner →</Link></div>
            </Panel>
            <Panel title="Position and paper order">
              <Table max="max-h-28" empty="No position in this instrument." head={["Account", "Strategy", "Qty", "Entry", "Stop"]} rows={(position.data?.positions ?? []).map((p: Json) => [p.broker_id, p.strategy ?? "—", num(p.qty, 0), num(p.avg_price), p.stop_price ? num(p.stop_price) : "—"])} />
              {(position.data?.open_orders ?? []).length > 0 && <p className="mt-2 text-xs text-dim">Waiting: {position.data.open_orders.map((o: Json) => `${o.side} ${num(o.qty, 0)} ${o.type}${o.stop_price ? ` @ ${num(o.stop_price)}` : ""}`).join("; ")}</p>}
              {canTrade && d.instrument_id && (
                <div className="mt-3 space-y-2 border-t border-line pt-3">
                  <div className="flex flex-wrap items-end gap-2">
                    <Field label="Paper account"><select value={broker} onChange={(e) => setTicket({ ...ticket, broker: e.target.value })}>{paper.length === 0 && <option value="">none — create one in Execution</option>}{paper.map((b: Json) => <option key={b.id} value={b.id}>{b.id}</option>)}</select></Field>
                    <Field label="Quantity"><input type="number" min="0" className="w-24" value={ticket.qty} onChange={(e) => setTicket({ ...ticket, qty: e.target.value })} /></Field>
                    <Field label="Stop % below"><input type="number" min="0.5" max="90" className="w-20" value={ticket.stop} onChange={(e) => setTicket({ ...ticket, stop: e.target.value })} /></Field>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button disabled={act.busy || !broker || !(Number(ticket.qty) > 0)} onClick={() => order("buy")}>Buy (paper)</Button>
                    <Button kind="ghost" disabled={act.busy || !mine} onClick={() => order("close")}>Close chart position</Button>
                    {last !== null && Number(ticket.qty) > 0 && <span className="text-xs text-dim">≈ {num(Number(ticket.qty) * last, 0)} {d.currency}, stop near {num(last * (1 - Number(ticket.stop) / 100))}</span>}
                  </div>
                  <Msg msg={act.msg} />
                  <p className="text-xs text-dim">Paper accounts only; live trading is disabled. Every order is checked by the risk engine and fills at the open of the next bar, never at the price on screen.</p>
                </div>)}
            </Panel>
          </div>)}
      </div>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Chart user={user} />}</Shell>;
}
