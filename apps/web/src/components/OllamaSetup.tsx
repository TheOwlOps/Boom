import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Copy,
  Download,
  Loader2,
  RefreshCw,
  Terminal,
  XCircle,
} from "lucide-react";
import { getOllamaStatus } from "../lib/api";
import type { OllamaStatus } from "../lib/api";

/**
 * OllamaSetup — wizard step that guides the user through installing Ollama
 * and pulls a starter model, or confirms the local daemon is already ready.
 *
 * Backend contract (gateway/api.py, extends webui API_CONTRACT.md):
 *   GET /api/providers/ollama/status -> { running: boolean, models: string[] }
 *
 * The endpoint always answers 200: `running: false` means Ollama is not
 * installed, not running, or unreachable on localhost:11434 — the UI then
 * shows the 3-step install guide instead of an error.
 */

interface Props {
  /** Called once when Ollama reports running with at least one model. */
  onReady?: () => void;
}

interface UseOllamaStatusResult {
  /** Whether the local Ollama daemon answered the status probe. */
  running: boolean;
  /** True while a status check is in flight. */
  checking: boolean;
  /** Installed model names reported by Ollama (`[]` when not running). */
  models: string[];
  /** Re-run the status check against the backend. */
  recheck: () => void;
}

/**
 * Polls GET /api/providers/ollama/status once on mount. Transport failures
 * (backend down, timeout) degrade to `running: false` — the setup guide
 * renders as the fallback UI, never an uncaught error.
 */
