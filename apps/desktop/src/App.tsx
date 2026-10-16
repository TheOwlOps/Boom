import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { Menu, MessageSquare, Settings, Store } from "lucide-react";
import Sidebar from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import StatusBar from "./components/StatusBar";
import { SettingsModal, type SettingsSection } from "./components/SettingsModal";
import UpdateChecker from "./components/UpdateChecker";
// Marketplace modal (types re-exported for consumers by the component itself).
import Marketplace, {
  type BotSummary,
  type MarketplaceItem,
} from "./components/Marketplace";
import { Toast } from "./components/Toast";
import BackendStatus from "./components/BackendStatus";
import WelcomeGate from "./components/WelcomeGate";
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
 * The old tab bar is gone — top bar keeps only Chat, Settings, Marketplace.
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
 * Simplified top bar: Chat is always the main view. Settings opens the
 * consolidated SettingsModal; Marketplace opens its own modal.
 * All management tabs (Groups, Logs, Activity, Bots, DAG,
 * Tools, Skills) moved into SettingsModal. Channels live inside Tools.
 */
function MainNav({
  onOpenSettings,
  onOpenMarketplace,
}: {
  onOpenSettings(): void;
  onOpenMarketplace(): void;
}) {
  return (
    <nav
      aria-label="Main navigation"
      className="flex items-center gap-1 border-b border-[var(--border)] bg-[var(--bg)] px-2 py-2"
    >
      <span className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium text-[var(--text)]">
        <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
        Chat
      </span>
      <span
        className="mx-1 h-5 w-px shrink-0 bg-[var(--border)]"
        aria-hidden="true"
      />
      <button
        type="button"
        onClick={onOpenSettings}
        title="Settings"
        aria-label="Open settings"
        className="flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium text-[var(--muted)] transition-colors hover:text-[var(--text)]"
      >
        <Settings className="h-3.5 w-3.5" aria-hidden="true" />
        Settings
      </button>
      {/* Marketplace opens a modal — pinned to the right end. */}
      <button
        type="button"
        onClick={onOpenMarketplace}
        title="Marketplace"
        aria-label="Open marketplace"
        className="ml-auto flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--border)] px-3 py-1.5 text-[13px] font-medium text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
      >
        <Store className="h-3.5 w-3.5" aria-hidden="true" />
        Marketplace
      </button>
    </nav>
  );
}

/**
 * The old 320px inspector panel was merged into the main navigation — all
 * eight views now render in the center column. ChatView still declares a
 * required onTogglePanel prop (owned by another worker), so a no-op is
 * passed until that toggle button is removed or repurposed there.
 */
function noop(): void {
  /* intentionally empty — see comment above */
}

export default function App() {
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = usePersistedState(
    LS_SIDEBAR_COLLAPSED,
    false,
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

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

  const sidebarProps = {
    collapsed: sidebarCollapsed,
    onToggle: () => setSidebarCollapsed((v) => !v),
    activeSession,
    onSelectSession: (id: string | null) => {
      setActiveSession(id);
      setMobileNavOpen(false);
    },
    onNewChat: () => {
      setActiveSession(null);
      setMobileNavOpen(false);
    },
    onOpenSettings: () => setSettingsOpen(true),
  };

  return (
    <WelcomeGate>
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

      {/* Center column — status bar, main nav, chat view */}
      <main className="flex min-w-0 flex-1 flex-col bg-[var(--bg)]">
        {/* Status bar row: existing StatusBar + backend health dot. */}
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <StatusBar />
          </div>
          <BackendStatus compact />
        </div>
        <MainNav
          onOpenSettings={() => setSettingsOpen(true)}
          onOpenMarketplace={openMarketplace}
        />
        {/* Chat is the main view; all management lives in SettingsModal. */}
        <div className="min-h-0 flex-1 overflow-y-auto" role="tabpanel" aria-label="Chat view">
          {/* Full backend health panel — only visible while disconnected. */}
          <BackendStatus />
          <ChatView
            sessionId={activeSession}
            onTogglePanel={noop}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        </div>
      </main>

      {/* Sidebar as overlay drawer on <768px */}
      {mobileNavOpen && (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-label="Sessions">
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

      {/* Mobile nav trigger (<768px) */}
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
    </div>
    </WelcomeGate>
  );
}
