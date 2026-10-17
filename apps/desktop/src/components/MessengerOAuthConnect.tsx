import { useEffect, useRef, useState } from "react";
import { ExternalLink, Facebook, Loader2, Plug } from "lucide-react";
import {
  getChannels,
  getMessengerOAuthConfig,
  getMessengerOAuthUrl,
  saveMessengerOAuthConfig,
  type MessengerOAuthConfigStatus,
} from "../lib/api";

const btnCls =
  "inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50";

const inputCls =
  "w-full rounded border border-[var(--border)] bg-[var(--bg)] px-2.5 py-1.5 text-xs text-[var(--text)] placeholder:text-[var(--muted)] focus:border-[var(--muted)] focus:outline-none";

/** Open a URL in the system browser (Tauri) or a new tab (web dev). */
async function openExternal(url: string): Promise<void> {
  try {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
  } catch {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * Messenger connect via Facebook OAuth: the user logs in with Facebook in
 * their browser instead of pasting a Page token by hand.
 */
export default function MessengerOAuthConnect({
  onConnected,
  onManualConnect,
}: {
  onConnected(): void;
  /** Fall back to the manual Page-token dialog. */
  onManualConnect(): void;
}) {
  const [cfg, setCfg] = useState<MessengerOAuthConfigStatus | null>(null);
  const [cfgError, setCfgError] = useState("");
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [starting, setStarting] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [startError, setStartError] = useState("");
  const pollRef = useRef<number | null>(null);
  const deadlineRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    getMessengerOAuthConfig()
      .then((c) => {
        if (!cancelled) setCfg(c);
      })
      .catch((e) => {
        if (!cancelled) setCfgError(e instanceof Error ? e.message : "Failed to load OAuth config");
      });
    return () => {
      cancelled = true;
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, []);

  function stopPolling() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setWaiting(false);
  }

  async function pollForConnection() {
    try {
      const channels = await getChannels();
      const messenger = channels.find((c) => c.id === "messenger");
      if (messenger?.state === "connected") {
        stopPolling();
        onConnected();
        return;
      }
    } catch {
      // Transient backend hiccup — keep polling until the deadline.
    }
    if (Date.now() > deadlineRef.current) {
      stopPolling();
      setStartError(
        "Hết thời gian chờ — nếu bạn đã đăng nhập xong, danh sách channels sẽ tự cập nhật.",
      );
    }
  }

  async function handleSaveConfig() {
    setSaveError("");
    if (!appId.trim() || !appSecret.trim()) {
      setSaveError("Nhập cả App ID và App Secret.");
      return;
    }
    setSaving(true);
    try {
      await saveMessengerOAuthConfig(appId, appSecret);
      const c = await getMessengerOAuthConfig();
      setCfg(c);
      setAppSecret("");
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleConnect() {
    setStartError("");
    setStarting(true);
    try {
      const url = await getMessengerOAuthUrl();
      await openExternal(url);
      // The browser tab lands back on this server; poll until the
      // callback persists the Page token.
      deadlineRef.current = Date.now() + POLL_TIMEOUT_MS;
      setWaiting(true);
      pollRef.current = window.setInterval(() => void pollForConnection(), POLL_MS);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : "Could not start Facebook login");
    } finally {
      setStarting(false);
    }
  }

  if (cfg === null) {
    return cfgError ? (
      <p className="text-xs text-[var(--muted)]" role="alert">{cfgError}</p>
    ) : (
      <p className="flex items-center gap-1.5 text-xs text-[var(--muted)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Đang tải…
      </p>
    );
  }

  // Step 1: no Facebook App configured yet.
  if (!cfg.configured) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-[var(--muted)]">
          Đăng nhập bằng Facebook cần một Facebook App (miễn phí).{" "}
          <a
            href="https://developers.facebook.com/docs/development/create-an-app/"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 underline hover:text-[var(--text)]"
          >
            Cách tạo App <ExternalLink className="h-3 w-3" />
          </a>
        </p>
        <ol className="list-decimal space-y-0.5 pl-4 text-[11px] text-[var(--muted)]">
          <li>Tạo App tại developers.facebook.com, thêm sản phẩm <b>Facebook Login</b>.</li>
          <li>Copy <b>App ID</b> và <b>App Secret</b> (App Settings → Basic).</li>
          <li>Dán vào 2 ô dưới rồi bấm Save.</li>
        </ol>
        <input
          value={appId}
          onChange={(e) => setAppId(e.target.value)}
          placeholder="Facebook App ID"
          autoComplete="off"
          spellCheck={false}
          className={inputCls}
          aria-label="Facebook App ID"
        />
        <input
          value={appSecret}
          onChange={(e) => setAppSecret(e.target.value)}
          placeholder="Facebook App Secret"
          type="password"
          autoComplete="off"
          spellCheck={false}
          className={inputCls}
          aria-label="Facebook App Secret"
        />
        {saveError && (
          <p className="text-xs text-[var(--muted)]" role="alert">{saveError}</p>
        )}
        <div className="flex gap-1.5 pt-0.5">
          <button type="button" onClick={() => void handleSaveConfig()} disabled={saving} className={btnCls}>
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            Save
          </button>
        </div>
      </div>
    );
  }

  // Step 2: configured — one click opens the Facebook login in the browser.
  return (
    <div className="space-y-2">
      <div className="flex gap-1.5 pt-0.5">
        <button
          type="button"
          onClick={() => void handleConnect()}
          disabled={starting || waiting}
          className={btnCls}
        >
          {starting || waiting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Facebook className="h-3.5 w-3.5" />
          )}
          {waiting ? "Đang chờ đăng nhập…" : "Connect with Facebook"}
        </button>
      </div>
      {waiting && (
        <button
          type="button"
          onClick={() => void pollForConnection()}
          className="text-[11px] text-[var(--muted)] underline hover:text-[var(--text)]"
        >
          Tôi đã đăng nhập xong — kiểm tra lại
        </button>
      )}
      {startError && (
        <p className="text-xs text-[var(--muted)]" role="alert">{startError}</p>
      )}
      <button
        type="button"
        onClick={onManualConnect}
        className="flex items-center gap-1 text-[11px] text-[var(--muted)] underline hover:text-[var(--text)]"
      >
        <Plug className="h-3 w-3" /> Hoặc dán Page token thủ công
      </button>
    </div>
  );
}
