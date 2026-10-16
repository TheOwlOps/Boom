import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Plug,
  Save,
  XCircle,
} from "lucide-react";
import { API_BASE } from "../lib/api";

// Backend contract (gateway/api.py, extends webui API_CONTRACT.md):
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

interface CardState {
  apiKey: string;
  model: string;
  baseUrl: string;
  showKey: boolean;
  conn: ConnState;
  connMessage: string | null;
  saving: boolean;
  error: string | null;
}

function initialCard(meta: ProviderMeta, drafts: LocalDrafts): CardState {
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
      <span className="inline-flex items-center gap-1.5 rounded-full border border-neutral-700 bg-neutral-800 px-2.5 py-1 text-xs text-neutral-300">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Testing…
      </span>
    );
  }
  const ok = state === "ok";
  return (
    <span
      title={message ?? undefined}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
        ok
          ? "border-emerald-800 bg-emerald-950 text-emerald-300"
          : "border-red-800 bg-red-950 text-red-300"
      }`}
    >
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
      {ok ? "Connected" : "Failed"}
    </span>
  );
}

function ProviderCard({
  meta,
  active,
  card,
  onChange,
}: {
  meta: ProviderMeta;
  active: boolean;
  card: CardState;
  onChange: (patch: Partial<CardState>) => void;
}) {
  const set = onChange;

  async function handleSave(): Promise<void> {
    set({ saving: true, error: null });
    try {
      // 1. Local cache (drafts survive reloads even if the backend is down).
      const drafts = readLocalDrafts();
      drafts[meta.id] = {
        api_key: card.apiKey,
        model: card.model,
        base_url: card.baseUrl,
      };
      writeLocalDrafts(drafts);
      // 2. Backend (source of truth; writes ~/.boom/providers.json 0600).
      const res = await fetch(`${API_BASE}/api/providers/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider_id: meta.id,
          api_key: card.apiKey,
          model: card.model,
          base_url: meta.show_base_url ? card.baseUrl : undefined,
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
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "Save failed" });
    } finally {
      set({ saving: false });
    }
  }

  async function handleTest(): Promise<void> {
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

  const inputCls =
    "w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500 focus:border-neutral-500 focus:outline-none";
  const labelCls =
    "mb-1 block text-xs font-medium uppercase tracking-wide text-neutral-400";

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Plug className="h-4 w-4 text-neutral-400" />
          <h3 className="text-sm font-semibold text-neutral-100">{meta.name}</h3>
          {active && (
            <span className="rounded-full border border-sky-800 bg-sky-950 px-2 py-0.5 text-[11px] font-medium text-sky-300">
              Active
            </span>
          )}
          {meta.has_key && !active && (
            <span className="rounded-full border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-[11px] text-neutral-400">
              Key saved
            </span>
          )}
        </div>
        <StatusPill state={card.conn} message={card.connMessage} />
      </div>

      {meta.needs_key && (
        <div className="mb-3">
          <label className={labelCls} htmlFor={`${meta.id}-key`}>
            API key
          </label>
          <div className="relative">
            <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-500" />
            <input
              id={`${meta.id}-key`}
              type={card.showKey ? "text" : "password"}
              value={card.apiKey}
              onChange={(e) => set({ apiKey: e.target.value, conn: "idle" })}
              placeholder={meta.has_key ? "•••••••• (saved)" : "Enter API key"}
              autoComplete="off"
              spellCheck={false}
              className={`${inputCls} pl-9 pr-10`}
            />
            <button
              type="button"
              onClick={() => set({ showKey: !card.showKey })}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-neutral-500 hover:text-neutral-300"
              aria-label={card.showKey ? "Hide API key" : "Show API key"}
            >
              {card.showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </button>
          </div>
        </div>
      )}

      <div className="mb-3">
        <label className={labelCls} htmlFor={`${meta.id}-model`}>
          Model
        </label>
        {meta.models.length > 0 && !meta.allow_custom_model ? (
          <select
            id={`${meta.id}-model`}
            value={card.model}
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
            value={meta.models.includes(card.model) ? card.model : "__custom"}
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
          (meta.models.length === 0 || !meta.models.includes(card.model))) && (
          <input
            id={meta.models.length > 0 ? undefined : `${meta.id}-model`}
            type="text"
            value={card.model}
            onChange={(e) => set({ model: e.target.value, conn: "idle" })}
            placeholder="Model name (e.g. provider/model-id)"
            autoComplete="off"
            spellCheck={false}
            className={`${inputCls} ${meta.models.length > 0 ? "mt-2" : ""}`}
          />
        )}
      </div>

      {meta.show_base_url && (
        <div className="mb-3">
          <label className={labelCls} htmlFor={`${meta.id}-url`}>
            Base URL
          </label>
          <input
            id={`${meta.id}-url`}
            type="text"
            value={card.baseUrl}
            onChange={(e) => set({ baseUrl: e.target.value, conn: "idle" })}
            placeholder={meta.default_base_url || "https://your-endpoint/v1"}
            autoComplete="off"
            spellCheck={false}
            className={inputCls}
          />
        </div>
      )}

      {card.connMessage && card.conn !== "idle" && (
        <p
          className={`mb-3 text-xs ${
            card.conn === "ok" ? "text-emerald-400" : "text-red-400"
          }`}
        >
          {card.connMessage}
        </p>
      )}
      {card.error && (
        <p className="mb-3 rounded-lg border border-red-900 bg-red-950 px-3 py-2 text-xs text-red-300">
          {card.error}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="button"
          onClick={handleSave}
          disabled={card.saving}
          className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-50"
        >
          {card.saving ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          Save
        </button>
        <button
          type="button"
          onClick={handleTest}
          disabled={card.conn === "testing"}
          className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-50"
        >
          {card.conn === "testing" ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Plug className="h-4 w-4" />
          )}
          Test connection
        </button>
      </div>
    </div>
  );
}

export function ProviderSettings() {
  const [metas, setMetas] = useState<ProviderMeta[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [cards, setCards] = useState<Record<string, CardState>>({});
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
        const next: Record<string, CardState> = {};
        for (const meta of parsed.providers) {
          next[meta.id] = initialCard(meta, drafts);
        }
        if (mounted.current) {
          setMetas(parsed.providers);
          setActiveId(parsed.active);
          setCards(next);
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

  const updateCard = useCallback((id: string, patch: Partial<CardState>) => {
    setCards((prev) => {
      const cur = prev[id];
      if (!cur) return prev;
      return { ...prev, [id]: { ...cur, ...patch } };
    });
  }, []);

  if (loadError) {
    return (
      <div className="rounded-xl border border-red-900 bg-red-950 p-4 text-sm text-red-300">
        Failed to load providers: {loadError}
      </div>
    );
  }
  if (metas === null) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-neutral-800 bg-neutral-900 p-4 text-sm text-neutral-400">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading providers…
      </div>
    );
  }
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {metas.map((meta) => (
        <ProviderCard
          key={meta.id}
          meta={meta}
          active={meta.id === activeId}
          card={cards[meta.id] ?? initialCard(meta, {})}
          onChange={(patch) => updateCard(meta.id, patch)}
        />
      ))}
    </div>
  );
}
