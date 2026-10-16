import { useEffect, useState } from "react";

interface ToastProps {
  /** Text to show. Renders nothing while null. */
  message: string | null;
  /** Accent color: green (default) or red. */
  type?: "success" | "error";
}

const DISPLAY_MS = 3000;
const FADE_MS = 250;

/**
 * Bottom-center toast notification (dark theme).
 *
 * Slides up + fades in on mount, auto-dismisses after 3s with a fade-out.
 * The parent should pass a unique `key` per toast so consecutive toasts —
 * even with identical text — re-trigger the enter animation.
 */
export function Toast({ message, type = "success" }: ToastProps) {
  const [leaving, setLeaving] = useState(false);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    if (message === null) return;
    const fadeTimer = window.setTimeout(
      () => setLeaving(true),
      DISPLAY_MS - FADE_MS,
    );
    const goneTimer = window.setTimeout(() => setGone(true), DISPLAY_MS);
    return () => {
      window.clearTimeout(fadeTimer);
      window.clearTimeout(goneTimer);
    };
  }, [message]);

  if (message === null || gone) return null;

  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-8 z-[100] flex justify-center px-4"
      role="status"
      aria-live="polite"
    >
      <div
        className={`flex items-center gap-2.5 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm font-medium text-[var(--text)] shadow-2xl ${
          leaving ? "toast-out" : "toast-in"
        }`}
      >
        <span
          className={`inline-block h-2 w-2 shrink-0 rounded-full ${
            type === "error" ? "bg-red-500" : "bg-green-500"
          }`}
          aria-hidden="true"
        />
        {message}
      </div>
      <style>{`
        @keyframes toast-in {
          from { opacity: 0; transform: translateY(12px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes toast-out {
          from { opacity: 1; transform: translateY(0); }
          to { opacity: 0; transform: translateY(8px); }
        }
        .toast-in { animation: toast-in 220ms cubic-bezier(0.22, 1, 0.36, 1); }
        .toast-out { animation: toast-out 250ms ease-in forwards; }
      `}</style>
    </div>
  );
}
