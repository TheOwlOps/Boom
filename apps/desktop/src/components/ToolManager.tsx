import { useEffect, useMemo, useState } from "react";

import { API_BASE } from "../lib/api";
import ChannelAccordion from "./ChannelAccordion";
import { EmptyState } from "./EmptyState";
import { SkeletonText } from "./Skeleton";
import { BoomiEmptyState } from "./BoomiEmptyState";

/** One tool as returned by GET /api/tools. */
interface ToolDef {
  name: string;
  description: string;
  group: string;
}

interface ToolsResponse {
  tools?: unknown;
  count?: number;
}

interface ConnectorSummary {
  id: string;
  name: string;
  connected: boolean;
}

interface ConnectorsResponse {
  connectors?: unknown;
}

/** Map a connector tool name → connector id (from GET /api/connectors).
 *  Most tools are `<connectorId>_<action>`; the exceptions are listed
 *  explicitly below. Returns null for non-connector tools. */
function toolToConnectorId(toolName: string): string | null {
  const n = toolName.toLowerCase();
  // Explicit exceptions first.
  if (n === "flight_search") return "duffel";
  if (n === "flight_status") return "flightaware";
  if (n.startsWith("plaid_") || n.startsWith("finance_")) return "finances";
  if (n.startsWith("event")) return "ticketmaster"; // events_search, event_details
  // Default: <connectorId>_rest — check against known connector ids.
  const KNOWN_IDS = [
    "gmail", "gcalendar", "gdrive", "github", "spotify", "social",
    "places", "health", "canva", "meta_ads", "shopify", "stripe", "tailscale",
  ];
  for (const id of KNOWN_IDS) {
    if (n === id || n.startsWith(id + "_")) return id;
  }
  return null;
}

function isToolDef(v: unknown): v is ToolDef {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.name === "string" &&
    typeof o.description === "string" &&
    typeof o.group === "string"
  );
}

function isConnectorSummary(v: unknown): v is ConnectorSummary {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return typeof o.id === "string" && typeof o.connected === "boolean";
}

/** Tool trial-run modal: enter JSON args, calls POST /api/tools/run.
 *  When the tool's connector is not connected, shows a clear "connect first"
 *  message instead of the args form — running would only return a
 *  "not connected" error from the backend. */
