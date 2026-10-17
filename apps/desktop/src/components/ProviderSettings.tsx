import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Plug,
  Save,
  XCircle,
} from "lucide-react";
import { API_BASE } from "../lib/api";
import ProviderLogo from "./ProviderLogo";

// Backend contract (crates/boom-core (Rust), extends webui API_CONTRACT.md):
//   GET  /api/providers        -> { providers: ProviderMeta[], active: string | null }
//   POST /api/providers/config -> { provider_id, api_key, model, base_url? } -> { ok: true }
//   POST /api/providers/test   -> { provider_id } -> { ok: boolean, message: string }

interface ProviderMeta {
  id: string;
  name: string;
  models: string[];
  needs_key: boolean;
  default_base_url: string;
  show_base_url: boolean;
  allow_custom_model: boolean;
  has_key: boolean;
}

interface ProvidersResponse {
  providers: ProviderMeta[];
  active: string | null;
}

/** Local cache of per-provider drafts (keys stay on this machine only). */
const STORAGE_KEY = "boom.providerConfig";
type LocalDrafts = Record<
  string,
  { api_key?: string; model?: string; base_url?: string }
>;

function readLocalDrafts(): LocalDrafts {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return {};
    return parsed as LocalDrafts;
  } catch {
    return {};
  }
}

function writeLocalDrafts(drafts: LocalDrafts): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(drafts));
  } catch {
    // Storage full or unavailable: backend remains the source of truth.
  }
}

type ConnState = "idle" | "testing" | "ok" | "fail";

interface RowState {
  apiKey: string;
  model: string;
  baseUrl: string;
  showKey: boolean;
  conn: ConnState;
  connMessage: string | null;
  saving: boolean;
  error: string | null;
}

function initialRow(meta: ProviderMeta, drafts: LocalDrafts): RowState {
  const draft = drafts[meta.id] ?? {};
  return {
    apiKey: draft.api_key ?? "",
    model: draft.model ?? meta.models[0] ?? "",
    baseUrl: draft.base_url ?? meta.default_base_url,
    showKey: false,
    conn: "idle",
    connMessage: null,
    saving: false,
    error: null,
  };
}

function StatusPill({ state, message }: { state: ConnState; message: string | null }) {
  if (state === "idle") return null;
  if (state === "testing") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1 text-xs text-[var(--text)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Testing…
      </span>
    );
  }
  const ok = state === "ok";
  return (
    <span
      title={message ?? undefined}
      className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg)] px-2.5 py-1 text-xs text-[var(--text)]"
    >
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
      {ok ? "Connected" : "Failed"}
    </span>
  );
}

