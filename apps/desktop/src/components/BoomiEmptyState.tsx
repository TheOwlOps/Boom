import type { BoomiStatus } from "./BoomiAvatar";
import { BoomiAvatar } from "./BoomiAvatar";

export interface BoomiEmptyStateProps {
  /** Boomi mascot animation to play for this empty state. */
  animation: BoomiStatus;
  /** Headline shown under the mascot. */
  title: string;
  /** Optional supporting line under the title. */
  description?: string;
  /** Optional call-to-action; rendered only together with onAction. */
  actionLabel?: string;
  /** CTA handler; rendered only together with actionLabel. */
  onAction?: () => void;
  /** Pixel size of the Boomi mascot. Defaults to 96. */
  size?: number;
  /** Extra class names, e.g. for tighter padding in the sidebar. */
  className?: string;
}

/**
 * Shared Boomi-powered empty state: mascot animation + title + optional
 * description and CTA. Monochrome (theme vars only), centered layout,
 * subtle `.fade-in` entrance from tokens/animations.css. Render-only —
 * no data fetching or side effects.
 */
export function BoomiEmptyState({
  animation,
  title,
  description,
  actionLabel,
  onAction,
  size = 96,
  className = "",
}: BoomiEmptyStateProps) {
  return (
    <div
      className={`fade-in flex flex-col items-center justify-center px-6 py-10 text-center ${className}`}
      role="status"
    >
      <BoomiAvatar size={size} status={animation} alt="Boomi" />
      <p className="mt-4 text-[15px] font-semibold text-[var(--text)]">{title}</p>
      {description && (
        <p className="mt-1.5 max-w-[300px] text-sm leading-relaxed text-[var(--muted)]">
          {description}
        </p>
      )}
      {actionLabel && onAction && (
        <button
          type="button"
          onClick={onAction}
          className="btn-hover mt-4 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-4 py-2 text-sm font-medium text-[var(--text)] transition-colors hover:bg-[var(--surface-hover)]"
        >
          {actionLabel}
        </button>
      )}
    </div>
  );
}
