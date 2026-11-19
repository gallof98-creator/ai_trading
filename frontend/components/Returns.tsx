"use client";
import { type Json, Panel, pct, Stat, Table, tone, useApi } from "@/components/ui";

const signedPct = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${pct(v, d)}`);

/** Today, this week and this month at a glance (Overview). */
export function ReturnTiles() {
  const { data: r } = useApi<Json>("/api/portfolio/returns", 60_000);
  return (
    <>
      <Stat label="Return today" value={signedPct(r?.today)} cls={tone(r?.today)} hint="UTC day, capital moves excluded" />
      <Stat label="Return this week" value={signedPct(r?.week_to_date)} cls={tone(r?.week_to_date)} />
      <Stat label="Return this month" value={signedPct(r?.month_to_date)} cls={tone(r?.month_to_date)} />
    </>
  );
}

/** Portfolio return in % day by day, per week and per month (Portfolio page). */
export function ReturnsPanel() {
  const { data: r, error } = useApi<Json>("/api/portfolio/returns", 60_000);
  if (error) return <Panel title="Returns"><p className="text-sm text-down">{error}</p></Panel>;
  if (!r) return <Panel title="Returns"><p className="text-sm text-dim">Loading…</p></Panel>;
  const row = (x: Json) => [x.period, <span key="r" className={tone(x.return)}>{signedPct(x.return)}</span>, x.days ?? ""];
  return (
    <Panel title="Returns">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Today" value={signedPct(r.today)} cls={tone(r.today)} />
        <Stat label="This week" value={signedPct(r.week_to_date)} cls={tone(r.week_to_date)} />
        <Stat label="This month" value={signedPct(r.month_to_date)} cls={tone(r.month_to_date)} />
        <Stat label={`Since ${r.first_day ?? "—"}`} value={signedPct(r.since_start)} cls={tone(r.since_start)} />
      </div>
      {r.daily.length === 0 ? <p className="mt-3 text-sm text-dim">No full day recorded yet: the first figures appear after the next valuation.</p> :
        <div className="mt-3 grid gap-4 md:grid-cols-3">
          <div><h3 className="mb-1 text-xs text-dim">Day by day</h3>
            <Table max="max-h-72" head={["Day", "Return", ""]} rows={[...r.daily].reverse().map((x: Json) => [x.period, <span key="r" className={tone(x.return)}>{signedPct(x.return)}</span>,
              x.flows ? <span key="f" className="text-dim" title="capital added or removed that day (not counted as return)">{x.flows > 0 ? "+" : ""}{Math.round(x.flows)} moved</span> : ""])} /></div>
          <div><h3 className="mb-1 text-xs text-dim">Week by week (ISO weeks)</h3>
            <Table max="max-h-72" head={["Week", "Return", "Days"]} rows={[...r.weekly].reverse().map(row)} /></div>
          <div><h3 className="mb-1 text-xs text-dim">Month by month</h3>
            <Table max="max-h-72" head={["Month", "Return", "Days"]} rows={[...r.monthly].reverse().map(row)} /></div>
        </div>}
      <p className="mt-2 text-xs text-dim">{r.meaning} Weeks and months compound the daily returns of the days in them.</p>
    </Panel>
  );
}
