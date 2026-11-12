"use client";
import { useState } from "react";
import { CalibrationChart, PlainBars } from "@/components/charts/Charts";
import { Shell } from "@/components/Shell";
import { Badge, Button, Field, type Json, Msg, Notice, num, Panel, pct, post, Stat, Table, TIMEFRAMES, tone, useAction, useApi, when } from "@/components/ui";
import type { User } from "@/services/api";

const MODELS = ["logistic_regression", "random_forest", "gradient_boosting", "xgboost", "neural_network"];
const statusKind = (s: string) => (s === "validated" || s === "applied" ? "up" : s === "rejected" || s === "failed_validation" ? "down" : "dim");

function QuantAI({ user }: { user: User }) {
  const instruments = useApi("/api/instruments");
  const library = useApi("/api/strategies");
  const brokers = useApi("/api/brokers/status");
  const [iid, setIid] = useState("CRYPTO:BTC/USDT");
  const [tf, setTf] = useState("1h");
  const q = `instrument_id=${encodeURIComponent(iid)}&timeframe=${tf}`;
  const regime = useApi(`/api/ai/regime?${q}`);
  const models = useApi("/api/ai/models");
  const reports = useApi("/api/ai/reports");
  const proposals = useApi("/api/ai/proposals");
  const [train, setTrain] = useState({ model: "gradient_boosting", horizon: 20 });
  const [suggest, setSuggest] = useState({ strategy: "ema_crossover", n_trials: 20, fx: "USDT=0.92, USD=0.92" });
  const [openModel, setOpenModel] = useState<Json>(null);
  const [drift, setDrift] = useState<Json>(null);
  const [openReport, setOpenReport] = useState<Json>(null);
  const act = useAction();
  const canWrite = user.role !== "viewer";
  const paper = (brokers.data ?? []).filter((b: Json) => !b.data_only);
  const fx = () => Object.fromEntries(suggest.fx.split(",").map((s) => s.split("=").map((x) => x.trim())).filter((p) => p.length === 2 && p[1]).map(([k, v]) => [k.toUpperCase(), Number(v)]));

  return (
    <div className="space-y-4">
      <Panel title="Research scope" right={<Msg msg={act.msg} />}>
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Instrument"><select value={iid} onChange={(e) => setIid(e.target.value)}>{(instruments.data ?? []).map((i: Json) => <option key={i.id}>{i.id}</option>)}</select></Field>
          <Field label="Timeframe"><select value={tf} onChange={(e) => setTf(e.target.value)}>{TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select></Field>
        </div>
      </Panel>
      <Panel title="Market regime analysis" right={regime.data && <Badge kind={regime.data.data_kind === "simulated" ? "accent" : "up"}>{regime.data.data_kind} data</Badge>}>
        {regime.error ? <Notice>{regime.error}</Notice> : !regime.data ? <p className="text-sm text-dim">Loading…</p> : (
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Trend" value={regime.data.current.trend.replace(/_/g, " ")} />
              <Stat label="Volatility level" value={regime.data.current.vol_level.replace(/_/g, " ")} />
              <Stat label="Volatility dynamics" value={regime.data.current.vol_dynamics.replace(/_/g, " ")} />
              <Stat label="In this regime for" value={`${regime.data.bars_in_current_regime} bars`} hint={`as of ${when(regime.data.as_of)}`} />
            </div>
            <div>
              <h3 className="mb-1 text-xs text-dim">Share of history spent in each regime</h3>
              <PlainBars layout="vertical" height={200} label="Share of bars (%)" xKey="regime" yKey="share"
                data={Object.entries(regime.data.distribution as Record<string, number>).map(([k, v]) => ({ regime: k.replace(/_/g, " "), share: v * 100 }))} />
            </div>
          </div>
        )}
      </Panel>
      <Panel title="Machine-learning models">
        <div className="space-y-4">
          <p className="text-xs text-dim">Models predict whether price reaches +2 ATR before −2 ATR within the horizon. They are scored only on purged out-of-sample folds. A model that does not beat the no-skill baseline is marked rejected and is not saved. Models are research outputs: they do not place or filter orders.</p>
          {canWrite && (
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Model"><select value={train.model} onChange={(e) => setTrain({ ...train, model: e.target.value })}>{MODELS.map((m) => <option key={m}>{m}</option>)}</select></Field>
              <Field label="Horizon (bars)"><input type="number" min="2" max="200" value={train.horizon} onChange={(e) => setTrain({ ...train, horizon: Number(e.target.value) })} /></Field>
              <Button disabled={act.busy} onClick={() => act.run(async () => { setOpenModel(await post("/api/ai/models/train", { instrument_id: iid, timeframe: tf, ...train })); setDrift(null); models.reload(); }, "Training finished.")}>{act.busy ? "Working…" : "Train and validate"}</Button>
            </div>
          )}
          <Table max="max-h-60" head={["Trained", "Model", "Status", "OOS AUC", "Brier / baseline", "Calibration error", "Calibrated", ""]}
            rows={(models.data ?? []).map((m: Json) => [when(m.created_at), m.title, <Badge key="s" kind={statusKind(m.status)}>{m.status}</Badge>, num(m.payload.metrics.auc, 3),
              `${num(m.payload.metrics.brier, 4)} / ${num(m.payload.metrics.baseline_brier, 4)}`, num(m.payload.metrics.calibration_error, 3), m.payload.calibrated ? "yes" : "no",
              <button key="o" className="text-accent hover:underline" onClick={() => { setOpenModel(m); setDrift(null); }}>Details</button>])} />
          {openModel && (
            <div className="space-y-3 rounded border border-line p-3">
              <div className="flex flex-wrap items-center gap-3">
                <h3 className="text-sm font-semibold">{openModel.title}</h3><Badge kind={statusKind(openModel.status)}>{openModel.status}</Badge>
                <Button kind="ghost" onClick={() => act.run(async () => setDrift(await post(`/api/ai/models/${openModel.id}/drift`, undefined, "GET")), "Drift check done.")}>Check feature drift</Button>
              </div>
              <p className={`text-sm ${openModel.payload.validated ? "text-up" : "text-warn"}`}>{openModel.payload.verdict}</p>
              {drift && <Notice kind={drift.status === "alert" ? "down" : drift.status === "warning" ? "warn" : "accent"}>Feature drift: {drift.status} (max PSI {num(drift.max_psi, 3)} over {drift.basis}){drift.drifted_features.length ? ` — ${drift.drifted_features.join(", ")}` : ""}</Notice>}
              <div className="grid gap-4 lg:grid-cols-2">
                <div><h4 className="mb-1 text-xs text-dim">Calibration: predicted probability vs observed frequency (out of sample)</h4><CalibrationChart data={openModel.payload.calibration_curve} /></div>
                <div><h4 className="mb-1 text-xs text-dim">Feature importance (permutation, last test fold)</h4>
                  <PlainBars layout="vertical" height={220} label="Importance" xKey="feature" yKey="importance" data={openModel.payload.feature_importance.slice(0, 8)} /></div>
              </div>
            </div>
          )}
        </div>
      </Panel>
      <Panel title="Optimisation results and proposed configurations">
        <div className="space-y-4">
          <p className="text-xs text-dim">The analyst searches strategy parameters on the first 60% of the data and judges candidates only on the last 40%. Proposals can contain strategy parameters only. Applying one requires an administrator and never activates a strategy or changes risk settings.</p>
          {canWrite && (
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Strategy"><select value={suggest.strategy} onChange={(e) => setSuggest({ ...suggest, strategy: e.target.value })}>{(library.data ?? []).map((s: Json) => <option key={s.name}>{s.name}</option>)}</select></Field>
              <Field label="Trials"><input type="number" min="5" max="60" value={suggest.n_trials} onChange={(e) => setSuggest({ ...suggest, n_trials: Number(e.target.value) })} /></Field>
              <Field label="Assumed FX to base"><input value={suggest.fx} onChange={(e) => setSuggest({ ...suggest, fx: e.target.value })} /></Field>
              <Button disabled={act.busy} onClick={() => act.run(() => post("/api/ai/proposals", { strategy: suggest.strategy, instrument_id: iid, timeframe: tf, n_trials: suggest.n_trials, static_fx: fx() }).then(proposals.reload), "Search finished.")}>Search and validate</Button>
            </div>
          )}
          <Table head={["Created", "Target", "Parameters", "In-sample Sharpe", "OOS return", "OOS trades", "Validation", ""]}
            rows={(proposals.data ?? []).map((p: Json) => [when(p.created_at), p.title, <span key="p" className="break-all">{JSON.stringify(p.payload.params)}</span>, num(p.payload.in_sample?.sharpe_ratio),
              <span key="o" className={tone(p.payload.out_of_sample?.total_return)}>{pct(p.payload.out_of_sample?.total_return)}</span>, p.payload.out_of_sample?.number_of_trades ?? "—",
              <span key="v" title={(p.payload.validation_reasons ?? []).join("; ")}><Badge kind={statusKind(p.status)}>{p.status.replace(/_/g, " ")}</Badge></span>,
              <span key="a" className="flex gap-2">
                {user.role === "admin" && p.status === "validated" && paper.length > 0 && <button className="text-accent hover:underline" onClick={() => window.confirm(`Apply these parameters to ${paper[0].id}? The deployment stays inactive until you activate it.`) && act.run(() => post(`/api/ai/proposals/${p.id}/apply`, { broker_id: paper[0].id }).then(proposals.reload), "Applied (inactive).")}>Apply</button>}
                {canWrite && !["dismissed", "applied"].includes(p.status) && <button className="text-dim hover:underline" onClick={() => act.run(() => post(`/api/ai/proposals/${p.id}/dismiss`).then(proposals.reload), "Dismissed.")}>Dismiss</button>}
              </span>])} />
        </div>
      </Panel>
      <Panel title="AI research reports">
        <Table max="max-h-48" empty="No reports yet. Create one from a backtest result." head={["Created", "Title", "Reproduced", ""]}
          rows={(reports.data ?? []).map((r: Json) => [when(r.created_at), r.title, r.payload.reproduced ? "yes" : "no", <button key="o" className="text-accent hover:underline" onClick={() => setOpenReport(r)}>Read</button>])} />
        {openReport && <pre className="mt-4 max-h-[32rem] overflow-auto whitespace-pre-wrap rounded border border-line bg-bg p-4 text-xs leading-5">{openReport.payload.markdown}</pre>}
      </Panel>
    </div>
  );
}

export default function Page() {
  return <Shell>{(user) => <QuantAI user={user} />}</Shell>;
}
