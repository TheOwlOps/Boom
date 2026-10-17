// formatTime.ts — smart, human-readable timestamps for the chat UI.
//
// formatSmartTime: "just now" (<60s), "Xm ago", "Xh ago",
//                  "Yesterday", "X days ago" (≤6), else locale date.
// formatFullTime:  full locale date-time, for title tooltips / aria.

/** Accepts Date | string | number (ms epoch); returns "" when invalid. */
function toDate(input: Date | string | number): Date | null {
  try {
    const d = input instanceof Date ? input : new Date(input);
    return Number.isFinite(d.getTime()) && d.getTime() > 0 ? d : null;
  } catch {
    return null;
  }
}

function isYesterday(now: Date, d: Date): boolean {
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const b = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((a.getTime() - b.getTime()) / 86_400_000);
  return diffDays === 1;
}

/** Relative, chat-friendly timestamp (iMessage-style). */
export function formatSmartTime(date: Date | string | number): string {
  const d = toDate(date);
  if (!d) return "";

  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  if (diffMs < 0) return ""; // future — don't render
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24 && d.getDate() === now.getDate()) return `${diffHr}h ago`;
  if (isYesterday(now, d)) return "Yesterday";

  const diffDays = Math.floor(diffMs / 86_400_000);
  if (diffDays < 7) return `${diffDays} days ago`;

  return d.toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: d.getFullYear() !== now.getFullYear() ? "numeric" : undefined,
  });
}

/** Full locale date-time for hover/title tooltips. */
export function formatFullTime(date: Date | string | number): string {
  const d = toDate(date);
  if (!d) return "";
  try {
    return d.toLocaleString([], {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}
