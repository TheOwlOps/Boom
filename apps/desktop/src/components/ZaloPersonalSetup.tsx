import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  MessageCircle,
  Phone,
  Unplug,
  X,
} from "lucide-react";
import {
  zaloPersonalLogin,
  zaloPersonalLogout,
  zaloPersonalStatus,
} from "../lib/api";

/**
 * ZaloPersonalSetup — modal for connecting a personal Zalo account.
 *
 * Backend contract (crates/boom-core (Rust)):
 *   GET  /api/channels/zalo-personal/status -> { connected, phone_masked }
 *   POST /api/channels/zalo-personal/login  -> { phone, password } -> { connected, phone_masked }
 *   POST /api/channels/zalo-personal/logout -> { ok: true }
 *
 * Security notes:
 * - Credentials are never persisted (no localStorage, no cookies). The
 *   password field is cleared from state whenever the modal closes.
 * - The backend never returns the full phone number — only `phone_masked`.
 */

interface Props {
  /** Whether the modal is visible. */
  open: boolean;
  /** Called when the modal should close (backdrop click, X button). */
  onClose: () => void;
}

type Phase = "loading" | "form" | "connecting" | "connected" | "disconnecting";

/** Fallback phone masker (first 2 + last 4 digits) when the backend sends no mask. */
function maskPhoneFallback(phone: string): string {
  const digits = phone.replace(/\D/g, "");
  if (digits.length <= 6) return "***";
  return `${digits.slice(0, 2)}***${digits.slice(-4)}`;
}

/** Red banner warning shown at the top of the modal, always. */
function UnofficialApiWarning() {
  return (
    <div
      role="alert"
      className="mb-4 flex items-start gap-2.5 rounded-lg border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_70%,transparent)] px-3.5 py-3"
    >
      <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-[var(--muted)]" />
      <p className="text-sm font-medium leading-snug text-[var(--text)]">
        ⚠️ Zalo Personal uses an unofficial API. Your account may be banned by
        Zalo. Use a secondary account only.
      </p>
    </div>
  );
}

export default function ZaloPersonalSetup({ open, onClose }: Props) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [maskedPhone, setMaskedPhone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /** Load the connection status each time the modal opens. */
  const refreshStatus = useCallback(async () => {
    setPhase("loading");
    setError(null);
    try {
      const st = await zaloPersonalStatus();
      setMaskedPhone(st.phoneMasked);
      setPhase(st.connected ? "connected" : "form");
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Failed to load Zalo Personal status"
      );
      setPhase("form");
    }
  }, []);

  useEffect(() => {
    if (open) void refreshStatus();
  }, [open, refreshStatus]);

  /** Wipe the password whenever the modal closes — never keep it in state. */
  function handleClose(): void {
    setPassword("");
    setShowPassword(false);
    setError(null);
    onClose();
  }

  async function handleConnect(): Promise<void> {
    const trimmedPhone = phone.trim();
    if (!trimmedPhone) {
      setError("Enter your phone number.");
      return;
    }
    if (!password) {
      setError("Enter your password.");
      return;
    }
    setPhase("connecting");
    setError(null);
    try {
      const st = await zaloPersonalLogin(trimmedPhone, password);
      // Prefer the backend's mask; fall back to a local mask of what we typed.
      setMaskedPhone(st.phoneMasked ?? maskPhoneFallback(trimmedPhone));
      setPassword(""); // drop the password as soon as login succeeds
      setPhase(st.connected ? "connected" : "form");
      if (!st.connected) {
        setError("Login did not complete. Check your phone number and password.");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Login failed");
      setPhase("form");
    }
  }

  async function handleDisconnect(): Promise<void> {
    setPhase("disconnecting");
    setError(null);
    try {
      await zaloPersonalLogout();
      setMaskedPhone(null);
      setPhase("form");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Disconnect failed");
      setPhase("connected"); // stay on the connected view; the session is likely still live
    }
  }

  if (!open) return null;

  const busy = phase === "loading" || phase === "connecting" || phase === "disconnecting";
  const inputCls =
    "w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)] focus:border-[var(--muted)] focus:outline-none disabled:opacity-50";
  const labelCls =
    "mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--muted)]";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={handleClose}
      role="dialog"
      aria-modal="true"
      aria-label="Zalo Personal setup"
    >
      <div
        className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <MessageCircle className="h-5 w-5 text-[var(--muted)]" />
            <h2 className="text-base font-semibold text-[var(--text)]">
              Zalo Personal
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

        <UnofficialApiWarning />

        {/* Status / initial load error */}
        {phase === "loading" && (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-[var(--muted)]">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading status…
          </div>
        )}

        {phase !== "loading" && error && (
          <div
            role="alert"
            className="mb-3 rounded-lg border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_60%,transparent)] px-3 py-2 text-sm text-[var(--text)]"
          >
            {error}
          </div>
        )}

        {/* Login form */}
        {(phase === "form" || phase === "connecting") && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleConnect();
            }}
          >
            <div className="mb-3">
              <label className={labelCls} htmlFor="zalo-phone">
                Phone number
              </label>
              <div className="relative">
                <Phone className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--muted)]" />
                <input
                  id="zalo-phone"
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="09xxxxxxxx"
                  autoComplete="tel"
                  spellCheck={false}
                  disabled={busy}
                  className={`${inputCls} pl-9`}
                />
              </div>
            </div>

            <div className="mb-4">
              <label className={labelCls} htmlFor="zalo-password">
                Password
              </label>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--muted)]" />
                <input
                  id="zalo-password"
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Zalo password"
                  autoComplete="current-password"
                  spellCheck={false}
                  disabled={busy}
                  className={`${inputCls} pl-9 pr-10`}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-[var(--muted)] hover:text-[var(--text)]"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </button>
              </div>
              <p className="mt-1.5 text-xs text-[var(--muted)]">
                Credentials are sent only to the local backend and are never
                stored by this app.
              </p>
            </div>

            <button
              type="submit"
              disabled={busy}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
            >
              {phase === "connecting" && (
                <Loader2 className="h-4 w-4 animate-spin" />
              )}
              {phase === "connecting" ? "Connecting…" : "Connect"}
            </button>
          </form>
        )}

        {/* Connected view */}
        {(phase === "connected" || phase === "disconnecting") && (
          <div>
            <div className="mb-4 flex items-center gap-3 rounded-lg border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] px-4 py-3">
              <CheckCircle2 className="h-5 w-5 shrink-0 text-[var(--muted)]" />
              <div>
                <p className="text-sm font-medium text-[var(--text)]">Connected</p>
                <p className="font-mono text-sm text-[color-mix(in_srgb,var(--text)_90%,transparent)]">
                  {maskedPhone ?? "•••"}
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => void handleDisconnect()}
              disabled={busy}
              className="flex w-full items-center justify-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-4 py-2 text-sm font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50"
            >
              {phase === "disconnecting" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Unplug className="h-4 w-4" />
              )}
              {phase === "disconnecting" ? "Disconnecting…" : "Disconnect"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
