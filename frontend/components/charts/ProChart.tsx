"use client";
import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, HistogramSeries, LineSeries, LineStyle, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

export type LiveTick = { time: number; price: number };
const STEP: Record<string, number> = { "1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "240": 14400, "1D": 86400 };

export type Bars = { t: number[]; o: number[]; h: number[]; l: number[]; c: number[]; v: number[] };
export type Studies = {
  t: number[]; overlays: Record<string, (number | null)[]>; panes: Record<string, (number | null)[]>;
  levels: { price: number; kind: string; touches: number }[]; signals: { time: number; action: string; strategy: string }[];
};
type Props = { bars: Bars; studies?: Studies | null; show: Record<string, boolean>; intraday: boolean; height?: number; live?: LiveTick | null; resolution?: string };

const OVERLAY: Record<string, { color: string; title: string; dashed?: boolean }> = {
  sma_20: { color: "#f0b90b", title: "SMA 20" }, sma_50: { color: "#4da3ff", title: "SMA 50" }, sma_200: { color: "#c084fc", title: "SMA 200" },
  ema_20: { color: "#fb923c", title: "EMA 20" }, bb_upper: { color: "#64748b", title: "BB upper", dashed: true }, bb_lower: { color: "#64748b", title: "BB lower", dashed: true },
};
const GROUP: Record<string, string[]> = { sma: ["sma_20", "sma_50", "sma_200"], ema: ["ema_20"], bollinger: ["bb_upper", "bb_lower"] };

export default function ProChart({ bars, studies, show, intraday, height = 560, live, resolution = "1D" }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const series = useRef<any>(null);
  const lastBar = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null);
  useEffect(() => {
    if (!ref.current || !bars.t.length) return;
    const chart = createChart(ref.current, {
      height, autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#11161d" }, textColor: "#7d8896", fontFamily: "ui-monospace, monospace", fontSize: 11, panes: { separatorColor: "#1f2732" } },
      grid: { vertLines: { color: "#1a212b" }, horzLines: { color: "#1a212b" } }, crosshair: { mode: 0 },
      rightPriceScale: { borderColor: "#1f2732" }, timeScale: { borderColor: "#1f2732", timeVisible: intraday, secondsVisible: false },
    });
    const time = (s: number) => s as UTCTimestamp;
    const candles = chart.addSeries(CandlestickSeries, { upColor: "#2ebd85", downColor: "#f6465d", borderVisible: false, wickUpColor: "#2ebd85", wickDownColor: "#f6465d" });
    candles.setData(bars.t.map((t, i) => ({ time: time(t), open: bars.o[i], high: bars.h[i], low: bars.l[i], close: bars.c[i] })));
    const k = bars.t.length - 1;
    series.current = candles;
    lastBar.current = { time: bars.t[k], open: bars.o[k], high: bars.h[k], low: bars.l[k], close: bars.c[k] };
    if (show.volume) {
      const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vol.setData(bars.t.map((t, i) => ({ time: time(t), value: bars.v[i], color: bars.c[i] >= bars.o[i] ? "rgba(46,189,133,0.45)" : "rgba(246,70,93,0.45)" })));
    }
    if (studies) {
      const known = new Set(bars.t);
      const line = (values: (number | null)[]) => studies.t.flatMap((t, i) => (values[i] === null || values[i] === undefined || !known.has(t) ? [] : [{ time: time(t), value: values[i] as number }]));
      for (const [group, names] of Object.entries(GROUP)) {
        if (!show[group]) continue;
        for (const n of names) {
          const s = chart.addSeries(LineSeries, { color: OVERLAY[n].color, lineWidth: 1, lineStyle: OVERLAY[n].dashed ? LineStyle.Dashed : LineStyle.Solid, priceLineVisible: false, lastValueVisible: false, title: OVERLAY[n].title });
          s.setData(line(studies.overlays[n] ?? []));
        }
      }
      if (show.levels) {
        for (const lv of studies.levels) {
          candles.createPriceLine({ price: lv.price, color: lv.kind === "support" ? "#2ebd85" : "#f6465d", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true,
            title: `${lv.kind === "support" ? "S" : "R"} ×${lv.touches}` });
        }
      }
      if (show.signals && studies.signals.length) {
        const first = bars.t[0];
        createSeriesMarkers(candles, studies.signals.filter((s) => s.time >= first).map((s) => {
          const buy = s.action === "BUY";
          // A signal is drawn on the last bar that had opened when it was produced.
          const at = bars.t.reduce((best, t) => (t <= s.time ? t : best), first);
          return { time: time(at), position: buy ? ("belowBar" as const) : ("aboveBar" as const), color: buy ? "#2ebd85" : "#f6465d", shape: buy ? ("arrowUp" as const) : ("arrowDown" as const), text: `${s.action} ${s.strategy}` };
        }).sort((a, b) => a.time - b.time));
      }
      let pane = 1;
      if (show.rsi) {
        const rsi = chart.addSeries(LineSeries, { color: "#c084fc", lineWidth: 1, priceLineVisible: false, title: "RSI 14" }, pane);
        rsi.setData(line(studies.panes.rsi_14 ?? []));
        for (const p of [30, 70]) rsi.createPriceLine({ price: p, color: "#475569", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "" });
        pane += 1;
      }
      if (show.macd) {
        const hist = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "MACD hist" }, pane);
        hist.setData(studies.t.flatMap((t, i) => { const v = studies.panes.macd_hist?.[i]; return v === null || v === undefined || !known.has(t) ? [] : [{ time: time(t), value: v, color: v >= 0 ? "rgba(46,189,133,0.6)" : "rgba(246,70,93,0.6)" }]; }));
        chart.addSeries(LineSeries, { color: "#4da3ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: "MACD" }, pane).setData(line(studies.panes.macd ?? []));
        chart.addSeries(LineSeries, { color: "#f0b90b", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: "Signal" }, pane).setData(line(studies.panes.macd_signal ?? []));
      }
      const panes = chart.panes();
      if (panes.length > 1) { panes[0].setStretchFactor(4); panes.slice(1).forEach((p) => p.setStretchFactor(1)); }
    }
    const n = bars.t.length;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 220), to: n + 4 });
    return () => { series.current = null; chart.remove(); };
  }, [bars, studies, show, intraday, height]);
  // A live tick moves the last candle, or opens the next one when its time bucket has started. Nothing else is redrawn.
  useEffect(() => {
    const last = lastBar.current;
    if (!live || !series.current || !last) return;
    const step = STEP[resolution];
    const bucket = step ? Math.floor(live.time / step) * step : last.time;
    if (bucket < last.time) return;
    const next = bucket > last.time ? { time: bucket, open: live.price, high: live.price, low: live.price, close: live.price }
      : { ...last, high: Math.max(last.high, live.price), low: Math.min(last.low, live.price), close: live.price };
    lastBar.current = next;
    series.current.update({ ...next, time: next.time as UTCTimestamp });
  }, [live, resolution]);
  return <div ref={ref} style={{ height }} className="w-full" />;
}
