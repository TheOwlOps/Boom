import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../lib/api";

interface TerminalLogProps {
  /** Log lines from props (used when /api/logs is unavailable). */
  logs?: string[];
  /** /api/logs poll interval (ms). Set 0 or negative to disable polling. Default 2000. */
  pollIntervalMs?: number;
  /** Maximum lines to keep so the DOM doesn't bloat. Default 1000. */
  maxLines?: number;
}

const DEFAULT_POLL_MS = 2_000;
const DEFAULT_MAX_LINES = 1_000;

/**
 * Reduce the /api/logs body to string[].
 * Accepts the backend shape {logs: [{ts, msg}]}, the legacy {lines: []},
 * or a bare array. Anything else yields [].
 */
function toLines(body: unknown): string[] {
  const rec = (Array.isArray(body) ? null : body) as { logs?: unknown; lines?: unknown } | null;
  const arr = Array.isArray(body) ? body : (rec?.logs ?? rec?.lines);
  if (!Array.isArray(arr)) return [];
  const out: string[] = [];
  for (const l of arr) {
    if (typeof l === "string") {
      out.push(l);
      continue;
    }
    if (typeof l === "object" && l !== null) {
      const o = l as Record<string, unknown>;
      if (typeof o.msg === "string") {
        out.push(typeof o.ts === "string" && o.ts.length > 0 ? `[${o.ts}] ${o.msg}` : o.msg);
      }
    }
  }
  return out;
}

/**
 * Terminal log: black frame, mono font, auto-scrolls to the bottom.
 * Polls /api/logs per pollIntervalMs; always renders logs from props too.
 */
export default function TerminalLog({ logs = [], pollIntervalMs = DEFAULT_POLL_MS, maxLines = DEFAULT_MAX_LINES }: TerminalLogProps) {
  const [polled, setPolled] = useState<string[]>([]);
  const boxRef = useRef<HTMLDivElement>(null);

  // Poll /api/logs; silently keep the current lines when the endpoint is missing.
  useEffect(() => {
    if (pollIntervalMs <= 0) return;
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/logs`);
        if (!res.ok || cancelled) return;
        const fresh = toLines(await res.json());
        if (fresh.length === 0 || cancelled) return;
        // Dedupe against what is already on screen inside the state updater,
        // so no unbounded "seen" set grows across polls in long sessions.
        setPolled((prev) => {
          const prevSet = new Set(prev);
          const added = fresh.filter((l) => !prevSet.has(l));
          if (added.length === 0) return prev;
          return [...prev, ...added].slice(-maxLines);
        });
      } catch { /* API unavailable — component still renders logs from props */ }
    };
    void load();
    const id = setInterval(() => void load(), pollIntervalMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [pollIntervalMs, maxLines]);

  const lines = [...logs, ...polled].slice(-maxLines);

  // Auto-scroll to the newest line.
  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  return (
    <div
      ref={boxRef}
      className="h-80 overflow-y-auto rounded-lg border border-neutral-800 bg-black p-3 font-mono text-xs leading-5"
      aria-label="Terminal log"
    >
      {lines.length === 0 ? (
        <span className="text-neutral-600">// no logs yet — waiting for /api/logs or the logs prop</span>
      ) : (
        lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-words text-neutral-300">
            <span className="mr-2 select-none text-neutral-700">{String(i + 1).padStart(4, " ")}</span>
            {line}
          </div>
        ))
      )}
    </div>
  );
}
