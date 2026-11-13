"use client";
import { useEffect, useRef, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type LiveQuote = {
  price: number; bid: number | null; ask: number | null; provider: string; status: string; venue: string; latencyMs: number | null;
  tsSource: string; receivedAt: number; ticks: number; direction: 1 | 0 | -1;
};

/** Listens to the server's event stream for one instrument. The browser receives every update as it happens:
 *  nothing is polled. `connected` is the state of the stream itself, not a claim about the data. */
export function useLiveQuote(instrumentId: string | null) {
  const [quote, setQuote] = useState<LiveQuote | null>(null);
  const [connected, setConnected] = useState(false);
  const count = useRef(0);
  useEffect(() => {
    setQuote(null);
    count.current = 0;
    if (!instrumentId) return;
    const es = new EventSource(`${BASE}/api/live/stream?symbols=${encodeURIComponent(instrumentId)}`, { withCredentials: true });
    const handle = (raw: MessageEvent, counted: boolean) => {
      const e = JSON.parse(raw.data);
      if (counted) count.current += 1;
      setQuote((prev) => ({ price: e.price, bid: e.bid, ask: e.ask, provider: e.provider, status: e.status, venue: e.venue ?? "", latencyMs: e.latency_ms ?? null,
        tsSource: e.ts_source, receivedAt: Date.now(), ticks: count.current, direction: prev ? (e.price > prev.price ? 1 : e.price < prev.price ? -1 : prev.direction) : 0 }));
    };
    es.addEventListener("tick", (e) => handle(e as MessageEvent, true));
    es.addEventListener("quote", (e) => handle(e as MessageEvent, false));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    return () => { es.close(); setConnected(false); };
  }, [instrumentId]);
  return { quote, connected };
}

/** Re-renders once a second so that "seconds since the last update" stays truthful on screen. */
export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}
