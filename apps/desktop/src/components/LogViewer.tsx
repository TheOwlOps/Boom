import { useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "../lib/api";

/* ---------------------------------------------------------------------------
 * LogViewer — a full-featured log browser for the Boom gateway's /api/logs.
 *
 * Backend contract (crates/boom-core (Rust)):
 *   GET /api/logs?limit=N[&offset=M]
 *   -> { "logs": [{ "ts": "2026-10-16T12:30:01", "msg": "..." }, ...] }
 *   Entries come out oldest-first (they are slices of an append-only buffer).
 *   NOTE: upstream entries only carry `ts` and `msg`; level and module are
 *   inferred client-side from the message text (see inferLevel/inferModule).
 * ------------------------------------------------------------------------- */

export type LogLevel = "INFO" | "WARN" | "ERROR" | "DEBUG";

export interface LogEntry {
  /** Unique client-side key: "<ts>|<msg>|<index>". */
  key: string;
  /** Raw ISO timestamp from the backend. */
  timestamp: string;
  /** Level inferred from the message text. */
  level: LogLevel;
  /** Module/source inferred from the message text. */
  module: string;
  /** The raw log message. */
  message: string;
}

type LevelFilter = "ALL" | LogLevel;

const DEFAULT_LIMIT = 200;
const PAGE_SIZE = 200;
const DEFAULT_POLL_MS = 3_000;

/* ---------------------------------------------------------------- helpers */

/** Narrow an unknown value to a parsed LogEntry (runtime validation). */
function parseLogEntry(v: unknown, index: number): LogEntry {
  if (typeof v !== "object" || v === null) {
    throw new Error(`malformed response: logs[${index}] must be an object`);
  }
  const rec = v as Record<string, unknown>;
  if (typeof rec.ts !== "string") {
    throw new Error(`malformed response: logs[${index}].ts must be a string`);
  }
  if (typeof rec.msg !== "string") {
    throw new Error(`malformed response: logs[${index}].msg must be a string`);
  }
  const message = rec.msg;
  return {
    key: `${rec.ts}|${message}|${index}`,
    timestamp: rec.ts,
    level: inferLevel(message),
    module: inferModule(message),
    message,
  };
}

/** Extract the server's {"error"} message for a failed response. */
async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (typeof body === "object" && body !== null && typeof (body as { error?: unknown }).error === "string") {
      return (body as { error: string }).error;
    }
  } catch {
    /* body was not JSON */
  }
  return `HTTP ${res.status} ${res.statusText}`.trim();
}

/**
 * Fetch `limit` entries starting `offset` entries back from the newest.
 * Offset is a no-op on backends that don't implement it (harmless query param).
 */
async function fetchLogPage(limit: number, offset: number): Promise<{ entries: LogEntry[]; hasMore: boolean }> {
  const res = await fetch(`${API_BASE}/api/logs?limit=${limit}&offset=${offset}`);
  if (!res.ok) throw new Error(await readErrorMessage(res));
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error("malformed response: /api/logs did not return JSON");
  }
  if (typeof body !== "object" || body === null || !Array.isArray((body as { logs?: unknown }).logs)) {
    throw new Error('malformed response: "logs" must be an array');
  }
  const raw = (body as { logs: unknown[] }).logs;
  const entries = raw.map((v, i) => parseLogEntry(v, i));
  return { entries, hasMore: entries.length >= limit };
}

/**
 * Infer the level from message text. The backend only stores plain strings,
 * so we match common prefixes: "[ERROR] ...", "WARN: ...", "debug ...", etc.
 * Anything unrecognised is treated as INFO.
 */
function inferLevel(message: string): LogLevel {
  const head = message.slice(0, 24).toUpperCase();
  if (/\b(ERROR|ERR|FATAL|CRITICAL)\b/.test(head)) return "ERROR";
  if (/\b(WARN|WARNING)\b/.test(head)) return "WARN";
  if (/\b(DEBUG|DBG|TRACE)\b/.test(head)) return "DEBUG";
  return "INFO";
}

/**
 * Infer the module/source from message text. Matches "[module] msg",
 * "module.sub: msg", or "/api/..." (gateway). Level words (ERROR, WARN, …)
 * are never treated as modules. Falls back to "app".
 */
function inferModule(message: string): string {
  const LEVEL_WORDS = /\b(ERROR|ERR|FATAL|CRITICAL|WARN|WARNING|INFO|INF|DEBUG|DBG|TRACE)\b/i;
  const bracketed = /^\s*\[([^\]\s]{1,32})\]\s*(.*)$/.exec(message);
  if (bracketed) {
    // "[ERROR] disk full" → the bracket is a level, not a module: look at the rest.
    if (LEVEL_WORDS.test(bracketed[1])) return inferModule(bracketed[2] || "");
    return bracketed[1].toLowerCase();
  }
  const prefixed = /^\s*([a-zA-Z][\w.-]{1,32})\s*:/.exec(message);
  if (prefixed && !LEVEL_WORDS.test(prefixed[1])) {
    return prefixed[1].toLowerCase();
  }
  if (/^\s*\//.test(message)) return "gateway";
  return "app";
}

