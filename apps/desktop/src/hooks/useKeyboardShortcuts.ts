import { useEffect } from "react";

/**
 * useKeyboardShortcuts — global keyboard shortcuts for the desktop app.
 *
 * Dispatches CustomEvents on `window` (decoupled by design):
 *   - `boom:open-command-palette` on Cmd/Ctrl+K
 *   - `boom:open-settings`         on Cmd/Ctrl+,
 *
 * Consumers (command palette, App) listen for these events themselves;
 * this hook owns no UI. `Enter` is deliberately NOT hijacked — the chat
 * composer owns it. Cmd+K works even when focus is inside an
 * input/textarea/contentEditable; Cmd+, is ignored there so typing is
 * never interrupted.
 */
export function useKeyboardShortcuts(): void {
  useEffect(() => {
    const inEditableField = (target: EventTarget | null): boolean => {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return (
        tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable
      );
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();

      if (key === "k") {
        // Always active, even from text fields.
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("boom:open-command-palette"));
      } else if (key === ",") {
        // Ignored inside editable fields to avoid interrupting typing.
        if (inEditableField(e.target)) return;
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("boom:open-settings"));
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
