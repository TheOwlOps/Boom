/**
 * ConnectorCard — unified card for third-party service connectors.
 *
 * Single pattern used everywhere connectors appear in the app:
 *   Logo + Service name + Short description + Status dot + Connect button
 *   (click to expand for technical details — tool names stay hidden by default)
 *
 * Props are deliberately dumb: the parent fetches connector status and
 * handles connect/disconnect flows.
 */

import { useState } from "react";
import { ChevronDown, Loader2, Plug, Unplug } from "lucide-react";
import ConnectorLogo, { hasConnectorLogo } from "./ConnectorLogo";
import { connectorLabel } from "./connectorLabels";

export interface ConnectorCardData {
  id: string;
  /** Backend display name (fallback when no Vietnamese label). */
  name: string;
  /** Backend description (fallback). */
  description: string;
  connected: boolean;
}

interface ConnectorCardProps {
  connector: ConnectorCardData;
  /** Tools belonging to this connector (shown only when expanded, dev view). */
  tools?: { name: string; description: string }[];
  /** Called when the user clicks a tool's Run button (dev view). */
  onRunTool?: (toolName: string, toolDescription: string) => void;
  /** Called when the user clicks Connect. */
  onConnect?: (id: string) => void;
  /** Called when the user clicks Disconnect. */
  onDisconnect?: (id: string) => void;
  busy?: boolean;
  /** Optional error/note line under the row. */
  note?: string;
}

function FallbackIcon({ name }: { name: string }) {
  return (
    <span
      className="flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded bg-[var(--surface-2)] text-[10px] font-semibold text-[var(--muted)]"
      aria-hidden
    >
      {(name[0] ?? "?").toUpperCase()}
    </span>
  );
}

export default function ConnectorCard({
  connector,
  tools,
  onRunTool,
  onConnect,
  onDisconnect,
  busy = false,
  note,
}: ConnectorCardProps) {
  const [expanded, setExpanded] = useState(false);
  const { label, description } = connectorLabel(connector.id);
  // Prefer the Vietnamese label; fall back to backend name/description.
  const displayName = label !== connector.id ? label : connector.name;
  const displayDesc =
    description !== "Dịch vụ kết nối" ? description : connector.description;

  return (
    <li>
      {/* Row header — click to expand details. */}
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        aria-label={`${displayName} settings`}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-[var(--surface-2)]"
      >
        {hasConnectorLogo(connector.id) ? (
          <ConnectorLogo connectorId={connector.id} size={18} className="shrink-0" />
        ) : (
          <FallbackIcon name={displayName} />
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-[var(--text)]">
            {displayName}
          </span>
          <span className="block truncate text-[11px] text-[var(--muted)]">
            {displayDesc}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 rounded-full ${
              connector.connected ? "bg-emerald-500" : "bg-[var(--border)]"
            }`}
          />
          <span className="whitespace-nowrap text-[11px] text-[var(--muted)]">
            {connector.connected ? "Đã kết nối" : "Chưa kết nối"}
          </span>
        </span>
        <ChevronDown
          aria-hidden="true"
          className={`h-3.5 w-3.5 shrink-0 text-[var(--muted)] transition-transform ${
            expanded ? "rotate-180" : ""
          }`}
        />
      </button>

      {/* Expanded detail. */}
      {expanded && (
        <div className="space-y-2.5 border-t border-[var(--border)] bg-[var(--bg)] px-3 py-3">
          <div className="flex gap-1.5">
            {connector.connected ? (
              <button
                type="button"
                onClick={() => onDisconnect?.(connector.id)}
                disabled={busy}
                className="inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50"
              >
                {busy ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Unplug className="h-3.5 w-3.5" />
                )}
                Ngắt kết nối
              </button>
            ) : (
              <button
                type="button"
                onClick={() => onConnect?.(connector.id)}
                disabled={busy}
                className="inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--text))] disabled:opacity-50"
              >
                {busy ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Plug className="h-3.5 w-3.5" />
                )}
                Kết nối
              </button>
            )}
          </div>

          {/* Dev view: raw tool names, hidden from the main view. */}
          {tools && tools.length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer text-[var(--muted)] hover:text-[var(--text)]">
                Chức năng kỹ thuật ({tools.length})
              </summary>
              <ul className="mt-2 divide-y divide-[var(--border)] rounded border border-[var(--border)]">
                {tools.map((t) => (
                  <li key={t.name} className="flex items-center gap-2 px-2 py-1.5">
                    <code className="font-mono text-[11px] text-[var(--text)]">
                      {t.name}
                    </code>
                    <span className="flex-1 truncate text-[11px] text-[var(--muted)]">
                      {t.description}
                    </span>
                    {onRunTool && (
                      <button
                        type="button"
                        onClick={() => onRunTool(t.name, t.description)}
                        disabled={!connector.connected}
                        className="rounded border border-[var(--border)] px-2 py-0.5 text-[11px] text-[var(--text)] hover:bg-[var(--surface-2)] disabled:opacity-50"
                      >
                        Run
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {note && (
            <p className="text-xs text-[var(--muted)]" role="alert">
              {note}
            </p>
          )}
        </div>
      )}
    </li>
  );
}
