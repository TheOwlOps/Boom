import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import {
  Activity,
  Menu,
  MessageSquare,
  Radio,
  ScrollText,
  Sparkles,
  Users,
  Workflow,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Sidebar from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import DagViewer from "./components/DagViewer";
import { ChannelMonitor } from "./components/ChannelMonitor";
import { SettingsPanel } from "./components/SettingsPanel";
import StatusBar from "./components/StatusBar";
import ToolManager from "./components/ToolManager";
import SkillList from "./components/SkillList";
// Dashboard views built by sibling workers — default-exported React components.
import GroupManager from "./components/GroupManager";
import LogViewer from "./components/LogViewer";
import { ActivityFeed } from "./components/ActivityFeed";

/**
 * Top-level dashboard views rendered in the center column.
 *
 * NOTE: the old right-panel "logs" tab rendered TerminalLog. That view is
 * superseded here: "logs" now renders LogViewer. TerminalLog.tsx is left
 * untouched on disk but is no longer referenced by App.
 */
type MainTab =
  | "chat"
  | "groups"
  | "logs"
  | "activity"
  | "dag"
  | "channels"
  | "tools"
  | "skills";

const MAIN_TABS: readonly MainTab[] = [
  "chat",
  "groups",
  "logs",
  "activity",
  "dag",
  "channels",
  "tools",
  "skills",
];

function isMainTab(value: unknown): value is MainTab {
  return (
    typeof value === "string" &&
    (MAIN_TABS as readonly string[]).includes(value)
  );
}

const LS_SIDEBAR_COLLAPSED = "boom.sidebarCollapsed";
const LS_MAIN_TAB = "boom.mainTab";

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

type NavGroup = "primary" | "ops";

const TABS: Array<{
  id: MainTab;
  label: string;
  icon: LucideIcon;
  group: NavGroup;
}> = [
  { id: "chat", label: "Chat", icon: MessageSquare, group: "primary" },
  { id: "groups", label: "Groups", icon: Users, group: "primary" },
  { id: "logs", label: "Logs", icon: ScrollText, group: "primary" },
  { id: "activity", label: "Activity", icon: Activity, group: "primary" },
  { id: "dag", label: "DAG", icon: Workflow, group: "ops" },
  { id: "channels", label: "Channels", icon: Radio, group: "ops" },
  { id: "tools", label: "Tools", icon: Wrench, group: "ops" },
  { id: "skills", label: "Skills", icon: Sparkles, group: "ops" },
];

/**
 * Main navigation bar rendered under the StatusBar. Horizontally scrollable
 * on narrow screens so every tab stays reachable on mobile.
 */
function MainNav({
  tab,
  onTab,
}: {
  tab: MainTab;
  onTab(t: MainTab): void;
}) {
  const renderButton = ({ id, label, icon: TabIcon }: (typeof TABS)[number]) => (
    <button
      key={id}
      type="button"
      role="tab"
      aria-selected={tab === id}
      onClick={() => onTab(id)}
      className={`relative flex shrink-0 items-center gap-1.5 rounded-t-lg px-3 pb-2.5 pt-1.5 text-[13px] font-medium transition-colors ${
        tab === id
          ? "text-[var(--text)]"
          : "text-[var(--muted)] hover:text-[var(--text)]"
      }`}
    >
      <TabIcon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
      {tab === id && (
        <span
          className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-[var(--accent)]"
          aria-hidden="true"
        />
      )}
    </button>
  );

  return (
    <nav
      aria-label="Main navigation"
      role="tablist"
      className="flex items-center gap-1 overflow-x-auto border-b border-[var(--border)] bg-[var(--bg)] px-2 pt-2"
    >
      {TABS.filter((t) => t.group === "primary").map(renderButton)}
      <span
        className="mx-1 h-5 w-px shrink-0 bg-[var(--border)]"
        aria-hidden="true"
      />
      {TABS.filter((t) => t.group === "ops").map(renderButton)}
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
  const [mainTab, setMainTab] = usePersistedState<MainTab>(
    LS_MAIN_TAB,
    "chat",
    isMainTab,
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const closeOverlays = () => {
    setMobileNavOpen(false);
    setSettingsOpen(false);
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

  const activeLabel = TABS.find((t) => t.id === mainTab)?.label ?? mainTab;

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      {/* Left column — desktop sidebar (260px, 68px when collapsed) */}
      <div
        className={`hidden shrink-0 overflow-hidden border-r border-[var(--border)] bg-[var(--surface)] md:block ${
          sidebarCollapsed ? "w-[68px]" : "w-[260px]"
        }`}
      >
        <Sidebar {...sidebarProps} />
      </div>

      {/* Center column — status bar, main nav, active view */}
      <main className="flex min-w-0 flex-1 flex-col bg-[var(--bg)]">
        <StatusBar />
        <MainNav tab={mainTab} onTab={setMainTab} />
        {/* Independent scroll region: each view scrolls here (ChatView
            manages its own inner scroll via .chat-messages). */}
        <div
          className="min-h-0 flex-1 overflow-y-auto"
          role="tabpanel"
          aria-label={`${activeLabel} view`}
        >
          {mainTab === "chat" && (
            <ChatView sessionId={activeSession} onTogglePanel={noop} />
          )}
          {mainTab === "groups" && <GroupManager />}
          {mainTab === "logs" && <LogViewer />}
          {mainTab === "activity" && <ActivityFeed />}
          {mainTab === "dag" && <DagViewer />}
          {mainTab === "channels" && <ChannelMonitor />}
          {mainTab === "tools" && <ToolManager />}
          {mainTab === "skills" && <SkillList />}
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

      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
