import { useCallback, useEffect, useRef, useState } from "react";
import {
  check,
  type DownloadEvent,
  type Update,
} from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Loader2,
  RotateCw,
  Sparkles,
  X,
} from "lucide-react";

/** Visual phase of the update banner. */
type BannerPhase = "available" | "downloading" | "installed" | "failed";

/**
 * Runs one manual update check against the configured Tauri updater
 * endpoints. Resolves with the pending `Update` when a newer version is
 * announced, or `null` when the app is up to date or the check itself
 * failed. A failed check is intentionally silent: update infrastructure
 * must never crash the app.
 *
 * Ownership note: the returned `Update` holds a resource on the Rust side.
 * Call `update.close()` when you are done with it without installing.
 */
export function useManualUpdateCheck(): {
  checking: boolean;
  checkNow: () => Promise<Update | null>;
} {
  const [checking, setChecking] = useState(false);

  const checkNow = useCallback(async (): Promise<Update | null> => {
    setChecking(true);
    try {
      // `check()` resolves with `null` when no update is available and
      // rejects when the updater is misconfigured or unreachable.
      return await check();
    } catch {
      return null;
    } finally {
      setChecking(false);
    }
  }, []);

  return { checking, checkNow };
}

/**
 * Update banner for the Tauri desktop app.
 *
 * Auto-checks once on mount. When an update is announced it shows a fixed
 * top banner: "Update now" downloads + installs with a live progress bar,
 * then the banner flips to "Update installed" with a "Restart now" button
 * that relaunches the app. Renders nothing when no update is pending;
 * every Tauri call is guarded so a broken updater backend can never crash
 * the app.
 */
