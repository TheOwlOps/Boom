import { useCallback, useEffect, useState } from "react";
import {
  Hash,
  MessageCircle,
  MessageSquare,
  MessagesSquare,
  Phone,
  RefreshCw,
  Send,
  Smartphone,
  type LucideIcon,
} from "lucide-react";
import { EmptyState } from "./EmptyState";
import { getChannels } from "../lib/api";
import type { ChannelStatus } from "../lib/types";
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

function StatusDot({ connected, size = 8 }: { connected: boolean; size?: number }) {
  return (
    <span
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        background: connected ? "var(--success)" : "var(--muted)",
        opacity: connected ? 1 : 0.45,
        flexShrink: 0,
      }}
    />
  );
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

export interface ChannelMonitorProps {
  /** Compact sidebar variant: dots row + "N/M connected". */
  compact?: boolean;
}

export function ChannelMonitor({ compact = false }: ChannelMonitorProps) {
  const [channels, setChannels] = useState<ChannelStatus[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [zaloOpen, setZaloOpen] = useState(false);

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
    const connected = channels.filter((c) => c.connected).length;
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }} role="status">
          {channels.map((c) => (
            <span
              key={c.name}
              title={`${capitalize(c.name)}: ${c.connected ? "connected" : (c.detail ?? "not connected")}`}
            >
              <StatusDot connected={c.connected} size={10} />
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
      {channels.map((c) => (
        <div
          key={c.name}
          title={c.connected ? undefined : (c.detail ?? undefined)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "8px 10px",
            borderRadius: 10,
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
            {!c.connected && c.detail && (
              <div
                style={{
                  fontSize: 12,
                  color: "var(--muted)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {c.detail}
              </div>
            )}
          </div>
          <StatusDot connected={c.connected} />
        </div>
      ))}
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
