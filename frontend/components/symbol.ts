"use client";
import { useCallback, useEffect, useState } from "react";

const KEY = "qat.symbol";

/** The instrument currently in focus, shared by the chart, Markets and Quant AI pages.
 *  A `?symbol=` in the address wins, then the last one used in this browser, then the fallback. */
export function useActiveSymbol(fallback: string, instrumentsOnly = false): [string, (s: string) => void] {
  const [symbol, setSymbol] = useState(fallback);
  useEffect(() => {
    let found: string | null = null;
    try { found = new URLSearchParams(window.location.search).get("symbol") ?? window.localStorage.getItem(KEY); } catch {}
    // Catalog entries that are not in the trading universe ("sec:…") only make sense on the chart page.
    if (found && !(instrumentsOnly && found.startsWith("sec:"))) setSymbol(found);
  }, [instrumentsOnly]);
  const set = useCallback((s: string) => {
    setSymbol(s);
    try {
      window.localStorage.setItem(KEY, s);
      const url = new URL(window.location.href);
      if (url.searchParams.has("symbol")) { url.searchParams.set("symbol", s); window.history.replaceState(null, "", url); }
    } catch {}
  }, []);
  return [symbol, set];
}

export const chartHref = (symbol: string) => `/chart?symbol=${encodeURIComponent(symbol)}`;
