import { useCallback, useEffect, useState } from "react";
import {
  Bell,
  Info,
  Moon,
  Palette,
  Plug,
  Sun,
  Type,
  Volume2,
  type LucideIcon,
} from "lucide-react";
// Provider list is owned by another worker; this page embeds it in the
// Providers section. The module is expected to export a named
// `ProviderSettings` React component.
import { ProviderSettings } from "./ProviderSettings";
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

const APP_VERSION: string = packageJson.version;
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
                description="Web UI version read from package.json."
                control={
                  <span className="rounded-md bg-[var(--surface-2)] px-2.5 py-1 font-mono text-xs text-[var(--text)]">
                    v{APP_VERSION}
                  </span>
                }
              />
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
            </SectionCard>
          )}
        </div>
      </div>
    </div>
  );
}
