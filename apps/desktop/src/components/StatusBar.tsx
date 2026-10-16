import { useEffect, useState } from "react";
import { API_BASE, getHealth } from "../lib/api";
import type { Health } from "../lib/types";

/** Number of Boom tools (counted from agent/src/environment.py, as of 2026-10-16). */
const TOOL_COUNT = 139;
/** Fallback when /api/skills is unavailable — count of skills/ folders in the repo. */
const SKILL_FALLBACK = 25;
/** Poll health every 5 seconds. */
const POLL_MS = 5_000;

/** Seconds -> "1h 23m" / "12m 5s" / "45s" */
function formatUptime(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Real-time status bar: API online/offline, tools, skills, uptime. */
export default function StatusBar() {
  const [health, setHealth] = useState<Health | null>(null);
  const [online, setOnline] = useState(false);
  const [skills, setSkills] = useState<number>(SKILL_FALLBACK);
  const [since, setSince] = useState<number | null>(null); // first successful poll (ms)
  const [, tick] = useState(0); // force a re-render every second to update uptime

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const h = await getHealth();
        if (cancelled) return;
        setHealth(h);
        setOnline(true);
        setSince((prev) => prev ?? Date.now());
        // Fetch the skill count; keep the fallback when the API is unavailable.
        try {
          const res = await fetch(`${API_BASE}/api/skills`);
          if (res.ok) {
            const body: unknown = await res.json();
            const arr = Array.isArray(body)
              ? body
              : (body as { skills?: unknown[] }).skills;
            if (Array.isArray(arr)) setSkills(arr.length);
          }
        } catch { /* ignore, keep the fallback */ }
      } catch {
        if (!cancelled) {
          setOnline(false);
          setHealth(null);
        }
      }
    };
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Update the uptime clock every second while online.
  useEffect(() => {
    if (!online) return;
    const id = setInterval(() => tick((n) => n + 1), 1_000);
    return () => clearInterval(id);
  }, [online]);

  const uptime = since !== null ? formatUptime(Math.floor((Date.now() - since) / 1000)) : "—";

  return (
    <div className="flex items-center gap-4 rounded-lg border border-neutral-800 bg-neutral-950 px-4 py-2 text-sm text-neutral-300">
      {/* Status dot: green = online, red = offline */}
      <span className="flex items-center gap-2" title={online ? "API online" : "API offline"}>
        <span
          className={`inline-block h-2.5 w-2.5 rounded-full ${online ? "bg-zinc-500" : "bg-zinc-500"}`}
        />
        <span className="font-medium text-neutral-100">{online ? "Online" : "Offline"}</span>
      </span>
      <span className="text-neutral-700">|</span>
      <span>Tools: <strong className="text-neutral-100">{TOOL_COUNT}</strong></span>
      <span>Skills: <strong className="text-neutral-100">{skills}</strong></span>
      <span>Uptime: <strong className="text-neutral-100">{uptime}</strong></span>
      {health && (
        <>
          <span className="ml-auto truncate text-xs text-neutral-400">
            v{health.version} · {health.model}
          </span>
        </>
      )}
    </div>
  );
}
