import type { LucideIcon } from "lucide-react";

export interface EmptyStateAction {
  label: string;
  onClick(): void;
}

export interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  hint?: string;
  action?: EmptyStateAction;
}

/**
 * Shared empty-state primitive: lucide icon + one line + optional action.
 * Entrance uses `.fade-in` from the shared animations.css (DESIGN_CONTRACT).
 */
export function EmptyState({ icon: Icon, title, hint, action }: EmptyStateProps) {
  return (
    <div
      className="fade-in flex h-full flex-col items-center justify-center gap-2 px-6 py-10 text-center"
      role="status"
    >
      <span className="mb-1 flex h-11 w-11 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface-2)]">
        <Icon className="h-5 w-5 text-[var(--muted)]" aria-hidden="true" />
      </span>
      <p className="font-display text-[15px] font-semibold text-[var(--text)]">{title}</p>
      {hint && (
        <p className="max-w-[280px] text-sm leading-relaxed text-[var(--muted)]">{hint}</p>
      )}
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="btn-hover mt-2 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--bg)] transition-colors hover:bg-[var(--accent-hover)]"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