export default function UpdateChecker() {
  const { checkNow } = useManualUpdateCheck();
  const [update, setUpdate] = useState<Update | null>(null);
  const [phase, setPhase] = useState<BannerPhase>("available");
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const downloadedBytes = useRef(0);
  const totalBytes = useRef<number | null>(null);

  // Auto-check once on mount. Silent on failure.
  useEffect(() => {
    let cancelled = false;
    void checkNow().then((found) => {
      if (cancelled) {
        // Unmounted before the check resolved: release the backend
        // resource instead of leaking it.
        void found?.close().catch(() => undefined);
        return;
      }
      if (found) setUpdate(found);
    });
    return () => {
      cancelled = true;
    };
  }, [checkNow]);

  /** Best-effort release of the pending update's backend resource. */
  const releaseUpdate = useCallback(async (u: Update | null) => {
    if (!u) return;
    try {
      await u.close();
    } catch {
      /* best effort — nothing to do */
    }
  }, []);

  const handleDismiss = useCallback(() => {
    const pending = update;
    setUpdate(null);
    setDismissed(true);
    void releaseUpdate(pending);
  }, [update, releaseUpdate]);

  const handleInstall = useCallback(async () => {
    if (!update || phase === "downloading") return;
    setError(null);
    setPhase("downloading");
    setProgress(0);
    downloadedBytes.current = 0;
    totalBytes.current = null;
    try {
      await update.downloadAndInstall((event: DownloadEvent) => {
        if (event.event === "Started") {
          totalBytes.current = event.data.contentLength ?? null;
        } else if (event.event === "Progress") {
          downloadedBytes.current += event.data.chunkLength;
          const total = totalBytes.current;
          // Cap at 99% until the Finished event so the bar never claims
          // completion early. Unknown size -> indeterminate bar (null).
          setProgress(
            total !== null && total > 0
              ? Math.min(
                  99,
                  Math.round((downloadedBytes.current / total) * 100),
                )
              : null,
          );
        } else {
          setProgress(100);
        }
      });
      setPhase("installed");
      await releaseUpdate(update);
    } catch (err) {
      setPhase("failed");
      setError(
        err instanceof Error
          ? `Update failed: ${err.message}`
          : "Update failed for an unknown reason.",
      );
      await releaseUpdate(update);
      setUpdate(null);
    }
  }, [update, phase, releaseUpdate]);

  const handleRelaunch = useCallback(async () => {
    try {
      await relaunch();
    } catch (err) {
      // `relaunch` requires the `process` plugin on the Rust side; if it is
      // not registered the call rejects — ask the user to restart manually.
      setError(
        err instanceof Error
          ? `Restart failed (${err.message}). Please restart Boom manually.`
          : "Restart failed. Please restart Boom manually.",
      );
    }
  }, []);

  // With no pending update, render nothing — never an error.
  if (dismissed || !update) return null;

  const versionLabel =
    phase === "available" ? (
      <>
        <Sparkles className="h-4 w-4 shrink-0 text-zinc-300" aria-hidden="true" />
        <span className="font-semibold text-neutral-100">
          🎉 New version {update.version} available!
        </span>
        <span className="hidden text-neutral-400 sm:inline">
          You&apos;re on {update.currentVersion}.
        </span>
      </>
    ) : phase === "downloading" ? (
      <>
        <Loader2
          className="h-4 w-4 shrink-0 animate-spin text-zinc-300"
          aria-hidden="true"
        />
        <span className="font-medium text-neutral-100">
          {progress === null
            ? "Downloading update…"
            : `Downloading update… ${progress}%`}
        </span>
      </>
    ) : phase === "installed" ? (
      <>
        <CheckCircle2
          className="h-4 w-4 shrink-0 text-zinc-300"
          aria-hidden="true"
        />
        <span className="font-semibold text-neutral-100">
          Update installed — restart to apply it.
        </span>
      </>
    ) : (
      <>
        <AlertTriangle
          className="h-4 w-4 shrink-0 text-zinc-300"
          aria-hidden="true"
        />
        <span className="font-medium text-neutral-100">
          {error ?? "Update failed."}
        </span>
      </>
    );

  return (
    <div
      role="status"
      aria-live="polite"
      className="fade-in slide-down fixed inset-x-0 top-0 z-40"
    >
      <div className="border-b border-zinc-700 bg-zinc-900 shadow-lg shadow-black/50">
        <div className="mx-auto flex max-w-4xl items-center gap-3 px-4 py-2.5">
          <div className="flex min-w-0 flex-1 items-center gap-2 text-sm">
            {versionLabel}
          </div>

          {phase === "available" && (
            <button
              type="button"
              onClick={() => void handleInstall()}
              className="btn-hover flex shrink-0 items-center gap-1.5 rounded-lg bg-white px-3 py-1.5 text-[13px] font-semibold text-black shadow-md shadow-black/50"
            >
              <Download className="h-3.5 w-3.5" aria-hidden="true" />
              Update now
            </button>
          )}
          {phase === "installed" && (
            <button
              type="button"
              onClick={() => void handleRelaunch()}
              className="btn-hover flex shrink-0 items-center gap-1.5 rounded-lg bg-white px-3 py-1.5 text-[13px] font-semibold text-black shadow-md shadow-black/50"
            >
              <RotateCw className="h-3.5 w-3.5" aria-hidden="true" />
              Restart now
            </button>
          )}
          {phase === "failed" && (
            <button
              type="button"
              onClick={() => void handleInstall()}
              className="btn-hover flex shrink-0 items-center gap-1.5 rounded-lg bg-white px-3 py-1.5 text-[13px] font-semibold text-black"
            >
              <RotateCw className="h-3.5 w-3.5" aria-hidden="true" />
              Retry
            </button>
          )}

          <button
            type="button"
            onClick={handleDismiss}
            disabled={phase === "downloading"}
            title="Dismiss"
            aria-label="Dismiss update banner"
            className="btn-hover flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-neutral-400 hover:bg-white/10 hover:text-neutral-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Download progress bar. Indeterminate (pulsing) when the update
            server did not report a content length. */}
        {phase === "downloading" && (
          <div className="h-1 w-full bg-zinc-950/80" aria-hidden="true">
            <div
              className={`h-full bg-white transition-[width] duration-200 ${
                progress === null ? "w-full animate-pulse" : ""
              }`}
              style={progress === null ? undefined : { width: `${progress}%` }}
            />
          </div>
        )}
      </div>
    </div>
  );
}
