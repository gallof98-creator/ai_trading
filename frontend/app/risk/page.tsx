"use client";
import Link from "next/link";
import { useState } from "react";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Meter, Msg, Notice, Panel, pct, post, Table, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const POLICY: Record<string, string> = {
  block_new_risk: "Block new risk-increasing orders. Existing positions and their stops are left alone.",
  cancel_open_orders: "Block new risk and cancel working entry orders. Protective stops stay in place.",
  flatten_positions: "Block new risk, cancel orders and CLOSE ALL POSITIONS at market.",
};

function Risk({ user }: { user: User }) {
  const { data, reload, error } = useApi("/api/risk", 5_000);
  const [reason, setReason] = useState("");
  const act = useAction();
  if (error) return <Notice kind="down">{error}</Notice>;
  if (!data) return <p className="text-sm text-dim">Loading…</p>;
  const { status, limits, utilisation: u } = data;
  const canAct = user.role !== "viewer";
  const isAdmin = user.role === "admin";
  const engage = () => {
    const extra = status.emergency_policy === "flatten_positions" ? "\n\nYour emergency policy will ALSO CLOSE ALL POSITIONS at market." : status.emergency_policy === "cancel_open_orders" ? "\n\nYour emergency policy will also cancel working entry orders." : "";
    if (window.confirm(`Engage the kill switch? All new risk-increasing orders will be rejected.${extra}`))
      act.run(() => post("/api/risk/kill-switch", { engage: true, reason: reason || "manual" }), "Kill switch engaged.").then(reload);
  };

  return (
    <div className="space-y-4">
      {status.halted
        ? <Notice kind="down"><strong>Trading is halted.</strong> {status.kill_switch ? `Kill switch: ${status.kill_switch}.` : ""} {Object.keys(status.breakers).length ? `Circuit breakers: ${Object.keys(status.breakers).join(", ")}.` : ""} Orders that reduce risk are still allowed.</Notice>
        : <Notice kind="accent">Risk engine operating normally. Every order is checked against the limits below before it can reach a broker.</Notice>}
      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Global kill switch" className="xl:col-span-1">
          <div className="space-y-3">
            <p className="text-xs text-dim">Emergency policy: <span className="text-ink">{status.emergency_policy.replace(/_/g, " ")}</span>. {POLICY[status.emergency_policy]} <Link href="/settings" className="underline">Change</Link></p>
            {!status.kill_switch ? (
              canAct && <>
                <Field label="Reason (recorded in the audit trail)"><input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} /></Field>
                <Button kind="danger" disabled={act.busy} onClick={engage}>Engage kill switch</Button>
              </>
            ) : isAdmin
              ? <Button kind="ghost" disabled={act.busy} onClick={() => window.confirm("Release the kill switch and allow new orders again?") && act.run(() => post("/api/risk/kill-switch", { engage: false }), "Kill switch released.").then(reload)}>Release kill switch</Button>
              : <p className="text-sm text-warn">Only an administrator can release the kill switch.</p>}
            <Msg msg={act.msg} />
          </div>
        </Panel>
        <Panel title="Limit utilisation" className="xl:col-span-2">
          {!u ? <p className="text-sm text-dim">{data.error ?? "No account to measure yet."}</p> : (
            <div className="grid gap-4 sm:grid-cols-2">
              <Meter label="Daily loss" value={u.daily_loss.value} limit={u.daily_loss.limit} />
              <Meter label="Drawdown from peak" value={u.drawdown.value} limit={u.drawdown.limit} />
              <Meter label="Portfolio exposure (gross leverage)" value={u.gross_leverage.value} limit={u.gross_leverage.limit} format={(v) => `${v.toFixed(2)}×`} />
              <Meter label="Margin utilisation" value={u.margin.value} limit={u.margin.limit} />
              <Meter label="Largest single-asset exposure" value={u.largest_asset_exposure.value} limit={u.largest_asset_exposure.limit} />
              <Meter label="Open positions" value={u.open_positions.value} limit={u.open_positions.limit} format={(v) => String(v)} />
            </div>
          )}
        </Panel>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Configured limits" right={<Link href="/settings" className="text-xs text-accent hover:underline">Edit</Link>}>
          <dl className="grid grid-cols-2 gap-y-1.5 font-mono text-xs">
            {[["Risk per trade", pct(limits.risk_per_trade)], ["Max daily loss", pct(limits.max_daily_loss)], ["Max drawdown", pct(limits.max_drawdown)],
              ["Per asset", pct(limits.max_exposure_per_asset, 0)], ["Per strategy", pct(limits.max_exposure_per_strategy, 0)], ["Per broker", pct(limits.max_exposure_per_broker, 0)],
              ["Gross leverage", `${limits.max_gross_leverage}×`], ["Margin utilisation", pct(limits.max_margin_utilization, 0)], ["Concurrent positions", limits.max_concurrent_positions],
              ["Correlated exposure", `${pct(limits.max_correlated_exposure, 0)} above ρ ${limits.correlation_threshold}`], ["Volatility target", limits.target_volatility ? pct(limits.target_volatility, 0) : "off"]]
              .map(([k, v]) => <div key={String(k)} className="contents"><dt className="text-dim">{k}</dt><dd>{v}</dd></div>)}
          </dl>
        </Panel>
        <Panel title="Circuit breakers" className="xl:col-span-2">
          <Table empty="No circuit breaker is tripped." head={["Breaker", "Since", "Detail", ""]}
            rows={Object.entries(status.breakers as Record<string, Json>).map(([name, b]) => [<Badge key="n" kind="down">{name}</Badge>, when(b.since),
              <span key="d" className="text-dim">{Object.entries(b).filter(([k]) => k !== "since").map(([k, v]) => `${k}: ${typeof v === "number" ? pct(v) : v}`).join(" · ")}</span>,
              isAdmin ? <Button key="r" kind="ghost" onClick={() => window.confirm(`Reset the ${name} breaker and allow new orders?`) && act.run(() => post(`/api/risk/breakers/${name}/reset`), "Breaker reset.").then(reload)}>Reset</Button>
                : name === "daily_loss" ? "clears next day" : "admin reset"])} />
          <p className="mt-2 text-xs text-dim">The daily-loss breaker clears at the next UTC day. Drawdown and reconciliation breakers stay tripped until an administrator resets them.</p>
        </Panel>
      </div>
      <Panel title="Risk events">
        <Table empty="No risk events recorded." head={["Time", "Severity", "Event", "Detail"]}
          rows={data.events.map((e: Json) => [when(e.ts), <Badge key="s" kind={e.severity === "critical" ? "down" : e.severity === "warning" ? "warn" : "dim"}>{e.severity}</Badge>, e.kind.replace(/_/g, " "),
            <span key="d" className="block max-w-3xl whitespace-normal text-dim">{e.detail.reasons ? `${e.detail.side} ${e.detail.qty} ${e.detail.instrument_id} (${e.detail.strategy}): ${e.detail.reasons.join("; ")}` : JSON.stringify(e.detail)}</span>])} />
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <Risk user={user} />}</Shell>;
}