export function useOllamaStatus(): UseOllamaStatusResult {
  const [status, setStatus] = useState<OllamaStatus>({ running: false, models: [] });
  const [checking, setChecking] = useState<boolean>(true);
  // Guard against setting state after unmount when a slow check resolves.
  const mounted = useRef<boolean>(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const recheck = useCallback(() => {
    setChecking(true);
    getOllamaStatus()
      .then((s) => {
        if (mounted.current) setStatus(s);
      })
      .catch(() => {
        // Backend unreachable: show the guide, not a crash.
        if (mounted.current) setStatus({ running: false, models: [] });
      })
      .finally(() => {
        if (mounted.current) setChecking(false);
      });
  }, []);

  useEffect(() => {
    recheck();
  }, [recheck]);

  return { running: status.running, checking, models: status.models, recheck };
}

const OLLAMA_DOWNLOAD_URL = "https://ollama.com/download";
const PULL_COMMAND = "ollama pull llama3.1";

/** Copy-to-clipboard button for the install command (feedback via ✓ state). */
function CopyCommandButton({ command }: { command: string }) {
  const [copied, setCopied] = useState<boolean>(false);

  async function handleCopy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API may be unavailable (non-secure context): the user can
      // still select and copy the command manually.
      setCopied(false);
    }
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      className="flex items-center gap-1.5 rounded-md border border-neutral-700 bg-neutral-800 px-2.5 py-1.5 text-xs font-medium text-neutral-200 transition-colors hover:border-neutral-500 hover:bg-neutral-700"
      aria-label="Copy install command"
    >
      {copied ? (
        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
      {copied ? "Copied!" : "Copy"}
    </button>
  );
}

/** Step 1–3 install guide shown when the daemon is not reachable. */
function OllamaInstallGuide({
  checking,
  onCheckAgain,
}: {
  checking: boolean;
  onCheckAgain: () => void;
}) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-neutral-400">
        Ollama runs large language models on your own machine — no API key
        needed. Follow these steps to get started:
      </p>

      <ol className="space-y-4">
        <li className="flex items-start gap-3">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-neutral-800 text-xs font-semibold text-neutral-300">
            1
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-neutral-200">
              Download and install Ollama
            </p>
            <a
              href={OLLAMA_DOWNLOAD_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 inline-flex items-center gap-1.5 text-sm text-indigo-300 underline-offset-2 hover:text-indigo-200 hover:underline"
            >
              <Download className="h-4 w-4" />
              {OLLAMA_DOWNLOAD_URL}
            </a>
          </div>
        </li>

        <li className="flex items-start gap-3">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-neutral-800 text-xs font-semibold text-neutral-300">
            2
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-neutral-200">
              Pull a model (open a terminal and run)
            </p>
            <div className="mt-2 flex items-center justify-between gap-2 rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-2.5">
              <code className="flex items-center gap-2 font-mono text-sm text-emerald-300">
                <Terminal className="h-4 w-4 shrink-0 text-neutral-500" />
                {PULL_COMMAND}
              </code>
              <CopyCommandButton command={PULL_COMMAND} />
            </div>
            <p className="mt-1.5 text-xs text-neutral-500">
              This downloads ~4.9 GB on first run — make sure you have space.
            </p>
          </div>
        </li>

        <li className="flex items-start gap-3">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-neutral-800 text-xs font-semibold text-neutral-300">
            3
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-neutral-200">
              Make sure Ollama is running, then check again
            </p>
            <button
              type="button"
              onClick={onCheckAgain}
              disabled={checking}
              className="mt-2 flex items-center gap-2 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
            >
              {checking ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              {checking ? "Checking..." : "Check again"}
            </button>
          </div>
        </li>
      </ol>
    </div>
  );
}

/** Success state: daemon reachable, installed models listed. */
function OllamaRunningPanel({
  models,
  checking,
  onRecheck,
}: {
  models: string[];
  checking: boolean;
  onRecheck: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2.5">
        <CheckCircle2 className="h-6 w-6 text-emerald-400" />
        <p className="text-base font-semibold text-neutral-100">
          Ollama is running
        </p>
      </div>

      <div>
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
          Installed models ({models.length})
        </p>
        {models.length > 0 ? (
          <ul className="flex flex-wrap gap-2">
            {models.map((m) => (
              <li
                key={m}
                className="rounded-md border border-emerald-900/60 bg-emerald-950/40 px-2.5 py-1 font-mono text-sm text-emerald-300"
              >
                {m}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-neutral-500">
            No models installed yet. Run{" "}
            <code className="rounded bg-neutral-800 px-1.5 py-0.5 font-mono text-xs text-emerald-300">
              {PULL_COMMAND}
            </code>{" "}
            to pull the starter model.
          </p>
        )}
      </div>

      <button
        type="button"
        onClick={onRecheck}
        disabled={checking}
        className="flex items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-800 px-4 py-2 text-sm font-medium text-neutral-200 transition-colors hover:border-neutral-500 hover:bg-neutral-700 disabled:opacity-50"
      >
        {checking ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <RefreshCw className="h-4 w-4" />
        )}
        {checking ? "Checking..." : "Recheck"}
      </button>
    </div>
  );
}

export function OllamaSetup({ onReady }: Props) {
  const { running, checking, models, recheck } = useOllamaStatus();
  // Fire onReady once, the first time we see running + at least one model.
  const readyFired = useRef<boolean>(false);
  useEffect(() => {
    if (!readyFired.current && running && models.length > 0) {
      readyFired.current = true;
      onReady?.();
    }
  }, [running, models, onReady]);

  const checkingInitial = checking && !running && models.length === 0;

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
      <h3 className="mb-1 text-base font-semibold text-neutral-100">
        Local models with Ollama
      </h3>
      <p className="mb-4 text-sm text-neutral-400">
        Run Boom with free local models — no cloud API key required.
      </p>

      {checkingInitial ? (
        <div className="flex items-center gap-2 text-sm text-neutral-400">
          <Loader2 className="h-4 w-4 animate-spin" />
          Checking for Ollama on this machine...
        </div>
      ) : running ? (
        <OllamaRunningPanel models={models} checking={checking} onRecheck={recheck} />
      ) : (
        <>
          <div className="mb-4 flex items-center gap-2.5 rounded-lg border border-amber-900/50 bg-amber-950/30 px-3.5 py-3">
            <XCircle className="h-5 w-5 shrink-0 text-amber-400" />
            <p className="text-sm text-amber-200">
              Ollama is not running on this machine.
            </p>
          </div>
          <OllamaInstallGuide checking={checking} onCheckAgain={recheck} />
        </>
      )}
    </div>
  );
}
