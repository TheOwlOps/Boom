import { useCallback, useEffect, useState } from "react";
import {
  FlaskConical,
  Hash,
  Loader2,
  MessageCircle,
  MessageSquare,
  MessagesSquare,
  Phone,
  Plug,
  RefreshCw,
  Send,
  Smartphone,
  Unplug,
  type LucideIcon,
} from "lucide-react";
import { EmptyState } from "./EmptyState";
import ChannelConnectDialog from "./ChannelConnectDialog";
import { disconnectChannel, getChannels, testChannel } from "../lib/api";
import type { ChannelState, ChannelStatus } from "../lib/types";
import ZaloPersonalSetup from "./ZaloPersonalSetup";

const REFRESH_MS = 30_000;

// Map channel name -> a recognizable lucide icon. Unknown names fall back to
// an initial letter in a grayscale dot.
const CHANNEL_ICONS: Record<string, LucideIcon> = {
  telegram: Send,
  discord: Hash,
  slack: Hash,
  whatsapp: Phone,
  messenger: MessageCircle,
  line: MessageSquare,
  zalo: Smartphone,
  feishu: MessagesSquare,
};

const FALLBACK_COLORS = [
  "#fafafa",
  "#e4e4e7",
  "#d4d4d8",
  "#a1a1aa",
  "#71717a",
  "#52525b",
];

function fallbackColor(name: string): string {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FALLBACK_COLORS[h % FALLBACK_COLORS.length];
}

function capitalize(s: string): string {
  return s.length > 0 ? s[0].toUpperCase() + s.slice(1) : s;
}

const iconBox: React.CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: 8,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "var(--surface-2)",
  color: "var(--muted)",
  flexShrink: 0,
};

function ChannelIcon({ name }: { name: string }) {
  const Icon = CHANNEL_ICONS[name.toLowerCase()];
  if (Icon) {
    return (
      <span style={iconBox} aria-hidden>
        <Icon size={16} />
      </span>
    );
  }
  const color = fallbackColor(name);
  return (
    <span
      style={{ ...iconBox, background: `${color}1f`, color }}
      aria-hidden
    >
      {(name[0] ?? "?").toUpperCase()}
    </span>
  );
}

/** Status dot: green-ish (var(--success)) when connected, gray otherwise.
 *  Unsupported channels render dimmed so they read as unavailable. */
function StatusDot({ state, size = 8 }: { state: ChannelState; size?: number }) {
  const connected = state === "connected";
  const dimmed = state === "unsupported";
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        background: connected ? "var(--success)" : "var(--muted)",
        opacity: connected ? 1 : dimmed ? 0.25 : 0.45,
        flexShrink: 0,
      }}
    />
  );
}

/** Honest one-line label for each backend state. */
function stateLabel(state: ChannelState): string {
  switch (state) {
    case "connected": return "Connected";
    case "disconnected": return "Not connected";
    case "not-configured": return "Not connected";
    case "unsupported": return "Not supported";
    case "unknown": return "Unknown";
  }
}

/** Whether the row may offer a Connect action (opens the token dialog). */
function canConnect(state: ChannelState): boolean {
  return state === "disconnected" || state === "not-configured" || state === "unknown";
}

/** Whether the row may offer Disconnect / Test actions. */
function canDisconnect(state: ChannelState): boolean {
  return state === "connected";
}

function ErrorCard({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      style={{
        padding: "12px 14px",
        borderRadius: 10,
        border: "1px solid var(--border)",
        background: "rgba(250,250,250,0.08)",
        color: "var(--text)",
        fontSize: 13,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 8,
      }}
    >
      <span>Couldn&apos;t reach backend</span>
      <button
        type="button"
        onClick={onRetry}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 10px",
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: "var(--surface-2)",
          color: "var(--text)",
          fontSize: 12,
          cursor: "pointer",
        }}
      >
        <RefreshCw size={13} />
        Retry
      </button>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading channels" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          className="skeleton-shimmer"
          style={{
            height: 48,
            borderRadius: 10,
            background: "var(--surface-2)",
          }}
        />
      ))}
    </div>
  );
}

function CompactSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading channels" style={{ display: "flex", alignItems: "center", gap: 8 }}>
      {[0, 1, 2, 3, 4].map((i) => (
        <span
          key={i}
          className="skeleton-shimmer"
          style={{ width: 10, height: 10, borderRadius: 999, background: "var(--surface-2)" }}
        />
      ))}
    </div>
  );
}

const actionBtn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  padding: "5px 9px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "var(--surface-2)",
  color: "var(--text)",
  fontSize: 12,
  cursor: "pointer",
  whiteSpace: "nowrap",
};

export interface ChannelMonitorProps {
  /** Compact sidebar variant: dots row + "N/M connected". */
  compact?: boolean;
}

