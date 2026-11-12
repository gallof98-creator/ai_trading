"use client";
import { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Msg, num, Panel, post, Table, useAction, useApi, when } from "@/components/ui";
import { api, RiskLimits, User, UserSettings } from "@/services/api";

const RISK_FIELDS: { key: keyof RiskLimits; label: string; percent?: boolean }[] = [
  { key: "risk_per_trade", label: "Risk per trade", percent: true }, { key: "max_daily_loss", label: "Max daily loss", percent: true },
  { key: "max_drawdown", label: "Max drawdown", percent: true }, { key: "max_exposure_per_asset", label: "Max exposure per asset", percent: true },
  { key: "max_exposure_per_strategy", label: "Max exposure per strategy", percent: true }, { key: "max_exposure_per_broker", label: "Max exposure per broker", percent: true },
  { key: "max_margin_utilization", label: "Max margin utilisation", percent: true }, { key: "max_correlated_exposure", label: "Max correlated exposure", percent: true },
  { key: "correlation_threshold", label: "Correlation threshold" }, { key: "max_gross_leverage", label: "Max gross leverage (×)" },
  { key: "max_concurrent_positions", label: "Max concurrent positions" },
];
const CATEGORIES = ["signal", "execution", "order_rejected", "approval", "broker", "risk", "reconciliation", "model", "strategy", "system"];

