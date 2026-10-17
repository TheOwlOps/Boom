import { useEffect, useRef, useState } from "react";
import {
  Brain,
  CheckCircle2,
  Globe,
  RefreshCw,
  Wrench,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { ThinkingDots } from "./ThinkingDots";
import { API_BASE } from "../lib/api";

/** Poll the activity endpoint this often (realtime feed). */
const POLL_MS = 1500;
/** An activity newer than this counts as "currently happening". */
const CURRENT_FRESH_MS = 30_000;

type ActivityType = "thinking" | "tool_call" | "llm_wait" | "done" | "error" | "idle";

interface ActivityEvent {
  id: string;
  timestamp: string;
  type: ActivityType;
  message: string;
  detail?: string | null;
}

interface CurrentActivity {
  type: ActivityType;
  message: string;
  started_at?: string | null;
}

interface ActivityResponse {
  activities: ActivityEvent[];
  current: CurrentActivity | null;
}

// -- styling -----------------------------------------------------------
// Icon shade per activity type. Resolved through the theme tokens (not fixed
// hexes) so icons stay visible in both dark and light mode; layout uses
// theme CSS variables.
const TYPE_STYLE: Record<ActivityType, { icon: LucideIcon; color: string; label: string }> = {
  thinking: { icon: Brain, color: "var(--success)", label: "Thinking" },
  tool_call: { icon: Wrench, color: "var(--muted)", label: "Tool call" },
  llm_wait: { icon: Globe, color: "var(--accent-hover)", label: "LLM call" },
  done: { icon: CheckCircle2, color: "var(--text)", label: "Done" },
  error: { icon: XCircle, color: "var(--danger)", label: "Error" },
  idle: { icon: CheckCircle2, color: "var(--muted)", label: "Idle" },
};

// -- parsing -----------------------------------------------------------
// Validate the /api/activity JSON shape at runtime (same pattern as lib/api.ts).
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}
function reqString(v: unknown, field: string): string {
  if (typeof v !== "string") throw new Error(`malformed response: ${field} must be a string`);
  return v;
}
function parseActivityType(v: unknown, field: string): ActivityType {
  const s = reqString(v, field);
  if (s === "thinking" || s === "tool_call" || s === "llm_wait" || s === "done" || s === "error" || s === "idle") return s;
  throw new Error(`malformed response: unknown activity type "${s}"`);
}
function parseEvent(v: unknown): ActivityEvent {
  if (!isRecord(v)) throw new Error("malformed response: activity entry");
  return {
    id: reqString(v.id, "activity.id"),
    timestamp: reqString(v.timestamp, "activity.timestamp"),
    type: parseActivityType(v.type, "activity.type"),
    message: reqString(v.message, "activity.message"),
    detail: v.detail === null || v.detail === undefined ? null : reqString(v.detail, "activity.detail"),
  };
}
function parseResponse(v: unknown): ActivityResponse {
  if (!isRecord(v)) throw new Error("malformed response: /api/activity");
  if (!Array.isArray(v.activities)) throw new Error("malformed response: activities must be an array");
  const current = v.current;
  return {
    activities: v.activities.map(parseEvent),
    current:
      current === null || current === undefined
        ? null
        : {
            type: parseActivityType((current as Record<string, unknown>).type, "current.type"),
            message: reqString((current as Record<string, unknown>).message, "current.message"),
            started_at: (current as Record<string, unknown>).started_at == null
              ? null
              : reqString((current as Record<string, unknown>).started_at, "current.started_at"),
          },
  };
}

