"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "@/services/api";

type Row = { id: string; symbol: string; asset_class: string; quote_currency: string; series: { timeframe: string; bars: number; kind: string }[] };

/** Search box for the whole universe. A drop-down cannot hold thousands of instruments, so this asks the server
 *  for matches as you type: every instrument is reachable, those with stored prices first. */
export function InstrumentPicker({ value, onChange, hasData, placeholder = "Type a ticker…" }: {
  value: string; onChange: (id: string, row?: Row) => void; hasData?: boolean; placeholder?: string;
}) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { setText(value); }, [value]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setState("loading");
    const id = setTimeout(async () => {
      try {
        // While the box still shows the selected instrument, list the universe instead of only that one.
        const q = text === value ? "" : text.trim();
        const found = await api<Row[]>(`/api/instruments?limit=60&q=${encodeURIComponent(q)}${hasData === undefined ? "" : `&has_data=${hasData}`}`);
        if (!cancelled) { setRows(found); setActive(0); setState("idle"); }
      } catch { if (!cancelled) setState("error"); }
    }, 200);
    return () => { cancelled = true; clearTimeout(id); };
  }, [text, open, value, hasData]);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) { setOpen(false); setText(value); } };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [value]);
  const pick = (r: Row) => { onChange(r.id, r); setText(r.id); setOpen(false); };
  return (
    <div ref={box} className="relative">
      <input role="combobox" aria-expanded={open} aria-controls="instrument-options" aria-label="Instrument" value={text} placeholder={placeholder} autoComplete="off"
        onFocus={(e) => { setOpen(true); e.target.select(); }} onChange={(e) => { setText(e.target.value); setOpen(true); }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(a + 1, rows.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
          else if (e.key === "Enter" && open && rows[active]) { e.preventDefault(); pick(rows[active]); }
          else if (e.key === "Escape") { setOpen(false); setText(value); }
        }} />
      {open && (
        <div id="instrument-options" role="listbox" className="absolute z-30 mt-1 max-h-72 w-full min-w-[18rem] overflow-auto rounded border border-line bg-panel shadow-lg">
          {rows.map((r, i) => (
            <button type="button" role="option" aria-selected={r.id === value} key={r.id} onMouseDown={(e) => { e.preventDefault(); pick(r); }} onMouseEnter={() => setActive(i)}
              className={`flex w-full items-baseline gap-2 px-2 py-1.5 text-left font-mono text-xs ${i === active ? "bg-line/60" : ""} ${r.id === value ? "text-accent" : ""}`}>
              <span className="font-semibold">{r.symbol}</span><span className="text-dim">{r.asset_class} · {r.quote_currency}</span>
              <span className="ml-auto text-dim">{r.series.length ? r.series.map((s) => s.timeframe).join(" ") : "no prices"}</span>
            </button>))}
          {state === "loading" && rows.length === 0 && <p className="px-2 py-2 text-xs text-dim">Searching…</p>}
          {state === "error" && <p className="px-2 py-2 text-xs text-down">The instrument list could not be loaded.</p>}
          {state === "idle" && rows.length === 0 && <p className="px-2 py-2 text-xs text-dim">Nothing matches{hasData ? " among instruments with stored prices" : ""}.</p>}
          {rows.length >= 60 && <p className="border-t border-line px-2 py-1.5 text-[11px] text-dim">First 60 matches shown. Keep typing to narrow the list: every instrument can be found.</p>}
        </div>)}
    </div>
  );
}
