import { useMemo, useState, useRef, useEffect } from "react";
import { PanelLeftClose, PanelLeftOpen, Plus, Search, Settings, Bot as BotIcon, Users, X, Pencil } from "lucide-react";
import type { Bot, BotDraft } from "../lib/types";
import { createBot, updateBot, deleteBot } from "../lib/api";
import { type BotGroup, type GroupPreview } from "../lib/botGroups";
import { formatSmartTime } from "../lib/formatTime";
import { MemberAvatarStack } from "./BotGroupManager";
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
  /** Bot groups for the "Groups" section, loaded by the parent. */
  groups: BotGroup[];
  /** Last-message previews keyed by group id. */
  groupPreviews: Record<string, GroupPreview>;
  /** True while the parent is fetching groups. */
  loadingGroups?: boolean;
  activeGroupId: string | null;
  onSelectGroup(id: string): void;
  onNewChat(): void;
  onCreateGroup(): void;
  onOpenSettings(): void;
  /** Called after a bot is created/renamed/deleted inline — parent refreshes the list.
   *  Receives the new bot's id when a bot was just created (to auto-select it). */
  onBotsChanged(selectBotId?: string): void;
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

/** Inline input row for creating a new bot (VS Code style). Enter = create, Esc = cancel. */
function CreateBotRow({
  onCreated,
  onCancel,
}: {
  onCreated(bot: Bot): void;
  onCancel(): void;
}) {
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    setError(null);
    try {
      const draft: BotDraft = {
        name: trimmed,
        description: "",
        system_prompt: "",
        avatar_emoji: "🤖",
        allowed_tools: [],
        denied_tools: [],
        channel_bindings: [],
      };
      const bot = await createBot(draft);
      onCreated(bot);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không tạo được bot");
      setSaving(false);
    }
  };

  return (
    <div
      className="teammate-item teammate-item-editing"
      role="option"
      aria-selected="false"
      aria-label="Tạo bot mới"
    >
      <span className="teammate-avatar-wrap">
        <span className="teammate-avatar-new" aria-hidden="true">
          <Plus size={20} />
        </span>
      </span>
      <span className="teammate-meta">
        <input
          ref={inputRef}
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void submit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              onCancel();
            }
          }}
          onBlur={() => {
            if (!name.trim() && !saving) onCancel();
          }}
          placeholder="Tên bot…"
          disabled={saving}
          className="teammate-name-input"
          aria-label="Tên bot mới"
        />
        {error ? (
          <span className="teammate-inline-error">{error}</span>
        ) : (
          <span className="teammate-preview">Enter để tạo · Esc để hủy</span>
        )}
      </span>
    </div>
  );
}

/** Inline input row for renaming a bot. Enter = save, Esc = cancel. */
function RenameRow({
  bot,
  onDone,
  onCancel,
}: {
  bot: Bot;
  onDone(): void;
  onCancel(): void;
}) {
  const [name, setName] = useState(bot.name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = inputRef.current;
    if (el) {
      el.focus();
      el.select();
    }
  }, []);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || saving) return;
    if (trimmed === bot.name) {
      onCancel();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const draft: BotDraft = {
        name: trimmed,
        description: bot.description,
        system_prompt: bot.system_prompt,
        avatar_emoji: bot.avatar_emoji,
        allowed_tools: bot.allowed_tools,
        denied_tools: bot.denied_tools,
        channel_bindings: bot.channel_bindings,
      };
      await updateBot(bot.id, draft);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không đổi tên được");
      setSaving(false);
    }
  };

  return (
    <div
      className="teammate-item teammate-item-editing"
      role="option"
      aria-selected="false"
      aria-label={`Đổi tên ${bot.name}`}
    >
      <span className="teammate-avatar-wrap">
        <BotAvatar
          botId={bot.id}
          name={bot.name}
          emoji={bot.avatar_emoji}
          size={40}
          status={bot.is_active ? "online" : "offline"}
        />
      </span>
      <span className="teammate-meta">
        <input
          ref={inputRef}
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void submit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              onCancel();
            }
          }}
          disabled={saving}
          className="teammate-name-input"
          aria-label="Tên mới của bot"
        />
        {error ? (
          <span className="teammate-inline-error">{error}</span>
        ) : (
          <span className="teammate-preview">Enter để lưu · Esc để hủy</span>
        )}
      </span>
    </div>
  );
}

