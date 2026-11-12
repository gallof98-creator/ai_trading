"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Panel, Shell } from "@/components/Shell";
import { api, UserSettings } from "@/services/api";

function Overview() {
  const [s, setS] = useState<UserSettings | null>(null);
  useEffect(() => { api<UserSettings>("/api/settings").then(setS).catch(() => {}); }, []);
  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel title="Account configuration">
        {!s ? <p className="text-sm text-dim">Loading…</p> : (
          <dl className="grid grid-cols-2 gap-y-2 font-mono text-sm">
            <dt className="text-dim">Base currency</dt><dd>{s.base_currency}</dd>
            <dt className="text-dim">Paper capital</dt>
            <dd>{s.initial_paper_capital === null
              ? <Link href="/settings" className="text-warn underline">Not configured — set it</Link>
              : `${s.initial_paper_capital.toLocaleString("en-GB")} ${s.base_currency}`}</dd>
            <dt className="text-dim">Trading mode</dt><dd>{s.trading_mode}</dd>
            <dt className="text-dim">Risk per trade</dt><dd>{pct(s.risk.risk_per_trade)}</dd>
            <dt className="text-dim">Max daily loss</dt><dd>{pct(s.risk.max_daily_loss)}</dd>
            <dt className="text-dim">Max drawdown</dt><dd>{pct(s.risk.max_drawdown)}</dd>
            <dt className="text-dim">Emergency policy</dt><dd>{s.risk.emergency_policy}</dd>
          </dl>
        )}
      </Panel>
      <Panel title="Build status">
        <p className="text-sm leading-6 text-dim">
          Authentication, settings and encrypted broker credentials are live. Portfolio value, P&amp;L, positions,
          strategies and broker panels appear here as their engines are built; nothing on this page is placeholder data.
        </p>
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{() => <Overview />}</Shell>;
}
