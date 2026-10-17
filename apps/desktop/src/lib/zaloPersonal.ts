/**
 * Zalo Personal bridge client.
 *
 * The Zalo Personal bridge is a separate minimal Python process
 * (`channels/zalo-personal/bridge.py`) running at 127.0.0.1:PORT
 * (default 17890). It is NOT part of the Rust backend — it speaks the
 * unofficial Zalo personal API via QR login.
 *
 * The bridge URL is configurable:
 * - Vite env `VITE_ZALO_BRIDGE_URL` (e.g. in `.env`), or
 * - localStorage key `boom.zaloBridgeUrl` (Settings override), or
 * - default `http://127.0.0.1:17890` (port from `BOOM_ZALO_BRIDGE_PORT`).
 */

const DEFAULT_PORT = 17890;
const LS_KEY = "boom.zaloBridgeUrl";

/** Base URL of the Zalo Personal bridge (no trailing slash). */
export function getZaloPersonalBridgeUrl(): string {
  try {
    const stored = window.localStorage.getItem(LS_KEY);
    if (stored && stored.trim()) return stored.trim().replace(/\/+$/, "");
  } catch {
    // localStorage unavailable — fall through to defaults.
  }
  const envUrl = (import.meta as unknown as { env?: Record<string, string | undefined> }).env
    ?.VITE_ZALO_BRIDGE_URL;
  if (envUrl && envUrl.trim()) return envUrl.trim().replace(/\/+$/, "");
  return `http://127.0.0.1:${DEFAULT_PORT}`;
}

/** Persist a custom bridge URL (empty string clears the override). */
export function setZaloPersonalBridgeUrl(url: string): void {
  try {
    if (url.trim()) {
      window.localStorage.setItem(LS_KEY, url.trim().replace(/\/+$/, ""));
    } else {
      window.localStorage.removeItem(LS_KEY);
    }
  } catch {
    // ignore — localStorage unavailable
  }
}

export type ZaloPersonalBridgeStatus = "qr_pending" | "connected" | "error" | "starting";

export interface ZaloPersonalStatus {
  status: ZaloPersonalBridgeStatus;
  error: string | null;
  qr_available: boolean;
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${getZaloPersonalBridgeUrl()}${path}`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Bridge responded ${res.status}`);
  }
  return (await res.json()) as T;
}

/**
 * `true` when the bridge process answers at all. Any HTTP response
 * (even 4xx/5xx) counts as "running" — only a network failure means
 * the bridge is down.
 */
export async function checkBridgeRunning(): Promise<boolean> {
  try {
    await fetch(`${getZaloPersonalBridgeUrl()}/status`, { cache: "no-store" });
    return true;
  } catch {
    return false;
  }
}

/** GET /status — never throws when the bridge is down (caller checks first). */
export async function getZaloPersonalStatus(): Promise<ZaloPersonalStatus> {
  const v = await fetchJson<Record<string, unknown>>("/status");
  const status = v.status;
  return {
    status:
      status === "connected" || status === "error" || status === "starting"
        ? status
        : "qr_pending",
    error: typeof v.error === "string" ? v.error : null,
    qr_available: v.qr_available === true,
  };
}

/**
 * GET /qr — returns an object URL for the PNG QR code.
 * Throws when the QR is expired/unavailable (bridge answers 404).
 * Callers must revoke the URL with `URL.revokeObjectURL` when done.
 */
export async function getZaloPersonalQR(): Promise<string> {
  const res = await fetch(`${getZaloPersonalBridgeUrl()}/qr`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(res.status === 404 ? "QR code expired" : `Bridge responded ${res.status}`);
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}
