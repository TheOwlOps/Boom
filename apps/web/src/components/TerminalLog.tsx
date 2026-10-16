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

/** Reduce the /api/logs body to string[]: accepts {lines: []} or a bare array. */
function toLines(body: unknown): string[] {
  const arr = Array.isArray(body) ? body : (body as { lines?: unknown }).lines;
  if (!Array.isArray(arr)) return [];
  return arr.filter((l): l is string => typeof l === "string");
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
    const seen = new Set<string>();
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/logs`);
        if (!res.ok || cancelled) return;
        const fresh = toLines(await res.json()).filter((l) => !seen.has(l));
        if (fresh.length === 0) return;
        fresh.forEach((l) => seen.add(l));
        setPolled((prev) => [...prev, ...fresh].slice(-maxLines));
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