function TradingSettings({ readOnly }: { readOnly: boolean }) {
  const [s, setS] = useState<UserSettings | null>(null);
  const act = useAction();
  useEffect(() => { api<UserSettings>("/api/settings").then(setS).catch((e) => act.setMsg({ ok: false, text: e.message })); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  if (!s) return <p className="text-sm text-dim">{act.msg?.text ?? "Loading…"}</p>;
  const setRisk = (key: keyof RiskLimits, value: number | string | null) => setS({ ...s, risk: { ...s.risk, [key]: value } });
  const save = (e: React.FormEvent) => {
    e.preventDefault();
    if (s.risk.emergency_policy === "flatten_positions" && !window.confirm("This policy lets the platform close positions automatically when a risk limit is breached. Authorize it?")) return;
    if (!s.live_orders_require_approval && !window.confirm("Real-money orders would be sent without a human approval whenever live trading is authorized. Continue?")) return;
    act.run(async () => setS(await api<UserSettings>("/api/settings", { method: "PUT", body: JSON.stringify(s) })), "Saved.");
  };
  return (
    <form onSubmit={save} className="space-y-4">
      <fieldset disabled={readOnly} className="grid gap-4 xl:grid-cols-2">
        <Panel title="Account and operating mode">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Base currency"><input value={s.base_currency} onChange={(e) => setS({ ...s, base_currency: e.target.value.toUpperCase() })} /></Field>
            <Field label={`Initial paper capital (${s.base_currency})`}><input type="number" min="0" step="any" placeholder="Required before paper trading" value={s.initial_paper_capital ?? ""}
              onChange={(e) => setS({ ...s, initial_paper_capital: e.target.value === "" ? null : Number(e.target.value) })} /></Field>
            <Field label="Trading mode"><select value={s.trading_mode} onChange={(e) => setS({ ...s, trading_mode: e.target.value as UserSettings["trading_mode"] })}>
              <option value="manual">Manual (signals only)</option><option value="semi_auto">Semi-automatic (approval required)</option><option value="auto">Automatic</option></select></Field>
            <Field label="FX conversion cost (bps)"><input type="number" min="0" step="any" value={s.fx_conversion_cost_bps} onChange={(e) => setS({ ...s, fx_conversion_cost_bps: Number(e.target.value) })} /></Field>
            <label className="flex items-center gap-2 text-xs text-dim sm:col-span-2">
              <input type="checkbox" className="!w-auto" checked={s.live_orders_require_approval} onChange={(e) => setS({ ...s, live_orders_require_approval: e.target.checked })} />
              Real-money orders always wait for a human approval (recommended)
            </label>
          </div>
        </Panel>
        <Panel title="Risk limits">
          <div className="grid gap-3 sm:grid-cols-2">
            {RISK_FIELDS.map((f) => (
              <Field key={f.key} label={`${f.label}${f.percent ? " (%)" : ""}`}>
                <input type="number" step="any" min="0" value={f.percent ? +((s.risk[f.key] as number) * 100).toFixed(6) : (s.risk[f.key] as number)}
                  onChange={(e) => setRisk(f.key, f.percent ? Number(e.target.value) / 100 : Number(e.target.value))} />
              </Field>
            ))}
            <Field label="Volatility target for sizing (%, blank = off)">
              <input type="number" step="any" min="0" value={s.risk.target_volatility === null ? "" : +(s.risk.target_volatility * 100).toFixed(6)}
                onChange={(e) => setRisk("target_volatility", e.target.value === "" ? null : Number(e.target.value) / 100)} />
            </Field>
            <Field label="Emergency policy when a limit is breached" className="sm:col-span-2">
              <select value={s.risk.emergency_policy} onChange={(e) => setRisk("emergency_policy", e.target.value)}>
                <option value="block_new_risk">Block new risk-increasing orders (never liquidates)</option>
                <option value="cancel_open_orders">Block new risk and cancel open entry orders</option>
                <option value="flatten_positions">Block, cancel and close all positions</option>
              </select>
            </Field>
          </div>
        </Panel>
        <Panel title="Notifications" className="xl:col-span-2">
          <div className="space-y-3">
            <p className="text-xs text-dim">Every event is recorded in the app. Email and Telegram (configured by the operator through environment variables) receive events at or above the chosen severity, except muted categories.</p>
            <Field label="Send to external channels from severity" className="max-w-xs">
              <select value={s.notifications.external_min_severity} onChange={(e) => setS({ ...s, notifications: { ...s.notifications, external_min_severity: e.target.value as "info" | "warning" | "critical" } })}>
                <option value="info">info (everything)</option><option value="warning">warning</option><option value="critical">critical only</option></select>
            </Field>
            <div className="flex flex-wrap gap-3">
              {CATEGORIES.map((c) => (
                <label key={c} className="flex items-center gap-1 font-mono text-xs text-dim">
                  <input type="checkbox" className="!w-auto" checked={s.notifications.muted_categories.includes(c)}
                    onChange={(e) => setS({ ...s, notifications: { ...s.notifications, muted_categories: e.target.checked ? [...s.notifications.muted_categories, c] : s.notifications.muted_categories.filter((x) => x !== c) } })} />
                  mute {c.replace(/_/g, " ")}
                </label>
              ))}
            </div>
          </div>
        </Panel>
      </fieldset>
      <div className="flex items-center gap-4">
        {readOnly ? <p className="text-sm text-dim">Only administrators can change settings.</p> : <Button type="submit" disabled={act.busy}>Save settings</Button>}
        <Msg msg={act.msg} />
      </div>
    </form>
  );
}

function FxPanel({ isAdmin }: { isAdmin: boolean }) {
  const fx = useApi("/api/fx", 30_000);
  const [rate, setRate] = useState({ currency: "USD", rate: "" });
  const act = useAction();
  return (
    <Panel title="Exchange rates to base currency">
      <div className="space-y-3">
        <Table max="max-h-56" head={["Currency", "Rate", "Source", "Observed"]}
          rows={(fx.data ?? []).map((r: Json) => [r.currency, r.available ? num(r.rate, 5) : <span key="u" className="text-warn">unavailable</span>, r.available ? r.source : "—", r.available ? when(r.ts) : "add a rate below"])} />
        <p className="text-xs text-dim">Valuations use the newest rate observed at or before each event; rates older than the configured maximum age are refused. Stablecoins are valued through the pegs configured by the operator.</p>
        {isAdmin && (
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Currency"><input value={rate.currency} onChange={(e) => setRate({ ...rate, currency: e.target.value.toUpperCase() })} /></Field>
            <Field label="1 unit in base currency"><input type="number" step="any" min="0" value={rate.rate} onChange={(e) => setRate({ ...rate, rate: e.target.value })} /></Field>
            <Button kind="ghost" disabled={act.busy || !rate.rate} onClick={() => act.run(() => post("/api/fx", { currency: rate.currency, rate: Number(rate.rate) }), "Rate recorded.").then(fx.reload)}>Add manual rate</Button>
            <Button kind="ghost" disabled={act.busy} onClick={() => act.run(() => post("/api/fx/refresh-ecb"), "ECB reference rates loaded.").then(fx.reload)}>Load ECB reference rates</Button>
          </div>
        )}
        <Msg msg={act.msg} />
      </div>
    </Panel>
  );
}

function BrokerPanel({ isAdmin }: { isAdmin: boolean }) {
  const brokers = useApi("/api/brokers");
  const status = useApi("/api/brokers/status", 15_000);
  const [form, setForm] = useState({ id: "", adapter: "ccxt", label: "", currency: "USDT", exchange: "binance", market_type: "spot", host: "127.0.0.1", port: "7497" });
  const [cred, setCred] = useState({ broker: "", api_key: "", api_secret: "", passphrase: "" });
  const act = useAction();
  const connected = (id: string) => (status.data ?? []).some((b: Json) => b.id === id && b.connected);
  const refresh = () => { brokers.reload(); status.reload(); };
  const create = () => act.run(() => post("/api/brokers", { id: form.id, adapter: form.adapter, label: form.label || form.id, currency: form.currency, is_live: false,
    config: form.adapter === "ccxt" ? { exchange: form.exchange, market_type: form.market_type } : { host: form.host, port: Number(form.port) } }), "Account added.").then(refresh);
  return (
    <Panel title="Broker management and API credentials">
      <div className="space-y-4">
        <Table max="max-h-56" empty="No external broker accounts. Paper accounts are managed under Execution." head={["Account", "Adapter", "Currency", "Mode", "Credentials", "Connection", ""]}
          rows={(brokers.data ?? []).filter((b: Json) => b.adapter !== "paper").map((b: Json) => [b.id, `${b.adapter}${b.config.exchange ? ` · ${b.config.exchange}` : ""}`, b.currency,
            b.is_live ? <Badge key="m" kind="warn">LIVE</Badge> : <Badge key="m">testnet / paper gateway</Badge>, b.has_credentials ? `stored (…${b.key_hint})` : "none",
            <Badge key="c" kind={connected(b.id) ? "up" : "dim"}>{connected(b.id) ? "connected" : "not connected"}</Badge>,
            isAdmin ? <span key="a" className="flex gap-2">
              <button className="text-accent hover:underline" onClick={() => setCred({ ...cred, broker: b.id })}>Credentials</button>
              {connected(b.id) ? <button className="text-accent hover:underline" onClick={() => act.run(() => post(`/api/brokers/${b.id}/disconnect`), "Disconnected.").then(refresh)}>Disconnect</button>
                : <button className="text-accent hover:underline" onClick={() => act.run(() => post(`/api/brokers/${b.id}/connect`), "Connected.").then(refresh)}>Connect</button>}
            </span> : "—"])} />
        {isAdmin && (
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="space-y-3 rounded border border-line p-3">
              <h3 className="text-sm font-semibold">Add an exchange or broker account</h3>
              <div className="grid gap-3 sm:grid-cols-3">
                <Field label="Account id"><input value={form.id} placeholder="binance-test" onChange={(e) => setForm({ ...form, id: e.target.value })} /></Field>
                <Field label="Adapter"><select value={form.adapter} onChange={(e) => setForm({ ...form, adapter: e.target.value, currency: e.target.value === "ccxt" ? "USDT" : "EUR" })}><option value="ccxt">CCXT (crypto)</option><option value="ibkr">Interactive Brokers</option></select></Field>
                <Field label="Account currency"><input value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value.toUpperCase() })} /></Field>
                {form.adapter === "ccxt" ? <>
                  <Field label="Exchange (CCXT id)"><input value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })} /></Field>
                  <Field label="Market"><select value={form.market_type} onChange={(e) => setForm({ ...form, market_type: e.target.value })}><option>spot</option><option>swap</option></select></Field>
                </> : <>
                  <Field label="Gateway host"><input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} /></Field>
                  <Field label="Gateway port"><input value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} /></Field>
                </>}
              </div>
              <p className="text-xs text-dim">Accounts added here connect to the exchange testnet or an IBKR paper gateway. Real-money accounts cannot be created unless the deployment itself authorizes live trading.</p>
              <Button kind="ghost" disabled={act.busy || !form.id} onClick={create}>Add account</Button>
            </div>
            <div className="space-y-3 rounded border border-line p-3">
              <h3 className="text-sm font-semibold">Store API credentials {cred.broker && <span className="font-mono text-accent">for {cred.broker}</span>}</h3>
              <p className="text-xs text-dim">Use a key limited to reading and trading. Keys with withdrawal permission are refused. Credentials are encrypted at rest and never shown again.</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Account"><select value={cred.broker} onChange={(e) => setCred({ ...cred, broker: e.target.value })}><option value="">Select…</option>{(brokers.data ?? []).filter((b: Json) => b.adapter !== "paper").map((b: Json) => <option key={b.id}>{b.id}</option>)}</select></Field>
                <Field label="API key"><input autoComplete="off" value={cred.api_key} onChange={(e) => setCred({ ...cred, api_key: e.target.value })} /></Field>
                <Field label="API secret"><input type="password" autoComplete="new-password" value={cred.api_secret} onChange={(e) => setCred({ ...cred, api_secret: e.target.value })} /></Field>
                <Field label="Passphrase (if any)"><input type="password" autoComplete="new-password" value={cred.passphrase} onChange={(e) => setCred({ ...cred, passphrase: e.target.value })} /></Field>
              </div>
              <Button kind="ghost" disabled={act.busy || !cred.broker || !cred.api_key} onClick={() => act.run(() => post(`/api/brokers/${cred.broker}/credentials`, { api_key: cred.api_key, api_secret: cred.api_secret, passphrase: cred.passphrase, permissions: ["read", "trade"] }, "PUT"), "Credentials stored (encrypted).")
                .then((ok) => { if (ok) { setCred({ broker: "", api_key: "", api_secret: "", passphrase: "" }); refresh(); } })}>Store credentials</Button>
            </div>
          </div>
        )}
        <Msg msg={act.msg} />
      </div>
    </Panel>
  );
}

