"use client";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const axis = { stroke: "#7d8896", fontSize: 11, tickLine: false, axisLine: { stroke: "#1f2732" } } as const;
const tip = { contentStyle: { background: "#0a0d12", border: "1px solid #1f2732", fontSize: 12, fontFamily: "ui-monospace, monospace" }, labelStyle: { color: "#7d8896" } };
const short = (iso: string) => String(iso).slice(0, 10);
const compact = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(Math.abs(v) < 10 ? 2 : 0));

export function EquityChart({ data, height = 240 }: { data: { ts: string; equity: number }[]; height?: number }) {
  if (data.length < 2) return <p className="text-sm text-dim">Not enough observations yet.</p>;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 5, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="#1a212b" vertical={false} />
        <XAxis dataKey="ts" tickFormatter={short} minTickGap={60} {...axis} />
        <YAxis domain={["auto", "auto"]} tickFormatter={compact} width={52} {...axis} />
        <Tooltip {...tip} labelFormatter={(l) => String(l).replace("T", " ").slice(0, 16)} formatter={(v) => [Number(v).toLocaleString("en-GB", { maximumFractionDigits: 2 }), "Equity"]} />
        <Area type="monotone" dataKey="equity" stroke="#4da3ff" strokeWidth={1.5} fill="#4da3ff" fillOpacity={0.08} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function DrawdownChart({ data, height = 160 }: { data: { ts: string; drawdown: number }[]; height?: number }) {
  if (data.length < 2) return null;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data.map((d) => ({ ts: d.ts, dd: -d.drawdown * 100 }))} margin={{ top: 5, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="#1a212b" vertical={false} />
        <XAxis dataKey="ts" tickFormatter={short} minTickGap={60} {...axis} />
        <YAxis tickFormatter={(v) => `${v.toFixed(0)}%`} width={52} {...axis} />
        <Tooltip {...tip} labelFormatter={(l) => String(l).replace("T", " ").slice(0, 16)} formatter={(v) => [`${Number(v).toFixed(2)}%`, "Drawdown"]} />
        <Area type="monotone" dataKey="dd" stroke="#f6465d" strokeWidth={1.5} fill="#f6465d" fillOpacity={0.12} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** Bars coloured by sign (returns, P&L). */
export function SignedBars({ data, xKey, yKey, height = 180, unit = "", label }: { data: Record<string, unknown>[]; xKey: string; yKey: string; height?: number; unit?: string; label: string }) {
  if (!data.length) return <p className="text-sm text-dim">No data.</p>;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 5, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="#1a212b" vertical={false} />
        <XAxis dataKey={xKey} minTickGap={20} {...axis} />
        <YAxis tickFormatter={(v) => `${compact(v)}${unit}`} width={52} {...axis} />
        <Tooltip {...tip} cursor={{ fill: "#1f273255" }} formatter={(v) => [`${Number(v).toFixed(2)}${unit}`, label]} />
        <ReferenceLine y={0} stroke="#7d8896" />
        <Bar dataKey={yKey} isAnimationActive={false}>
          {data.map((d, i) => <Cell key={i} fill={Number(d[yKey]) >= 0 ? "#2ebd85" : "#f6465d"} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function PlainBars({ data, xKey, yKey, height = 180, label, layout = "horizontal" }: { data: Record<string, unknown>[]; xKey: string; yKey: string; height?: number; label: string; layout?: "horizontal" | "vertical" }) {
  if (!data.length) return <p className="text-sm text-dim">No data.</p>;
  const vertical = layout === "vertical";
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout={layout} margin={{ top: 5, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="#1a212b" horizontal={!vertical} vertical={vertical} />
        {vertical ? <XAxis type="number" tickFormatter={compact} {...axis} /> : <XAxis dataKey={xKey} minTickGap={10} {...axis} />}
        {vertical ? <YAxis type="category" dataKey={xKey} width={130} {...axis} /> : <YAxis tickFormatter={compact} width={52} {...axis} />}
        <Tooltip {...tip} cursor={{ fill: "#1f273255" }} formatter={(v) => [Number(v).toLocaleString("en-GB", { maximumFractionDigits: 4 }), label]} />
        <Bar dataKey={yKey} fill="#4da3ff" isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Reliability diagram: predicted probability vs observed frequency, with the diagonal as the ideal. */
export function CalibrationChart({ data, height = 220 }: { data: { predicted: number; observed: number }[]; height?: number }) {
  if (!data.length) return <p className="text-sm text-dim">No calibration data.</p>;
  const rows = [...data].sort((a, b) => a.predicted - b.predicted).map((d) => ({ ...d, ideal: d.predicted }));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={rows} margin={{ top: 5, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="#1a212b" />
        <XAxis dataKey="predicted" type="number" domain={[0, 1]} tickFormatter={(v) => v.toFixed(1)} {...axis} />
        <YAxis domain={[0, 1]} tickFormatter={(v) => v.toFixed(1)} width={36} {...axis} />
        <Tooltip {...tip} formatter={(v, n) => [Number(v).toFixed(3), n === "observed" ? "Observed frequency" : "Perfect calibration"]} labelFormatter={(l) => `Predicted ${Number(l).toFixed(3)}`} />
        <Line dataKey="ideal" stroke="#7d8896" strokeDasharray="4 4" dot={false} isAnimationActive={false} />
        <Line dataKey="observed" stroke="#4da3ff" strokeWidth={1.5} dot={{ r: 3 }} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
