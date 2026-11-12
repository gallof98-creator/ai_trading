"use client";
import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, LineSeries, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

export type Candle = { ts: string; open: number; high: number; low: number; close: number };
type Props = { candles: Candle[]; lines?: { name: string; color: string; values: (number | null)[] }[]; signals?: { ts: string; action: string }[]; height?: number };

const t = (iso: string) => Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;

export default function CandleChart({ candles, lines = [], signals = [], height = 380 }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || !candles.length) return;
    const chart = createChart(ref.current, {
      height, autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#11161d" }, textColor: "#7d8896", fontFamily: "ui-monospace, monospace", fontSize: 11 },
      grid: { vertLines: { color: "#1a212b" }, horzLines: { color: "#1a212b" } },
      rightPriceScale: { borderColor: "#1f2732" }, timeScale: { borderColor: "#1f2732", timeVisible: true },
    });
    const series = chart.addSeries(CandlestickSeries, { upColor: "#2ebd85", downColor: "#f6465d", borderVisible: false, wickUpColor: "#2ebd85", wickDownColor: "#f6465d" });
    series.setData(candles.map((c) => ({ time: t(c.ts), open: c.open, high: c.high, low: c.low, close: c.close })));
    for (const l of lines) {
      const s = chart.addSeries(LineSeries, { color: l.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: l.name });
      s.setData(candles.flatMap((c, i) => (l.values[i] === null || l.values[i] === undefined ? [] : [{ time: t(c.ts), value: l.values[i] as number }])));
    }
    if (signals.length) {
      createSeriesMarkers(series, signals.map((s) => ({
        time: t(s.ts), position: s.action === "BUY" ? ("belowBar" as const) : ("aboveBar" as const),
        color: s.action === "BUY" ? "#2ebd85" : "#f6465d", shape: s.action === "BUY" ? ("arrowUp" as const) : ("arrowDown" as const), text: s.action,
      })));
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, lines, signals, height]);
  return <div ref={ref} style={{ height }} className="w-full" />;
}
