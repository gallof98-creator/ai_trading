"use client";
import Link from "next/link";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { useNow } from "@/components/live";
import { chartHref } from "@/components/symbol";
import { Badge, Button, Field, type Json, Msg, Notice, num, Panel, pct, post, Stat, Table, tone, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const nice = (t: string | null | undefined) => (t ?? "—").replace(/_/g, " ");
const sym = (id: string) => id.split(":")[1] ?? id;
const TFS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"];

function Proposal({ p, canTrade, onDone }: { p: Json; canTrade: boolean; onDone: () => void }) {
  const act = useAction();
  const [open, setOpen] = useState(false);
  const r = p.rationale ?? {};
  const left = p.seconds_left === null ? null : Math.max(0, Math.round(p.seconds_left));
  const decide = (approve: boolean) => act.run(async () => {
    const o = await post(`/api/approvals/${p.id}`, { approve });
    if (approve && o.status === "rejected") throw new Error(`Refused by the risk engine: ${o.reason}`);
  }, approve ? "Order sent." : "Proposal rejected.").then(onDone);
  return (
    <div className="rounded border border-line bg-bg/40 p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge kind={p.side === "BUY" ? "up" : "down"}>{p.side}</Badge>
        <Link href={chartHref(p.instrument_id)} className="font-mono text-base font-semibold text-accent hover:underline">{p.symbol}</Link>
        <span className="text-xs text-dim">{p.exchange} · {nice(p.strategy)} · {p.timeframe ?? "—"} · {p.broker_id}</span>
        <Badge kind={p.is_simulated ? "accent" : "warn"}>{p.is_simulated ? "PAPER" : "BROKER ACCOUNT"}</Badge>
        {p.reduce_only && <Badge>closes a position</Badge>}
        <span className={`ml-auto font-mono text-xs ${left !== null && left < 120 ? "text-warn" : "text-dim"}`}>{left === null ? "does not expire" : `expires in ${Math.floor(left / 60)}m ${String(left % 60).padStart(2, "0")}s`}</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-xs sm:grid-cols-4 xl:grid-cols-8">
        {[["Entry", `${num(p.entry_price)} ${p.currency}`], ["Stop loss", p.stop_price ? num(p.stop_price) : "—"], ["Take profit", p.take_profit_price ? num(p.take_profit_price) : "—"],
          ["Quantity", num(p.qty, p.qty < 1 ? 5 : 0)], ["Value", `${num(p.notional_base)} ${p.base_currency}`], ["Risk", p.risk_base ? `${num(p.risk_base)} ${p.base_currency} (${pct(p.risk_fraction)})` : "—"],
          ["Reward / risk", p.reward_risk ? `${num(p.reward_risk, 1)} : 1` : "—"], ["Est. costs", `${num(p.estimated_costs_base)} ${p.base_currency}`]]
          .map(([k, v]) => <div key={k}><div className="text-[11px] text-dim">{k}</div><div>{v}</div></div>)}
      </div>
      {r.opportunity?.why && <p className="mt-2 text-sm">{r.opportunity.why}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <Badge kind="up">risk engine: approved when proposed</Badge>
        {p.live ? <Badge kind={p.live.fresh ? "up" : "warn"}>{p.live.fresh ? `● LIVE ${num(p.live.price)}` : `price ${Math.round(p.live.age_seconds)}s old`}</Badge> : <Badge>no live feed: priced on the last stored bar</Badge>}
        {r.validation && <Badge>validated: {r.validation.profitable_folds}/{r.validation.folds} unseen periods profitable, {r.validation.number_of_trades} trades</Badge>}
        <button className="text-accent underline" onClick={() => setOpen(!open)}>{open ? "hide details" : "why and how it was checked"}</button>
      </div>
      {open && (
        <div className="mt-2 space-y-1 border-t border-line pt-2 text-xs text-dim">
          <p>Strategy: {nice(r.strategy)} v{r.strategy_version} ({nice(r.category)}). {r.description} Parameters: {Object.entries(r.params ?? {}).map(([k, v]) => `${k}=${v}`).join(", ")}.</p>
          <p>Signal at {when(r.signal_time)} on price {num(r.signal_price)}; market regime {nice(r.regime)}{r.opportunity ? `, which suits ${nice(r.opportunity.family)} (${r.opportunity.regime_fits.map(nice).join(", ")})` : ""}.</p>
          {r.validation && <p>Validation {when(r.validation.tested_at)}: out-of-sample return {pct(r.validation.total_return)}, drawdown {pct(r.validation.max_drawdown)}, {pct(r.validation.walk_forward_efficiency, 0)} of the in-sample result survived. {r.validation.caveat}</p>}
          {r.data && <p>Data: {r.data.kind} from {r.data.source}, last bar closed {when(r.data.last_bar_close)}, {r.data.bars} bars.</p>}
          <p>No probability of success is shown because none was measured. On approval the price, its age and the risk limits are checked again; if anything changed the proposal is cancelled and nothing is sent.</p>
        </div>)}
      {canTrade && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button disabled={act.busy} onClick={() => window.confirm(`Approve this single order: ${p.side} ${p.qty} ${p.symbol} on ${p.broker_id}?`) && decide(true)}>APPROVE ORDER</Button>
          <Button kind="ghost" disabled={act.busy} onClick={() => decide(false)}>REJECT ORDER</Button>
          <Msg msg={act.msg} />
        </div>)}
    </div>
  );
}

function Autonomous({ user }: { user: User }) {
  const d = useApi("/api/quant/dashboard", 4_000);
  const modules = useApi("/api/quant/modules");
  const strategies = useApi("/api/strategies");
  const instruments = useApi("/api/instruments?has_data=true&limit=500");
  const act = useAction();
  const [scan, setScan] = useState<Json>(null);
  const [v, setV] = useState({ strategy: "ema_crossover", instrument_id: "", timeframe: "1d" });
  const [liveId, setLiveId] = useState("");
  useNow();
  const canTrade = user.role !== "viewer";
  if (!d.data) return <p className="text-sm text-dim">{d.error ?? "Loading…"}</p>;
  const { configuration: c, overview: ov, proposals, brokers, live, universes: u, validations, deployments, positions } = d.data;
  const halted = ov.risk?.halted;
  const paper = (brokers ?? []).filter((b: Json) => b.capabilities?.trading_enabled);
  const account = paper[0]?.id ?? "";
  const ccy = c.base_currency;
  const refresh = () => d.reload();
  const feedKind = (p: Json) => (p.state === "streaming" ? "up" : p.state === "polling" ? "warn" : p.state === "error" ? "down" : "dim");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-sm font-semibold uppercase tracking-wider">Autonomous Quant Trading</h1>
        <Badge kind="accent">{nice(c.trading_mode).toUpperCase()}</Badge>
        <Badge kind={c.live_trading_authorized ? "warn" : "dim"}>LIVE TRADING {c.live_trading_authorized ? "AUTHORIZED" : "DISABLED"}</Badge>
        {halted && <Badge kind="down">TRADING PAUSED: {ov.risk.kill_switch || Object.keys(ov.risk.breakers ?? {}).join(", ")}</Badge>}
        {canTrade && <span className="ml-auto flex flex-wrap gap-2">
          {halted ? <Button onClick={() => act.run(() => post("/api/quant/resume"), "Trading resumed.").then(refresh)}>RESUME TRADING</Button>
            : <Button kind="ghost" onClick={() => act.run(() => post("/api/quant/pause", { reason: "paused by the user" }), "Trading paused: no new risk will be taken.").then(refresh)}>PAUSE TRADING</Button>}
          <Button kind="danger" onClick={() => window.confirm("EMERGENCY STOP: block every new order now? Open positions keep their protective stops; what else happens follows the emergency policy in Settings.") && act.run(() => post("/api/quant/pause", { reason: "EMERGENCY STOP" }), "Emergency stop engaged.").then(refresh)}>EMERGENCY STOP</Button>
        </span>}
      </div>
      <Msg msg={act.msg} />
      {c.trading_mode !== "semi_auto" && <Notice>{c.trading_mode === "manual" ? "Manual mode: signals are recorded but no order proposal is created by deployed strategies. Switch to semi-automatic in Settings." : "Automatic mode: paper orders are sent without asking. Real-money orders still wait for approval."}</Notice>}
      {!ov.configured && <Notice>No account yet: create a paper account under Execution (it starts with {num(c.virtual_capital)} {ccy}, changeable in Settings).</Notice>}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Virtual capital" value={`${num(c.virtual_capital, 0)} ${ccy}`} hint="set in Settings" />
        <Stat label="Equity" value={ov.equity !== undefined ? `${num(ov.equity)} ${ccy}` : "—"} />
        <Stat label="Cash available" value={ov.available_capital !== undefined ? `${num(ov.available_capital)} ${ccy}` : "—"} hint="equity minus margin in use" />
        <Stat label="P&L today" value={ov.daily_pnl !== undefined ? num(ov.daily_pnl) : "—"} cls={tone(ov.daily_pnl)} />
        <Stat label="P&L total" value={ov.total_pnl !== undefined ? num(ov.total_pnl) : "—"} cls={tone(ov.total_pnl)} />
        <Stat label="Drawdown" value={pct(ov.drawdown)} hint={`limit ${pct(c.max_drawdown, 0)}`} />
        <Stat label="Risk per trade" value={pct(c.risk_per_trade, 1)} hint={c.risk_amount ? `≈ ${num(c.risk_amount)} ${ccy} at the stop, costs included` : ""} />
        <Stat label="Open positions" value={positions.length} hint={`${proposals.length} proposals waiting`} />
      </div>

      <Panel title={`Proposals waiting for your decision (${proposals.length})`} right={canTrade && account ? (
        <Button kind="ghost" disabled={act.busy || halted} onClick={() => act.run(async () => setScan(await post("/api/quant/scan", { broker_id: account })), "Scan complete.").then(refresh)}>Scan for opportunities</Button>) : undefined}>
        {proposals.length === 0 ? <p className="text-sm text-dim">Nothing is waiting. A proposal appears when a validated strategy opens a position on its latest bar, or when a deployed strategy signals. Nothing is ever sent without your approval in this mode.</p>
          : <div className="space-y-3">{proposals.map((p: Json) => <Proposal key={p.id} p={p} canTrade={canTrade} onDone={refresh} />)}</div>}
        {scan && <div className="mt-3 border-t border-line pt-3 text-xs">
          <p className="text-dim">Scan of {when(scan.scanned_at)} on {scan.broker_id}: {scan.validated} validated setups, {scan.proposed.length} proposed, {scan.skipped.length} not proposed.</p>
          <Table max="max-h-40" empty="" head={["Strategy", "Instrument", "Timeframe", "Why not proposed"]} rows={scan.skipped.map((s: Json) => [nice(s.strategy), sym(s.instrument_id), s.timeframe, <span key="r" className="whitespace-normal">{s.reason}</span>])} /></div>}
      </Panel>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Market data">
          <div className="space-y-2">
            {Object.entries(live.providers as Record<string, Json>).map(([name, p]) => (
              <div key={name} className="flex flex-wrap items-center gap-2 text-xs"><Badge kind={feedKind(p)}>{name}: {p.state}</Badge>
                <span className="text-dim">{p.transport}{p.ticks_last_minute ? ` · ${p.ticks_last_minute} updates/min` : ""}{p.median_latency_ms !== null ? ` · median delay ${Math.round(p.median_latency_ms)} ms` : ""}{p.last_error ? ` · ${p.last_error}` : ""}</span></div>))}
            {Object.keys(live.providers).length === 0 && <p className="text-xs text-dim">No live source running. Subscribe an instrument below: crypto in USD needs no key; US stocks need an Alpaca or Finnhub key (Markets page).</p>}
            <Table max="max-h-56" empty="No live subscriptions." head={["Instrument", "Status", "Price", "Bid / ask", "Age", "Source", ""]}
              rows={d.data.live_subscriptions.map((s: Json) => { const q = s.quote; return [<Link key="s" className="text-accent hover:underline" href={chartHref(s.instrument_id)}>{sym(s.instrument_id)}</Link>,
                q ? <Badge key="b" kind={q.fresh ? "up" : "warn"}>{q.fresh ? `● ${q.status}` : "STALE"}</Badge> : <Badge key="b">waiting</Badge>, q ? num(q.price) : "—",
                q?.bid ? `${num(q.bid)} / ${num(q.ask)}` : "—", q ? `${Math.round(q.age_seconds)}s` : "—", `${s.source}${q?.venue ? ` · ${q.venue}` : ""}`,
                canTrade ? <button key="x" className="text-dim underline" onClick={() => post("/api/live/unsubscribe", { instrument_ids: [s.instrument_id] }).then(refresh)}>stop</button> : ""]; })} />
            {canTrade && <div className="flex flex-wrap items-end gap-2">
              <Field label="Go live on an instrument (e.g. CRYPTO:BTC/USD, STOCK:AAPL)"><input className="w-64" value={liveId} onChange={(e) => setLiveId(e.target.value)} /></Field>
              <Button kind="ghost" disabled={!liveId.trim()} onClick={() => act.run(async () => { const r = await post("/api/live/subscribe", { instrument_ids: [liveId.trim()] }); const no = r.unavailable[liveId.trim()]; if (no) throw new Error(no); setLiveId(""); }, "Live feed requested.").then(refresh)}>Subscribe</Button>
            </div>}
            <p className="text-xs text-dim">{live.stale_after_seconds}s without an update makes a price STALE: nothing is sized or approved on it. Instruments without a live source are END OF DAY: their last stored bar is used and labelled as such.</p>
          </div>
        </Panel>
        <Panel title="Universes and broker">
          <div className="grid grid-cols-3 gap-3">
            <Stat label="Catalogued" value={num(u.catalogued.count, 0)} hint="listed, known to the platform" />
            <Stat label="Analysable" value={num(u.analysable.count, 0)} hint="have stored market prices" />
            <Stat label="Live now" value={u.live.count} hint="receiving live prices" />
          </div>
          <Table max="max-h-48" empty="No trading account connected." head={["Account", "Environment", "Status", "Tradable", "Broker equity", "Buying power"]}
            rows={(brokers ?? []).filter((b: Json) => !b.data_only).map((b: Json) => [b.id, <Badge key="e" kind={b.environment === "live" ? "warn" : "accent"}>{String(b.environment).toUpperCase()}</Badge>,
              b.connected ? "connected" : "disconnected", u.tradable[b.id] ? `${u.tradable[b.id].count} (${u.tradable[b.id].with_prices} with prices)` : "—",
              b.broker_account ? `${num(b.broker_account.equity)} ${b.broker_account.currency}` : "—", b.broker_account ? num(b.broker_account.buying_power) : "—"])} />
          <p className="mt-2 text-xs text-dim">Being listed does not make an instrument tradable: only what the connected account can place orders in is proposed. With an Alpaca account the broker&apos;s own equity and buying power are shown here and are never used as your capital: sizing uses the virtual capital.</p>
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Strategy validation">
          {canTrade && <div className="mb-3 flex flex-wrap items-end gap-2">
            <Field label="Strategy"><select value={v.strategy} onChange={(e) => setV({ ...v, strategy: e.target.value })}>{(strategies.data ?? []).map((s: Json) => <option key={s.name} value={s.name}>{nice(s.name)}</option>)}</select></Field>
            <Field label="Instrument"><select value={v.instrument_id} onChange={(e) => setV({ ...v, instrument_id: e.target.value })}><option value="">Choose…</option>{(instruments.data?.items ?? instruments.data ?? []).map((i: Json) => <option key={i.id} value={i.id}>{i.id}</option>)}</select></Field>
            <Field label="Timeframe"><select value={v.timeframe} onChange={(e) => setV({ ...v, timeframe: e.target.value })}>{TFS.map((t) => <option key={t}>{t}</option>)}</select></Field>
            <Button kind="ghost" disabled={act.busy || !v.instrument_id} onClick={() => act.run(async () => { const r = await post("/api/quant/validations", v); if (r.status !== "validated") throw new Error(`Not validated: ${r.checks.filter((c: Json) => !c.passed).map((c: Json) => c.name).join("; ")}`); }, "Validated: it may now be proposed.").then(refresh)}>Validate</Button>
          </div>}
          <Table max="max-h-64" empty="No strategy has been validated yet, so nothing can be proposed by a scan." head={["Strategy", "Instrument", "TF", "Status", "Unseen periods", "OOS return", "OOS drawdown", "Trades", "Tested"]}
            rows={validations.map((x: Json) => [nice(x.strategy), sym(x.instrument_id), x.timeframe, <Badge key="s" kind={x.status === "validated" ? "up" : x.status === "expired" ? "warn" : "down"}>{x.status}</Badge>,
              `${x.profitable_folds}/${x.folds} profitable`, <span key="r" className={tone(x.out_of_sample.total_return)}>{pct(x.out_of_sample.total_return)}</span>, pct(x.out_of_sample.max_drawdown), x.out_of_sample.number_of_trades ?? "—", when(x.tested_at)])} />
          <p className="mt-2 text-xs text-dim">A strategy must be profitable in at least {modules.data?.criteria.min_profitable_folds} of {modules.data?.criteria.min_folds} unseen periods, with at least {modules.data?.criteria.min_out_of_sample_trades} trades, on market (not simulated) prices. One good backtest is not enough, and passing is not a promise of profit. Valid for {modules.data?.criteria.valid_for_days} days.</p>
        </Panel>
        <Panel title="Active strategies and performance">
          <Table max="max-h-40" empty="No strategy is active." head={["Strategy", "Instrument", "Timeframe", "Account"]} rows={deployments.map((x: Json) => [nice(x.strategy), sym(x.instrument_id), x.timeframe, x.broker_id])} />
          <h3 className="mb-1 mt-3 text-xs text-dim">Closed trades by strategy</h3>
          <Table max="max-h-40" empty="No closed trade yet." head={["Strategy", "Trades", "Win rate", "P&L (quote currency)"]} rows={d.data.performance_by_strategy.map((x: Json) => [nice(x.strategy), x.trades, pct(x.win_rate, 0), <span key="p" className={tone(x.pnl_quote)}>{num(x.pnl_quote)}</span>])} />
          <h3 className="mb-1 mt-3 text-xs text-dim">Open positions</h3>
          <Table max="max-h-40" empty="No open position." head={["Instrument", "Strategy", "Quantity", "Entry", "Last", "Stop", "Account"]}
            rows={positions.map((x: Json) => [<Link key="s" className="text-accent hover:underline" href={chartHref(x.instrument_id)}>{sym(x.instrument_id)}</Link>, nice(x.strategy), num(x.qty, Math.abs(x.qty) < 1 ? 5 : 0), num(x.avg_price), x.price ? num(x.price) : "—", x.stop_price ? num(x.stop_price) : "—", x.broker_id])} />
        </Panel>
      </div>

      <Panel title="Strategy modules">
        <div className="grid gap-3 md:grid-cols-3">
          {(modules.data?.modules ?? []).map((m: Json) => (
            <div key={m.name} className="rounded border border-line px-3 py-2 text-xs">
              <div className="flex items-center gap-2"><span className="text-sm text-ink">{m.label}</span><Badge>{m.kind === "signal" ? "signal family" : "horizon"}</Badge></div>
              <p className="mt-1 text-dim">{m.description}</p>
              <p className="mt-1 font-mono text-dim">{m.kind === "signal" ? m.strategies.map((s: Json) => nice(s.name)).join(", ") : `${m.timeframes.join(", ")} · stop ${m.exits.stop_atr} ATR · target ${m.exits.take_profit_atr} ATR${m.needs_live ? " · needs live data" : ""}`}</p>
            </div>))}
        </div>
        <p className="mt-2 text-xs text-dim">{modules.data?.note}</p>
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Autonomous user={user} />}</Shell>;
}