function TeammateRow({
  bot,
  active,
  onSelect,
  onBotsChanged,
}: {
  bot: Bot;
  active: boolean;
  onSelect(id: string): void;
  onBotsChanged(): void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const online = bot.is_active;
  const preview = (bot.description ?? "").trim() || "Chưa có mô tả";

  if (renaming) {
    return (
      <RenameRow
        bot={bot}
        onDone={() => {
          setRenaming(false);
          onBotsChanged();
        }}
        onCancel={() => setRenaming(false)}
      />
    );
  }

  if (confirmingDelete) {
    return (
      <div
        className="teammate-item teammate-item-danger"
        role="option"
        aria-selected={active}
        aria-label={`Xác nhận xóa ${bot.name}`}
      >
        <span className="teammate-meta">
          <span className="teammate-name">Xóa "{bot.name}"?</span>
          <span className="teammate-preview">Không thể hoàn tác</span>
        </span>
        <span className="teammate-row-actions">
          <button
            type="button"
            className="teammate-confirm-btn danger"
            disabled={deleting}
            onClick={async () => {
              setDeleting(true);
              try {
                await deleteBot(bot.id);
                onBotsChanged();
              } catch {
                setDeleting(false);
                setConfirmingDelete(false);
              }
            }}
          >
            {deleting ? "…" : "Xóa"}
          </button>
          <button
            type="button"
            className="teammate-confirm-btn"
            disabled={deleting}
            onClick={() => setConfirmingDelete(false)}
          >
            Hủy
          </button>
        </span>
      </div>
    );
  }

  return (
    <div
      className={`teammate-item stagger-item${active ? " active" : ""}`}
      onClick={() => onSelect(bot.id)}
      onDoubleClick={(e) => {
        e.stopPropagation();
        setRenaming(true);
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        setConfirmingDelete(true);
      }}
      title={`${bot.name} — ${online ? "Online" : "Offline"}`}
      role="option"
      aria-selected={active}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(bot.id);
        } else if (e.key === "F2") {
          e.preventDefault();
          setRenaming(true);
        }
      }}
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
      <span className="teammate-hover-actions">
        <button
          type="button"
          className="teammate-icon-btn"
          title="Đổi tên"
          aria-label={`Đổi tên ${bot.name}`}
          onClick={(e) => {
            e.stopPropagation();
            setRenaming(true);
          }}
        >
          <Pencil size={14} />
        </button>
        <button
          type="button"
          className="teammate-icon-btn danger"
          title="Xóa bot"
          aria-label={`Xóa ${bot.name}`}
          onClick={(e) => {
            e.stopPropagation();
            setConfirmingDelete(true);
          }}
        >
          <X size={14} />
        </button>
      </span>
    </div>
  );
}

