/**
 * SettingsModal — consolidated settings and management hub.
 *
 * Replaces the old top-bar tab navigation (Groups, Activity, Bots,
 * DAG, Tools, Skills, Settings). Opens as a full-screen modal
 * (same pattern as Marketplace) with a left sidebar for section navigation
 * and the main content area rendering the corresponding component.
 *
 * Sections:
 *   - General: theme, font size, backend, notifications, about (SettingsPage)
 *   - Groups: GroupManager
 *   - Activity: ActivityFeed
 *   - Bots: BotManager
 *   - Bot Groups: BotGroupManager
 *   - DAG: DagViewer
 *   - Tools: ToolManager (includes the Channels subsection at the top)
 *   - Skills: SkillList
 */

import { useEffect, useRef, useState } from "react";
import {
  Activity,
  Bell,
  Bot,
  Info,
  Palette,
  Plug,
  Radio,

  Search,
  Sparkles,
  Users,
  UsersRound,
  Workflow,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { ActivityFeed } from "./ActivityFeed";
import { ShortcutHint } from "./ShortcutHint";
import BotGroupManager from "./BotGroupManager";
import BotManager from "./BotManager";
import DagViewer from "./DagViewer";
import GroupManager from "./GroupManager";
import SettingsPage from "./SettingsPage";
import SkillList from "./SkillList";
import ToolManager from "./ToolManager";

export type SettingsSection =
  | "providers"
  | "appearance"
  | "notifications"
  | "about"
  | "groups"
  | "activity"
  | "bots"
  | "bot-groups"
  | "dag"
  | "tools"
  | "skills";

const SECTIONS: Array<{
  id: SettingsSection;
  label: string;
  icon: LucideIcon;
  keywords: string[];
}> = [
  { id: "providers", label: "Providers", icon: Plug, keywords: ["provider", "api key", "model", "llm", "openai", "anthropic"] },
  { id: "appearance", label: "Appearance", icon: Palette, keywords: ["theme", "appearance", "font", "dark", "light"] },
  { id: "notifications", label: "Notifications", icon: Bell, keywords: ["notification", "sound", "alert"] },
  { id: "about", label: "About", icon: Info, keywords: ["about", "version", "update"] },
  { id: "groups", label: "Groups", icon: Users, keywords: ["group", "chat group", "member"] },
  { id: "activity", label: "Activity", icon: Activity, keywords: ["activity", "event", "history"] },
  { id: "bots", label: "Bots", icon: Bot, keywords: ["bot", "assistant"] },
  { id: "bot-groups", label: "Bot Groups", icon: UsersRound, keywords: ["bot group", "group chat", "bots", "members"] },
  { id: "dag", label: "DAG", icon: Workflow, keywords: ["dag", "workflow", "pipeline", "graph"] },
  { id: "tools", label: "Tools", icon: Wrench, keywords: ["tool", "function", "channel", "telegram", "discord", "zalo"] },
  { id: "skills", label: "Skills", icon: Sparkles, keywords: ["skill", "capability"] },
];

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
  /** Section to open on; defaults to "general". */
  initialSection?: SettingsSection;
}

export function SettingsModal({ open, onClose, initialSection = "providers" }: SettingsModalProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection);
  const [search, setSearch] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);

  // Reset to initial section each time the modal opens.
  useEffect(() => {
    if (open) {
      setSection(initialSection);
      setSearch("");
    }
  }, [open, initialSection]);

  // ESC closes; focus close button on open.
  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const query = search.trim().toLowerCase();
  const filtered = query
    ? SECTIONS.filter(
        (s) =>
          s.label.toLowerCase().includes(query) ||
          s.keywords.some((k) => k.includes(query)),
      )
    : SECTIONS;

  const activeLabel = SECTIONS.find((s) => s.id === section)?.label ?? "Providers";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
    >
      <div
        className="flex h-[85vh] w-full max-w-5xl overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg)] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Left sidebar: search + section nav */}
        <aside className="flex w-60 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)]">
          <div className="border-b border-[var(--border)] p-4">
            <h2 className="text-base font-semibold text-[var(--text)]">Settings</h2>
            <div className="relative mt-3">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--muted)]"
                aria-hidden="true"
              />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search settings…"
                aria-label="Search settings sections"
                className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] py-1.5 pl-8 pr-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)] focus:border-[var(--accent)] focus:outline-none"
              />
            </div>
          </div>
          <nav aria-label="Settings sections" className="flex-1 overflow-y-auto p-2">
            {filtered.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => setSection(id)}
                aria-current={section === id ? "page" : undefined}
                className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  section === id
                    ? "bg-[var(--surface-2)] text-[var(--text)]"
                    : "text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                }`}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {label}
              </button>
            ))}
            {filtered.length === 0 && (
              <p className="px-3 py-4 text-sm text-[var(--muted)]">
                No sections match “{search}”.
              </p>
            )}
          </nav>
          {/* Keyboard shortcut hints — subtle, bottom of sidebar. */}
          <div className="border-t border-[var(--border)] p-3">
            <div className="flex items-center justify-between">
              <span className="text-[11px] text-[var(--muted)]">
                Command palette
              </span>
              <ShortcutHint keys={["⌘", "K"]} />
            </div>
            <div className="mt-1.5 flex items-center justify-between">
              <span className="text-[11px] text-[var(--muted)]">Settings</span>
              <ShortcutHint keys={["⌘", ","]} />
            </div>
          </div>
        </aside>

        {/* Main content */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4">
            <h3 className="text-lg font-semibold text-[var(--text)]">{activeLabel}</h3>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              aria-label="Close settings"
              className="rounded-lg p-1.5 text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
            >
              <X className="h-5 w-5" aria-hidden="true" />
            </button>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-6">
            {(section === "providers" || section === "appearance" || section === "notifications" || section === "about") && (
              <SettingsPage section={section} />
            )}
            {section === "groups" && <GroupManager />}
            {section === "activity" && <ActivityFeed />}
            {section === "bots" && <BotManager />}
            {section === "bot-groups" && <BotGroupManager />}
            {section === "dag" && <DagViewer />}
            {section === "tools" && <ToolManager />}
            {section === "skills" && <SkillList />}
          </div>
        </div>
      </div>
    </div>
  );
}

// Re-export for the notification icon used in SettingsPage section nav.
export { Bell };
