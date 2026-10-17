import { useEffect, useState } from "react";
import { Eye, EyeOff, KeyRound, Loader2, X } from "lucide-react";
import { connectChannel } from "../lib/api";

/**
 * ChannelConnectDialog — modal for connecting a channel with a bot token.
 *
 * Backend contract (new):
 *   POST /api/channels/:id/connect  body { token } -> 2xx on success,
 *   non-2xx with {"error": "..."} on failure. The real backend error message
 *   is shown verbatim — never replaced with placeholder text.
 *
 * Security: the token lives only in component state and is wiped when the
 * dialog closes. It is sent only to the local backend, never persisted.
 */

interface Props {
  /** Channel id (stable, used in the /api/channels/:id/connect URL). */
  channelId: string | null;
  /** Human-readable channel name for the title. */
  channelName: string;
  /** Called when the modal should close (backdrop click, X, or success). */
  onClose: () => void;
  /** Called after a successful connect so the list can refresh. */
  onConnected: () => void;
}

export default function ChannelConnectDialog({
  channelId,
  channelName,
  onClose,
  onConnected,
}: Props) {
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the form every time the dialog opens for a (possibly) new channel.
  useEffect(() => {
    if (channelId !== null) {
      setToken("");
      setShowToken(false);
      setBusy(false);
      setError(null);
    }
  }, [channelId]);

  if (channelId === null) return null;

  function handleClose(): void {
    setToken(""); // wipe the credential from state
    setShowToken(false);
    setError(null);
    onClose();
  }

  async function handleSave(): Promise<void> {
    if (token.trim() === "") {
      setError("Enter the bot token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await connectChannel(channelId as string, token);
      setToken("");
      setBusy(false);
      onConnected();
      onClose();
    } catch (e) {
      // Surface the backend's real error verbatim.
      setError(e instanceof Error ? e.message : "Connect failed");
      setBusy(false);
    }
  }

  const inputCls =
    "w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)] focus:border-[var(--muted)] focus:outline-none disabled:opacity-50 font-mono";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={handleClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Connect ${channelName}`}
    >
      <div
        className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <KeyRound className="h-5 w-5 text-[var(--muted)]" />
            <h2 className="text-base font-semibold text-[var(--text)]">
              Connect {channelName}
            </h2>
          </div>
          <button
            type="button"
            onClick={handleClose}
            className="rounded p-1 text-[var(--muted)] hover:text-[var(--text)]"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {error && (
          <div
            role="alert"
            className="mb-3 rounded-lg border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_60%,transparent)] px-3 py-2 text-sm text-[var(--text)]"
          >
            {error}
          </div>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSave();
          }}
        >
          <div className="mb-4">
            <label
              className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--muted)]"
              htmlFor="channel-token"
            >
              Bot token
            </label>
            <div className="relative">
              <input
                id="channel-token"
                type={showToken ? "text" : "password"}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Paste the bot token here"
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
                autoFocus
                className={`${inputCls} pr-10`}
              />
              <button
                type="button"
                onClick={() => setShowToken((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-[var(--muted)] hover:text-[var(--text)]"
                aria-label={showToken ? "Hide token" : "Show token"}
              >
                {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
            <p className="mt-1.5 text-xs text-[var(--muted)]">
              The token is sent only to the local backend and is never stored
              by this app.
            </p>
          </div>

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={handleClose}
              disabled={busy}
              className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-4 py-2 text-sm font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className="flex items-center justify-center gap-2 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />}
              {busy ? "Connecting…" : "Save & Connect"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
