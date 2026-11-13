"use client";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Meter, Msg, Notice, num, Panel, post, Stat, Table, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";
import { Analysis, Scanner, Strategies } from "./tabs";

const TYPES = ["common_stock", "preferred_stock", "etf", "reit", "adr", "depositary_receipt", "closed_end_fund", "other"];
const label = (t: string) => t.replace(/_/g, " ");

function Coverage({ user, onExchange }: { user: User; onExchange: (code: string) => void }) {
  const cov = useApi("/api/global/coverage", 15_000);
  const providers = useApi("/api/global/providers");
  const job = useApi("/api/global/sync", 2_000);
  const [provider, setProvider] = useState("alphavantage");
  const [picked, setPicked] = useState<string[]>([]);
  const [region, setRegion] = useState("All");
  const [key, setKey] = useState("");
  const [figiKey, setFigiKey] = useState("");
  const act = useAction();
  const canWrite = user.role !== "viewer";
  const p = (providers.data ?? []).find((x: Json) => x.name === provider);
  const figi = (providers.data ?? []).find((x: Json) => x.name === "openfigi");
  const running = job.data && ["pending", "running"].includes(job.data.status);
  if (!cov.data) return <p className="text-sm text-dim">{cov.error ?? "Loading…"}</p>;
  const t = cov.data.totals;
  const regions: string[] = ["All", ...Array.from(new Set<string>(cov.data.exchanges.map((e: Json) => e.region)))];
  const rows = cov.data.exchanges.filter((e: Json) => region === "All" || e.region === region);
  const toggle = (code: string) => setPicked(picked.includes(code) ? picked.filter((c) => c !== code) : [...picked, code]);
  const start = (exchanges: string[] | null) => act.run(() => post("/api/global/sync", { provider, exchanges }), "Synchronisation started.").then(() => job.reload());

  return (
    <div className="space-y-4">
      <Notice kind="accent">{cov.data.statement}</Notice>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Stat label="Exchanges with data" value={`${cov.data.exchanges_with_data} / ${cov.data.exchanges_in_registry}`} />
        <Stat label="Instruments discovered" value={num(t.discovered, 0)} />
        <Stat label="Active listings" value={num(t.active, 0)} />
        <Stat label="Delisted" value={num(t.delisted, 0)} hint="seen since first sync" />
        <Stat label="Common stocks" value={num(t.equities_active, 0)} hint="active, equities only" />
        <Stat label="With ISIN / FIGI" value={`${num(t.with_isin, 0)} / ${num(t.with_figi, 0)}`} />
        <Stat label="With price history" value={num(t.with_historical_prices, 0)} hint={`${num(t.missing_prices, 0)} without · ${num(t.stale_price_series, 0)} stale`} />
        <Stat label="Real-time / delayed" value={`${t.with_realtime_data} / ${t.with_delayed_data}`} hint="no live feed connected" />
      </div>
      {canWrite && (
        <Panel title="Synchronise the catalog">
          <div className="space-y-3">
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Discovery provider"><select value={provider} onChange={(e) => { setProvider(e.target.value); setPicked([]); }}>
                {(providers.data ?? []).filter((x: Json) => x.name !== "openfigi").map((x: Json) => <option key={x.name} value={x.name}>{x.label}{x.configured ? "" : " (no key)"}</option>)}</select></Field>
              <Button disabled={act.busy || running || !p?.configured || picked.length === 0} onClick={() => start(picked)}>Synchronise selected ({picked.length})</Button>
              <Button kind="ghost" disabled={act.busy || running || !p?.configured} onClick={() => window.confirm(`Synchronise all ${p?.exchanges.length} exchanges ${p?.label} can list?`) && start(null)}>Synchronise all {p?.exchanges.length ?? 0}</Button>
              {running && <Button kind="ghost" onClick={() => post("/api/global/sync/cancel").then(job.reload)}>Cancel</Button>}
            </div>
            <p className="text-xs text-dim">{p ? `${p.label}: ${p.verified} ` : ""}{p?.configured ? "Tick exchanges in the table below, or synchronise all. Identifiers (FIGI) are then added through OpenFIGI, a few thousand per run." : ""}</p>
            {user.role === "admin" && (
              <div className="flex flex-wrap items-end gap-3">
                <Field label={`${p?.label ?? ""} API key${p?.configured ? ` (stored ${p.key_hint})` : ""}`}><input type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} /></Field>
                <Button kind="ghost" disabled={act.busy || key.trim().length < 4} onClick={() => act.run(() => post(`/api/market/providers/${provider}/key`, { api_key: key }, "PUT"), "Key stored (encrypted).").then((ok) => { if (ok) { setKey(""); providers.reload(); } })}>Save key</Button>
                <Field label={`OpenFIGI key (optional, faster)${figi?.key_hint ? ` (stored ${figi.key_hint})` : ""}`}><input type="password" autoComplete="new-password" value={figiKey} onChange={(e) => setFigiKey(e.target.value)} /></Field>
                <Button kind="ghost" disabled={act.busy || figiKey.trim().length < 4} onClick={() => act.run(() => post("/api/market/providers/openfigi/key", { api_key: figiKey }, "PUT"), "OpenFIGI key stored.").then((ok) => { if (ok) { setFigiKey(""); providers.reload(); } })}>Save</Button>
              </div>
            )}
            {job.data && job.data.status !== "idle" && (
              <div className="space-y-2 rounded border border-line p-3">
                <Meter label={`${job.data.provider}: ${job.data.status} · ${job.data.phase}${job.data.current ? ` · ${job.data.current}` : ""}`} value={job.data.done} limit={job.data.total} format={(v) => String(v)} />
                <p className="font-mono text-xs">identifiers checked {job.data.figi.checked}, matched {job.data.figi.matched}{job.data.message ? ` · ${job.data.message}` : ""}</p>
                <Table max="max-h-40" head={["Exchange", "Result", "Received", "New", "Updated", "Ticker changes", "Delisted", "Note"]}
                  rows={job.data.results.map((r: Json) => [r.name, <Badge key="s" kind={r.status === "ok" ? "up" : "down"}>{r.status}</Badge>, r.received ?? "—", r.new ?? "—", r.updated ?? "—",
                    r.ticker_changes ?? "—", r.delisted ?? "—", <span key="m" className="whitespace-normal text-dim">{r.message ?? (r.delist_check !== "done" ? r.delist_check : "")}</span>])} />
              </div>
            )}
            <Msg msg={act.msg} />
          </div>
        </Panel>
      )}
      <Panel title="Exchange directory and coverage" right={
        <select aria-label="Region" value={region} onChange={(e) => setRegion(e.target.value)} className="!w-auto">{regions.map((r) => <option key={r}>{r}</option>)}</select>}>
        <Table max="max-h-[34rem]" head={["", "Exchange", "Country", "Ccy", "Discovered", "Active", "Stocks", "ETFs", "Delisted", "ISIN", "FIGI", "Price series · price providers", "Listing providers (last result)"]}
          rows={rows.map((e: Json) => [
            canWrite && p?.exchanges.includes(e.code) ? <input key="c" aria-label={`Select ${e.name}`} type="checkbox" className="!w-auto" checked={picked.includes(e.code)} onChange={() => toggle(e.code)} /> : "",
            <button key="n" className="text-left hover:underline" title={e.note} onClick={() => onExchange(e.code)}>{e.name}</button>, e.country, e.currency,
            num(e.discovered, 0), num(e.active, 0), num(e.by_type.common_stock ?? 0, 0), num(e.by_type.etf ?? 0, 0), num(e.delisted, 0), num(e.with_isin, 0), num(e.with_figi, 0),
            <span key="p" title={e.price_providers.length ? `Price history available from: ${e.price_providers.join(", ")}` : "No connected provider has prices for this exchange"}>{num(e.with_historical_prices, 0)}{e.stale_price_series ? <span className="text-warn"> ({e.stale_price_series} stale)</span> : ""} <span className="text-dim">· {e.price_providers.join(", ") || "no provider"}</span></span>,
            <span key="pr" className="flex flex-wrap gap-1">{Object.entries(e.providers as Record<string, Json>).map(([name, st]) => (
              <span key={name} title={st.message || (st.last_sync ? `last sync ${when(st.last_sync)}, ${st.received} received` : "")}>
                <Badge kind={st.status === "ok" ? "up" : st.status === "failed" ? "down" : "dim"}>{name}: {st.status === "never synchronised" ? "not synced" : st.status}</Badge></span>))}</span>])} />
        <p className="mt-2 text-xs text-dim">A listing-provider badge shows what its last synchronisation of that exchange actually returned; grey means it was never tried. The price column counts catalog securities with a stored daily history and names the providers that can supply prices for that exchange; outside the United States only the paid provider can.</p>
      </Panel>
    </div>
  );
}

