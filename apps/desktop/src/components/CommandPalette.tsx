/**
 * CommandPalette — VSCode/Raycast-style command palette (Cmd/Ctrl+K).
 *
 * Self-contained open state: opens on Cmd/Ctrl+K via its own window
 * keydown listener, and on the `boom:open-command-palette` window
 * CustomEvent (dispatched by the keyboard-shortcuts owner and by the
 * header ⌘K hint chip). ESC closes; Enter runs the highlighted action.
 *
 * Monochrome: CSS vars only (var(--bg), var(--surface), var(--surface-2),
 * var(--border), var(--text), var(--muted)).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Search, Terminal } from "lucide-react";
import "../styles/command-palette.css";

export interface CommandAction {
  id: string;
  label: string;
  hint?: string;
  keywords?: string;
  run: () => void;
}

export interface CommandPaletteProps {
  actions: CommandAction[];
}

/** Event name another worker/window can dispatch to open the palette. */
export const OPEN_COMMAND_PALETTE_EVENT = "boom:open-command-palette";

export default function CommandPalette({ actions }: CommandPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset search state each time the palette opens.
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      inputRef.current?.focus();
    }
  }, [open ]);

  // Open triggers: Cmd/Ctrl+K fallback listener + the shared CustomEvent.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (
        (e.metaKey || e.ctrlKey) &&
        e.key.toLowerCase() === "k" &&
        !e.altKey
      ) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    const onCustom = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, onCustom);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, onCustom);
    };
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return actions;
    return actions.filter((a) => {
      const haystack = `${a.label} ${a.keywords ?? ""}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [actions, query]);

  const close = () => setOpen(false);

  const runAction = (action: CommandAction) => {
    close();
    action.run();
  };

  const onInputKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) =>
        filtered.length === 0 ? 0 : (i + 1) % filtered.length,
      );
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) =>
        filtered.length === 0
          ? 0
          : (i - 1 + filtered.length) % filtered.length,
      );
    } else if (e.key === "Enter") {
      e.preventDefault();
      const action = filtered[activeIndex];
      if (action) runAction(action);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };

  if (!open) return null;

  return (
    <div
      className="cmdk-backdrop"
      onClick={close}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className="cmdk-panel"
        onClick={(e) => e.stopPropagation()}
        role="listbox"
        aria-label="Available actions"
      >
        <div className="cmdk-input-row">
          <Search
            className="cmdk-input-icon"
            aria-hidden="true"
          />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={onInputKey}
            placeholder="Type a command or search…"
            aria-label="Search commands"
            role="combobox"
            aria-expanded="true"
            aria-controls="cmdk-list"
            aria-activedescendant={
              filtered.length > 0
                ? `cmdk-action-${filtered[activeIndex]?.id}`
                : undefined
            }
            className="cmdk-input"
          />
          <kbd className="cmdk-esc" aria-hidden="true">
            esc
          </kbd>
        </div>
        <div id="cmdk-list" className="cmdk-list">
          {filtered.map((action, i) => (
            <button
              key={action.id}
              id={`cmdk-action-${action.id}`}
              type="button"
              role="option"
              aria-selected={i === activeIndex}
              onMouseEnter={() => setActiveIndex(i)}
              onClick={() => runAction(action)}
              className={`cmdk-item${
                i === activeIndex ? " cmdk-item-active" : ""
              }`}
            >
              <Terminal
                className="cmdk-item-icon"
                aria-hidden="true"
              />
              <span className="cmdk-item-label">{action.label}</span>
              {action.hint && (
                <kbd className="cmdk-item-hint" aria-hidden="true">
                  {action.hint}
                </kbd>
              )}
            </button>
          ))}
          {filtered.length === 0 && (
            <p className="cmdk-empty">
              No commands match “{query.trim()}”.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
