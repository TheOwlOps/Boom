import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { Menu, PanelRightClose, Radio, Workflow, X, Wrench, Sparkles, TerminalSquare } from "lucide-react";
import Sidebar from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import DagViewer from "./components/DagViewer";
import { ChannelMonitor } from "./components/ChannelMonitor";
import { SettingsPanel } from "./components/SettingsPanel";
import StatusBar from "./components/StatusBar";
import ToolManager from "./components/ToolManager";
import SkillList from "./components/SkillList";
import TerminalLog from "./components/TerminalLog";

type PanelTab = "dag" | "channels" | "tools" | "skills" | "logs";

const LS_SIDEBAR_COLLAPSED = "boom.sidebarCollapsed";
const LS_PANEL_OPEN = "boom.panelOpen";
const LS_PANEL_TAB = "boom.panelTab";

/** useState mirrored to localStorage; falls back to `initial` when unavailable. */
function usePersistedState<T>(
  key: string,
  initial: T,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
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

const TABS: Array<{ id: PanelTab; label: string; icon: typeof Workflow }> = [
  { id: "dag", label: "DAG", icon: Workflow },
  { id: "channels", label: "Channels", icon: Radio },
  { id: "tools", label: "Tools", icon: Wrench },
  { id: "skills", label: "Skills", icon: Sparkles },
  { id: "logs", label: "Logs", icon: TerminalSquare },
];

/** Right-panel body shared by the desktop column and the <1024px drawer. */
function RightPanel({
  tab,
  onTab,
  onClose,
}: {
  tab: PanelTab;
  onTab(t: PanelTab): void;
  onClose(): void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b border-[var(--border)] px-2 pt-2">
        {TABS.map(({ id, label, icon: TabIcon }) => (
          <button
            key={id}
            type="button"
            onClick={() => onTab(id)}
            aria-pressed={tab === id}
            className={`relative flex items-center gap-1.5 rounded-t-lg px-3 pb-2.5 pt-1.5 text-[13px] font-medium transition-colors ${
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
        ))}
        <span className="flex-1" />
        <button
          type="button"
          onClick={onClose}
          title="Close panel"
          aria-label="Close panel"
          className="btn-hover mb-1 rounded-lg p-1.5 text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "dag" && <DagViewer />}
        {tab === "channels" && <ChannelMonitor />}
        {tab === "tools" && <ToolManager />}
        {tab === "skills" && <SkillList />}
        {tab === "logs" && <TerminalLog />}
      </div>
    </div>
  );
}

export default function App() {
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = usePersistedState(
    LS_SIDEBAR_COLLAPSED,
    false,
  );
  const [panelOpen, setPanelOpen] = usePersistedState(LS_PANEL_OPEN, true);
  const [panelTab, setPanelTab] = usePersistedState<PanelTab>(LS_PANEL_TAB, "dag");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const togglePanel = () => setPanelOpen((v) => !v);
  const closeOverlays = () => {
    setMobileNavOpen(false);
    setSettingsOpen(false);
  };

  // Escape closes any overlay (settings, mobile nav, mobile panel drawer).
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
    <div className="flex h-dvh w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      {/* Left column — desktop sidebar (260px, 68px when collapsed) */}
      <div
        className={`hidden shrink-0 overflow-hidden border-r border-[var(--border)] bg-[var(--surface)] md:block ${
          sidebarCollapsed ? "w-[68px]" : "w-[260px]"
        }`}
      >
        <Sidebar {...sidebarProps} />
      </div>

      {/* Center column — chat */}
      <main className="flex min-w-0 flex-1 flex-col bg-[var(--bg)]">
        <StatusBar />
        <ChatView sessionId={activeSession} onTogglePanel={togglePanel} />
      </main>

      {/* Right column — desktop inspector (320px), hidden <1024px */}
      {panelOpen && (
        <aside
          className="panel-slide-in hidden w-[320px] shrink-0 border-l border-[var(--border)] bg-[var(--surface)] lg:block"
          aria-label="Inspector panel"
        >
          <RightPanel
            tab={panelTab}
            onTab={setPanelTab}
            onClose={() => setPanelOpen(false)}
          />
        </aside>
      )}

      {/* Right panel as overlay drawer on <1024px */}
      {panelOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-label="Inspector panel">
          <div
            className="fade-in absolute inset-0 bg-black/60"
            onClick={() => setPanelOpen(false)}
            aria-hidden="true"
          />
          <aside className="panel-slide-in absolute inset-y-0 right-0 w-[320px] max-w-[85vw] border-l border-[var(--border)] bg-[var(--surface)]">
            <RightPanel
              tab={panelTab}
              onTab={setPanelTab}
              onClose={() => setPanelOpen(false)}
            />
          </aside>
        </div>
      )}

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

      {/* Mobile panel trigger (<1024px) — mirrors ChatView's desktop toggle */}
      {!panelOpen && (
        <button
          type="button"
          onClick={() => setPanelOpen(true)}
          title="Open inspector panel"
          aria-label="Open inspector panel"
          className="btn-hover fixed bottom-5 right-5 z-30 flex h-11 w-11 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface-2)] text-[var(--muted)] shadow-lg lg:hidden"
        >
          <PanelRightClose className="h-5 w-5" aria-hidden="true" />
        </button>
      )}

      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
