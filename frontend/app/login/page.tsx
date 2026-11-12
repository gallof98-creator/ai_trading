"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/services/api";

export default function Login() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error && err.message !== "Failed to fetch" ? err.message : "Cannot reach the API.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded border border-line bg-panel p-6">
        <h1 className="font-mono text-sm font-semibold tracking-widest text-accent">QUANT AI TRADER</h1>
        <label className="block text-xs text-dim">Email
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} className="mt-1" />
        </label>
        <label className="block text-xs text-dim">Password
          <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="mt-1" />
        </label>
        {error && <p role="alert" className="text-sm text-down">{error}</p>}
        <button disabled={busy} className="w-full rounded bg-accent py-2 text-sm font-semibold text-bg disabled:opacity-50">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