function GroupRow({
  group,
  bots,
  preview,
  active,
  onSelect,
}: {
  group: BotGroup;
  bots: Bot[];
  preview?: GroupPreview;
  active: boolean;
  onSelect(id: string): void;
}) {
  const members = group.bot_ids
    .map((id) => bots.find((b) => b.id === id))
    .filter((b): b is Bot => b !== undefined)
    .map((b) => ({ id: b.id, name: b.name, avatar_emoji: b.avatar_emoji ?? "" }));
  const previewText = preview?.text.trim() || `${group.bot_ids.length} member${group.bot_ids.length === 1 ? "" : "s"}`;
  const time = preview ? formatSmartTime(preview.created_at) : "";
  return (
    <button
      type="button"
      className={`teammate-item stagger-item${active ? " active" : ""}`}
      onClick={() => onSelect(group.id)}
      title={group.name}
      role="option"
      aria-selected={active}
    >
      <span className="teammate-avatar-wrap">
        <MemberAvatarStack bots={members} size={40} />
      </span>
      <span className="teammate-meta">
        <span className="teammate-name-row">
          <span className="teammate-name">{group.name}</span>
          {time && <span className="teammate-time">{time}</span>}
        </span>
        <span className="teammate-preview">{previewText}</span>
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
  groups,
  groupPreviews,
  loadingGroups = false,
  activeGroupId,
  onSelectGroup,
  onNewChat,
  onCreateGroup,
  onOpenSettings,
  onBotsChanged,
}: SidebarProps) {
  const [query, setQuery] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  /** Inline bot creation (VS Code style) — true while the name input row is shown. */
  const [creatingBot, setCreatingBot] = useState(false);

  /** Start inline bot creation. Expands the sidebar first when collapsed. */
  const startCreatingBot = () => {
    if (collapsed) onToggle();
    setCreatingBot(true);
  };

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

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter((g) => g.name.toLowerCase().includes(q));
  }, [groups, query]);

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
              <button type="button" role="menuitem" onClick={closeAnd(startCreatingBot)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
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
        {groups.length > 0 && (
          <div className="teammate-rail" aria-label="Groups">
            {groups.map((g) => (
              <button
                key={g.id}
                type="button"
                className={`teammate-rail-item${g.id === activeGroupId ? " active" : ""}`}
                onClick={() => onSelectGroup(g.id)}
                title={g.name}
                aria-label={g.name}
              >
                <span className="flex h-9 w-9 items-center justify-center rounded-full bg-[var(--surface-2)] text-[var(--muted)]">
                  <Users size={18} />
                </span>
              </button>
            ))}
          </div>
        )}
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
                <button type="button" role="menuitem" onClick={closeAnd(startCreatingBot)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-[var(--text)] hover:bg-[var(--surface-2)]">
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
          placeholder="Search"
          aria-label="Search teammates and groups"
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

        {!loadingBots && creatingBot && (
          <CreateBotRow
            onCreated={(bot) => {
              setCreatingBot(false);
              onBotsChanged(bot.id);
            }}
            onCancel={() => setCreatingBot(false)}
          />
        )}

        {!loadingBots &&
          filtered.map((b) => (
            <TeammateRow
              key={b.id}
              bot={b}
              active={b.id === activeBotId}
              onSelect={onSelectBot}
              onBotsChanged={onBotsChanged}
            />
          ))}
      </nav>

      <div className="sidebar-section-header">
        <span className="sidebar-section-title">
          Groups
          {groups.length > 0 && (
            <span className="sidebar-section-count">{groups.length}</span>
          )}
        </span>
        <button
          type="button"
          className="icon-btn icon-btn-sm"
          onClick={onCreateGroup}
          title="New group"
          aria-label="Create new bot group"
        >
          <Plus size={15} />
        </button>
      </div>

      <nav className="teammate-list group-list" role="listbox" aria-label="Groups" aria-busy={loadingGroups}>
        {loadingGroups && (
          <div className="sk-stack" style={{ padding: "4px 2px" }} role="status" aria-label="Loading groups">
            {Array.from({ length: 2 }).map((_, i) => (
              <div key={i} className="sk-row" style={{ padding: "8px 12px" }} aria-hidden="true">
                <SkeletonAvatar size={40} />
                <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                  <Skeleton width="55%" height={13} />
                  <Skeleton width="85%" height={11} />
                </div>
              </div>
            ))}
          </div>
        )}

        {!loadingGroups && filteredGroups.length === 0 && !query.trim() && (
          <p className="sidebar-empty-note">
            No groups yet — create one to chat with several bots at once.
          </p>
        )}

        {!loadingGroups &&
          filteredGroups.map((g) => (
            <GroupRow
              key={g.id}
              group={g}
              bots={bots}
              preview={groupPreviews[g.id]}
              active={g.id === activeGroupId}
              onSelect={onSelectGroup}
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