function RunModal({
  tool,
  connectorName,
  connectorConnected,
  onClose,
}: {
  tool: ToolDef;
  connectorName: string | null;
  connectorConnected: boolean;
  onClose(): void;
}) {
  const [args, setArgs] = useState("{}");
  const [result, setResult] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const run = async () => {
    setRunning(true);
    setResult(null);
    try {
      const parsed: unknown = JSON.parse(args); // surface JSON errors immediately instead of sending garbage
      const res = await fetch(`${API_BASE}/api/tools/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Send both field names: the backend accepts `tool` (canonical) and `name` (alias).
        body: JSON.stringify({ tool: tool.name, name: tool.name, args: parsed }),
      });
      const body: unknown = await res.json().catch(() => null);
      setResult(res.ok ? JSON.stringify(body, null, 2) : `Error ${res.status}: ${JSON.stringify(body)}`);
    } catch (err) {
      setResult(err instanceof Error ? `Error: ${err.message}` : "Unknown error");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="w-full max-w-lg rounded-lg bg-[var(--surface)] p-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-1 font-semibold text-[var(--text)]">Run: {tool.name}</h3>
        <p className="mb-3 text-sm text-[var(--muted)]">{tool.description}</p>
        {connectorName !== null && !connectorConnected ? (
          <div className="mb-3 rounded border border-[var(--border)] bg-[var(--bg)] p-3">
            <p className="text-sm font-medium text-[var(--text)]">
              {connectorName} is not connected
            </p>
            <p className="mt-1 text-sm text-[var(--muted)]">
              This tool needs {connectorName} connected to work. Open Settings →
              Connectors, connect {connectorName}, then try again.
            </p>
          </div>
        ) : (
          <>
            <label className="mb-1 block text-xs text-[var(--muted)]">Args (JSON)</label>
            <textarea
              value={args}
              onChange={(e) => setArgs(e.target.value)}
              rows={5}
              spellCheck={false}
              className="mb-3 w-full rounded border border-[var(--border)] bg-[var(--bg)] p-2 font-mono text-sm text-[var(--text)]"
            />
          </>
        )}
        {result !== null && (
          <pre className="mb-3 max-h-48 overflow-auto rounded bg-[var(--bg)] p-2 font-mono text-xs text-[var(--text)]">
            {result}
          </pre>
        )}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--text)]">
            Close
          </button>
          {connectorConnected && (
            <button
              onClick={() => void run()}
              disabled={running}
              className="rounded bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
            >
              {running ? "Running…" : "Execute"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Tool manager: channels subsection first, then tools fetched live from the backend. */
export default function ToolManager() {
  const [query, setQuery] = useState("");
  const [tools, setTools] = useState<ToolDef[] | null>(null);
  const [connectors, setConnectors] = useState<Map<string, ConnectorSummary> | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const [active, setActive] = useState<ToolDef | null>(null);

  // The tool list comes from the live backend (GET /api/tools). There is no
  // static fallback catalog on purpose: a stale catalog would show tools the
  // backend does not have, and every Run click on one would 404.
  // Connector connection status comes from GET /api/connectors so each tool
  // row can show whether its service is connected.
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    fetch(`${API_BASE}/api/tools`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`GET /api/tools → ${res.status}`);
        const body = (await res.json()) as ToolsResponse;
        const list = Array.isArray(body.tools) ? body.tools.filter(isToolDef) : [];
        if (!cancelled) setTools(list);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Could not reach the backend.");
        setTools([]);
      });
    fetch(`${API_BASE}/api/connectors`)
      .then(async (res) => {
        if (!res.ok) return;
        const body = (await res.json()) as ConnectorsResponse;
        if (!Array.isArray(body.connectors)) return;
        const map = new Map<string, ConnectorSummary>();
        for (const c of body.connectors.filter(isConnectorSummary)) {
          map.set(c.id.toLowerCase(), c);
        }
        if (!cancelled) setConnectors(map);
      })
      .catch(() => {
        // Connection status is best-effort: tools still list without it.
      });
    return () => {
      cancelled = true;
    };
  }, [retryTick]);

  /** Connector status for a tool: {id, name, connected} or null when unknown. */
  const connectorStatus = (toolName: string): ConnectorSummary | null => {
    const id = toolToConnectorId(toolName);
    if (id === null || connectors === null) return null;
    return connectors.get(id) ?? null;
  };

  // Filter by tool name (case-insensitive).
  // Son's request: hide system/built-in tools, only show connector tools
  // (the external services/channels like Gmail, GitHub, Spotify, etc.)
  const CONNECTOR_PREFIXES = [
    "gmail_", "gcalendar_", "gdrive_",
    "github_",
    "spotify_",
    "social_",
    "places_",
    "flight", "duffel_",
    "event",
    "canva_",
    "health_",
    "plaid_", "finance_",
    "meta_",
    "shopify_", "stripe_",
    "tailscale_",
  ];
  const isConnectorTool = (name: string) =>
    CONNECTOR_PREFIXES.some((p) => name.toLowerCase().startsWith(p));

  const filtered = useMemo(
    () =>
      (tools ?? []).filter(
        (t) =>
          isConnectorTool(t.name) &&
          t.name.toLowerCase().includes(query.trim().toLowerCase()),
      ),
    [tools, query],
  );

  // Groups are derived from the live data — never hardcoded.
  const groups = useMemo(() => {
    const seen = new Map<string, ToolDef[]>();
    for (const t of filtered) {
      const g = t.group || "other";
      const list = seen.get(g);
      if (list) list.push(t);
      else seen.set(g, [t]);
    }
    return [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [filtered]);

  return (
    <div>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search tools by name…"
        className="mb-4 w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)]"
      />
      <ChannelAccordion query={query} />
      {tools === null ? (
        <SkeletonText lines={6} label="Loading tools" />
      ) : loadError !== null ? (
        <BoomiEmptyState
          animation="error"
          title="Could not load tools"
          description={`The backend did not return a tool list (${loadError}). No tools are shown rather than a stale catalog.`}
          actionLabel="Retry"
          onAction={() => {
            setTools(null);
            setRetryTick((t) => t + 1);
          }}
        />
      ) : groups.length === 0 ? (
        <BoomiEmptyState
          animation="idle"
          title={query.trim() === "" ? "No tools registered" : "No tools match your search"}
          description={
            query.trim() === ""
              ? "The backend returned an empty tool list. Check that the gateway is running the expected build."
              : undefined
          }
        />
      ) : (
        groups.map(([group, list]) => (
          <section key={group} className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
              {group} ({list.length})
            </h3>
            <ul className="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)]">
              {list.map((t) => {
                const cs = connectorStatus(t.name);
                return (
                  <li key={t.name} className="flex items-center gap-3 px-3 py-2">
                    <span
                      title={
                        cs === null
                          ? "Connection status unknown"
                          : cs.connected
                            ? `${cs.name} connected`
                            : `${cs.name} not connected`
                      }
                      className={`inline-block h-2 w-2 shrink-0 rounded-full ${
                        cs === null
                          ? "bg-[var(--muted)]"
                          : cs.connected
                            ? "bg-green-500"
                            : "bg-[var(--muted)] opacity-50"
                      }`}
                    />
                    <code className="font-mono text-sm text-[var(--text)]">{t.name}</code>
                    <span className="flex-1 text-sm text-[var(--muted)]">
                      {t.description}
                      {cs !== null && !cs.connected && (
                        <span className="ml-2 text-xs">({cs.name} not connected)</span>
                      )}
                    </span>
                    <button
                      onClick={() => setActive(t)}
                      className="rounded border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--text)] hover:bg-[var(--surface-2)]"
                    >
                      Run
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))
      )}
      {active &&
        (() => {
          const cs = connectorStatus(active.name);
          return (
            <RunModal
              tool={active}
              connectorName={cs?.name ?? null}
              connectorConnected={cs?.connected ?? true}
              onClose={() => setActive(null)}
            />
          );
        })()}
    </div>
  );
}
