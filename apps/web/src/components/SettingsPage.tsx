import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Download,
  Info,
  Loader2,
  Moon,
  Palette,
  Plug,
  RotateCw,
  Sun,
  Type,
  Volume2,
  Wand2,
  type LucideIcon,
} from "lucide-react";
import { relaunch } from "@tauri-apps/plugin-process";
import type {
  DownloadEvent,
  Update,
} from "@tauri-apps/plugin-updater";
// Provider list is owned by another worker; this page embeds it in the
// Providers section. The module is expected to export a named
// `ProviderSettings` React component.
import { ProviderSettings } from "./ProviderSettings";
import { useManualUpdateCheck } from "./UpdateChecker";
import { getAppVersion, isTauriEnvironment } from "../lib/appVersion";
import { SETUP_COMPLETE_KEY } from "../hooks/useSetupWizard";
import packageJson from "../../package.json";

/* ------------------------------------------------------------------ */
/* Storage keys — persisted to localStorage, applied immediately.      */
/* ------------------------------------------------------------------ */

const LS_THEME = "boom.theme";
const LS_FONT_SIZE = "boom.fontSize";
const LS_NOTIFICATIONS = "boom.notifications";
const LS_SOUND = "boom.sound";

type Theme = "dark" | "light";
type FontSize = "small" | "medium" | "large";
type SettingsSection = "providers" | "appearance" | "notifications" | "about";

const THEME_OPTIONS: ReadonlyArray<{
  value: Theme;
  label: string;
  Icon: LucideIcon;
}> = [
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "light", label: "Light", Icon: Sun },
];

const FONT_SIZE_OPTIONS: ReadonlyArray<{ value: FontSize; label: string }> = [
  { value: "small", label: "Small" },
  { value: "medium", label: "Medium" },
  { value: "large", label: "Large" },
];

/** Pixel sizes applied to the `--boom-font-size` CSS variable. */
const FONT_SIZE_VALUES: Record<FontSize, string> = {
  small: "13px",
  medium: "14px",
  large: "16px",
};

const SECTION_NAV: ReadonlyArray<{
  id: SettingsSection;
  label: string;
  Icon: LucideIcon;
}> = [
  { id: "providers", label: "Providers", Icon: Plug },
  { id: "appearance", label: "Appearance", Icon: Palette },
  { id: "notifications", label: "Notifications", Icon: Bell },
  { id: "about", label: "About", Icon: Info },
];

/* ------------------------------------------------------------------ */
/* Hooks and helpers                                                   */
/* ------------------------------------------------------------------ */

/**
 * useState mirrored to localStorage. Invalid or missing stored values fall
 * back to `initial`. Every change is written synchronously so a setting
 * never lives only in React state.
 */
function useStoredSetting<T>(
  key: string,
  initial: T,
  validate: (value: unknown) => value is T,
): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return initial;
      const parsed: unknown = JSON.parse(raw);
      return validate(parsed) ? parsed : initial;
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        // Private mode / storage unavailable: setting still applies this session.
      }
      // Let other parts of the app react immediately (e.g. a global
      // notification bus reading `boom.notifications`).
      window.dispatchEvent(
        new CustomEvent("boom:settings-changed", {
          detail: { key, value: next },
        }),
      );
    },
    [key],
  );

  return [value, set];
}

function isTheme(value: unknown): value is Theme {
  return value === "dark" || value === "light";
}

function isFontSize(value: unknown): value is FontSize {
  return value === "small" || value === "medium" || value === "large";
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean";
}

/** Applies the theme to the document root. */
function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  // Spec'd hook for the light theme.
  root.classList.toggle("light", theme === "light");
  // Keep the app's token ramp in sync — light tokens switch on data-theme.
  root.dataset.theme = theme;
}

/** Applies the font size to the document root via CSS variable. */
function applyFontSize(size: FontSize): void {
  document.documentElement.style.setProperty(
    "--boom-font-size",
    FONT_SIZE_VALUES[size],
  );
}

const GITHUB_REPO_URL = "https://github.com/TheOwlOps/Boom";

/* ------------------------------------------------------------------ */
/* Presentational building blocks                                      */
/* ------------------------------------------------------------------ */

function SectionCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5">
      <h3 className="text-sm font-semibold text-[var(--text)]">{title}</h3>
      <p className="mt-1 text-xs text-[var(--muted)]">{description}</p>
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

