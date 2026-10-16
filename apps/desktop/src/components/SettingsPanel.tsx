import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Cpu,
  Loader2,
  Monitor,
  Moon,
  Plug,
  Server,
  Sun,
  Type,
  X,
  XCircle,
} from "lucide-react";
// Seam: `../lib/api` is staged by the api-client worker. It MUST export
// getHealth() -> Promise<{ status: string; version: string; model: string }>
// and API_BASE (the base URL getHealth talks to).
import { getHealth, API_BASE } from "../lib/api";
import { useTheme, type ThemeMode } from "../lib/useTheme";

type FontSize = "small" | "medium" | "large";

const FONT_STORAGE_KEY = "boom-font-size";
const FONT_SCALES: Record<FontSize, number> = {
  small: 0.9375,
  medium: 1,
  large: 1.0625,
};

const THEME_OPTIONS: { value: ThemeMode; label: string; Icon: typeof Moon }[] = [
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "light", label: "Light", Icon: Sun },
  { value: "system", label: "System", Icon: Monitor },
];

const FONT_OPTIONS: { value: FontSize; label: string }[] = [
  { value: "small", label: "Small" },
  { value: "medium", label: "Medium" },
  { value: "large", label: "Large" },
];

function readFontSize(): FontSize {
  try {
    const v = window.localStorage.getItem(FONT_STORAGE_KEY);
    return v === "small" || v === "large" ? v : "medium";
  } catch {
    return "medium";
  }
}

function applyFontSize(size: FontSize): void {
  document.documentElement.style.setProperty(
    "--font-scale",
    String(FONT_SCALES[size]),
  );
}

type ConnState = "idle" | "testing" | "ok" | "fail";
type ConnResult = { status: ConnState; detail: string | null };

interface SettingsPanelProps {
  open: boolean;
  onClose: () => void;
}

export function SettingsPanel({ open, onClose }: SettingsPanelProps) {
  const { mode, setMode } = useTheme();
  const [fontSize, setFontSize] = useState<FontSize>(readFontSize);
  const [model, setModel] = useState<string | null>(null);
  const [version, setVersion] = useState<string | null>(null);
  const [modelError, setModelError] = useState<string | null>(null);
  const [conn, setConn] = useState<ConnResult>({ status: "idle", detail: null });
  const closeRef = useRef<HTMLButtonElement>(null);

  // Model info is read-only: backend has no model-switch API (see API_CONTRACT),
  // so we display whatever /api/health reports — never a fake dropdown.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (cancelled) return;
        setModel(h.model);
        setVersion(h.version);
        setModelError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setModel(null);
        setVersion(null);
        setModelError(e instanceof Error ? e.message : "Health check failed");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    applyFontSize(fontSize);
  }, [fontSize]);

  // Escape closes; focus the close button on open.
  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const handleFontChange = (s: FontSize) => {
    setFontSize(s);
    try {
      window.localStorage.setItem(FONT_STORAGE_KEY, s);
    } catch {
      // storage unavailable: font still applies for this session
    }
  };

  const testConnection = () => {
    setConn({ status: "testing", detail: null });
    getHealth()
      .then((h) => {
        setModel(h.model);
        setVersion(h.version);
        setModelError(null);
        setConn({
          status: "ok",
          detail: `Connected — version ${h.version}`,
        });
      })
      .catch((e: unknown) => {
        setConn({
          status: "fail",
          detail: e instanceof Error ? e.message : "Connection failed",
        });
      });
  };

  const row = (icon: React.ReactNode, label: string, desc: string) => (
    <div className="flex items-start gap-3">
      <span className="settings-icon">{icon}</span>
      <div className="settings-text">
        <div className="settings-label">{label}</div>
        <div className="settings-desc">{desc}</div>
      </div>
    </div>
  );

  return (
    <div
      className={`settings-overlay ${open ? "is-open" : ""}`}
      aria-hidden={!open}
      onClick={onClose}
    >
      <aside
        className={`settings-drawer ${open ? "is-open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Settings</h2>
          <button
            ref={closeRef}
            type="button"
            className="settings-close"
            onClick={onClose}
            aria-label="Close settings"
          >
            <X size={18} />
          </button>
        </header>

        <div className="settings-body">
          {/* Model — read-only: no backend API to change the model. */}
          <section className="settings-section">
            {row(
              <Cpu size={16} />,
              "Model",
              "Active LLM from the backend health endpoint.",
            )}
            <div className="settings-value">
              {model ? (
                <>
                  <span className="settings-model-name">{model}</span>
                  {version && (
                    <span className="settings-version">v{version}</span>
                  )}
                </>
              ) : (
                <span className="settings-error">
                  {modelError ?? "Loading…"}
                </span>
              )}
            </div>
          </section>

          {/* Theme — client-side only, stored in localStorage. */}
          <section className="settings-section">
            {row(
              <Moon size={16} />,
              "Theme",
              "Client-side only — stored in this browser (boom-theme).",
            )}
            <div
              className="settings-segmented"
              role="radiogroup"
              aria-label="Theme"
            >
              {THEME_OPTIONS.map(({ value, label, Icon }) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={mode === value}
                  className={`settings-option ${mode === value ? "is-active" : ""}`}
                  onClick={() => setMode(value)}
                >
                  <Icon size={14} />
                  {label}
                </button>
              ))}
            </div>
          </section>

          {/* Font size — client-side only, stored in localStorage. */}
          <section className="settings-section">
            {row(
              <Type size={16} />,
              "Font size",
              "Client-side only — scales UI text via --font-scale.",
            )}
            <div
              className="settings-segmented"
              role="radiogroup"
              aria-label="Font size"
            >
              {FONT_OPTIONS.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={fontSize === value}
                  className={`settings-option ${fontSize === value ? "is-active" : ""}`}
                  onClick={() => handleFontChange(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </section>

          {/* Backend — live health check, honest result. */}
          <section className="settings-section">
            {row(
              <Server size={16} />,
              "Backend",
              "API the desktop app is currently talking to.",
            )}
            <code className="settings-base-url">{API_BASE}</code>
            <button
              type="button"
              className="settings-test-btn"
              onClick={testConnection}
              disabled={conn.status === "testing"}
            >
              {conn.status === "testing" ? (
                <Loader2 size={14} className="spin" />
              ) : (
                <Plug size={14} />
              )}
              {conn.status === "testing" ? "Testing…" : "Test connection"}
            </button>
            {conn.status !== "idle" && (
              <div
                className={`settings-conn ${conn.status}`}
                role="status"
                aria-live="polite"
              >
                {conn.status === "ok" && <CheckCircle2 size={14} />}
                {conn.status === "fail" && <XCircle size={14} />}
                <span>{conn.detail}</span>
              </div>
            )}
          </section>
        </div>
      </aside>
    </div>
  );
}
