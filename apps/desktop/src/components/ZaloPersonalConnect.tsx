import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, QrCode, RefreshCw, Smartphone, Unplug } from "lucide-react";
import {
  checkBridgeRunning,
  getZaloPersonalQR,
  getZaloPersonalStatus,
  type ZaloPersonalBridgeStatus,
} from "../lib/zaloPersonal";

const btnCls =
  "inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50";

/** Auto-refresh the QR every 10s (the bridge QR expires after 120s). */
const QR_REFRESH_MS = 10_000;
/** Poll bridge status every 3s to catch a successful QR scan. */
const STATUS_POLL_MS = 3_000;

/**
 * Zalo Personal connect via QR login.
 *
 * Talks directly to the separate Python bridge process
 * (`channels/zalo-personal/bridge.py`) — not through the Rust backend.
 */
export default function ZaloPersonalConnect({
  onConnected,
}: {
  onConnected(): void;
}) {
  const [phase, setPhase] = useState<"checking" | "offline" | "ready">("checking");
  const [status, setStatus] = useState<ZaloPersonalBridgeStatus>("qr_pending");
  const [statusError, setStatusError] = useState<string | null>(null);
  const [qrUrl, setQrUrl] = useState<string | null>(null);
  const [qrLoading, setQrLoading] = useState(false);
  const [qrError, setQrError] = useState("");
  const qrUrlRef = useRef<string | null>(null);
  const timersRef = useRef<number[]>([]);
  const notifiedRef = useRef(false);

  function clearTimers() {
    for (const t of timersRef.current) window.clearInterval(t);
    timersRef.current = [];
  }

  const revokeQr = useCallback(() => {
    if (qrUrlRef.current) {
      URL.revokeObjectURL(qrUrlRef.current);
      qrUrlRef.current = null;
    }
  }, []);

  const loadQr = useCallback(async () => {
    setQrError("");
    setQrLoading(true);
    try {
      const url = await getZaloPersonalQR();
      revokeQr();
      qrUrlRef.current = url;
      setQrUrl(url);
    } catch (e) {
      setQrError(e instanceof Error ? e.message : "Could not load QR code");
    } finally {
      setQrLoading(false);
    }
  }, [revokeQr]);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await getZaloPersonalStatus();
      setStatus(s.status);
      setStatusError(s.error);
      if (s.status === "connected" && !notifiedRef.current) {
        notifiedRef.current = true;
        clearTimers();
        onConnected();
      }
    } catch {
      // Bridge went away mid-session — flip back to offline.
      setPhase("offline");
      clearTimers();
    }
  }, [onConnected]);

  const start = useCallback(async () => {
    clearTimers();
    revokeQr();
    setQrUrl(null);
    notifiedRef.current = false;
    setPhase("checking");
    const running = await checkBridgeRunning();
    if (!running) {
      setPhase("offline");
      return;
    }
    setPhase("ready");
    await refreshStatus();
    await loadQr();
    timersRef.current.push(window.setInterval(() => void loadQr(), QR_REFRESH_MS));
    timersRef.current.push(window.setInterval(() => void refreshStatus(), STATUS_POLL_MS));
  }, [loadQr, refreshStatus, revokeQr]);

  useEffect(() => {
    void start();
    return () => {
      clearTimers();
      revokeQr();
    };
  }, [start, revokeQr]);

  // --- Bridge offline -----------------------------------------------------
  if (phase === "checking") {
    return (
      <p className="flex items-center gap-1.5 text-xs text-[var(--muted)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Đang kiểm tra bridge…
      </p>
    );
  }

  if (phase === "offline") {
    return (
      <div className="space-y-2">
        <p className="text-xs text-[var(--muted)]">
          Bridge Zalo Personal chưa chạy. Khởi động nó rồi bấm thử lại:
        </p>
        <code className="block rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 font-mono text-[11px] text-[var(--text)]">
          python channels/zalo-personal/bridge.py
        </code>
        <div className="flex gap-1.5 pt-0.5">
          <button type="button" onClick={() => void start()} className={btnCls}>
            <RefreshCw className="h-3.5 w-3.5" />
            Thử lại
          </button>
        </div>
        <p className="text-[11px] text-[var(--muted)]">
          Lưu ý: đây là API không chính thức — nên dùng tài khoản phụ, Zalo có thể
          khóa tài khoản tự động hóa.
        </p>
      </div>
    );
  }

  // --- Connected ----------------------------------------------------------
  if (status === "connected") {
    return (
      <div className="space-y-2">
        <p className="flex items-center gap-1.5 text-xs text-[var(--text)]">
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
          Đã kết nối Zalo Personal
        </p>
        <p className="text-[11px] text-[var(--muted)]">
          Bridge đang chạy — tắt bridge để ngắt kết nối.
        </p>
        <div className="flex gap-1.5 pt-0.5">
          <button type="button" onClick={() => void refreshStatus()} className={btnCls}>
            <Unplug className="h-3.5 w-3.5" />
            Kiểm tra lại
          </button>
        </div>
      </div>
    );
  }

  // --- QR login -----------------------------------------------------------
  return (
    <div className="space-y-2">
      <p className="flex items-center gap-1.5 text-xs text-[var(--muted)]">
        <Smartphone className="h-3.5 w-3.5" />
        Mở app Zalo trên điện thoại → quét mã QR để đăng nhập
      </p>
      <div className="flex items-center justify-center rounded-lg border border-[var(--border)] bg-white p-3">
        {qrLoading && !qrUrl ? (
          <Loader2 className="h-8 w-8 animate-spin text-neutral-400" />
        ) : qrUrl ? (
          <img
            src={qrUrl}
            alt="Mã QR đăng nhập Zalo"
            className="h-40 w-40"
            draggable={false}
          />
        ) : (
          <QrCode className="h-10 w-10 text-neutral-300" />
        )}
      </div>
      {statusError && (
        <p className="text-xs text-[var(--muted)]" role="alert">{statusError}</p>
      )}
      {qrError && (
        <p className="text-xs text-[var(--muted)]" role="alert">{qrError}</p>
      )}
      <div className="flex gap-1.5 pt-0.5">
        <button type="button" onClick={() => void loadQr()} disabled={qrLoading} className={btnCls}>
          {qrLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          Đổi mã QR
        </button>
      </div>
      <p className="text-[11px] text-[var(--muted)]">
        Mã QR tự đổi mỗi 10 giây (hết hạn sau 2 phút). Đang tự kiểm tra đăng nhập…
      </p>
    </div>
  );
}