function SettingRow({
  icon,
  label,
  description,
  control,
}: {
  icon: React.ReactNode;
  label: string;
  description: string;
  control: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--surface-2)] text-[var(--muted)]">
          {icon}
        </span>
        <span className="min-w-0">
          <span className="block text-sm font-medium text-[var(--text)]">
            {label}
          </span>
          <span className="block text-xs text-[var(--muted)]">{description}</span>
        </span>
      </div>
      <div className="shrink-0">{control}</div>
    </div>
  );
}

/** Update check lifecycle state shown in the About section. */
type UpdateCheckState =
  | { kind: "idle" }
  | { kind: "uptodate" }
  | { kind: "available"; version: string }
  | { kind: "downloading"; version: string; progress: number | null }
  | { kind: "installed"; version: string }
  | { kind: "error"; message: string };

/**
 * Manual update check row for the About section (Tauri desktop only).
 *
 * Reuses `useManualUpdateCheck()` from the update banner module so both
 * surfaces run the same check. On "Check for updates": the row shows a
 * spinner, then either "You're on the latest version", or the new version
 * with an "Update now" button that downloads + installs in place and
 * offers a restart. The pending `Update` handle is released when the
 * check is re-run, on unmount, or after install — it holds a backend
 * resource and must not leak.
 */
function UpdateCheckRow() {
  const { checking, checkNow } = useManualUpdateCheck();
  const [state, setState] = useState<UpdateCheckState>({ kind: "idle" });
  const pendingUpdate = useRef<Update | null>(null);

  const releasePending = useCallback(() => {
    const pending = pendingUpdate.current;
    pendingUpdate.current = null;
    if (pending) {
      // Best effort: the updater backend resource must not leak.
      void pending.close().catch(() => undefined);
    }
  }, []);

  // Release the pending update handle if the row unmounts mid-check.
  useEffect(() => releasePending, [releasePending]);

  const handleCheck = useCallback(async () => {
    if (checking) return;
    releasePending();
    const found = await checkNow();
    if (found) {
      pendingUpdate.current = found;
      setState({ kind: "available", version: found.version });
    } else {
      setState({ kind: "uptodate" });
    }
  }, [checking, checkNow, releasePending]);

  const handleInstall = useCallback(async () => {
    const pending = pendingUpdate.current;
    if (!pending || state.kind !== "available") return;
    let downloadedBytes = 0;
    let totalBytes: number | null = null;
    setState({ kind: "downloading", version: pending.version, progress: 0 });
    try {
      await pending.downloadAndInstall((event: DownloadEvent) => {
        if (event.event === "Started") {
          totalBytes = event.data.contentLength ?? null;
        } else if (event.event === "Progress") {
          downloadedBytes += event.data.chunkLength;
          setState({
            kind: "downloading",
            version: pending.version,
            progress:
              totalBytes !== null && totalBytes > 0
                ? Math.min(
                    99,
                    Math.round((downloadedBytes / totalBytes) * 100),
                  )
                : null,
          });
        }
      });
      setState({ kind: "installed", version: pending.version });
      releasePending();
    } catch (err) {
      setState({
        kind: "error",
        message:
          err instanceof Error
            ? `Update failed: ${err.message}`
            : "Update failed for an unknown reason.",
      });
      releasePending();
    }
  }, [state.kind, releasePending]);

  const handleRelaunch = useCallback(async () => {
    try {
      await relaunch();
    } catch (err) {
      setState({
        kind: "error",
        message:
          err instanceof Error
            ? `Restart failed (${err.message}). Please restart Boom manually.`
            : "Restart failed. Please restart Boom manually.",
      });
    }
  }, []);

  const description =
    state.kind === "uptodate"
      ? "You're on the latest version."
      : state.kind === "available"
        ? `Version ${state.version} is ready to install.`
        : state.kind === "downloading"
          ? state.progress === null
            ? "Downloading update…"
            : `Downloading update… ${state.progress}%`
          : state.kind === "installed"
            ? `Version ${state.version} installed — restart to apply it.`
            : state.kind === "error"
              ? state.message
              : "Check for a newer desktop release on GitHub.";

  const control =
    state.kind === "uptodate" ? (
      <span className="flex items-center gap-1.5 text-xs font-medium text-emerald-400">
        <CheckCircle2 size={14} aria-hidden="true" />
        Up to date
      </span>
    ) : state.kind === "available" ? (
      <button
        type="button"
        onClick={() => void handleInstall()}
        className="btn-hover flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 px-3 py-1.5 text-xs font-semibold text-white shadow-md shadow-purple-950/50"
      >
        <Download size={13} aria-hidden="true" />
        Update to v{state.version}
      </button>
    ) : state.kind === "downloading" ? (
      <span className="flex items-center gap-1.5 text-xs font-medium text-indigo-300">
        <Loader2 size={14} className="animate-spin" aria-hidden="true" />
        Installing…
      </span>
    ) : state.kind === "installed" ? (
      <button
        type="button"
        onClick={() => void handleRelaunch()}
        className="btn-hover flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 px-3 py-1.5 text-xs font-semibold text-white shadow-md shadow-emerald-950/50"
      >
        <RotateCw size={13} aria-hidden="true" />
        Restart now
      </button>
    ) : (
      <button
        type="button"
        onClick={() => void handleCheck()}
        disabled={checking}
        className="btn-hover flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[var(--surface-3)] disabled:cursor-not-allowed disabled:opacity-50"
      >
        {checking ? (
          <Loader2 size={13} className="animate-spin" aria-hidden="true" />
        ) : state.kind === "error" ? (
          <AlertTriangle size={13} aria-hidden="true" />
        ) : (
          <RotateCw size={13} aria-hidden="true" />
        )}
        {checking
          ? "Checking…"
          : state.kind === "error"
            ? "Retry"
            : "Check for updates"}
      </button>
    );

  return (
    <SettingRow
      icon={<RotateCw size={16} />}
      label="Updates"
      description={description}
      control={control}
    />
  );
}