async function fetchActivity(): Promise<ActivityResponse> {
  const res = await fetch(`${API_BASE}/api/activity`);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`.trim());
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error("malformed response: /api/activity did not return JSON");
  }
  return parseResponse(body);
}

// -- helpers -----------------------------------------------------------
function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}
function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

/** A soft-colored pulsing dot used for the "current activity" card. */
function PulseDot({ color }: { color: string }) {
  return (
    <span
      aria-hidden="true"
      style={{
        width: 10,
        height: 10,
        borderRadius: "50%",
        backgroundColor: color,
        boxShadow: `0 0 12px ${color}`,
        animation: "activity-pulse 1.2s ease-in-out infinite",
        flexShrink: 0,
      }}
    />
  );
}

export function ActivityFeed() {
  const [data, setData] = useState<ActivityResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Ticks every second so the "elapsed" counter on the current card runs live.
  const [, setNow] = useState(Date.now());
  const timerRef = useRef<number | null>(null);

  const load = async () => {
    try {
      const fresh = await fetchActivity();
      setData(fresh);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load activity");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    timerRef.current = window.setInterval(() => {
      void load();
      setNow(Date.now());
    }, POLL_MS);
    return () => {
      if (timerRef.current !== null) window.clearInterval(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleDetail = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const current = data?.current ?? null;
  const isIdle = !current || current.type === "idle";
  const currentStyle = TYPE_STYLE[current?.type ?? "idle"];
  const CurrentIcon = currentStyle.icon;
  const elapsedMs = current?.started_at
    ? Date.now() - new Date(current.started_at).getTime()
    : null;

  return (
    <section
      aria-label="Bot activity"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        padding: 16,
        backgroundColor: "var(--bg)",
        color: "var(--text)",
        borderRadius: 12,
      }}
    >
      {/* Keyframes for the pulse dot (local to this component). */}
      <style>{`
        @keyframes activity-pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.45; transform: scale(0.8); }
        }
      `}</style>

      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>Activity</h2>
        <button
          type="button"
          onClick={() => {
            setLoading(true);
            void load();
          }}
          aria-label="Refresh activity"
          style={{
            background: "transparent",
            border: "1px solid var(--border)",
            borderRadius: 8,
            color: "var(--muted)",
            cursor: "pointer",
            padding: 6,
            display: "inline-flex",
          }}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      {loading && !data && (
        // Loading state: skeleton rows.
        <div role="status" aria-label="Loading activity" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              style={{
                height: 44,
                borderRadius: 10,
                backgroundColor: "var(--surface)",
                opacity: 0.6,
              }}
            />
          ))}
        </div>
      )}

      {error && !loading && (
        // Error state: honest message + retry button.
        <div
          role="alert"
          style={{
            border: "1px solid var(--danger)",
            borderRadius: 10,
            padding: 12,
            color: "var(--danger)",
            fontSize: 13,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <span>Could not load activity: {error}</span>
          <button
            type="button"
            onClick={() => {
              setLoading(true);
              void load();
            }}
            style={{
              background: "transparent",
              border: "1px solid var(--danger)",
              borderRadius: 8,
              color: "var(--danger)",
              cursor: "pointer",
              padding: "4px 10px",
              fontSize: 12,
            }}
          >
            Retry
          </button>
        </div>
      )}

      {data && !error && (
        <>
          {/* Current activity card — the most prominent element. */}
          <div
            role="status"
            style={{
              border: `1px solid ${isIdle ? "var(--border)" : currentStyle.color}`,
              borderRadius: 12,
              padding: 14,
              backgroundColor: "var(--surface)",
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            {isIdle ? (
              <>
                <PulseDot color={currentStyle.color} />
                <span style={{ fontSize: 14, color: "var(--muted)" }}>Idle — no activity right now</span>
              </>
            ) : (
              <>
                <PulseDot color={currentStyle.color} />
                <CurrentIcon size={18} style={{ color: currentStyle.color, flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 14, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {current!.message}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 2, display: "flex", alignItems: "center", gap: 6 }}>
                    <ThinkingDots label={currentStyle.label} />
                    {elapsedMs !== null && !Number.isNaN(elapsedMs) && (
                      <span aria-label="elapsed time">· {formatElapsed(elapsedMs)}</span>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Timeline of recent events. */}
          {data.activities.length === 0 ? (
            // Empty state: backend honestly reported no activity.
            <div
              style={{
                textAlign: "center",
                color: "var(--muted)",
                fontSize: 13,
                padding: "24px 12px",
                border: "1px dashed var(--border)",
                borderRadius: 10,
              }}
            >
              No activity yet — the bot hasn&apos;t done anything in this session.
            </div>
          ) : (
            <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
              {data.activities.map((ev) => {
                const st = TYPE_STYLE[ev.type];
                const Icon = st.icon;
                const open = expanded.has(ev.id);
                return (
                  <li
                    key={ev.id}
                    style={{
                      border: "1px solid var(--border)",
                      borderLeft: `3px solid ${st.color}`,
                      borderRadius: 10,
                      padding: "10px 12px",
                      backgroundColor: "var(--surface)",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <Icon size={15} style={{ color: st.color, flexShrink: 0 }} />
                      <span
                        style={{
                          flex: 1,
                          minWidth: 0,
                          fontSize: 13,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                        title={ev.message}
                      >
                        {ev.message}
                      </span>
                      <time style={{ fontSize: 11, color: "var(--muted)", flexShrink: 0 }}>
                        {formatTime(ev.timestamp)}
                      </time>
                      {ev.detail && (
                        <button
                          type="button"
                          onClick={() => toggleDetail(ev.id)}
                          aria-expanded={open}
                          aria-label={open ? "Hide details" : "Show details"}
                          style={{
                            background: "transparent",
                            border: "1px solid var(--border)",
                            borderRadius: 6,
                            color: "var(--muted)",
                            cursor: "pointer",
                            fontSize: 11,
                            padding: "2px 8px",
                            flexShrink: 0,
                          }}
                        >
                          {open ? "Hide" : "Detail"}
                        </button>
                      )}
                    </div>
                    {ev.detail && open && (
                      <pre
                        style={{
                          margin: "8px 0 0",
                          fontSize: 11,
                          color: "var(--muted)",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          maxHeight: 160,
                          overflowY: "auto",
                        }}
                      >
                        {ev.detail}
                      </pre>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </>
      )}
    </section>
  );
}

// Re-exported for the "currently happening" freshness rule (testable in isolation).
export { CURRENT_FRESH_MS };
