import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  FlaskConical,
  Loader2,
  Plug,
  Unplug,
} from "lucide-react";
import ChannelLogo from "./ChannelLogo";
import ChannelConnectDialog from "./ChannelConnectDialog";
import MessengerOAuthConnect from "./MessengerOAuthConnect";
import ZaloPersonalConnect from "./ZaloPersonalConnect";
import { disconnectChannel, getChannels, testChannel } from "../lib/api";
import type { ChannelState, ChannelStatus } from "../lib/types";

// Channel brand logos come from ChannelLogo (official Simple Icons paths).
// Unknown channel ids fall back to an initial letter chip.

function capitalize(s: string): string {
  return s.length > 0 ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Pretty display names for channels whose id isn't a clean single word. */
const CHANNEL_DISPLAY_NAMES: Record<string, string> = {
  "zalo-personal": "Zalo Personal",
};

function displayName(channel: ChannelStatus): string {
  return CHANNEL_DISPLAY_NAMES[channel.id] ?? capitalize(channel.name);
}

/** Brand logo with letter-chip fallback for unknown channel ids. */
function ChannelIcon({ name }: { name: string }) {
  const id = name.toLowerCase();
  const known = [
    "telegram",
    "discord",
    "messenger",
    "slack",
    "whatsapp",
    "line",
    "zalo",
    "zalo-personal",
    "feishu",
  ].includes(id);
  if (known) return <ChannelLogo channelId={id} size={18} className="shrink-0" />;
  return (
    <span
      className="flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded bg-[var(--surface-2)] text-[10px] font-semibold text-[var(--muted)]"
      aria-hidden
    >
      {(name[0] ?? "?").toUpperCase()}
    </span>
  );
}

function stateLabel(state: ChannelState): string {
  switch (state) {
    case "connected": return "Connected";
    case "disconnected":
    case "not-configured": return "Not connected";
    case "unsupported": return "Not supported";
    case "unknown": return "Unknown";
  }
}

const btnCls =
  "inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50";

/** One channel accordion row — compact Providers style. */
function ChannelRow({
  channel,
  expanded,
  onToggle,
  busy,
  rowError,
  testNote,
  onDisconnect,
  onTest,
  onConnect,
  onMessengerConnected,
  onZaloPersonalConnected,
}: {
  channel: ChannelStatus;
  expanded: boolean;
  onToggle(): void;
  busy: boolean;
  rowError: string | undefined;
  testNote: string | undefined;
  onDisconnect(): void;
  onTest(): void;
  onConnect(): void;
  onMessengerConnected(): void;
  onZaloPersonalConnected(): void;
}) {
  const connected = channel.state === "connected";
  const unsupported = channel.state === "unsupported";
  const name = displayName(channel);
  const isMessenger = channel.id === "messenger";
  const isZaloPersonal = channel.id === "zalo-personal";

  return (
    <li>
      {/* Row header — click to expand (accordion). */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-label={`${name} channel settings`}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-[var(--surface-2)]"
      >
        <ChannelIcon name={channel.name} />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[var(--text)]">
          {name}
        </span>
        <span className="flex shrink-0 items-center gap-1">
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 rounded-full ${
              connected ? "bg-emerald-500" : "bg-[var(--border)]"
            }`}
          />
          <span className="whitespace-nowrap text-[11px] text-[var(--muted)]">
            {stateLabel(channel.state)}
          </span>
        </span>
        <ChevronDown
          aria-hidden="true"
          className={`h-3.5 w-3.5 shrink-0 text-[var(--muted)] transition-transform ${
            expanded ? "rotate-180" : ""
          }`}
        />
      </button>

      {/* Expanded detail — compact. */}
      {expanded && (
        <div className="space-y-2.5 border-t border-[var(--border)] bg-[var(--bg)] px-3 py-3">
          {channel.detail && (
            <p className="text-xs text-[var(--muted)]">{channel.detail}</p>
          )}
          {unsupported ? (
            <p className="text-xs text-[var(--muted)]">Chưa hỗ trợ trong bản này.</p>
          ) : isMessenger && !connected ? (
            /* Messenger: browser-based Facebook login instead of token paste. */
            <MessengerOAuthConnect
              onConnected={onMessengerConnected}
              onManualConnect={onConnect}
            />
          ) : isZaloPersonal ? (
            /* Zalo Personal: QR login via the separate Python bridge process. */
            <ZaloPersonalConnect onConnected={onZaloPersonalConnected} />
          ) : (
            <div className="flex gap-1.5 pt-0.5">
              {connected ? (
                <>
                  <button type="button" onClick={onTest} disabled={busy} className={btnCls}>
                    {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
                    Test
                  </button>
                  <button type="button" onClick={onDisconnect} disabled={busy} className={btnCls}>
                    {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Unplug className="h-3.5 w-3.5" />}
                    Disconnect
                  </button>
                </>
              ) : (
                <button type="button" onClick={onConnect} className={btnCls}>
                  <Plug className="h-3.5 w-3.5" />
                  Connect
                </button>
              )}
            </div>
          )}
          {(rowError || testNote) && (
            <p className="text-xs text-[var(--muted)]" role={rowError ? "alert" : undefined}>
              {rowError ?? testNote}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

const REFRESH_MS = 30_000;

/**
 * Channels as a compact accordion (Providers style) for the Tools section.
 * Single-open: expanding one row collapses the others.
 */
export default function ChannelAccordion({ query }: { query: string }) {
  const [channels, setChannels] = useState<ChannelStatus[] | null>(null);
  const [failed, setFailed] = useState(false);
  /** Single-open expanded channel id. */
  const [expandedId, setExpandedId] = useState<string | null>(null);
  /** Channel currently in the Connect dialog. */
  const [connecting, setConnecting] = useState<ChannelStatus | null>(null);
  /** Channel ids with an in-flight disconnect or test. */
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [testNotes, setTestNotes] = useState<Record<string, string>>({});
  /**
   * Zalo Personal runs as a separate Python bridge process, not through the
   * Rust backend — its connection state is tracked locally. The bridge's
   * own /status endpoint is the source of truth when the row is expanded.
   */
  const [zaloPersonalConnected, setZaloPersonalConnected] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await getChannels();
      setChannels(list);
      setFailed(false);
    } catch {
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
        [c.id]: res.ok ? (res.message ?? "Test passed") : (res.message ?? "Test failed"),
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

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    // Zalo Personal is a synthetic row: its bridge runs as a separate Python
    // process, so it never appears in the backend channel list.
    const zaloPersonal: ChannelStatus = {
      id: "zalo-personal",
      name: "zalo-personal",
      state: zaloPersonalConnected ? "connected" : "not-configured",
      detail: "Tài khoản Zalo cá nhân — đăng nhập bằng QR (bridge Python riêng).",
      connected: zaloPersonalConnected,
    };
    const all = [...(channels ?? []), zaloPersonal];
    if (!q) return all;
    return all.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        "zalo personal".includes(q) ||
        "channels".includes(q),
    );
  }, [channels, query, zaloPersonalConnected]);

  if (channels === null) {
    if (failed) return null; // stay silent; tools list shows the error state
    return null;
  }
  if (visible === null || visible.length === 0) return null;

  return (
    <section className="mb-5">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
        Channels ({visible.filter((c) => c.state === "connected").length}/{visible.length} connected)
      </h3>
      <ul className="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)]">
        {visible.map((c) => (
          <ChannelRow
            key={c.id}
            channel={c}
            expanded={expandedId === c.id}
            onToggle={() => setExpandedId((prev) => (prev === c.id ? null : c.id))}
            busy={busyIds.has(c.id)}
            rowError={rowErrors[c.id] || undefined}
            testNote={testNotes[c.id] || undefined}
            onDisconnect={() => void handleDisconnect(c)}
            onTest={() => void handleTest(c)}
            onConnect={() => setConnecting(c)}
            onMessengerConnected={() => void load()}
            onZaloPersonalConnected={() => setZaloPersonalConnected(true)}
          />
        ))}
      </ul>
      <ChannelConnectDialog
        channelId={connecting?.id ?? null}
        channelName={connecting ? capitalize(connecting.name) : ""}
        onClose={() => setConnecting(null)}
        onConnected={() => void load()}
      />
    </section>
  );
}
