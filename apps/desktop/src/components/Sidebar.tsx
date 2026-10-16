import { useMemo, useState } from "react";
import { Bot as BotIcon, PanelLeftClose, PanelLeftOpen, Plus, Search } from "lucide-react";
import type { Bot } from "../lib/types";
import { ChannelMonitor } from "./ChannelMonitor";
import { BoomiAvatar } from "./BoomiAvatar";
import "./Sidebar.css";

export interface SidebarProps {
  collapsed: boolean;
  onToggle(): void;
  /** Teammates, loaded by the parent via listBots(). Online = bot.is_active. */
  bots: Bot[];
  /** True while the parent is fetching bots — shows skeleton rows. */
  loadingBots?: boolean;
  activeBotId: string | null;
  onSelectBot(id: string): void;
  onNewChat(): void;
}

function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="sidebar-logo" title="Boom">
      {/* Boomi mascot as the app icon. */}
      <BoomiAvatar size={28} alt="Boomi" />
      {!collapsed && <span className="font-display logo-word">Boom</span>}
    </div>
  );
}

function TeammateRow({
  bot,
  active,
  onSelect,
}: {
  bot: Bot;
  active: boolean;
  onSelect(id: string): void;
}) {
  const online = bot.is_active;
  const preview = (bot.description ?? "").trim() || "No description yet";
  return (
    <button
      type="button"
      className={`teammate-item stagger-item${active ? " active" : ""}`}
      onClick={() => onSelect(bot.id)}
      title={`${bot.name} — ${online ? "Online" : "Offline"}`}
      aria-current={active ? "true" : undefined}
    >
      <span className="teammate-avatar-wrap">
        <BoomiAvatar size={40} status={online ? "idle" : "sleeping"} alt={bot.name} />
        <span
          className={`teammate-presence${online ? " online" : ""}`}
          aria-hidden="true"
        />
      </span>
      <span className="teammate-meta">
        <span className="teammate-name-row">
          <span className="teammate-name">{bot.name}</span>
          <span className={`teammate-status${online ? " online" : ""}`}>
            <span className="status-dot" aria-hidden="true" />
            {online ? "Online" : "Offline"}
          </span>
        </span>
        <span className="teammate-preview">{preview}</span>
      </span>
    </button>
  );
}

export default function Sidebar({
  collapsed,
  onToggle,
  bots,
  loadingBots = false,
  activeBotId,
  onSelectBot,
  onNewChat,
}: SidebarProps) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return bots;
    return bots.filter(
      (b) =>
        b.name.toLowerCase().includes(q) ||
        (b.description ?? "").toLowerCase().includes(q)
    );
  }, [bots, query]);

  if (collapsed) {
    return (
      <aside className="sidebar sidebar-collapsed" aria-label="Sidebar collapsed">
        <button className="icon-btn" onClick={onToggle} title="Expand sidebar" aria-label="Expand sidebar">
          <PanelLeftOpen size={18} />
        </button>
        <button className="icon-btn accent" onClick={onNewChat} title="New chat" aria-label="New chat">
          <Plus size={18} />
        </button>
        <div className="teammate-rail" aria-label="Teammates">
          {filtered.map((b) => (
            <button
              key={b.id}
              type="button"
              className={`teammate-rail-item${b.id === activeBotId ? " active" : ""}`}
              onClick={() => onSelectBot(b.id)}
              title={`${b.name} — ${b.is_active ? "Online" : "Offline"}`}
            >
              <BoomiAvatar
                size={36}
                status={b.is_active ? "idle" : "sleeping"}
                alt={b.name}
              />
            </button>
          ))}
        </div>
        <div className="sidebar-collapsed-spacer" />
        <ChannelMonitor compact />
      </aside>
    );
  }

  return (
    <aside className="sidebar" aria-label="Teammates">
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
          placeholder="Search teammates"
          aria-label="Search teammates"
        />
      </div>

      <nav className="teammate-list" aria-label="Teammates" aria-busy={loadingBots}>
        {loadingBots && (
          <div className="skeleton-stack" aria-hidden="true">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="skeleton skeleton-row" />
            ))}
          </div>
        )}

        {!loadingBots && filtered.length === 0 && (
          <div className="empty-state">
            <BotIcon size={24} aria-hidden="true" />
            <p>{query.trim() ? "No matching teammates" : "No teammates yet"}</p>
            {query.trim() ? null : (
              <button className="btn-ghost" onClick={onNewChat}>
                Start chatting
              </button>
            )}
          </div>
        )}

        {!loadingBots &&
          filtered.map((b) => (
            <TeammateRow
              key={b.id}
              bot={b}
              active={b.id === activeBotId}
              onSelect={onSelectBot}
            />
          ))}
      </nav>

      <footer className="sidebar-footer">
        <ChannelMonitor compact />
      </footer>
    </aside>
  );
}
