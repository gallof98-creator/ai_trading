"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, Health, User } from "@/services/api";

// Modules without an href are not built yet; they are listed so the gap is visible, not hidden.
const NAV: { label: string; href?: string }[] = [
  { label: "Overview", href: "/" }, { label: "Markets" }, { label: "Strategies" }, { label: "Quant AI" },
  { label: "Backtesting" }, { label: "Portfolio" }, { label: "Execution" }, { label: "Risk Center" },
  { label: "Settings", href: "/settings" },
];

export function Shell({ children }: { children: (user: User) => React.ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    api<User>("/api/auth/me").then(setUser).catch((e) => {
      if (e instanceof ApiError && e.status === 401) router.replace("/login");
      else setOffline(true);
    });
    const poll = () => api<Health>("/health").then((h) => { setHealth(h); setOffline(false); }).catch(() => setOffline(true));
    poll();
    const id = setInterval(poll, 10_000);
    return () => clearInterval(id);
  }, [router]);

  const logout = async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); router.replace("/login"); };
  const dot = offline ? "bg-down" : health?.status === "ok" ? "bg-up" : "bg-warn";

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b border-line bg-panel md:w-52 md:border-b-0 md:border-r">
        <div className="px-4 py-4 font-mono text-sm font-semibold tracking-widest text-accent">QUANT AI TRADER</div>
        <nav className="flex flex-wrap gap-1 px-2 pb-3 md:flex-col">
          {NAV.map((n) => n.href ? (
            <Link key={n.label} href={n.href} className={`rounded px-3 py-1.5 text-sm ${path === n.href ? "bg-line text-ink" : "text-dim hover:text-ink"}`}>{n.label}</Link>
          ) : (
            <span key={n.label} title="Not built yet" className="cursor-not-allowed rounded px-3 py-1.5 text-sm text-dim/40">{n.label}</span>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-line bg-panel px-4 py-2 font-mono text-xs text-dim">
          <span className="flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${dot}`} />API {offline ? "unreachable" : health?.status ?? "…"}</span>
          <span>DB {health ? (health.database ? "ok" : "down") : "…"}</span>
          <span className="rounded border border-up/40 px-1.5 text-up">PAPER</span>
          <span className={health?.live_trading_authorized ? "text-warn" : ""}>LIVE {health?.live_trading_authorized ? "AUTHORIZED" : "disabled"}</span>
          <span className="ml-auto">{user ? `${user.email} · ${user.role}` : ""}</span>
          {user && <button onClick={logout} className="text-dim hover:text-ink">Sign out</button>}
        </header>
        <main className="flex-1 p-4 md:p-6">{user ? children(user) : <p className="text-sm text-dim">{offline ? "Cannot reach the API." : "Loading…"}</p>}</main>
      </div>
    </div>
  );
}

export function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded border border-line bg-panel">
      <h2 className="border-b border-line px-4 py-2 text-xs font-semibold uppercase tracking-wider text-dim">{title}</h2>
      <div className="p-4">{children}</div>
    </section>
  );
}
