import { useEffect, useMemo, useState } from "react";

import { API_BASE } from "../lib/api";
import ChannelAccordion from "./ChannelAccordion";
import ConnectorCard from "./ConnectorCard";
import type { ConnectorCardData } from "./ConnectorCard";
import { connectorLabel } from "./connectorLabels";
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
  description?: string;
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
              {connectorName} chưa được kết nối
            </p>
            <p className="mt-1 text-sm text-[var(--muted)]">
              Công cụ này cần {connectorName} được kết nối mới hoạt động.
              Bấm "Kết nối" ở thẻ {connectorName} rồi thử lại.
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
            Đóng
          </button>
          {connectorConnected && (
            <button
              onClick={() => void run()}
              disabled={running}
              className="rounded bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
            >
              {running ? "Đang chạy…" : "Chạy"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Tool manager: channels subsection first, then connectors as unified cards. */
export default function ToolManager() {
  const [query, setQuery] = useState("");
  const [tools, setTools] = useState<ToolDef[] | null>(null);
  const [connectors, setConnectors] = useState<ConnectorSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const [active, setActive] = useState<ToolDef | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState<Record<string, string>>({});

  // The tool list comes from the live backend (GET /api/tools). There is no
  // static fallback catalog on purpose: a stale catalog would show tools the
  // backend does not have, and every Run click on one would 404.
  // Connector list + connection status comes from GET /api/connectors.
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/tools`);
        if (!res.ok) throw new Error(`GET /api/tools → ${res.status}`);
        const body = (await res.json()) as ToolsResponse;
        const list = Array.isArray(body.tools) ? body.tools.filter(isToolDef) : [];
        if (!cancelled) setTools(list);
      } catch (err: unknown) {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Could not reach the backend.");
        setTools([]);
      }
      try {
        const res = await fetch(`${API_BASE}/api/connectors`);
        if (!res.ok) return;
        const body = (await res.json()) as ConnectorsResponse;
        if (!Array.isArray(body.connectors)) return;
        const list = body.connectors.filter(isConnectorSummary);
        if (!cancelled) setConnectors(list);
      } catch {
        // Connection status is best-effort: tools still list without it.
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [retryTick]);

  /** Connector status lookup by id. */
  const connectorById = (id: string): ConnectorSummary | null => {
    if (!connectors) return null;
    return connectors.find((c) => c.id.toLowerCase() === id.toLowerCase()) ?? null;
  };

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

  /** Group connector tools by connector id, filtered by search query. */
  const connectorGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const byConnector = new Map<string, ToolDef[]>();
    for (const t of tools ?? []) {
      if (!isConnectorTool(t.name)) continue;
      const cid = toolToConnectorId(t.name);
      if (!cid) continue;
      // Search matches connector label, id, or tool name.
      if (q) {
        const { label } = connectorLabel(cid);
        const hay = `${label} ${cid} ${t.name} ${t.description}`.toLowerCase();
        if (!hay.includes(q)) continue;
      }
      const list = byConnector.get(cid);
      if (list) list.push(t);
      else byConnector.set(cid, [t]);
    }
    // Sort by Vietnamese label.
    return [...byConnector.entries()].sort((a, b) =>
      connectorLabel(a[0]).label.localeCompare(connectorLabel(b[0]).label, "vi"),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tools, query, connectors]);

  /** Connectors with no tools (or not yet loaded) still show as cards. */
  const allConnectorIds = useMemo(() => {
    const ids = new Set<string>();
    for (const [cid] of connectorGroups) ids.add(cid);
    for (const c of connectors ?? []) ids.add(c.id.toLowerCase());
    return [...ids].sort((a, b) =>
      connectorLabel(a).label.localeCompare(connectorLabel(b).label, "vi"),
    );
  }, [connectorGroups, connectors]);

  const handleConnect = async (id: string) => {
    setBusyIds((prev) => new Set(prev).add(id));
    setNotes((prev) => ({ ...prev, [id]: "" }));
    try {
      const res = await fetch(`${API_BASE}/api/connectors/${id}/connect`, {
        method: "POST",
      });
      const body = (await res.json().catch(() => null)) as {
        connect_url?: string;
        instructions?: string;
      } | null;
      if (!res.ok) {
        throw new Error(
          (body as { error?: string } | null)?.error ?? `Connect failed (${res.status})`,
        );
      }
      if (body?.connect_url) {
        window.open(body.connect_url, "_blank", "noopener");
      }
      setNotes((prev) => ({
        ...prev,
        [id]: body?.instructions ?? "Hoàn tất kết nối trong trình duyệt rồi quay lại đây.",
      }));
    } catch (e) {
      setNotes((prev) => ({
        ...prev,
        [id]: e instanceof Error ? e.message : "Kết nối thất bại.",
      }));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const handleDisconnect = async (id: string) => {
    setBusyIds((prev) => new Set(prev).add(id));
    setNotes((prev) => ({ ...prev, [id]: "" }));
    try {
      const res = await fetch(`${API_BASE}/api/connectors/${id}/disconnect`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`Disconnect failed (${res.status})`);
      // Refresh connector list.
      const listRes = await fetch(`${API_BASE}/api/connectors`);
      if (listRes.ok) {
        const body = (await listRes.json()) as ConnectorsResponse;
        if (Array.isArray(body.connectors)) {
          setConnectors(body.connectors.filter(isConnectorSummary));
        }
      }
    } catch (e) {
      setNotes((prev) => ({
        ...prev,
        [id]: e instanceof Error ? e.message : "Ngắt kết nối thất bại.",
      }));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const cardData = (cid: string): ConnectorCardData => {
    const cs = connectorById(cid);
    return {
      id: cid,
      name: cs?.name ?? cid,
      description: cs?.description ?? "",
      connected: cs?.connected ?? false,
    };
  };

  const connectedCount = (connectors ?? []).filter((c) => c.connected).length;

  return (
    <div>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Tìm dịch vụ… (ví dụ: Gmail, Spotify)"
        className="mb-4 w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)]"
      />
      <ChannelAccordion query={query} />
      {tools === null ? (
        <SkeletonText lines={6} label="Đang tải dịch vụ" />
      ) : loadError !== null ? (
        <BoomiEmptyState
          animation="error"
          title="Không tải được danh sách"
          description={`Backend không trả về danh sách công cụ (${loadError}).`}
          actionLabel="Thử lại"
          onAction={() => {
            setTools(null);
            setRetryTick((t) => t + 1);
          }}
        />
      ) : allConnectorIds.length === 0 ? (
        <BoomiEmptyState
          animation="idle"
          title={query.trim() === "" ? "Chưa có dịch vụ nào" : "Không tìm thấy dịch vụ"}
          description={
            query.trim() === ""
              ? "Backend trả về danh sách rỗng. Kiểm tra gateway đang chạy đúng bản build."
              : undefined
          }
        />
      ) : (
        <section className="mb-5">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
            Dịch vụ kết nối ({connectedCount}/{allConnectorIds.length} đã kết nối)
          </h3>
          <ul className="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)]">
            {allConnectorIds.map((cid) => {
              const toolsForConnector =
                connectorGroups.find(([g]) => g === cid)?.[1] ?? [];
              return (
                <ConnectorCard
                  key={cid}
                  connector={cardData(cid)}
                  tools={toolsForConnector.map((t) => ({
                    name: t.name,
                    description: t.description,
                  }))}
                  onRunTool={(name, description) =>
                    setActive({ name, description, group: cid })
                  }
                  onConnect={handleConnect}
                  onDisconnect={handleDisconnect}
                  busy={busyIds.has(cid)}
                  note={notes[cid] || undefined}
                />
              );
            })}
          </ul>
        </section>
      )}
      {active &&
        (() => {
          const cid = toolToConnectorId(active.name);
          const cs = cid ? connectorById(cid) : null;
          return (
            <RunModal
              tool={active}
              connectorName={cs ? connectorLabel(cs.id).label : null}
              connectorConnected={cs?.connected ?? true}
              onClose={() => setActive(null)}
            />
          );
        })()}
    </div>
  );
}
