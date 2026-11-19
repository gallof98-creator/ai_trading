"use client";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, type Json, Msg, Notice, num, Panel, pct, post, Stat, Table, tone, useAction, useApi } from "@/components/ui";
import type { User } from "@/services/api";

const GROUPINGS: [string, string][] = [["asset_class,strategy,exit_reason", "asset class × strategy × exit"], ["asset_class,strategy", "asset class × strategy"],
  ["strategy,exit_reason", "strategy × exit"], ["exit_reason", "exit"], ["asset_class", "asset class"]];
const r2 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}R`);
const label = (s: string) => s.replace(/_/g, " ");

function Analytics({ user }: { user: User }) {
  const [by, setBy] = useState(GROUPINGS[0][0]);
  const [days, setDays] = useState("");
  const d = useApi(`/api/analytics/trades?by=${by}${days ? `&days=${days}` : ""}`, 60_000);
  const act = useAction();
  if (d.error) return <Notice kind="down">{d.error}</Notice>;
  if (!d.data) return <p className="text-sm text-dim">Loading…</p>;
  const r = d.data, t = r.total, sh = r.shadow, ccy = r.base_currency;
  const winRate = (x: Json) => (x.insufficient_sample ? <span className="text-dim" title={`fewer than ${r.min_sample} trades`}>insufficient sample</span> : pct(x.win_rate, 0));
  const keys: string[] = r.by;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex items-center gap-2">Group by <select value={by} onChange={(e) => setBy(e.target.value)}>{GROUPINGS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
        <label className="flex items-center gap-2">Last <select value={days} onChange={(e) => setDays(e.target.value)}><option value="">all</option><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option><option value="365">1 year</option></select></label>
        {user.role !== "viewer" && <Button kind="ghost" disabled={act.busy} onClick={() => act.run(() => post("/api/analytics/refresh"), "Updated.").then(d.reload)}>Update shadows & backfill</Button>}
        <Msg msg={act.msg} />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Trades" value={t.trades} hint={t.without_fx ? `${t.without_fx} without an exchange rate (excluded from ${ccy})` : undefined} />
        <Stat label={`Net P&L (${ccy})`} value={num(t.pnl_base)} cls={tone(t.pnl_base)} />
        <Stat label="Expectancy" value={`${num(t.expectancy_base)} · ${r2(t.expectancy_r)}`} cls={tone(t.expectancy_base)} hint={`${ccy} and R per trade`} />
        <Stat label="Profit factor" value={t.profit_factor === null ? (t.profit_factor_note ?? "—") : num(t.profit_factor)} />
        <Stat label="Win rate" value={winRate(t)} />
        <Stat label="Max drawdown" value={`${num(t.max_drawdown_base)} · ${r2(t.max_drawdown_r)}`} hint="closed trades, in order" />
        <Stat label="P&L without top 3" value={num(t.pnl_base_without_top3)} cls={tone(t.pnl_base_without_top3)} />
        <Stat label="Closed by hand" value={pct(t.manual_close_share, 0)} cls={t.manual_close_share > 0.2 ? "text-warn" : ""} />
      </div>
      <Panel title="Your manual exits against the system's exits (shadow)">
        {sh.manual_trades === 0 ? <p className="text-sm text-dim">No trade closed by hand.</p> : <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Closed by hand" value={sh.manual_trades} hint={Object.entries(sh.status).map(([k, v]) => `${v} ${label(k)}`).join(" · ")} />
            <Stat label="Compared" value={sh.compared} hint={sh.insufficient_sample ? "insufficient sample" : undefined} />
            <Stat label="Your exits" value={`${r2(sh.actual_total_r)} total · ${r2(sh.actual_expectancy_r)}/trade`} cls={tone(sh.actual_total_r)} />
            <Stat label="System's exits" value={`${r2(sh.shadow_total_r)} total · ${r2(sh.shadow_expectancy_r)}/trade`} cls={tone(sh.shadow_total_r)}
              hint={Object.entries(sh.shadow_exit_reasons).map(([k, v]) => `${v} ${label(k)}`).join(" · ")} />
          </div>
          <p className="mt-2 text-xs text-dim">{sh.meaning}</p></>}
      </Panel>
      <Panel title="Breakdown">
        <Table max="max-h-[32rem]" empty="No closed trade yet."
          head={[...keys.map(label), "Trades", `Net (${ccy})`, `Expectancy (${ccy})`, "Expectancy (R)", "Profit factor", `Avg win / loss (${ccy})`, "Avg win / loss (R)",
            `Max DD (${ccy})`, "Without top 3", "Win rate", "By hand", "Cost / trade"]}
          rows={r.rows.map((x: Json) => [...keys.map((k) => <span key={k}>{k === "exit_reason" ? <Badge kind={x[k].startsWith("closed") ? "warn" : "dim"}>{label(x[k])}</Badge> : label(String(x[k]))}</span>),
            x.trades, <span key="n" className={tone(x.pnl_base)}>{num(x.pnl_base)}</span>, num(x.expectancy_base), r2(x.expectancy_r),
            x.profit_factor === null ? (x.profit_factor_note ?? "—") : num(x.profit_factor), `${num(x.avg_win_base)} / ${num(x.avg_loss_base)}`, `${r2(x.avg_win_r)} / ${r2(x.avg_loss_r)}`,
            num(x.max_drawdown_base), x.pnl_base_without_top3 === null ? "—" : num(x.pnl_base_without_top3), winRate(x), pct(x.manual_close_share, 0), num(x.avg_cost_quote)])} />
        <ul className="mt-2 list-disc pl-5 text-xs text-dim">{r.notes.map((n: string) => <li key={n}>{n}</li>)}</ul>
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Analytics user={user} />}</Shell>;
}