/** Format an ISO timestamp as HH:mm:ss (local time); pass through if unparseable. */
function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

/* ------------------------------------------------------------------ theme */

const LEVEL_STYLES: Record<LogLevel, string> = {
  INFO: "bg-[color-mix(in_srgb,var(--muted)_15%,transparent)] text-[var(--muted)] border-[color-mix(in_srgb,var(--border)_30%,transparent)]",
  WARN: "bg-[color-mix(in_srgb,var(--muted)_15%,transparent)] text-[var(--muted)] border-[color-mix(in_srgb,var(--border)_30%,transparent)]",
  ERROR: "bg-[color-mix(in_srgb,var(--muted)_15%,transparent)] text-[var(--muted)] border-[color-mix(in_srgb,var(--border)_30%,transparent)]",
  DEBUG: "bg-[color-mix(in_srgb,var(--muted)_15%,transparent)] text-[var(--muted)] border-[color-mix(in_srgb,var(--border)_30%,transparent)]",
};

const LEVEL_FILTERS: LevelFilter[] = ["ALL", "INFO", "WARN", "ERROR", "DEBUG"];

/* -------------------------------------------------------------- component */

export interface LogViewerProps {
  /** How many entries to request on first load. Default 200 (backend cap). */
  initialLimit?: number;
  /** Polling interval in ms while not paused. 0 disables polling. Default 3000. */
  pollIntervalMs?: number;
}

