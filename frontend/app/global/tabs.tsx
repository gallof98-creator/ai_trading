"use client";
import { useState } from "react";
import { EquityChart } from "@/components/charts/Charts";
import { Badge, Button, Field, type Json, Msg, Notice, num, Panel, pct, post, Stat, Table, tone, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const nice = (t: string | null | undefined) => (t ?? "—").replace(/_/g, " ");
const spct = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`);
const Pct = ({ v }: { v: number | null | undefined }) => <span className={tone(v)}>{spct(v)}</span>;

function MetricsBar({ canWrite, onDone }: { canWrite: boolean; onDone?: () => void }) {
  const job = useApi("/api/scanner/metrics", 2_500);
  const act = useAction();
  const s = job.data ?? {};
  const running = ["pending", "running"].includes(s.status);
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-dim">
      <span>Metrics: {s.status === "idle" || !s.status ? "not computed in this session" : `${s.status}${s.total ? ` — ${s.done ?? 0} / ${s.total}` : ""}${s.finished_at ? `, ${when(s.finished_at)}` : ""}`}</span>
      {canWrite && <Button kind="ghost" disabled={act.busy || running} onClick={() => act.run(() => post("/api/scanner/metrics", {}), "Computation started.").then(() => { job.reload(); onDone?.(); })}>Recompute metrics</Button>}
      <span>They are recomputed automatically after every price download.</span>
      <Msg msg={act.msg} />
    </div>
  );
}

// ---------------------------------------------------------------- scanner
type Filter = { field: string; op: string; value: string };

export function Scanner({ user }: { user: User }) {
  const fields = useApi("/api/scanner/fields");
  const saved = useApi("/api/scanner/saved");
  const lists = useApi("/api/scanner/watchlists");
  const [filters, setFilters] = useState<Filter[]>([]);
  const [sort, setSort] = useState({ field: "change_1d", descending: true });
  const [markets, setMarkets] = useState("");
  const [watch, setWatch] = useState("");
  const [result, setResult] = useState<Json>(null);
  const [name, setName] = useState("");
  const [auto, setAuto] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [listName, setListName] = useState("");
  const act = useAction();
  const canWrite = user.role !== "viewer";
  if (!fields.data) return <p className="text-sm text-dim">{fields.error ?? "Loading…"}</p>;
  const all: Json[] = [...fields.data.metrics, ...fields.data.derived, ...fields.data.descriptive];
  const kind = (f: string) => all.find((x) => x.name === f)?.type ?? "number";
  const opsFor = (f: string) => (kind(f) === "bool" ? ["is_true", "is_false"] : kind(f) === "text" ? ["==", "!=", "in"] : [">=", "<=", ">", "<", "==", "!="]);
  const definition = () => ({
    filters: filters.map((f) => ({ field: f.field, op: f.op, value: kind(f.field) === "bool" ? null : f.op === "in" ? f.value.split(",").map((s) => s.trim()).filter(Boolean)
      : kind(f.field) === "text" ? f.value : Number(f.value) / (kind(f.field) === "percent" ? 100 : 1) })),
    sort, limit: 200,
    universe: { ...(markets.trim() ? { markets: markets.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean) } : {}), ...(watch ? { watchlist: [watch] } : {}) },
  });
  const load = (d: Json) => {
    setFilters((d.filters ?? []).map((f: Json) => ({ field: f.field, op: f.op, value: f.value === null || f.value === undefined ? "" : Array.isArray(f.value) ? f.value.join(",") : String(kind(f.field) === "percent" ? f.value * 100 : f.value) })));
    if (d.sort) setSort(d.sort);
    setMarkets((d.universe?.markets ?? []).join(","));
    setWatch(d.universe?.watchlist?.[0] ?? "");
  };
  const run = (d?: Json) => act.run(async () => { setResult(await post("/api/scanner/run", { definition: d ?? definition() })); setPicked([]); }, "Scan complete.");
  const show = (r: Json, f: string) => { const v = r[f]; const k = kind(f); return v === null || v === undefined ? "—" : k === "percent" ? <Pct v={v} /> : k === "bool" ? (v ? "yes" : "no") : k === "text" ? nice(v) : num(v, Math.abs(v) >= 1000 ? 0 : 2); };
  const cols = Array.from(new Set(["change_1d", "rel_volume", "ret_1m", "ret_6m", "rsi_14", "regime", sort.field, ...filters.map((f) => f.field)])).filter((c) => !["market", "country", "currency"].includes(c));

  return (
    <div className="space-y-4">
      <MetricsBar canWrite={canWrite} />
      <Panel title="Scan the universe" right={<span className="text-xs text-dim">Percent fields are entered as percentages (5 = 5%)</span>}>
        <div className="space-y-3">
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(fields.data.presets as Record<string, Json>).map(([n, d]) => <button key={n} onClick={() => { load(d); run(d); }} className="rounded border border-line px-2 py-1 text-xs text-dim hover:border-accent hover:text-accent">{n}</button>)}
          </div>
          {filters.map((f, i) => (
            <div key={i} className="flex flex-wrap items-end gap-2">
              <Field label="Field"><select value={f.field} onChange={(e) => setFilters(filters.map((x, j) => (j === i ? { field: e.target.value, op: opsFor(e.target.value)[0], value: "" } : x)))}>
                {all.map((x) => <option key={x.name} value={x.name} title={x.description}>{x.label}</option>)}</select></Field>
              <Field label="Condition"><select value={f.op} onChange={(e) => setFilters(filters.map((x, j) => (j === i ? { ...x, op: e.target.value } : x)))}>{opsFor(f.field).map((o) => <option key={o} value={o}>{nice(o)}</option>)}</select></Field>
              {kind(f.field) !== "bool" && <Field label={f.op === "in" ? "Values (comma separated)" : "Value"}><input value={f.value} onChange={(e) => setFilters(filters.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} /></Field>}
              <Button kind="ghost" onClick={() => setFilters(filters.filter((_, j) => j !== i))}>Remove</Button>
            </div>))}
          <div className="flex flex-wrap items-end gap-3">
            <Button kind="ghost" onClick={() => setFilters([...filters, { field: "change_1d", op: ">=", value: "" }])}>Add condition</Button>
            <Field label="Sort by"><select value={sort.field} onChange={(e) => setSort({ ...sort, field: e.target.value })}>{all.filter((x) => x.type !== "bool").map((x) => <option key={x.name} value={x.name}>{x.label}</option>)}</select></Field>
            <Field label="Order"><select value={sort.descending ? "d" : "a"} onChange={(e) => setSort({ ...sort, descending: e.target.value === "d" })}><option value="d">Highest first</option><option value="a">Lowest first</option></select></Field>
            <Field label="Markets (e.g. US,IT; empty = all)"><input value={markets} onChange={(e) => setMarkets(e.target.value)} /></Field>
            <Field label="Watchlist"><select value={watch} onChange={(e) => setWatch(e.target.value)}><option value="">Whole universe</option>{(lists.data ?? []).map((w: Json) => <option key={w.id} value={w.name}>{w.name} ({w.instrument_ids.length})</option>)}</select></Field>
            <Button disabled={act.busy} onClick={() => run()}>Run scan</Button>
          </div>
          {canWrite && (
            <div className="flex flex-wrap items-end gap-3 border-t border-line pt-3">
              <Field label="Save this scan as"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
              <label className="flex items-center gap-1.5 text-xs text-dim"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />Run after every price update and notify me of new matches</label>
              <Button kind="ghost" disabled={act.busy || !name.trim()} onClick={() => act.run(() => post("/api/scanner/saved", { name: name.trim(), definition: definition(), run_after_update: auto, notify: auto }), "Scan saved.").then((ok) => { if (ok) { setName(""); saved.reload(); } })}>Save scan</Button>
            </div>)}
          <Msg msg={act.msg} />
        </div>
      </Panel>
      {result && (
        <Panel title={`Results — ${result.matched} of ${result.total_universe} instruments match${result.matched > result.rows.length ? ` (first ${result.rows.length} shown)` : ""}`}
          right={canWrite && picked.length > 0 ? <span className="flex items-end gap-2"><input placeholder="Watchlist name" value={listName} onChange={(e) => setListName(e.target.value)} />
            <Button kind="ghost" disabled={!listName.trim()} onClick={() => act.run(() => post("/api/scanner/watchlists", { name: listName.trim(), instrument_ids: Array.from(new Set([...((lists.data ?? []).find((w: Json) => w.name === listName.trim())?.instrument_ids ?? []), ...picked])) }, "PUT"), "Watchlist saved.").then(() => { lists.reload(); setPicked([]); })}>Add {picked.length} to watchlist</Button></span> : undefined}>
          <Table max="max-h-[32rem]" empty={result.total_universe ? "No instrument matches these conditions." : "No metrics yet: download prices, then recompute metrics."}
            head={["", "Symbol", "Market", "Price", ...cols.map((c) => all.find((x) => x.name === c)?.label ?? c), "As of"]}
            rows={result.rows.map((r: Json) => [
              <input key="c" type="checkbox" aria-label={`Select ${r.symbol}`} checked={picked.includes(r.instrument_id)} onChange={() => setPicked(picked.includes(r.instrument_id) ? picked.filter((x) => x !== r.instrument_id) : [...picked, r.instrument_id])} />,
              <span key="s" title={r.name}>{r.symbol}{r.data_kind === "simulated" && <> <Badge kind="accent">SIM</Badge></>}</span>, r.market, `${num(r.close)} ${r.currency}`, ...cols.map((c) => show(r, c)), String(r.as_of).slice(0, 10)])} />
        </Panel>)}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Saved scans">
          <Table empty="No saved scans." head={["Name", "After updates", "Last run", "Matches", ""]} rows={(saved.data ?? []).map((s: Json) => [s.name, s.run_after_update ? "yes" : "no", when(s.last_run), s.last_result ? s.last_result.matched : "—",
            <span key="a" className="flex gap-2"><button className="text-accent" onClick={() => { load(s.definition); run(s.definition); }}>Run</button>
              {canWrite && <button className="text-down" onClick={() => act.run(() => post(`/api/scanner/saved/${s.id}`, undefined, "DELETE"), "Deleted.").then(saved.reload)}>Delete</button>}</span>])} />
        </Panel>
        <Panel title="Watchlists">
          <Table empty="No watchlists. Tick results of a scan to create one." head={["Name", "Instruments", ""]} rows={(lists.data ?? []).map((w: Json) => [w.name, <span key="i" className="whitespace-normal">{w.instrument_ids.slice(0, 12).map((i: string) => i.split(":")[1]).join(", ")}{w.instrument_ids.length > 12 ? ` … +${w.instrument_ids.length - 12}` : ""}</span>,
            <span key="a" className="flex gap-2"><button className="text-accent" onClick={() => { setWatch(w.name); run({ ...definition(), universe: { watchlist: [w.name] } }); }}>Scan</button>
              {canWrite && <button className="text-down" onClick={() => act.run(() => post(`/api/scanner/watchlists/${w.id}`, undefined, "DELETE"), "Deleted.").then(lists.reload)}>Delete</button>}</span>])} />
        </Panel>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- analysis and rankings
const heat = (v: number | null | undefined, scale: number) => {
  if (v === null || v === undefined) return "rgba(125,136,150,0.12)";
  const a = Math.min(Math.abs(v) / scale, 1) * 0.55 + 0.08;
  return v >= 0 ? `rgba(38,166,91,${a})` : `rgba(229,83,75,${a})`;
};

function Movers({ title, rows, col, fmt }: { title: string; rows: Json[]; col: string; fmt: (v: number) => React.ReactNode }) {
  return (
    <Panel title={title}>
      <Table max="max-h-64" empty="None today." head={["Symbol", "Market", title.includes("olume") || title.includes("olatility") || title.includes("reakout") ? "Reading" : "Change", "Today"]}
        rows={rows.map((r) => [<span key="s" title={r.name}>{r.symbol}</span>, r.market, fmt(r[col]), col === "change_1d" ? "" : <Pct key="p" v={r.change_1d} />])} />
    </Panel>
  );
}

export function Analysis({ user }: { user: User }) {
  const [groupBy, setGroupBy] = useState("market");
  const [horizon, setHorizon] = useState<"avg_change_1d" | "avg_ret_1m" | "avg_ret_3m">("avg_change_1d");
  const [model, setModel] = useState("multi_factor");
  const ov = useApi(`/api/global/overview?group_by=${groupBy}`, 60_000);
  const models = useApi("/api/global/ranking-models");
  const rk = useApi(`/api/global/rankings?model=${model}&limit=50`);
  const [report, setReport] = useState<Json>(null);
  const act = useAction();
  const canWrite = user.role !== "viewer";
  if (!ov.data) return <p className="text-sm text-dim">{ov.error ?? "Loading…"}</p>;
  const d = ov.data;
  if (!d.breadth) return <div className="space-y-3"><Notice>No metrics have been computed yet. Download prices for some instruments (Markets page), then recompute metrics.</Notice><MetricsBar canWrite={canWrite} onDone={ov.reload} /></div>;
  const b = d.breadth;
  const scale = horizon === "avg_change_1d" ? 0.02 : horizon === "avg_ret_1m" ? 0.08 : 0.15;
  const m = (models.data ?? []).find((x: Json) => x.name === model);
  return (
    <div className="space-y-4">
      <Notice kind="accent">Analysis of {num(d.equities, 0)} equities with stored prices, data as of {String(d.as_of).slice(0, 10)}{d.oldest_data !== d.as_of ? ` (oldest series: ${String(d.oldest_data).slice(0, 10)})` : ""}. It describes what happened; it does not forecast.{d.simulated_series ? ` ${d.simulated_series} series are simulated.` : ""}</Notice>
      <MetricsBar canWrite={canWrite} onDone={ov.reload} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <Stat label="Advancers / decliners" value={<><span className="text-up">{b.advancers}</span> / <span className="text-down">{b.decliners}</span></>} hint={`${b.unchanged} unchanged`} />
        <Stat label="Median move" value={spct(b.median_change_1d)} cls={tone(b.median_change_1d)} />
        <Stat label="Above 50-day average" value={pct(b.above_sma_50, 0)} />
        <Stat label="Above 200-day average" value={pct(b.above_sma_200, 0)} />
        <Stat label="At 52-week high" value={b.at_52w_high} />
        <Stat label="20-day breakouts / breakdowns" value={`${b.breakouts_20d} / ${b.breakdowns_20d}`} />
        <Stat label="Regime changes" value={d.regime_changes_total} hint="since the previous computation" />
      </div>
      <Panel title="Heatmap" right={
        <span className="flex gap-2"><select aria-label="Group by" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}><option value="market">By exchange</option><option value="country">By country</option><option value="sector">By sector</option></select>
          <select aria-label="Horizon" value={horizon} onChange={(e) => setHorizon(e.target.value as typeof horizon)}><option value="avg_change_1d">1 day</option><option value="avg_ret_1m">1 month</option><option value="avg_ret_3m">3 months</option></select></span>}>
        {groupBy === "sector" && d.sector_coverage.with_sector === 0
          ? <p className="text-sm text-dim">No sector data: none of the connected free sources supplies sectors, so instruments are not assigned to one. Group by exchange or country instead.</p>
          : <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-6">
            {d.groups.map((g: Json) => (
              <div key={g.group} className="rounded border border-line px-3 py-2" style={{ background: heat(g[horizon], scale) }}>
                <div className="truncate text-xs">{g.group}</div>
                <div className="font-mono text-lg">{spct(g[horizon])}</div>
                <div className="text-[11px] text-dim">{g.instruments} instruments · {pct(g.advancers_share, 0)} up today</div>
              </div>))}
          </div>}
        {groupBy === "sector" && d.sector_coverage.with_sector > 0 && d.sector_coverage.without > 0 && <p className="mt-2 text-xs text-dim">{d.sector_coverage.without} equities have no sector on record.</p>}
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
        <Movers title="Top gainers" rows={d.top_gainers} col="change_1d" fmt={(v) => <Pct v={v} />} />
        <Movers title="Top losers" rows={d.top_losers} col="change_1d" fmt={(v) => <Pct v={v} />} />
        <Movers title="Unusual volume" rows={d.unusual_volume} col="rel_volume" fmt={(v) => `${num(v, 1)}× usual`} />
        <Movers title="Breakout candidates" rows={d.breakout_candidates} col="rel_volume" fmt={(v) => `${num(v, 1)}× volume`} />
        <Movers title="Volatility expansion" rows={d.volatility_expansion} col="atr_ratio" fmt={(v) => `ATR ratio ${num(v)}`} />
        <Panel title="Abnormal moves and regimes">
          <Table max="max-h-28" empty="No abnormal price move today." head={["Symbol", "Change", "Robust deviations"]} rows={d.abnormal_moves.map((r: Json) => [r.symbol, <Pct key="p" v={r.change_1d} />, num(r.robust_z, 1)])} />
          <div className="mt-3 flex flex-wrap gap-1.5">{Object.entries(d.regimes as Record<string, number>).map(([k, v]) => <Badge key={k}>{nice(k)}: {v}</Badge>)}</div>
          {d.regime_changes.length > 0 && <p className="mt-2 text-xs text-dim">Changed regime: {d.regime_changes.slice(0, 8).map((r: Json) => `${r.symbol} (${nice(r.from)} → ${nice(r.to)})`).join(", ")}{d.regime_changes_total > 8 ? " …" : ""}</p>}
        </Panel>
      </div>
      <Panel title="Rankings" right={<select aria-label="Ranking model" value={model} onChange={(e) => setModel(e.target.value)}>{(models.data ?? []).map((x: Json) => <option key={x.name} value={x.name}>{x.label}</option>)}</select>}>
        {m && <p className="mb-2 text-sm">{m.description}</p>}
        {m && <div className="mb-2 flex flex-wrap gap-1.5">{m.factors.map((f: Json) => <Badge key={f.metric}>{f.label}: {pct(f.weight, 0)}, {f.direction}</Badge>)}</div>}
        {rk.data && <>
          <p className="mb-2 text-xs text-dim">{rk.data.method} Ranked {rk.data.ranked} of {rk.data.eligible} eligible instruments (price ≥ {rk.data.eligibility.min_price}, average traded value ≥ {num(rk.data.eligibility.min_dollar_volume, 0)}; universe {rk.data.universe}).</p>
          <p className="mb-2 text-xs">{rk.data.validation.status === "tested"
            ? <>Last tested {when(rk.data.validation.tested_at)} on {rk.data.validation.instruments} instruments, {rk.data.validation.period}: return <Pct v={rk.data.validation.total_return} /> (equal-weight universe <Pct v={rk.data.validation.benchmark_total_return} />), Sharpe {num(rk.data.validation.sharpe_ratio)}, maximum drawdown {pct(rk.data.validation.max_drawdown)}. Simulated, with the caveats shown under Global strategies.</>
            : <span className="text-warn">{rk.data.validation.note}</span>}</p>
          <Table max="max-h-[28rem]" empty="Nothing to rank: too few instruments have enough history and liquidity."
            head={["Rank", "Symbol", "Market", "Score", ...(m?.factors ?? []).map((f: Json) => f.label), "Contributions", "Price"]}
            rows={rk.data.rows.map((r: Json) => [r.rank, <span key="s" title={r.name}>{r.symbol}</span>, r.market, <span key="sc" className={tone(r.score)}>{r.score > 0 ? "+" : ""}{num(r.score)}</span>,
              ...(m?.factors ?? []).map((f: Json) => (f.metric === "adx_14" ? num(r.values[f.metric], 1) : spct(r.values[f.metric], 1))),
              Object.values(r.contributions as Record<string, number>).map((c) => `${c > 0 ? "+" : ""}${c.toFixed(2)}`).join("  "), num(r.close)])} />
          <p className="mt-2 text-xs text-dim">{rk.data.disclaimer}</p></>}
      </Panel>
      <Panel title="Research report" right={canWrite ? <Button kind="ghost" disabled={act.busy} onClick={() => act.run(async () => setReport(await post("/api/global/report", { group_by: groupBy, model })), "Report saved under Quant AI research.")}>Write report</Button> : undefined}>
        <Msg msg={act.msg} />
        {report ? <pre className="max-h-96 overflow-auto whitespace-pre-wrap font-mono text-xs">{report.markdown}</pre> : <p className="text-sm text-dim">A written summary of today&apos;s breadth, groups, unusual activity and the selected ranking. It is stored with the other research reports.</p>}
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- strategies and portfolio
export function Strategies({ user }: { user: User }) {
  const opts = useApi("/api/global/strategies/options");
  const tests = useApi("/api/global/strategies/tests");
  const brokers = useApi("/api/brokers/status");
  const holdings = useApi("/api/global/portfolio/holdings", 15_000);
  const proposals = useApi("/api/global/portfolio/proposals");
  const risk = useApi("/api/risk");
  const [spec, setSpec] = useState<Json>(null);
  const [markets, setMarkets] = useState("");
  const [bt, setBt] = useState<Json>(null);
  const [target, setTarget] = useState<Json>(null);
  const [proposal, setProposal] = useState<Json>(null);
  const [account, setAccount] = useState("");
  const act = useAction();
  const canWrite = user.role !== "viewer";
  if (!opts.data) return <p className="text-sm text-dim">{opts.error ?? "Loading…"}</p>;
  const s = spec ?? opts.data.defaults;
  const set = (k: string, v: unknown) => setSpec({ ...s, [k]: v });
  const body = () => ({ ...s, markets: markets.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean) });
  const paper = (brokers.data ?? []).filter((b: Json) => b.is_simulated);
  const acct = account || paper[0]?.id || "";
  const numField = (label: string, k: string, scale = 1, step = "any") => <Field label={label}><input type="number" step={step} className="w-28" value={s[k] === null || s[k] === undefined ? "" : +(s[k] * scale).toFixed(6)} onChange={(e) => set(k, e.target.value === "" ? null : Number(e.target.value) / scale)} /></Field>;
  const refresh = () => { holdings.reload(); proposals.reload(); };
  const limits = risk.data?.limits;

  return (
    <div className="space-y-4">
      <Notice kind="accent">Portfolio strategies rank the whole universe and hold the best-ranked instruments. They trade on paper accounts only; every order passes the central risk engine. Past and simulated results do not guarantee future returns.</Notice>
      <Panel title="Strategy definition">
        <div className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Ranking model"><select value={s.model} onChange={(e) => set("model", e.target.value)}>{opts.data.models.map((m: Json) => <option key={m.name} value={m.name}>{m.label}</option>)}</select></Field>
            {numField("Positions", "top_n", 1, "1")}
            <Field label="Rebalance"><select value={s.rebalance} onChange={(e) => set("rebalance", e.target.value)}><option value="monthly">Monthly</option><option value="weekly">Weekly</option></select></Field>
            <Field label="Weights"><select value={s.weighting} onChange={(e) => set("weighting", e.target.value)}>{Object.keys(opts.data.weightings).map((w) => <option key={w} value={w}>{nice(w)}</option>)}</select></Field>
            <Field label="Rotation"><select value={s.rotation ?? ""} onChange={(e) => set("rotation", e.target.value || null)}><option value="">None</option><option value="country">Leading countries</option><option value="sector">Leading sectors</option></select></Field>
            {s.rotation && numField("Groups kept", "top_groups", 1, "1")}
            <Field label="Markets (empty = all)"><input className="w-32" value={markets} onChange={(e) => setMarkets(e.target.value)} placeholder="US,IT" /></Field>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            {numField("Max weight %", "max_weight", 100)}{numField("Invested share %", "invest", 100)}{numField("Max per country %", "max_country_weight", 100)}{numField("Max per currency %", "max_currency_weight", 100)}
            {numField("Max per sector %", "max_sector_weight", 100)}{numField("Volatility target %", "target_volatility", 100)}{numField("Protective stop %", "stop_pct", 100)}
          </div>
          <div className="flex flex-wrap items-end gap-3">
            {numField("Min price", "min_price")}{numField("Min traded value / day", "min_dollar_volume")}{numField("Max share of daily volume %", "max_participation", 100)}{numField("Costs (bps)", "cost_bps")}{numField("Test capital", "initial_capital")}
            <Button disabled={act.busy || !canWrite} onClick={() => act.run(async () => { setBt(await post("/api/global/strategies/backtest", body())); tests.reload(); }, "Backtest complete.")}>Run backtest</Button>
            <Button kind="ghost" disabled={act.busy} onClick={() => act.run(async () => setTarget(await post("/api/global/portfolio/target", body())), "Portfolio computed.")}>Show today&apos;s portfolio</Button>
          </div>
          <p className="text-xs text-dim">{opts.data.models.find((m: Json) => m.name === s.model)?.description} {opts.data.weightings[s.weighting]}</p>
          <ul className="list-disc pl-5 text-xs text-dim">{opts.data.rules.map((r: string) => <li key={r}>{r}</li>)}</ul>
          <Msg msg={act.msg} />
        </div>
      </Panel>
      {bt && (
        <Panel title={`Backtest — ${bt.universe.instruments} instruments, ${bt.universe.test_start} to ${bt.universe.end}`}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
            <Stat label="Total return" value={spct(bt.metrics.total_return)} cls={tone(bt.metrics.total_return)} hint={`universe ${spct(bt.benchmark.total_return)}`} />
            <Stat label="Per year" value={spct(bt.metrics.annualized_return)} hint={`universe ${spct(bt.benchmark.annualized_return)}`} />
            <Stat label="Sharpe" value={num(bt.metrics.sharpe_ratio)} hint={`universe ${num(bt.benchmark.sharpe_ratio)}`} />
            <Stat label="Max drawdown" value={pct(bt.metrics.max_drawdown)} hint={`universe ${pct(bt.benchmark.max_drawdown)}`} />
            <Stat label="Round trips" value={bt.activity.round_trips} hint={`win rate ${pct(bt.metrics.win_rate, 0)}`} />
            <Stat label="Stops hit" value={bt.activity.stops_hit} />
            <Stat label="Turnover / year" value={`${num(bt.activity.turnover_per_year, 1)}×`} hint={`costs ${num(bt.activity.costs_paid, 0)}`} />
            <Stat label="Average positions" value={num(bt.activity.average_positions, 1)} />
          </div>
          <div className="mt-4"><EquityChart data={bt.equity_curve} /></div>
          <div className="mt-3 grid gap-4 lg:grid-cols-2">
            <Table head={["Period", "From", "To", "Return", "Sharpe", "Max drawdown"]} rows={[...bt.sub_periods, { label: "Benchmark (whole period)", ...bt.benchmark }].map((p: Json) => [p.label.startsWith("Equal") ? "Benchmark" : p.label, p.start, p.end, <Pct key="r" v={p.total_return} />, num(p.sharpe_ratio), pct(p.max_drawdown)])} />
            <div><h3 className="mb-1 text-xs text-warn">What this test cannot tell you</h3><ul className="list-disc space-y-1 pl-5 text-xs text-dim">{bt.caveats.map((c: string) => <li key={c}>{c}</li>)}</ul>
              <p className="mt-2 text-xs text-dim">Benchmark: {bt.benchmark.label}.</p></div>
          </div>
        </Panel>)}
      {target && (
        <Panel title={`Today's portfolio — as of ${target.as_of}, ${target.eligible} eligible of ${target.universe}`}>
          {!target.enough_history && <Notice>Only {target.history_bars} daily bars are stored; about thirteen months are needed for dependable factor values.</Notice>}
          {target.note && <p className="mb-2 text-xs text-dim">{target.note}</p>}
          <Table empty="No instrument qualifies." head={["Symbol", "Market", "Country", "Currency", "Weight", "Score", "Price"]} rows={target.rows.map((r: Json) => [<span key="s" title={r.name}>{r.symbol}</span>, r.market, r.country ?? "—", r.currency, pct(r.weight, 1), num(r.score), num(r.price)])} />
          <p className="mt-2 text-xs text-dim">Invested {pct(target.invested, 1)}, cash {pct(target.cash, 1)} (before the risk engine&apos;s limits).</p>
          {canWrite && (
            <div className="mt-3 flex flex-wrap items-end gap-3 border-t border-line pt-3">
              <Field label="Paper account"><select value={acct} onChange={(e) => setAccount(e.target.value)}>{paper.length === 0 && <option value="">No paper account — create one under Execution</option>}{paper.map((b: Json) => <option key={b.id} value={b.id}>{b.id}</option>)}</select></Field>
              <Button disabled={act.busy || !acct} onClick={() => act.run(async () => { setProposal(await post("/api/global/portfolio/proposals", { broker_id: acct, definition: body() })); refresh(); }, "Proposal prepared. Nothing has been ordered yet.")}>Prepare rebalance</Button>
              {limits && <span className="text-xs text-dim">Risk limits: {pct(limits.risk_per_trade, 1)} risk per trade, {pct(limits.max_exposure_per_asset, 0)} per instrument, {pct(limits.max_exposure_per_strategy, 0)} per strategy, {limits.max_concurrent_positions} positions. Change them under Risk.</span>}
            </div>)}
        </Panel>)}
      {proposal && (
        <Panel title={`Rebalance proposal — ${proposal.strategy} on ${proposal.broker_id}, equity ${num(proposal.equity)} ${proposal.base_currency}`}
          right={<Badge kind={proposal.status === "executed" ? "up" : "warn"}>{proposal.status}</Badge>}>
          <Table empty="Nothing to change: the account already holds this portfolio." head={["", "Symbol", "Quantity", "Reference price", "Target", "After limits", "Stop", "Risk engine", ...(proposal.results ? ["Order"] : [])]}
            rows={proposal.orders.map((o: Json) => [<Badge key="b" kind={o.side === "BUY" ? "up" : "down"}>{o.side}</Badge>, o.symbol, num(o.qty, 0), num(o.price), pct(o.weight, 1), pct(o.actual_weight, 1), o.stop_price ? num(o.stop_price) : "—",
              <span key="r" className={`whitespace-normal ${o.approved ? "" : "text-down"}`}>{o.approved ? "approved" : "refused"}{o.reasons.length ? ` — ${o.reasons.join("; ")}` : ""}</span>,
              ...(proposal.results ? [(() => { const r = proposal.results.find((x: Json) => x.instrument_id === o.instrument_id); return r ? `${r.status}${r.status === "rejected" && r.reason ? ` — ${r.reason}` : ""}` : "—"; })()] : [])])} />
          {proposal.kept?.length > 0 && <p className="mt-2 text-xs text-dim">Kept unchanged: {proposal.kept.map((i: string) => i.split(":")[1]).join(", ")}.</p>}
          {proposal.status === "proposed"
            ? <div className="mt-3 flex flex-wrap items-center gap-3"><Button disabled={act.busy || !proposal.orders.some((o: Json) => o.approved)} onClick={() => window.confirm("Send the approved orders to the paper account?") && act.run(async () => { setProposal(await post(`/api/global/portfolio/proposals/${proposal.id}/execute`)); refresh(); }, "Orders sent to the paper account.")}>Send approved orders (paper)</Button>
              <span className="text-xs text-dim">Each order is checked by the risk engine again when sent. A proposal expires after 24 hours.</span></div>
            : <p className="mt-3 text-xs text-dim">{proposal.next}</p>}
        </Panel>)}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Positions of portfolio strategies (paper)" right={canWrite ? <Button kind="ghost" disabled={act.busy} onClick={() => act.run(async () => { const r = await post("/api/global/portfolio/settle"); act.setMsg({ ok: true, text: `${r.bars} new daily bars applied.` }); refresh(); })}>Apply stored prices</Button> : undefined}>
          <Table max="max-h-64" empty="No positions." head={["Account", "Symbol", "Strategy", "Quantity", "Entry", "Last", "Result", "Stop", "Last bar"]}
            rows={(holdings.data?.positions ?? []).map((h: Json) => [h.broker_id, h.symbol, h.strategy.replace("global:", ""), num(h.qty, 0), num(h.avg_price), num(h.price), <Pct key="p" v={h.unrealized_pct} />, h.stop_price ? num(h.stop_price) : "—", String(h.last_bar ?? "—").slice(0, 10)])} />
          {(holdings.data?.open_orders ?? []).length > 0 && <><h3 className="mb-1 mt-3 text-xs text-dim">Waiting for the next daily bar</h3>
            <Table max="max-h-40" head={["Account", "Instrument", "Side", "Quantity", "Type", "Why"]} rows={holdings.data.open_orders.map((o: Json) => [o.broker_id, o.instrument_id.split(":")[1], o.side, num(o.qty, 0), o.type, nice(o.reason)])} /></>}
          <p className="mt-2 text-xs text-dim">Orders fill at the open of the next daily bar after they were sent. Bars are applied automatically after each price download.</p>
        </Panel>
        <Panel title="Strategy rankings (your backtests)">
          <Table max="max-h-64" empty="No backtest run yet." head={["Model", "Tested", "Instruments", "Return", "Universe", "Sharpe", "Max drawdown", ""]}
            rows={[...(tests.data ?? [])].sort((a: Json, b: Json) => (b.sharpe_ratio ?? -9) - (a.sharpe_ratio ?? -9)).map((t: Json) => [nice(t.model), when(t.tested_at), t.instruments, <Pct key="r" v={t.total_return} />, <Pct key="b" v={t.benchmark_total_return} />, num(t.sharpe_ratio), pct(t.max_drawdown),
              <button key="l" className="text-accent" onClick={() => { setSpec(t.definition); setMarkets((t.definition.markets ?? []).join(",")); }}>Load</button>])} />
          <p className="mt-2 text-xs text-dim">Sorted by Sharpe ratio. Comparing many variants on the same history favours lucky ones; treat the best result with suspicion.</p>
        </Panel>
      </div>
      <Panel title="Recent proposals">
        <Table max="max-h-48" empty="None." head={["Created", "Strategy", "Account", "Prices as of", "Orders", "Approved", "Status"]} rows={(proposals.data ?? []).map((p: Json) => [when(p.created_at), p.strategy, p.broker_id, p.as_of, p.orders, p.approved, p.status])} />
      </Panel>
    </div>
  );
}