export function ChannelMonitor({ compact = false }: ChannelMonitorProps) {
  const [channels, setChannels] = useState<ChannelStatus[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [zaloOpen, setZaloOpen] = useState(false);
  /** Channel currently in the Connect dialog (full variant only). */
  const [connecting, setConnecting] = useState<ChannelStatus | null>(null);
  /** Channel ids with an in-flight disconnect or test. */
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  /** Last action error per channel id (shown inline under the row). */
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  /** Last test outcome per channel id ("Test passed" / backend message). */
  const [testNotes, setTestNotes] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const list = await getChannels();
      setChannels(list);
      setFailed(false);
    } catch {
      // Keep stale rows on screen when we already have data; show the error
      // card only when there is nothing to display.
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  function markBusy(id: string, on: boolean) {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });
  }

  async function handleDisconnect(c: ChannelStatus) {
    markBusy(c.id, true);
    setRowErrors((prev) => ({ ...prev, [c.id]: "" }));
    try {
      await disconnectChannel(c.id);
      await load();
    } catch (e) {
      setRowErrors((prev) => ({
        ...prev,
        [c.id]: e instanceof Error ? e.message : "Disconnect failed",
      }));
    } finally {
      markBusy(c.id, false);
    }
  }

  async function handleTest(c: ChannelStatus) {
    markBusy(c.id, true);
    setTestNotes((prev) => ({ ...prev, [c.id]: "" }));
    setRowErrors((prev) => ({ ...prev, [c.id]: "" }));
    try {
      const res = await testChannel(c.id);
      setTestNotes((prev) => ({
        ...prev,
        [c.id]: res.ok
          ? (res.message ?? "Test passed")
          : (res.message ?? "Test failed"),
      }));
    } catch (e) {
      setRowErrors((prev) => ({
        ...prev,
        [c.id]: e instanceof Error ? e.message : "Test failed",
      }));
    } finally {
      markBusy(c.id, false);
    }
  }

  // Still loading and never fetched successfully.
  if (channels === null) {
    if (failed) return <ErrorCard onRetry={load} />;
    return compact ? <CompactSkeleton /> : <ListSkeleton />;
  }

  if (channels.length === 0) {
    return (
      <EmptyState
        icon={MessageCircle}
        title="No channels configured"
        hint="Connect a channel to see its live status here."
      />
    );
  }

  if (compact) {
    const connected = channels.filter((c) => c.state === "connected").length;
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }} role="status">
          {channels.map((c) => (
            <span
              key={c.id}
              title={`${capitalize(c.name)}: ${stateLabel(c.state)}${c.detail ? ` — ${c.detail}` : ""}`}
            >
              <StatusDot state={c.state} size={10} />
            </span>
          ))}
        </div>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          {connected}/{channels.length} connected
        </span>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }} role="status">
      {channels.map((c) => {
        const busy = busyIds.has(c.id);
        const unsupported = c.state === "unsupported";
        const rowError = rowErrors[c.id];
        const testNote = testNotes[c.id];
        return (
          <div key={c.id} style={{ borderRadius: 10 }}>
            <div
              title={c.detail ?? undefined}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "8px 10px",
                borderRadius: 10,
                opacity: unsupported ? 0.55 : 1,
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "var(--surface-2)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "transparent";
              }}
            >
              <ChannelIcon name={c.name} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14, color: "var(--text)" }}>{capitalize(c.name)}</div>
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--muted)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {unsupported
                    ? "Chưa hỗ trợ trong bản này"
                    : c.detail ?? stateLabel(c.state)}
                </div>
              </div>
              {/* Actions: connected -> Test + Disconnect; not connected -> Connect; unsupported -> none. */}
              {canDisconnect(c.state) && (
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    type="button"
                    onClick={() => void handleTest(c)}
                    disabled={busy}
                    style={{ ...actionBtn, opacity: busy ? 0.5 : 1 }}
                    aria-label={`Test ${c.name} connection`}
                  >
                    {busy ? <Loader2 size={12} className="animate-spin" /> : <FlaskConical size={12} />}
                    Test
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDisconnect(c)}
                    disabled={busy}
                    style={{ ...actionBtn, opacity: busy ? 0.5 : 1 }}
                    aria-label={`Disconnect ${c.name}`}
                  >
                    {busy ? <Loader2 size={12} className="animate-spin" /> : <Unplug size={12} />}
                    Disconnect
                  </button>
                </div>
              )}
              {canConnect(c.state) && (
                <button
                  type="button"
                  onClick={() => setConnecting(c)}
                  style={actionBtn}
                  aria-label={`Connect ${c.name}`}
                >
                  <Plug size={12} />
                  Connect
                </button>
              )}
              <StatusDot state={c.state} />
            </div>
            {(rowError || testNote) && (
              <div
                style={{
                  fontSize: 12,
                  color: "var(--muted)",
                  padding: "2px 10px 6px 54px",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                role={rowError ? "alert" : undefined}
              >
                {rowError ?? testNote}
              </div>
            )}
          </div>
        );
      })}
      <ChannelConnectDialog
        channelId={connecting?.id ?? null}
        channelName={connecting ? capitalize(connecting.name) : ""}
        onClose={() => setConnecting(null)}
        onConnected={() => void load()}
      />
      <button
        type="button"
        onClick={() => setZaloOpen(true)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "8px 10px",
          borderRadius: 10,
          border: "1px dashed var(--border)",
          background: "transparent",
          color: "var(--muted)",
          fontSize: 13,
          cursor: "pointer",
          marginTop: 4,
        }}
      >
        <Smartphone size={18} />
        <span>Connect Zalo Personal</span>
      </button>
      <ZaloPersonalSetup open={zaloOpen} onClose={() => setZaloOpen(false)} />
    </div>
  );
}
