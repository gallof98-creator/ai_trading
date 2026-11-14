// The browser never holds API keys or tokens: the session is an httpOnly cookie set by the backend.
const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-Requested-With": "qat-dashboard", ...init.headers },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export type User = { id: number; email: string; role: "admin" | "trader" | "viewer" };
export type Health = { status: string; version: string; database: boolean; environment: string; live_trading_authorized: boolean };
export type RiskLimits = {
  risk_per_trade: number; max_daily_loss: number; max_drawdown: number; max_exposure_per_asset: number;
  max_exposure_per_strategy: number; max_exposure_per_broker: number; max_gross_leverage: number;
  max_concurrent_positions: number; max_margin_utilization: number; target_volatility: number | null; max_correlated_exposure: number; correlation_threshold: number;
  emergency_policy: "block_new_risk" | "cancel_open_orders" | "flatten_positions";
  est_spread_bps: number; est_slippage_bps: number; est_commission_bps: number; max_spread_bps: number; max_data_age_seconds: number;
};
export type UserSettings = {
  base_currency: string; initial_paper_capital: number | null; trading_mode: "manual" | "semi_auto" | "auto";
  fx_conversion_cost_bps: number; live_orders_require_approval: boolean; risk: RiskLimits;
  proposal_ttl_minutes: number; max_entry_drift: number; safety_limits_confirmed: boolean;
  notifications: { external_min_severity: "info" | "warning" | "critical"; muted_categories: string[] };
};
