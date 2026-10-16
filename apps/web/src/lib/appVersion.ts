/**
 * Current app version resolution.
 *
 * The Tauri desktop shell is the source of truth: `getVersion()` reads the
 * bundle version from native app metadata. In a plain browser (web dev /
 * preview) there is no native shell, so we fall back to the build-time
 * `VITE_APP_VERSION` env var, then to the npm package version, and finally
 * to a hardcoded fallback. Never throws.
 */

import packageJson from "../../package.json";

/** Hardcoded last-resort version when nothing else is available. */
const FALLBACK_VERSION = "0.1.0";

/**
 * Detects whether the frontend runs inside the Tauri desktop shell.
 * Safe to call anywhere (including plain browsers): it only reads a
 * well-known property off `window`.
 */
export function isTauriEnvironment(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

let cachedVersion: Promise<string> | null = null;

/**
 * Resolves the current app version. The result is cached after the first
 * call so repeated reads (update banner, About page) do not re-invoke the
 * native bridge.
 */
export function getAppVersion(): Promise<string> {
  if (cachedVersion) return cachedVersion;
  cachedVersion = (async (): Promise<string> => {
    if (isTauriEnvironment()) {
      try {
        // Dynamic import: `@tauri-apps/api/app` is only meaningful inside
        // the Tauri shell; keep the static module graph web-safe.
        const { getVersion } = await import("@tauri-apps/api/app");
        const nativeVersion = await getVersion();
        if (typeof nativeVersion === "string" && nativeVersion.length > 0) {
          return nativeVersion;
        }
      } catch {
        /* Native bridge unavailable — fall through to build-time values. */
      }
    }
    const envVersion: unknown = import.meta.env.VITE_APP_VERSION;
    if (typeof envVersion === "string" && envVersion.length > 0) {
      return envVersion;
    }
    const pkgVersion: unknown = packageJson.version;
    return typeof pkgVersion === "string" && pkgVersion.length > 0
      ? pkgVersion
      : FALLBACK_VERSION;
  })();
  return cachedVersion;
}
