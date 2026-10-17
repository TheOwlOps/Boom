import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { Menu } from "lucide-react";
import Sidebar from "./components/Sidebar";
import { ChatView, type BotApi } from "./components/ChatView";
import { SettingsModal, type SettingsSection } from "./components/SettingsModal";
import UpdateChecker from "./components/UpdateChecker";
// Marketplace modal (types re-exported for consumers by the component itself).
import Marketplace, {
  type BotSummary,
  type MarketplaceItem,
} from "./components/Marketplace";
import { Toast } from "./components/Toast";
import { Tooltip } from "./components/Tooltip";
import BackendStatus from "./components/BackendStatus";
import WelcomeGate from "./components/WelcomeGate";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import CommandPalette, {
  OPEN_COMMAND_PALETTE_EVENT,
  type CommandAction,
} from "./components/CommandPalette";
import type { Bot } from "./lib/types";
import {
  getMarketplace,
  getMarketplaceInstalled,
  installMarketplaceItem,
  listBots,
  setActiveBot,
} from "./lib/api";
import {
  MARKETPLACE_CATEGORIES,
  MARKETPLACE_ITEMS,
} from "./data/marketplace";

/**
 * Top-level app: Chat is the main view. All management views (Groups, Logs,
 * Activity, Bots, DAG, Tools, Skills) live inside the SettingsModal.
 * The old tab bar is gone — the header is a slim iMessage-style title row:
 * bot identity on the left, backend status dot + Settings/Marketplace
 * icon actions on the right.
 */

const LS_SIDEBAR_COLLAPSED = "boom.sidebarCollapsed";

/**
 * useState mirrored to localStorage. `validate` rejects stale/foreign values
 * (e.g. from an older tab id set) so the app falls back to `initial`.
 */
function usePersistedState<T>(
  key: string,
  initial: T,
  validate?: (value: unknown) => value is T,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return initial;
      const parsed: unknown = JSON.parse(raw);
      if (validate && !validate(parsed)) return initial;
      return parsed as T;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage unavailable — state still works in memory */
    }
  }, [key, value]);
  return [value, setValue];
}

/**
 * Slim header row (52px), iMessage-style chat title bar.
 * Left: bot identity — avatar monogram + name + subtitle.
 * Right: backend status dot + command palette hint.
 * Settings / Marketplace now live in the sidebar footer.
 * Monochrome: CSS vars only (var(--bg), var(--surface), var(--border),
 * var(--text), var(--muted)). Icon buttons are ghost circles with a gray
 * hover background.
 */
function Header({
  onOpenCommandPalette,
}: {
  onOpenCommandPalette(): void;
}) {
  return (
    <header className="flex h-[52px] shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--bg)] px-3">
      {/* Left — bot identity (iMessage-style title). */}
      <div className="flex min-w-0 items-center gap-2.5">
        <div
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--surface)] text-[13px] font-semibold text-[var(--text)]"
          aria-hidden="true"
        >
          B
        </div>
        <div className="flex min-w-0 flex-col leading-tight">
          <span className="truncate text-[13px] font-semibold text-[var(--text)]">
            Boomi
          </span>
          <span className="truncate text-[11px] text-[var(--muted)]">
            AI assistant
          </span>
        </div>
      </div>

      {/* Right — backend status dot + command palette. */}
      <div className="ml-auto flex shrink-0 items-center gap-1">
        <BackendStatus compact />
        {/* Command palette hint chip — opens the palette on click. */}
        <Tooltip label="Command palette (⌘K)">
          <button
            type="button"
            onClick={onOpenCommandPalette}
            aria-label="Open command palette"
            className="flex h-9 items-center justify-center rounded-full px-2 text-[var(--muted)] transition-colors hover:bg-[var(--surface)] hover:text-[var(--text)]"
          >
            <kbd className="border border-[var(--border)] rounded bg-[var(--surface)] px-1.5 py-0.5 font-[inherit] text-[11px] font-medium leading-none">
              ⌘K
            </kbd>
          </button>
        </Tooltip>
      </div>
    </header>
  );
}

/**
 * The old 320px inspector panel was merged into the main navigation — all
 * eight views now render in the center column. ChatView still declares a
 * required onTogglePanel prop (owned by the ui-chat worker), so a no-op is
 * passed until that toggle button is removed or repurposed there.
 * NOTE for coordinator: see REPORT.md — do NOT make the prop optional here;
 * that edit belongs to the ui-chat worker in components/ChatView.tsx.
 */