function ProviderRow({
  meta,
  active,
  row,
  expanded,
  onToggle,
  onChange,
}: {
  meta: ProviderMeta;
  active: boolean;
  row: RowState;
  expanded: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<RowState>) => void;
}) {
  const set = onChange;

  /** Persist the current form values; returns true when the save succeeded. */
  async function saveConfig(): Promise<boolean> {
    set({ saving: true, error: null });
    try {
      // 1. Local cache (drafts survive reloads even if the backend is down).
      const drafts = readLocalDrafts();
      drafts[meta.id] = {
        api_key: row.apiKey,
        model: row.model,
        base_url: row.baseUrl,
      };
      writeLocalDrafts(drafts);
      // 2. Backend (source of truth; writes ~/.boom/providers.json 0600).
      // An untouched key input sends "" — the backend keeps the stored key
      // (sentinel "keep existing").
      const res = await fetch(`${API_BASE}/api/providers/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider_id: meta.id,
          api_key: row.apiKey,
          model: row.model,
          base_url: meta.show_base_url ? row.baseUrl : undefined,
        }),
      });
      if (!res.ok) {
        const body: unknown = await res.json().catch(() => null);
        const msg =
          typeof body === "object" && body !== null && "error" in body
            ? String((body as { error: unknown }).error)
            : `HTTP ${res.status}`;
        throw new Error(msg);
      }
      return true;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "Save failed" });
      return false;
    } finally {
      set({ saving: false });
    }
  }

  async function handleSave(): Promise<void> {
    await saveConfig();
  }

  async function handleTest(): Promise<void> {
    // The probe must run against the values currently in the form, not the
    // previously saved config — save first, then test.
    const saved = await saveConfig();
    if (!saved) return;
    set({ conn: "testing", connMessage: null, error: null });
    try {
      const res = await fetch(`${API_BASE}/api/providers/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: meta.id }),
      });
      const body: unknown = await res.json().catch(() => null);
      if (!res.ok) {
        const msg =
          typeof body === "object" && body !== null && "error" in body
            ? String((body as { error: unknown }).error)
            : `HTTP ${res.status}`;
        throw new Error(msg);
      }
      const ok =
        typeof body === "object" && body !== null && "ok" in body
          ? Boolean((body as { ok: unknown }).ok)
          : false;
      const message =
        typeof body === "object" && body !== null && "message" in body
          ? String((body as { message: unknown }).message)
          : null;
      set({ conn: ok ? "ok" : "fail", connMessage: message });
    } catch (e) {
      set({
        conn: "fail",
        connMessage: null,
        error: e instanceof Error ? e.message : "Test failed",
      });
    }
  }

  const connected = meta.has_key || row.conn === "ok";

  const inputCls =
    "w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)] focus:border-[var(--muted)] focus:outline-none";
  const labelCls =
    "mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--muted)]";

  return (
    <li>
      {/* Row header — click to expand the detail form (accordion). */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-label={`${meta.name} provider settings`}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-[var(--surface-2)]"
      >
        <ProviderLogo
          providerId={meta.id}
          size={18}
          colored
          className="shrink-0"
        />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[var(--text)]">
          {meta.name}
        </span>
        {active && (
          <span className="shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg)] px-1.5 py-px text-[10px] font-medium text-[var(--text)]">
            Active
          </span>
        )}
        <span className="flex shrink-0 items-center gap-1">
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 rounded-full ${
              connected ? "bg-emerald-500" : "bg-[var(--border)]"
            }`}
          />
          <span className="whitespace-nowrap text-[11px] text-[var(--muted)]">
            {connected ? "Connected" : "Not configured"}
          </span>
        </span>
        <ChevronDown
          aria-hidden="true"
          className={`h-3.5 w-3.5 shrink-0 text-[var(--muted)] transition-transform ${
            expanded ? "rotate-180" : ""
          }`}
        />
      </button>

      {/* Expanded detail form — compact. */}
      {expanded && (
        <div className="space-y-2.5 border-t border-[var(--border)] bg-[var(--bg)] px-3 py-3">
          {meta.needs_key && (
            <div>
              <label className={labelCls} htmlFor={`${meta.id}-key`}>
                API key
              </label>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--muted)]" />
                <input
                  id={`${meta.id}-key`}
                  type={row.showKey ? "text" : "password"}
                  value={row.apiKey}
                  onChange={(e) => set({ apiKey: e.target.value, conn: "idle" })}
                  placeholder={meta.has_key ? "•••••••• (saved)" : "Enter API key"}
                  autoComplete="off"
                  spellCheck={false}
                  className={`${inputCls} pl-9 pr-10`}
                />
                <button
                  type="button"
                  onClick={() => set({ showKey: !row.showKey })}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-[var(--muted)] hover:text-[var(--text)]"
                  aria-label={row.showKey ? "Hide API key" : "Show API key"}
                >
                  {row.showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>
          )}

          <div>
            <label className={labelCls} htmlFor={`${meta.id}-model`}>
              Model
            </label>
            {meta.models.length > 0 && !meta.allow_custom_model ? (
              <select
                id={`${meta.id}-model`}
                value={row.model}
                onChange={(e) => set({ model: e.target.value, conn: "idle" })}
                className={inputCls}
              >
                {meta.models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            ) : meta.models.length > 0 ? (
              <select
                id={`${meta.id}-model`}
                value={meta.models.includes(row.model) ? row.model : "__custom"}
                onChange={(e) =>
                  set({
                    model: e.target.value === "__custom" ? "" : e.target.value,
                    conn: "idle",
                  })
                }
                className={inputCls}
              >
                {meta.models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
                <option value="__custom">Custom…</option>
              </select>
            ) : null}
            {(meta.allow_custom_model &&
              (meta.models.length === 0 || !meta.models.includes(row.model))) && (
              <input
                id={meta.models.length > 0 ? undefined : `${meta.id}-model`}
                type="text"
                value={row.model}
                onChange={(e) => set({ model: e.target.value, conn: "idle" })}
                placeholder="Model name (e.g. provider/model-id)"
                autoComplete="off"
                spellCheck={false}
                className={`${inputCls} ${meta.models.length > 0 ? "mt-2" : ""}`}
              />
            )}
          </div>

          {meta.show_base_url && (
            <div>
              <label className={labelCls} htmlFor={`${meta.id}-url`}>
                Base URL
              </label>
              <input
                id={`${meta.id}-url`}
                type="text"
                value={row.baseUrl}
                onChange={(e) => set({ baseUrl: e.target.value, conn: "idle" })}
                placeholder={meta.default_base_url || "https://your-endpoint/v1"}
                autoComplete="off"
                spellCheck={false}
                className={inputCls}
              />
            </div>
          )}

          <StatusPill state={row.conn} message={row.connMessage} />
          {row.connMessage && row.conn !== "idle" && (
            <p className="text-xs text-[var(--muted)]">{row.connMessage}</p>
          )}
          {row.error && (
            <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-xs text-[var(--text)]">
              {row.error}
            </p>
          )}

          <div className="flex gap-1.5 pt-0.5">
            <button
              type="button"
              onClick={handleSave}
              disabled={row.saving}
              className="inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50"
            >
              {row.saving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Save className="h-3.5 w-3.5" />
              )}
              Save
            </button>
            <button
              type="button"
              onClick={handleTest}
              disabled={row.conn === "testing" || row.saving}
              title="Saves the current values first, then tests the connection"
              className="inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50"
            >
              {row.conn === "testing" || row.saving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Plug className="h-3.5 w-3.5" />
              )}
              Test
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

export function ProviderSettings() {
  const [metas, setMetas] = useState<ProviderMeta[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Load provider metadata from the backend on mount.
  useEffect(() => {
    let cancelled = false;
    async function load(): Promise<void> {
      try {
        const res = await fetch(`${API_BASE}/api/providers`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body: unknown = await res.json();
        if (
          typeof body !== "object" ||
          body === null ||
          !Array.isArray((body as ProvidersResponse).providers)
        ) {
          throw new Error("malformed /api/providers response");
        }
        const parsed = body as ProvidersResponse;
        if (cancelled) return;
        const drafts = readLocalDrafts();
        const next: Record<string, RowState> = {};
        for (const meta of parsed.providers) {
          next[meta.id] = initialRow(meta, drafts);
        }
        if (mounted.current) {
          setMetas(parsed.providers);
          setActiveId(parsed.active);
          setRows(next);
          // Open the most relevant provider by default: the active one,
          // otherwise the first with a saved key, otherwise the first row.
          setExpandedId(
            parsed.active ??
              parsed.providers.find((p) => p.has_key)?.id ??
              parsed.providers[0]?.id ??
              null,
          );
        }
      } catch (e) {
        if (!cancelled && mounted.current) {
          setLoadError(e instanceof Error ? e.message : "Failed to load providers");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const updateRow = useCallback((id: string, patch: Partial<RowState>) => {
    setRows((prev) => {
      const cur = prev[id];
      if (!cur) return prev;
      return { ...prev, [id]: { ...cur, ...patch } };
    });
  }, []);

  if (loadError) {
    return (
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg)] p-4 text-sm text-[var(--text)]">
        Failed to load providers: {loadError}
      </div>
    );
  }
  if (metas === null) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 text-sm text-[var(--muted)]">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading providers…
      </div>
    );
  }
  return (
    <div>
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-[var(--text)]">Providers</h3>
        <p className="mt-1 text-xs text-[var(--muted)]">
          Configure API keys and models for each LLM provider. Click a provider
          to edit its settings.
        </p>
      </div>
      <ul className="divide-y divide-[var(--border)] overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--surface)]">
        {metas.map((meta) => (
          <ProviderRow
            key={meta.id}
            meta={meta}
            active={meta.id === activeId}
            row={rows[meta.id] ?? initialRow(meta, {})}
            expanded={expandedId === meta.id}
            onToggle={() =>
              setExpandedId((cur) => (cur === meta.id ? null : meta.id))
            }
            onChange={(patch) => updateRow(meta.id, patch)}
          />
        ))}
      </ul>
    </div>
  );
}