/** Segmented radio-style control for a small set of options. */
function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: ReadonlyArray<{
    value: T;
    label: string;
    Icon?: LucideIcon;
  }>;
  value: T;
  onChange: (next: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="inline-flex rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-1"
    >
      {options.map(({ value: optionValue, label, Icon }) => (
        <button
          key={optionValue}
          type="button"
          role="radio"
          aria-checked={value === optionValue}
          onClick={() => onChange(optionValue)}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
            value === optionValue
              ? "bg-[var(--accent)] text-white"
              : "text-[var(--muted)] hover:text-[var(--text)]"
          }`}
        >
          {Icon && <Icon className="h-3.5 w-3.5" aria-hidden="true" />}
          {label}
        </button>
      ))}
    </div>
  );
}

/** Accessible toggle switch. */
function ToggleSwitch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full border border-[var(--border)] transition-colors ${
        checked ? "bg-[var(--accent)]" : "bg-[var(--surface-2)]"
      }`}
    >
      <span
        aria-hidden="true"
        className={`absolute top-1/2 h-4 w-4 -translate-y-1/2 rounded-full bg-white transition-all ${
          checked ? "left-[22px]" : "left-[3px]"
        }`}
      />
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* SettingsPage                                                        */
/* ------------------------------------------------------------------ */

/**
 * Full settings page: vertical section nav on the left, the active
 * section's content on the right. Every change is written to
 * localStorage and applied immediately (theme class, CSS variables,
 * and a `boom:settings-changed` event for global listeners).
 */
export default function SettingsPage() {
  const [section, setSection] = useState<SettingsSection>("providers");

  const [theme, setTheme] = useStoredSetting(LS_THEME, "dark" as Theme, isTheme);
  const [fontSize, setFontSize] = useStoredSetting<FontSize>(
    LS_FONT_SIZE,
    "medium",
    isFontSize,
  );
  const [notifications, setNotifications] = useStoredSetting(
    LS_NOTIFICATIONS,
    true,
    isBoolean,
  );
  const [sound, setSound] = useStoredSetting(LS_SOUND, true, isBoolean);

  // Current app version resolved once: Tauri native metadata in the
  // desktop shell, otherwise the build-time / package.json fallback.
  // Starts on the package version so the About row paints immediately.
  const [appVersion, setAppVersion] = useState<string>(packageJson.version);
  useEffect(() => {
    void getAppVersion().then(setAppVersion);
  }, []);

  // Apply appearance settings immediately — on mount and on every change.
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    applyFontSize(fontSize);
  }, [fontSize]);

  return (
    <div className="flex h-full min-h-0">
      {/* Section nav — left sidebar */}
      <nav
        aria-label="Settings sections"
        className="w-52 shrink-0 overflow-y-auto border-r border-[var(--border)] bg-[var(--surface)] p-3"
      >
        <h2 className="px-2 pb-2 text-sm font-semibold text-[var(--text)]">
          Settings
        </h2>
        <ul className="space-y-1">
          {SECTION_NAV.map(({ id, label, Icon }) => (
            <li key={id}>
              <button
                type="button"
                onClick={() => setSection(id)}
                aria-current={section === id ? "page" : undefined}
                className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  section === id
                    ? "bg-[var(--surface-2)] text-[var(--text)]"
                    : "text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                }`}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {label}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      {/* Section content — right */}
      <div className="min-w-0 flex-1 overflow-y-auto bg-[var(--bg)]">
        <div className="mx-auto max-w-2xl space-y-4 p-6">
          {section === "providers" && <ProviderSettings />}

          {section === "appearance" && (
            <SectionCard
              title="Appearance"
              description="Client-side only — applied immediately and stored in this browser."
            >
              <SettingRow
                icon={<Moon size={16} />}
                label="Theme"
                description="Dark or light interface. Saved as boom.theme."
                control={
                  <SegmentedControl
                    ariaLabel="Theme"
                    options={THEME_OPTIONS}
                    value={theme}
                    onChange={setTheme}
                  />
                }
              />
              <SettingRow
                icon={<Type size={16} />}
                label="Font size"
                description="Scales UI text via the --boom-font-size CSS variable. Saved as boom.fontSize."
                control={
                  <SegmentedControl
                    ariaLabel="Font size"
                    options={FONT_SIZE_OPTIONS}
                    value={fontSize}
                    onChange={setFontSize}
                  />
                }
              />
            </SectionCard>
          )}

          {section === "notifications" && (
            <SectionCard
              title="Notifications"
              description="How Boom alerts you when work finishes. Saved in this browser."
            >
              <SettingRow
                icon={<Bell size={16} />}
                label="Task completion notifications"
                description="Show a notification when the bot finishes a task. Saved as boom.notifications."
                control={
                  <ToggleSwitch
                    checked={notifications}
                    onChange={setNotifications}
                    label="Task completion notifications"
                  />
                }
              />
              <SettingRow
                icon={<Volume2 size={16} />}
                label="Sound"
                description="Play a sound with notifications. Saved as boom.sound."
                control={
                  <ToggleSwitch
                    checked={sound}
                    onChange={setSound}
                    label="Notification sound"
                  />
                }
              />
            </SectionCard>
          )}

          {section === "about" && (
            <SectionCard
              title="About"
              description="Version and links for this Boom web client."
            >
              <SettingRow
                icon={<Info size={16} />}
                label="Version"
                description="Current app version (native shell metadata in the desktop app)."
                control={
                  <span className="rounded-md bg-[var(--surface-2)] px-2.5 py-1 font-mono text-xs text-[var(--text)]">
                    v{appVersion}
                  </span>
                }
              />
              {/* Update checks need the Tauri updater plugin — desktop only. */}
              {isTauriEnvironment() && <UpdateCheckRow />}
              <SettingRow
                icon={<Plug size={16} />}
                label="Source code"
                description="Boom is developed in the open on GitHub."
                control={
                  <a
                    href={GITHUB_REPO_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs font-medium text-[var(--accent)] hover:text-[var(--accent-hover)] hover:underline"
                  >
                    TheOwlOps/Boom
                  </a>
                }
              />
              <SettingRow
                icon={<Plug size={16} />}
                label="Issues"
                description="Report bugs and request features."
                control={
                  <a
                    href={`${GITHUB_REPO_URL}/issues`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs font-medium text-[var(--accent)] hover:text-[var(--accent-hover)] hover:underline"
                  >
                    Issue tracker
                  </a>
                }
              />
              <SettingRow
                icon={<Wand2 size={16} />}
                label="Setup wizard"
                description="Re-run the first-run setup to reconfigure your provider."
                control={
                  <button
                    type="button"
                    onClick={() => {
                      try {
                        window.localStorage.removeItem(SETUP_COMPLETE_KEY);
                      } catch {
                        // Storage unavailable: reload still resets in-memory state.
                      }
                      window.location.reload();
                    }}
                    className="btn-hover flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[var(--surface-3)]"
                  >
                    <Wand2 size={13} aria-hidden="true" />
                    Run setup again
                  </button>
                }
              />
            </SectionCard>
          )}
        </div>
      </div>
    </div>
  );
}