function SecurityPanel({ user }: { user: User }) {
  const [form, setForm] = useState({ email: "", password: "", role: "viewer" });
  const act = useAction();
  return (
    <Panel title="Security and users">
      <div className="space-y-3">
        <ul className="list-disc space-y-1 pl-5 text-xs text-dim">
          <li>Signed in as <span className="text-ink">{user.email}</span> ({user.role}). Sessions last 60 minutes; signing out ends all of your sessions.</li>
          <li>Roles: viewer reads; trader manages strategies, backtests and approvals and can engage the kill switch; admin also manages settings, credentials, users and resets.</li>
          <li>The browser never holds API keys or tokens; the session cookie is httpOnly.</li>
        </ul>
        {user.role === "admin" && (
          <div className="flex flex-wrap items-end gap-3">
            <Field label="New user email"><input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
            <Field label="Password (min 12 characters)"><input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
            <Field label="Role"><select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}><option>viewer</option><option>trader</option><option>admin</option></select></Field>
            <Button kind="ghost" disabled={act.busy || form.password.length < 12 || !form.email} onClick={() => act.run(() => post("/api/auth/users", form), "User created.").then((ok) => { if (ok) setForm({ email: "", password: "", role: "viewer" }); })}>Create user</Button>
          </div>
        )}
        <Msg msg={act.msg} />
      </div>
    </Panel>
  );
}

export default function Page() {
  return (
    <Shell>{(user) => (
      <div className="space-y-4">
        <TradingSettings readOnly={user.role !== "admin"} />
        <FxPanel isAdmin={user.role === "admin"} />
        <BrokerPanel isAdmin={user.role === "admin"} />
        <SecurityPanel user={user} />
      </div>
    )}</Shell>
  );
}