function Universe({ user, exchange, setExchange }: { user: User; exchange: string[]; setExchange: (v: string[]) => void }) {
  const cov = useApi("/api/global/coverage");
  const [q, setQ] = useState("");
  const [types, setTypes] = useState<string[]>(["common_stock"]);
  const [countries, setCountries] = useState<string[]>([]);
  const [status, setStatus] = useState("active");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<number[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const act = useAction();
  const params = new URLSearchParams({ q, listing_status: status, limit: "50", offset: String(page * 50) });
  exchange.forEach((e) => params.append("exchange", e));
  countries.forEach((c) => params.append("country", c));
  types.forEach((t) => params.append("type", t));
  const list = useApi(`/api/global/securities?${params.toString()}`);
  const profile = useApi(open ? `/api/global/securities/${open}` : null);
  const exchanges: Json[] = (cov.data?.exchanges ?? []).filter((e: Json) => e.discovered > 0);
  const allCountries: string[] = Array.from(new Set<string>(exchanges.map((e) => e.country))).sort();
  const multi = (value: string[], set: (v: string[]) => void) => (e: React.ChangeEvent<HTMLSelectElement>) => { set(Array.from(e.target.selectedOptions).map((o) => o.value)); setPage(0); void value; };
  const canWrite = user.role !== "viewer";
  const total = list.data?.total ?? 0;

  return (
    <div className="space-y-4">
      <Panel title="Global stock universe">
        <div className="space-y-3">
          <div className="grid gap-3 md:grid-cols-5">
            <Field label="Search company, ticker, ISIN or FIGI" className="md:col-span-2"><input value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} placeholder="e.g. Ferrari, ENI, IT0003132476" /></Field>
            <Field label="Exchanges (none = all)"><select multiple size={4} value={exchange} onChange={multi(exchange, setExchange)}>{exchanges.map((e) => <option key={e.code} value={e.code}>{e.code} · {e.name}</option>)}</select></Field>
            <Field label="Countries (none = all)"><select multiple size={4} value={countries} onChange={multi(countries, setCountries)}>{allCountries.map((c) => <option key={c}>{c}</option>)}</select></Field>
            <Field label="Instrument types (none = all)"><select multiple size={4} value={types} onChange={multi(types, setTypes)}>{TYPES.map((t) => <option key={t} value={t}>{label(t)}</option>)}</select></Field>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <select aria-label="Listing status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(0); }} className="!w-auto"><option value="active">Active listings</option><option value="delisted">Delisted</option><option value="all">All</option></select>
            <span className="font-mono text-xs text-dim">{num(total, 0)} matches · page {page + 1} of {Math.max(1, Math.ceil(total / 50))}</span>
            <Button kind="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</Button>
            <Button kind="ghost" disabled={(page + 1) * 50 >= total} onClick={() => setPage(page + 1)}>Next</Button>
            {canWrite && <Button disabled={act.busy || selected.length === 0} onClick={() => act.run(async () => {
              const r = await post("/api/global/securities/activate", { ids: selected });
              setSelected([]); list.reload();
              if (r.unsupported.length) throw new Error(`${r.activated.length} added. ${r.unsupported.length} could not be added: ${r.unsupported[0].reason}`);
            }, "Added to the trading universe. Download their prices under Markets.")}>Add {selected.length} to trading universe</Button>}
            <Msg msg={act.msg} />
          </div>
          <Table max="max-h-[30rem]" empty={exchanges.length ? "No security matches these filters." : "The catalog is empty. Synchronise an exchange under Coverage first."}
            head={["", "Ticker", "Company", "Exchange", "Type", "Ccy", "ISIN", "FIGI", "Status", "Sources", "Trading universe"]}
            rows={(list.data?.items ?? []).map((s: Json) => [
              canWrite && !s.instrument_id && s.listing_status === "active" ? <input key="c" aria-label={`Select ${s.ticker}`} type="checkbox" className="!w-auto" checked={selected.includes(s.id)}
                onChange={() => setSelected(selected.includes(s.id) ? selected.filter((i) => i !== s.id) : [...selected, s.id])} /> : "",
              <button key="t" className="text-accent hover:underline" onClick={() => setOpen(s.id)}>{s.ticker}</button>, <span key="n" className="block max-w-xs truncate" title={s.name}>{s.name}</span>,
              `${s.exchange}${s.mic ? ` · ${s.mic}` : ""}`, label(s.security_type), s.currency, s.isin ?? "—", s.figi ?? "—",
              <Badge key="s" kind={s.listing_status === "active" ? "up" : "down"}>{s.listing_status}</Badge>, s.providers.join(", "), s.instrument_id ?? "—"])} />
        </div>
      </Panel>
      {open && profile.data && (
        <Panel title={`Instrument profile · ${profile.data.ticker} · ${profile.data.name}`} right={<button className="text-xs text-dim hover:text-ink" onClick={() => setOpen(null)}>Close</button>}>
          <div className="grid gap-4 lg:grid-cols-2">
            <dl className="grid grid-cols-2 gap-y-1.5 font-mono text-xs">
              {[["Platform id", profile.data.id], ["Exchange", `${profile.data.exchange} (${profile.data.mic ?? "—"})`], ["Country / currency", `${profile.data.country} / ${profile.data.currency}`],
                ["Type", `${label(profile.data.security_type)}${profile.data.raw_type ? ` (${profile.data.raw_type})` : ""}`], ["ISIN", profile.data.isin ?? "not provided"], ["FIGI", profile.data.figi ?? "not found"],
                ["Share-class FIGI", profile.data.share_class_figi ?? "—"], ["Sector / industry", profile.data.sector ? `${profile.data.sector} / ${profile.data.industry}` : "no source connected"],
                ["Market capitalisation", profile.data.market_cap ? num(profile.data.market_cap, 0) : "no source connected"], ["Listing status", profile.data.listing_status],
                ["First seen", when(profile.data.first_seen)], ["Last synchronised", when(profile.data.last_synced)], ["Trading universe", profile.data.instrument_id ?? "not added"]]
                .map(([k, v]) => <div key={String(k)} className="contents"><dt className="text-dim">{k}</dt><dd className="break-all">{v}</dd></div>)}
            </dl>
            <div className="space-y-3">
              <div><h3 className="mb-1 text-xs text-dim">Data sources</h3>
                <Table max="max-h-32" head={["Provider", "Its symbol", "Last seen", "Missing since"]} rows={Object.entries(profile.data.sources as Record<string, Json>).map(([n, v]) => [n, v.symbol, when(v.synced_at), v.missing_since ? when(v.missing_since) : "—"])} /></div>
              <div><h3 className="mb-1 text-xs text-dim">History</h3>
                <Table max="max-h-40" head={["When", "Event", "From", "To", "Source"]} rows={profile.data.history.map((h: Json) => [when(h.ts), label(h.kind), h.old ?? "—", h.new ?? "—", h.source])} /></div>
              {profile.data.other_listings.length > 0 && <div><h3 className="mb-1 text-xs text-dim">Other listings of the same security</h3>
                <Table max="max-h-32" head={["Exchange", "Ticker", "Currency"]} rows={profile.data.other_listings.map((o: Json) => [o.exchange, o.ticker, o.currency])} /></div>}
            </div>
          </div>
        </Panel>
      )}
    </div>
  );
}

const TABS = [["analysis", "Market analysis"], ["scanner", "Scanner"], ["strategies", "Global strategies"], ["universe", "Stock universe"], ["coverage", "Exchanges and coverage"]] as const;
type Tab = (typeof TABS)[number][0];

function GlobalMarkets({ user }: { user: User }) {
  const [tab, setTab] = useState<Tab>("analysis");
  const [exchange, setExchange] = useState<string[]>([]);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-1">
        {TABS.map(([t, name]) => (
          <button key={t} onClick={() => setTab(t)} className={`rounded border px-3 py-1.5 text-sm ${tab === t ? "border-accent text-accent" : "border-line text-dim hover:text-ink"}`}>{name}</button>))}
      </div>
      {tab === "coverage" && <Coverage user={user} onExchange={(code) => { setExchange([code]); setTab("universe"); }} />}
      {tab === "universe" && <Universe user={user} exchange={exchange} setExchange={setExchange} />}
      {tab === "scanner" && <Scanner user={user} />}
      {tab === "analysis" && <Analysis user={user} />}
      {tab === "strategies" && <Strategies user={user} />}
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <GlobalMarkets user={user} />}</Shell>;
}