export default function LogViewer({ initialLimit = DEFAULT_LIMIT, pollIntervalMs = DEFAULT_POLL_MS }: LogViewerProps) {
  // All entries known to the client, oldest first.
  const [entries, setEntries] = useState<LogEntry[]>([]);
  // offset == how far back from the newest entry we've loaded so far.
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [levelFilter, setLevelFilter] = useState<LevelFilter>("ALL");
  const [search, setSearch] = useState("");
  const [moduleFilter, setModuleFilter] = useState<string>("ALL");

  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);

  const boxRef = useRef<HTMLDivElement>(null);
  // Freshest known entry key, used to avoid re-rendering on empty polls.
  const newestKeyRef = useRef<string | null>(null);

  /** Merge newly fetched entries: dedupe by key, keep chronological order. */
  const mergeEntries = (fresh: LogEntry[]) => {
    setEntries((prev) => {
      if (fresh.length === 0) return prev;
      const seen = new Set(prev.map((e) => e.key));
      const added = fresh.filter((e) => !seen.has(e.key));
      if (added.length === 0) return prev;
      return [...prev, ...added].sort((a, b) => (a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0));
    });
  };

  /** Initial load + manual retry. */
  const loadInitial = async () => {
    setLoading(true);
    setError(null);
    try {
      const { entries: fresh, hasMore: more } = await fetchLogPage(initialLimit, 0);
      setEntries(fresh);
      setOffset(initialLimit);
      setHasMore(more);
      newestKeyRef.current = fresh.length > 0 ? fresh[fresh.length - 1].key : null;
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load logs");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadInitial();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialLimit]);

  /** Load older entries (increases the offset into the buffer). */
  const loadMore = async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const { entries: fresh, hasMore: more } = await fetchLogPage(PAGE_SIZE, offset);
      mergeEntries(fresh);
      setOffset((o) => o + PAGE_SIZE);
      setHasMore(more);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load more logs");
    } finally {
      setLoadingMore(false);
    }
  };

  // Poll the newest entries every pollIntervalMs unless paused.
  useEffect(() => {
    if (paused || pollIntervalMs <= 0) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const { entries: fresh } = await fetchLogPage(initialLimit, 0);
        if (cancelled || fresh.length === 0) return;
        const newest = fresh[fresh.length - 1];
        if (newest.key === newestKeyRef.current) return; // nothing new
        newestKeyRef.current = newest.key;
        mergeEntries(fresh);
      } catch {
        /* polling is best-effort; surfaced errors only come from explicit loads */
      }
    };
    const id = setInterval(() => void tick(), pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [paused, pollIntervalMs, initialLimit]);

  // Auto-scroll to the newest line when enabled.
  useEffect(() => {
    if (!autoScroll) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries.length, autoScroll]);

  /* ------------------------------------------------------------ filtering */

  // Module list is derived from the entries actually present.
  const modules = useMemo(() => {
    const s = new Set(entries.map((e) => e.module));
    return [...s].sort();
  }, [entries]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return entries.filter((e) => {
      if (levelFilter !== "ALL" && e.level !== levelFilter) return false;
      if (moduleFilter !== "ALL" && e.module !== moduleFilter) return false;
      if (q && !e.message.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [entries, levelFilter, moduleFilter, search]);

  const levelCounts = useMemo(() => {
    const counts: Record<LogLevel, number> = { INFO: 0, WARN: 0, ERROR: 0, DEBUG: 0 };
    for (const e of entries) counts[e.level] += 1;
    return counts;
  }, [entries]);

  /* ------------------------------------------------------------------ UI */

  return (
    <div className="flex h-full min-h-0 flex-col rounded-lg border border-[var(--border)] bg-[var(--surface)] text-[var(--text)]">
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] p-3">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by level">
          {LEVEL_FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setLevelFilter(f)}
              className={`rounded border px-2 py-0.5 font-mono text-xs transition-colors ${
                levelFilter === f
                  ? "border-[var(--border)] bg-[var(--surface-2)] text-[var(--text)]"
                  : "border-[var(--border)] bg-[var(--surface)] text-[var(--muted)] hover:border-[var(--muted)]"
              }`}
            >
              {f}
              {f !== "ALL" && <span className="ml-1 text-[var(--muted)]">{levelCounts[f]}</span>}
            </button>
          ))}
        </div>

        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search messages…"
          aria-label="Search log messages"
          className="min-w-40 flex-1 rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-1 font-mono text-xs text-[var(--text)] placeholder-[var(--muted)] focus:border-[var(--muted)] focus:outline-none"
        />

        <select
          value={moduleFilter}
          onChange={(e) => setModuleFilter(e.target.value)}
          aria-label="Filter by module"
          className="rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-1 font-mono text-xs text-[var(--text)] focus:border-[var(--muted)] focus:outline-none"
        >
          <option value="ALL">all modules</option>
          {modules.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>

        <div className="ml-auto flex items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 font-mono text-xs text-[var(--muted)]">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="accent-[var(--muted)]"
            />
            auto-scroll
          </label>
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            className={`rounded border px-2 py-1 font-mono text-xs transition-colors ${
              paused
                ? "border-[color-mix(in_srgb,var(--border)_50%,transparent)] bg-[color-mix(in_srgb,var(--muted)_15%,transparent)] text-[var(--muted)]"
                : "border-[var(--border)] bg-[var(--surface-2)] text-[var(--text)] hover:border-[var(--border)]"
            }`}
          >
            {paused ? "▶ Resume" : "⏸ Pause"}
          </button>
        </div>
      </div>

      {/* Log lines */}
      <div ref={boxRef} className="min-h-0 flex-1 overflow-y-auto p-2 font-mono text-xs leading-5" aria-label="Gateway logs">
        {loading ? (
          <div className="flex h-full items-center justify-center">
            <span className="animate-pulse text-[var(--muted)]">// loading logs…</span>
          </div>
        ) : error && entries.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <span className="text-[var(--muted)]">// failed to load logs: {error}</span>
            <button
              type="button"
              onClick={() => void loadInitial()}
              className="rounded border border-[var(--border)] bg-[var(--surface-2)] px-3 py-1 font-mono text-xs text-[var(--text)] hover:border-[var(--border)]"
            >
              Retry
            </button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <span className="text-[var(--muted)]">// no log entries match the current filters</span>
          </div>
        ) : (
          filtered.map((e) => (
            <div key={e.key} className="flex gap-2 whitespace-pre-wrap break-words rounded px-1 py-px hover:bg-[color-mix(in_srgb,var(--surface-2)_60%,transparent)]">
              <span className="shrink-0 select-none text-[var(--muted)]">{formatTime(e.timestamp)}</span>
              <span className={`shrink-0 select-none rounded border px-1 leading-5 ${LEVEL_STYLES[e.level]}`}>{e.level}</span>
              <span className="shrink-0 select-none text-[var(--muted)]">[{e.module}]</span>
              <span className="min-w-0 text-[var(--text)]">{e.message}</span>
            </div>
          ))
        )}
      </div>

      {/* Footer: count + load more */}
      <div className="flex items-center gap-3 border-t border-[var(--border)] px-3 py-2 font-mono text-xs text-[var(--muted)]">
        <span>
          {filtered.length} / {entries.length} entries
          {paused && <span className="ml-2 text-[var(--muted)]">(polling paused)</span>}
        </span>
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={loadingMore || !hasMore || loading}
          className="rounded border border-[var(--border)] bg-[var(--surface-2)] px-2 py-0.5 text-[var(--text)] transition-colors hover:border-[var(--border)] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {loadingMore ? "loading…" : hasMore ? "Load more (older)" : "no more entries"}
        </button>
        {error && entries.length > 0 && (
          <span className="text-[var(--muted)]">// last load failed: {error}</span>
        )}
      </div>
    </div>
  );
}