function noop(): void {
  /* intentionally empty — see comment above */
}

export default function App() {
  // Global shortcuts: Cmd/Ctrl+K → command palette, Cmd/Ctrl+, → settings
  // (dispatches window CustomEvents; App listens for the settings one).
  useKeyboardShortcuts();
  useEffect(() => {
    const openSettings = () => setSettingsOpen(true);
    window.addEventListener("boom:open-settings", openSettings);
    return () => window.removeEventListener("boom:open-settings", openSettings);
  }, []);

  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = usePersistedState(
    LS_SIDEBAR_COLLAPSED,
    false,
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("providers");

  const openSettingsSection = (section: SettingsSection) => {
    setSettingsSection(section);
    setSettingsOpen(true);
  };
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // Bot roster for the teammates sidebar. ChatView owns the fetch +
  // switch logic and reports it via onBotApi; App mirrors it here so the
  // Sidebar (dumb component) can render and trigger switches.
  const [bots, setBots] = useState<Bot[]>([]);
  const [activeBotId, setActiveBotId] = useState<string | null>(null);
  const botApiRef = useRef<BotApi | null>(null);
  const handleBotApi = useCallback((api: BotApi) => {
    botApiRef.current = api;
    setBots(api.bots);
    setActiveBotId(api.activeBotId);
  }, []);

  // Marketplace modal state.
  const [marketplaceOpen, setMarketplaceOpen] = useState(false);
  const [marketplaceItems, setMarketplaceItems] =
    useState<MarketplaceItem[]>(MARKETPLACE_ITEMS);
  const [installedIds, setInstalledIds] = useState<string[]>([]);
  const [marketBots, setMarketBots] = useState<BotSummary[]>([]);
  const [toast, setToast] = useState<{
    id: number;
    message: string;
    type: "success" | "error";
  } | null>(null);

  const showToast = (message: string, type: "success" | "error" = "success") =>
    setToast((prev) => ({ id: (prev?.id ?? 0) + 1, message, type }));

  /**
   * Open the Marketplace modal and (re)load its data.
   * The gateway may not implement every marketplace endpoint, so each fetch
   * falls back to local data instead of failing the modal.
   */
  const openMarketplace = () => {
    setMarketplaceOpen(true);
    void (async () => {
      try {
        setMarketplaceItems(await getMarketplace());
      } catch {
        setMarketplaceItems(MARKETPLACE_ITEMS);
      }
      try {
        setInstalledIds(await getMarketplaceInstalled());
      } catch {
        setInstalledIds([]);
      }
      try {
        const bots = await listBots();
        setMarketBots(
          bots.map((b) => ({
            id: b.id,
            name: b.name,
            icon: b.avatar_emoji || "🤖",
            description: b.description || b.name,
          })),
        );
      } catch {
        setMarketBots([]);
      }
    })();
  };

  const handleMarketplaceAdd = async (item: MarketplaceItem) => {
    try {
      await installMarketplaceItem(item.id);
      setInstalledIds((ids) =>
        ids.includes(item.id) ? ids : [...ids, item.id],
      );
      showToast(`Added ${item.name} ✓`, "success");
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : `Failed to add ${item.name}`,
        "error",
      );
    }
  };

  const handleUseBot = async (id: string) => {
    try {
      await setActiveBot(id);
      setMarketplaceOpen(false);
      showToast("Active bot updated ✓", "success");
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : "Failed to set active bot",
        "error",
      );
    }
  };

  const closeOverlays = () => {
    setMobileNavOpen(false);
    setSettingsOpen(false);
    setMarketplaceOpen(false);
  };

  // Escape closes any overlay (settings, mobile sidebar drawer).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeOverlays();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Command palette actions: static app commands + one entry per bot from
  // the mirrored ChatView roster (botApiRef owns the actual switch call).
  const commandActions = useMemo<CommandAction[]>(
    () => [
      {
        id: "new-chat",
        label: "New chat",
        keywords: "create session conversation start",
        run: () => {
          setActiveSession(null);
          setMobileNavOpen(false);
        },
      },
      {
        id: "open-settings",
        label: "Open settings",
        keywords: "preferences configuration options",
        run: () => setSettingsOpen(true),
      },
      {
        id: "open-marketplace",
        label: "Open marketplace",
        keywords: "plugins bots store install",
        run: () => openMarketplace(),
      },
      {
        id: "toggle-sidebar",
        label: "Toggle sidebar",
        keywords: "collapse expand navigation panel",
        run: () => setSidebarCollapsed((v) => !v),
      },
      ...bots.map((bot) => ({
        id: `switch-bot-${bot.id}`,
        label: `Switch to ${bot.name}`,
        keywords: `bot assistant switch ${bot.name} ${bot.id}`,
        run: () => {
          void botApiRef.current?.switchBot(bot.id);
        },
      })),
    ],
    [bots],
  );

  const sidebarProps = {
    collapsed: sidebarCollapsed,
    onToggle: () => setSidebarCollapsed((v) => !v),
    bots,
    activeBotId,
    onSelectBot: (id: string) => {
      // ChatView owns bot switching (API + localStorage); call through.
      void botApiRef.current?.switchBot(id);
      setMobileNavOpen(false);
    },
    onNewChat: () => {
      setActiveSession(null);
      setMobileNavOpen(false);
    },
    onCreateBot: () => {
      openSettingsSection("bots");
      setMobileNavOpen(false);
    },
    onCreateGroup: () => {
      openSettingsSection("bot-groups");
      setMobileNavOpen(false);
    },
    onOpenSettings: () => setSettingsOpen(true),
    onOpenMarketplace: openMarketplace,
  };

  return (
    <WelcomeGate>
    <a href="#boom-main" className="skip-link">
      Skip to content
    </a>
    <div className="flex h-dvh w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      {/* Update banner — Tauri desktop only (renders null in the browser). */}
      <UpdateChecker />
      {/* Left column — desktop sidebar (260px, 68px when collapsed) */}
      <div
        className={`hidden shrink-0 overflow-hidden border-r border-[var(--border)] bg-[var(--surface)] md:block ${
          sidebarCollapsed ? "w-[68px]" : "w-[260px]"
        }`}
      >
        <Sidebar {...sidebarProps} />
      </div>

      {/* Center column — slim header, chat view */}
      <main id="boom-main" className="flex min-w-0 flex-1 flex-col bg-[var(--bg)]">
        <Header
          onOpenCommandPalette={() =>
            window.dispatchEvent(new CustomEvent(OPEN_COMMAND_PALETTE_EVENT))
          }
        />
        {/* Chat is the main view; all management lives in SettingsModal. */}
        <div className="min-h-0 flex-1 overflow-y-auto" role="tabpanel" aria-label="Chat view">
          {/* Full backend health panel — only visible while disconnected. */}
          <BackendStatus />
          <ChatView
            sessionId={activeSession}
            onTogglePanel={noop}
            onOpenSettings={() => setSettingsOpen(true)}
            onBotApi={handleBotApi}
          />
        </div>
      </main>

      {/* Sidebar as overlay drawer on <768px */}
      {mobileNavOpen && (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Sessions">
          <div
            className="fade-in absolute inset-0 bg-black/60"
            onClick={() => setMobileNavOpen(false)}
            aria-hidden="true"
          />
          <div className="panel-slide-in-left absolute inset-y-0 left-0 w-[280px] max-w-[85vw] border-r border-[var(--border)] bg-[var(--surface)]">
            <Sidebar
              {...sidebarProps}
              collapsed={false}
              onToggle={() => setMobileNavOpen(false)}
            />
          </div>
        </div>
      )}

      {/* Mobile nav trigger (<768px) — unchanged. */}
      {!mobileNavOpen && (
        <button
          type="button"
          onClick={() => setMobileNavOpen(true)}
          title="Open sessions"
          aria-label="Open sessions"
          className="btn-hover fixed bottom-5 left-5 z-30 flex h-11 w-11 items-center justify-center rounded-full bg-[var(--accent)] text-white shadow-lg md:hidden"
        >
          <Menu className="h-5 w-5" aria-hidden="true" />
        </button>
      )}

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialSection={settingsSection}
      />

      {/* Marketplace modal + toast notifications */}
      <Marketplace
        open={marketplaceOpen}
        onClose={() => setMarketplaceOpen(false)}
        onAdd={handleMarketplaceAdd}
        onUseBot={handleUseBot}
        installedIds={installedIds}
        items={marketplaceItems}
        categories={MARKETPLACE_CATEGORIES}
        bots={marketBots}
      />
      {toast !== null && (
        <Toast key={toast.id} message={toast.message} type={toast.type} />
      )}

      {/* Command palette (Cmd/Ctrl+K). Self-contained open state. */}
      <CommandPalette actions={commandActions} />
    </div>
    </WelcomeGate>
  );
}
