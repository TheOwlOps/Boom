import { useMemo, useState, useRef, useEffect } from "react";
import { PanelLeftClose, PanelLeftOpen, Plus, Search, Settings, Bot as BotIcon, Users } from "lucide-react";
import type { Bot } from "../lib/types";
import { ChannelMonitor } from "./ChannelMonitor";
import { BoomiAvatar } from "./BoomiAvatar";
import { BotAvatar } from "./BotAvatar";
import { Skeleton, SkeletonAvatar } from "./Skeleton";
import { BoomiEmptyState } from "./BoomiEmptyState";
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
  onCreateBot(): void;
  onCreateGroup(): void;
  onOpenSettings(): void;
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
      role="option"
      aria-selected={active}
    >
      <span className="teammate-avatar-wrap">
        <BotAvatar
          botId={bot.id}
          name={bot.name}
          emoji={bot.avatar_emoji}
          size={40}
          status={online ? "online" : "offline"}
        />
        <span
          className={`teammate-presence${online ? " online" : ""}`}
          aria-hidden="true"
        />
      </span>
      <span className="teammate-meta">
        <span className="teammate-name">{bot.name}</span>
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
  onCreateBot,
  onCreateGroup,
  onOpenSettings,
}: SidebarProps) {
  const [query, setQuery] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close menu on outside click / Escape.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const closeAnd = (fn: () => void) => () => {
    setMenuOpen(false);
    fn();
  };

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
      <aside className="sidebar sidebar-collapsed" role="navigation" aria-label="Sidebar collapsed">
        <button className="icon-btn" onClick={onToggle} title="Expand sidebar" aria-label="Expand sidebar">
          <PanelLeftOpen size={18} />
        </button>
        <div className="relative" ref={menuRef}>
          <button className="icon-btn accent" onClick={() => setMenuOpen((v) => !v)} title="Create" aria-label="Create new" aria-haspopup="menu" aria-expanded={menuOpen}>
            <Plus size={18} />
          </button>
          {menuOpen && (
            <div role="menu" className="absolute left-0 top-full z-50 mt-1 w-44 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)] shadow-xl">
              <button type="button" role="menuitem" onClick={closeAnd(onNewChat)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
                <Plus size={15} className="shrink-0 text-[var(--muted)]" />
                New chat
              </button>
              <button type="button" role="menuitem" onClick={closeAnd(onCreateBot)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
                <BotIcon size={15} className="shrink-0 text-[var(--muted)]" />
                Create bot
              </button>
              <button type="button" role="menuitem" onClick={closeAnd(onCreateGroup)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
                <Users size={15} className="shrink-0 text-[var(--muted)]" />
                Create bot group
              </button>
            </div>
          )}
        </div>
        <div className="teammate-rail" aria-label="Teammates">
          {filtered.map((b) => (
            <button
              key={b.id}
              type="button"
              className={`teammate-rail-item${b.id === activeBotId ? " active" : ""}`}
              onClick={() => onSelectBot(b.id)}
              title={`${b.name} — ${b.is_active ? "Online" : "Offline"}`}
              aria-label={b.name}
            >
              <BotAvatar
                botId={b.id}
                name={b.name}
                emoji={b.avatar_emoji}
                size={36}
                status={b.is_active ? "online" : "offline"}
              />
            </button>
          ))}
        </div>
        <div className="sidebar-collapsed-spacer" />
        <button className="icon-btn" onClick={onOpenSettings} title="Settings" aria-label="Open settings">
          <Settings size={18} />
        </button>
        <ChannelMonitor compact />
      </aside>
    );
  }

  return (
    <aside className="sidebar" role="navigation" aria-label="Teammates">
      <div className="sidebar-header">
        <Logo collapsed={collapsed} />
        <div className="sidebar-header-actions">
          <div className="relative" ref={menuRef}>
            <button className="icon-btn accent" onClick={() => setMenuOpen((v) => !v)} title="Create" aria-label="Create new" aria-haspopup="menu" aria-expanded={menuOpen}>
              <Plus size={18} />
            </button>
            {menuOpen && (
              <div role="menu" className="absolute right-0 top-full z-50 mt-1 w-44 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)] shadow-xl">
                <button type="button" role="menuitem" onClick={closeAnd(onNewChat)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
                  <Plus size={15} className="shrink-0 text-[var(--muted)]" />
                  New chat
                </button>
                <button type="button" role="menuitem" onClick={closeAnd(onCreateBot)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
                  <BotIcon size={15} className="shrink-0 text-[var(--muted)]" />
                  Create bot
                </button>
                <button type="button" role="menuitem" onClick={closeAnd(onCreateGroup)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
                  <Users size={15} className="shrink-0 text-[var(--muted)]" />
                  Create bot group
                </button>
              </div>
            )}
          </div>
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

      <nav className="teammate-list" role="listbox" aria-label="Teammates" aria-busy={loadingBots}>
        {loadingBots && (
          <div
            className="sk-stack"
            style={{ padding: "4px 2px" }}
            role="status"
            aria-label="Loading teammates"
          >
            {Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="sk-row"
                style={{ padding: "8px 12px" }}
                aria-hidden="true"
              >
                <SkeletonAvatar size={40} />
                <div
                  style={{
                    flex: 1,
                    minWidth: 0,
                    display: "flex",
                    flexDirection: "column",
                    gap: 8,
                  }}
                >
                  <Skeleton width="55%" height={13} />
                  <Skeleton width="85%" height={11} />
                </div>
              </div>
            ))}
          </div>
        )}

        {!loadingBots && filtered.length === 0 && (
          <BoomiEmptyState
            animation="sleeping"
            size={48}
            className="py-6"
            title={query.trim() ? "No matching teammates" : "No teammates yet"}
            description={
              query.trim()
                ? undefined
                : "Your teammates will appear here. Say hello to Boomi to get started."
            }
            actionLabel={query.trim() ? undefined : "Start chatting"}
            onAction={query.trim() ? undefined : onNewChat}
          />
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
        <button type="button" className="settings-btn" onClick={onOpenSettings} aria-label="Open settings">
          <Settings size={18} aria-hidden="true" />
          <span>Settings</span>
        </button>
        <ChannelMonitor compact />
      </footer>
    </aside>
  );
}
