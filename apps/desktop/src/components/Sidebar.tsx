import { useEffect, useMemo, useState } from "react";
import { MessageSquare, PanelLeftClose, PanelLeftOpen, Plus, Search, Settings } from "lucide-react";
import { listSessions } from "../lib/api";
import type { Session } from "../lib/types";
import { ChannelMonitor } from "./ChannelMonitor";

export interface SidebarProps {
  collapsed: boolean;
  onToggle(): void;
  activeSession: string | null;
  onSelectSession(id: string | null): void;
  onNewChat(): void;
  onOpenSettings(): void;
}

/** Human-relative timestamp; no date library. */
function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  const w = Math.floor(d / 7);
  if (w < 5) return `${w}w ago`;
  const mo = Math.floor(d / 30);
  return `${mo}mo ago`;
}

function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="sidebar-logo" title="Boom">
      <span className="logo-dot" aria-hidden="true" />
      {!collapsed && <span className="font-display logo-word">Boom</span>}
    </div>
  );
}

export default function Sidebar({
  collapsed,
  onToggle,
  activeSession,
  onSelectSession,
  onNewChat,
  onOpenSettings,
}: SidebarProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setSessions(await listSessions());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load sessions");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) => (s.title ?? "Untitled").toLowerCase().includes(q));
  }, [sessions, query]);

  if (collapsed) {
    return (
      <aside className="sidebar sidebar-collapsed" aria-label="Sidebar collapsed">
        <button className="icon-btn" onClick={onToggle} title="Expand sidebar" aria-label="Expand sidebar">
          <PanelLeftOpen size={18} />
        </button>
        <button className="icon-btn accent" onClick={onNewChat} title="New chat" aria-label="New chat">
          <Plus size={18} />
        </button>
        <button className="icon-btn" onClick={onOpenSettings} title="Settings" aria-label="Settings">
          <Settings size={18} />
        </button>
        <div className="sidebar-collapsed-spacer" />
        <ChannelMonitor compact />
      </aside>
    );
  }

  return (
    <aside className="sidebar" aria-label="Sessions">
      <div className="sidebar-header">
        <Logo collapsed={collapsed} />
        <div className="sidebar-header-actions">
          <button className="icon-btn accent" onClick={onNewChat} title="New chat" aria-label="New chat">
            <Plus size={18} />
          </button>
          <button className="icon-btn" onClick={onToggle} title="Collapse sidebar" aria-label="Collapse sidebar">
            <PanelLeftClose size={18} />
          </button>
        </div>
      </div>

      <div className="sidebar-search">
        <Search size={15} className="sidebar-search-icon" aria-hidden="true" />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search conversations"
          aria-label="Search conversations"
        />
      </div>

      <nav className="session-list" aria-label="Conversation history" aria-busy={loading}>
        {loading && (
          <div className="skeleton-stack" aria-hidden="true">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="skeleton skeleton-row" />
            ))}
          </div>
        )}

        {!loading && error && (
          <div className="error-card" role="alert">
            <p>{error}</p>
            <button className="btn-ghost" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className="empty-state">
            <MessageSquare size={24} aria-hidden="true" />
            <p>{query.trim() ? "No matching conversations" : "No conversations yet"}</p>
            {query.trim() ? null : (
              <button className="btn-ghost" onClick={onNewChat}>
                Start one
              </button>
            )}
          </div>
        )}

        {!loading &&
          !error &&
          filtered.map((s, i) => (
            <button
              key={s.id}
              className={`session-item stagger-item${s.id === activeSession ? " active" : ""}`}
              style={{ animationDelay: `${Math.min(i, 20) * 0.03}s` }}
              onClick={() => onSelectSession(s.id)}
              title={s.title ?? "Untitled"}
            >
              <span className="session-title">{s.title ?? "Untitled"}</span>
              {s.updated_at && <span className="session-time">{timeAgo(s.updated_at)}</span>}
            </button>
          ))}
      </nav>

      <footer className="sidebar-footer">
        <ChannelMonitor compact />
        <button className="settings-btn" onClick={onOpenSettings} title="Settings">
          <Settings size={16} aria-hidden="true" />
          <span>Settings</span>
        </button>
      </footer>
    </aside>
  );
}
