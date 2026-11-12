"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/services/api";

/* eslint-disable @typescript-eslint/no-explicit-any */
export type Json = any;

export function useApi<T = Json>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!path);
  const current = useRef(path);
  current.current = path;
  const reload = useCallback(async () => {
    if (!path) { setData(null); setLoading(false); return; }
    try {
      const d = await api<T>(path);
      if (current.current === path) { setData(d); setError(null); }
    } catch (e) {
      if (current.current === path) { setError(e instanceof Error ? e.message : "Request failed"); setData(null); }
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    setLoading(!!path);
    reload();
    if (!refreshMs || !path) return;
    const id = setInterval(reload, refreshMs);
    return () => clearInterval(id);
  }, [reload, refreshMs, path]);
  return { data, error, loading, reload };
}

/** Runs a mutating call and reports the outcome inline. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const run = async (fn: () => Promise<unknown>, success = "Done.") => {
    setBusy(true);
    setMsg(null);
    try { await fn(); setMsg({ ok: true, text: success }); return true; }
    catch (e) { setMsg({ ok: false, text: e instanceof Error ? e.message : "Request failed" }); return false; }
    finally { setBusy(false); }
  };
  return { busy, msg, run, setMsg };
}

export const post = (path: string, body?: unknown, method = "POST") =>
  api<Json>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

export const num = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
export const pct = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
export const money = (v: number | null | undefined, ccy = "") => (v === null || v === undefined ? "—" : `${num(v)}${ccy ? " " + ccy : ""}`);
export const signed = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${num(v, d)}`);
export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 16) + "Z" : "—");
export const tone = (v: number | null | undefined) => (!v ? "" : v > 0 ? "text-up" : "text-down");

export function Panel({ title, right, children, className = "" }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`min-w-0 rounded border border-line bg-panel ${className}`}>
      <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-line px-4 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-dim">{title}</h2>
        {right}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, cls = "", hint }: { label: string; value: React.ReactNode; cls?: string; hint?: string }) {
  return (
    <div className="rounded border border-line bg-panel px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-dim">{label}</div>
      <div className={`mt-1 font-mono text-lg ${cls}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-dim">{hint}</div>}
    </div>
  );
}

export function Badge({ children, kind = "dim" }: { children: React.ReactNode; kind?: "up" | "down" | "warn" | "accent" | "dim" }) {
  const c = { up: "border-up/40 text-up", down: "border-down/40 text-down", warn: "border-warn/40 text-warn", accent: "border-accent/40 text-accent", dim: "border-line text-dim" }[kind];
  return <span className={`inline-block whitespace-nowrap rounded border px-1.5 py-px font-mono text-[11px] ${c}`}>{children}</span>;
}

export function Button({ children, onClick, kind = "primary", disabled, type = "button", title }: {
  children: React.ReactNode; onClick?: () => void; kind?: "primary" | "ghost" | "danger"; disabled?: boolean; type?: "button" | "submit"; title?: string;
}) {
  const c = { primary: "bg-accent text-bg", ghost: "border border-line text-ink hover:border-dim", danger: "bg-down text-white" }[kind];
  return <button type={type} title={title} disabled={disabled} onClick={onClick} className={`rounded px-3 py-1.5 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${c}`}>{children}</button>;
}

export function Msg({ msg }: { msg: { ok: boolean; text: string } | null }) {
  return msg ? <p role="status" className={`text-sm ${msg.ok ? "text-up" : "text-down"}`}>{msg.text}</p> : null;
}

export function Notice({ kind = "warn", children }: { kind?: "warn" | "down" | "accent"; children: React.ReactNode }) {
  const c = { warn: "border-warn/40 bg-warn/5 text-warn", down: "border-down/50 bg-down/10 text-down", accent: "border-accent/40 bg-accent/5 text-accent" }[kind];
  return <div className={`rounded border px-4 py-2 text-sm ${c}`}>{children}</div>;
}

export function Field({ label, children, className = "" }: { label: string; children: React.ReactNode; className?: string }) {
  return <label className={`block text-xs text-dim ${className}`}>{label}<div className="mt-1">{children}</div></label>;
}

export function Table({ head, rows, empty = "Nothing to show.", max = "max-h-96" }: { head: string[]; rows: React.ReactNode[][]; empty?: string; max?: string }) {
  if (!rows.length) return <p className="text-sm text-dim">{empty}</p>;
  return (
    <div className={`overflow-auto ${max}`}>
      <table className="w-full min-w-max text-left font-mono text-xs">
        <thead className="sticky top-0 bg-panel text-dim">
          <tr>{head.map((h) => <th key={h} className="border-b border-line px-2 py-1.5 font-normal">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => <tr key={i} className="border-b border-line/50 hover:bg-line/30">{r.map((c, j) => <td key={j} className="px-2 py-1.5 align-top">{c}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

export function Meter({ label, value, limit, format = pct }: { label: string; value: number; limit: number; format?: (v: number) => string }) {
  const ratio = limit > 0 ? Math.min(value / limit, 1) : 0;
  const colour = ratio >= 1 ? "bg-down" : ratio >= 0.75 ? "bg-warn" : "bg-up";
  return (
    <div>
      <div className="flex justify-between font-mono text-xs"><span className="text-dim">{label}</span><span>{format(value)} / {format(limit)}</span></div>
      <div className="mt-1 h-1.5 rounded bg-line"><div className={`h-1.5 rounded ${colour}`} style={{ width: `${ratio * 100}%` }} /></div>
    </div>
  );
}

export function SimBadge({ simulated }: { simulated: boolean }) {
  return simulated ? <Badge kind="accent">SIMULATED</Badge> : <Badge kind="warn">REAL</Badge>;
}

export const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"];

/** "USD=0.92, USDT=0.92" -> {USD: 0.92, USDT: 0.92} */
export function parseFx(text: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const part of text.split(",")) {
    const [k, v] = part.split("=").map((s) => s.trim());
    if (k && v && !Number.isNaN(Number(v))) out[k.toUpperCase()] = Number(v);
  }
  return out;
}
