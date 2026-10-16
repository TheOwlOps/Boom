import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, Play, RotateCw } from "lucide-react";
import { API_BASE, getBackendHelpText } from "../lib/api";

/** Poll the Python backend health endpoint every 5 seconds. */
const POLL_MS = 5_000;
/** Give up a single health probe after 5 seconds. */
const FETCH_TIMEOUT_MS = 5_000;

/** Connection state against the Python backend (gateway/api.py). */
type BackendState = "connected" | "disconnected" | "starting";

export interface BackendStatusProps {
  /** When true, renders only a small status dot (for header/status bar use). */
  compact?: boolean;
}

/** Health probe: HEAD-style GET with an AbortController timeout. */
async function probeHealth(): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}/api/health`, {
      signal: controller.signal,
    });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Asks the Tauri shell to start the bundled Python backend.
 * The frontend always runs inside Tauri, so no environment guard needed.
 */
async function startBackendNative(): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("start_backend");
  } catch (err) {
    console.error("[BackendStatus] start_backend invoke failed:", err);
    throw err;
  }
}

/**
 * Polls the Python backend health endpoint and surfaces a status UI.
 *
 * Full panel (default): shown at the top of the main content when the
 * backend is unreachable, with a manual-start command, a Tauri
 * "start backend" action, and a retry button. Compact mode renders only
 * a small colored dot for header/status-bar placement.
 */
export default function BackendStatus({ compact = false }: BackendStatusProps) {
  const [state, setState] = useState<BackendState>("starting");
  const [startingNative, setStartingNative] = useState(false);
  const mountedRef = useRef(true);

  const check = useCallback(async () => {
    const ok = await probeHealth();
    if (!mountedRef.current) return;
    setState((prev) => {
      // While a native start is in flight, keep the yellow "starting" state
      // until the probe actually succeeds.
      if (startingNative && !ok) return "starting";
      return ok ? "connected" : "disconnected";
    });
  }, [startingNative]);

  useEffect(() => {
    mountedRef.current = true;
    void check();
    const id = setInterval(() => void check(), POLL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [check]);

  const handleStart = useCallback(async () => {
    setStartingNative(true);
    setState("starting");
    try {
      await startBackendNative();
    } catch {
      // Error already logged; surface failure as disconnected.
      setStartingNative(false);
      await check();
      return;
    }
    // Give the backend a moment, then re-probe immediately.
    setTimeout(() => {
      if (!mountedRef.current) return;
      void check();
      setStartingNative(false);
    }, 2_000);
  }, [check]);

  const handleRetry = useCallback(() => {
    setState("starting");
    void check();
  }, [check]);

  if (compact) {
    const dotColor =
      state === "connected"
        ? "bg-green-500"
        : state === "starting"
          ? "bg-yellow-500"
          : "bg-red-500";
    const label =
      state === "connected"
        ? "Backend connected"
        : state === "starting"
          ? "Backend starting…"
          : "Backend disconnected";
    return (
      <span className="flex items-center" title={label} aria-label={label}>
        <span
          className={`inline-block h-2 w-2 rounded-full ${dotColor}`}
          aria-hidden="true"
        />
      </span>
    );
  }

  if (state === "connected" || state === "starting") {
    // Non-compact: only the disconnected panel is shown here. The connected
    // state is represented by the compact dot in the status bar.
    return null;
  }

  return (
    <div
      role="alert"
      className="mx-4 mt-4 flex items-start gap-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-5"
    >
      <AlertTriangle
        className="mt-0.5 h-6 w-6 shrink-0 text-amber-400"
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <h2 className="text-base font-semibold text-neutral-100">
          Backend is not running
        </h2>
        <p className="mt-1 text-sm text-neutral-400">
          {getBackendHelpText()}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void handleStart()}
            disabled={startingNative}
            className="btn-hover flex items-center gap-2 rounded-lg bg-[var(--accent)] px-3.5 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {startingNative ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Play className="h-4 w-4" aria-hidden="true" />
            )}
            Start backend
          </button>
          <button
            type="button"
            onClick={handleRetry}
            className="btn-hover flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3.5 py-2 text-sm font-medium text-neutral-200"
          >
            <RotateCw className="h-4 w-4" aria-hidden="true" />
            Retry
          </button>
        </div>
      </div>
    </div>
  );
}
