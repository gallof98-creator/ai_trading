"use client";
import { useEffect, useState } from "react";
import { Panel, Shell } from "@/components/Shell";
import { api, RiskLimits, User, UserSettings } from "@/services/api";

const RISK_FIELDS: { key: keyof RiskLimits; label: string; percent?: boolean }[] = [
  { key: "risk_per_trade", label: "Risk per trade", percent: true },
  { key: "max_daily_loss", label: "Max daily loss", percent: true },
  { key: "max_drawdown", label: "Max drawdown", percent: true },
  { key: "max_exposure_per_asset", label: "Max exposure per asset", percent: true },
  { key: "max_exposure_per_strategy", label: "Max exposure per strategy", percent: true },
  { key: "max_exposure_per_broker", label: "Max exposure per broker", percent: true },
  { key: "max_correlated_exposure", label: "Max correlated exposure", percent: true },
  { key: "correlation_threshold", label: "Correlation threshold" },
  { key: "max_gross_leverage", label: "Max gross leverage (×)" },
  { key: "max_concurrent_positions", label: "Max concurrent positions" },
];

function SettingsForm({ user }: { user: User }) {
  const [s, setS] = useState<UserSettings | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const readOnly = user.role !== "admin";
  useEffect(() => { api<UserSettings>("/api/settings").then(setS).catch((e) => setMsg({ ok: false, text: e.message })); }, []);
  if (!s) return <p className="text-sm text-dim">{msg?.text ?? "Loading…"}</p>;

  const setRisk = (key: keyof RiskLimits, value: number | string) => setS({ ...s, risk: { ...s.risk, [key]: value } });
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (s.risk.emergency_policy === "flatten_positions" &&
        !window.confirm("This policy lets the platform close positions automatically when a risk limit is breached. Authorize it?")) return;
    try {
      setS(await api<UserSettings>("/api/settings", { method: "PUT", body: JSON.stringify(s) }));
      setMsg({ ok: true, text: "Saved." });
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Save failed" });
    }
  };

  return (
    <form onSubmit={save} className="space-y-4">
      <fieldset disabled={readOnly} className="grid gap-4 lg:grid-cols-2">
        <Panel title="Account">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-dim">Base currency
              <input value={s.base_currency} onChange={(e) => setS({ ...s, base_currency: e.target.value.toUpperCase() })} className="mt-1" />
            </label>
            <label className="text-xs text-dim">Initial paper capital ({s.base_currency})
              <input type="number" min="0" step="any" placeholder="Required before paper trading" value={s.initial_paper_capital ?? ""}
                onChange={(e) => setS({ ...s, initial_paper_capital: e.target.value === "" ? null : Number(e.target.value) })} className="mt-1" />
            </label>
            <label className="text-xs text-dim">Trading mode
              <select value={s.trading_mode} onChange={(e) => setS({ ...s, trading_mode: e.target.value as UserSettings["trading_mode"] })} className="mt-1">
                <option value="manual">Manual (signals only)</option>
                <option value="semi_auto">Semi-automatic (approval required)</option>
                <option value="auto">Automatic</option>
              </select>
            </label>
            <label className="text-xs text-dim">FX conversion cost (bps)
              <input type="number" min="0" step="any" value={s.fx_conversion_cost_bps}
                onChange={(e) => setS({ ...s, fx_conversion_cost_bps: Number(e.target.value) })} className="mt-1" />
            </label>
          </div>
        </Panel>
        <Panel title="Risk limits">
          <div className="grid gap-3 sm:grid-cols-2">
            {RISK_FIELDS.map((f) => (
              <label key={f.key} className="text-xs text-dim">{f.label}{f.percent ? " (%)" : ""}
                <input type="number" step="any" min="0" className="mt-1"
                  value={f.percent ? +((s.risk[f.key] as number) * 100).toFixed(6) : (s.risk[f.key] as number)}
                  onChange={(e) => setRisk(f.key, f.percent ? Number(e.target.value) / 100 : Number(e.target.value))} />
              </label>
            ))}
            <label className="text-xs text-dim sm:col-span-2">Emergency policy when a limit is breached
              <select value={s.risk.emergency_policy} onChange={(e) => setRisk("emergency_policy", e.target.value)} className="mt-1">
                <option value="block_new_risk">Block new risk-increasing orders (never liquidates)</option>
                <option value="cancel_open_orders">Block new risk and cancel open orders</option>
                <option value="flatten_positions">Block, cancel and close all positions</option>
              </select>
            </label>
          </div>
        </Panel>
      </fieldset>
      <div className="flex items-center gap-4">
        {readOnly ? <p className="text-sm text-dim">Only administrators can change settings.</p>
          : <button className="rounded bg-accent px-4 py-2 text-sm font-semibold text-bg">Save settings</button>}
        {msg && <p role="status" className={`text-sm ${msg.ok ? "text-up" : "text-down"}`}>{msg.text}</p>}
      </div>
    </form>
  );
}

export default function Page() {
  return <Shell>{(user) => <SettingsForm user={user} />}</Shell>;
}
