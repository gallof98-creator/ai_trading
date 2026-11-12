"use client";
import type { Json } from "@/components/ui";

type Prop = { type?: string; anyOf?: { type: string }[]; default?: unknown; description?: string; title?: string };

/** Form generated from a strategy's JSON schema. Numbers, booleans and nullable numbers get typed inputs; anything else is edited as JSON. */
export function ParamForm({ schema, value, onChange }: { schema: { properties: Record<string, Prop> }; value: Record<string, Json>; onChange: (v: Record<string, Json>) => void }) {
  const set = (k: string, v: Json) => onChange({ ...value, [k]: v });
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {Object.entries(schema.properties).map(([name, p]) => {
        const types = p.type ? [p.type] : (p.anyOf ?? []).map((a) => a.type);
        const numeric = types.includes("integer") || types.includes("number");
        const nullable = types.includes("null");
        const v = value[name];
        const label = name.replace(/_/g, " ");
        if (types.includes("boolean")) {
          return (
            <label key={name} className="flex items-center gap-2 text-xs text-dim" title={p.description}>
              <input type="checkbox" className="!w-auto" checked={!!v} onChange={(e) => set(name, e.target.checked)} /> {label}
            </label>
          );
        }
        if (numeric) {
          return (
            <label key={name} className="block text-xs text-dim" title={p.description}>{label}{nullable ? " (blank = off)" : ""}
              <input type="number" step="any" className="mt-1" value={v ?? ""} onChange={(e) => set(name, e.target.value === "" ? (nullable ? null : undefined) : Number(e.target.value))} />
            </label>
          );
        }
        if (types.includes("string")) {
          return <label key={name} className="block text-xs text-dim" title={p.description}>{label}<input className="mt-1" value={v ?? ""} onChange={(e) => set(name, e.target.value)} /></label>;
        }
        return (
          <label key={name} className="block text-xs text-dim" title={p.description}>{label} (JSON)
            <input className="mt-1" defaultValue={JSON.stringify(v ?? {})} onBlur={(e) => { try { set(name, JSON.parse(e.target.value)); } catch { /* keep previous value */ } }} />
          </label>
        );
      })}
    </div>
  );
}
