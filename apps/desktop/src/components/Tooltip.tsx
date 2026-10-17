// Tooltip.tsx — lightweight CSS-only tooltip.
// Pure CSS (styles/polish.css); no positioning libs, no portals.
// Shows on hover AND keyboard focus; the bubble is aria-hidden because
// the trigger element must already expose the same label via aria-label.

import type { ReactNode } from "react";

export type TooltipPosition = "top" | "bottom" | "left" | "right";

interface TooltipProps {
  /** Text shown in the tooltip bubble. */
  label: string;
  /** Trigger element (button, icon, ...). */
  children: ReactNode;
  /** Where the bubble appears; defaults to "bottom". */
  position?: TooltipPosition;
  /** Extra class on the wrapper span. */
  className?: string;
}

export function Tooltip({
  label,
  children,
  position = "bottom",
  className = "",
}: TooltipProps) {
  if (!label) return <>{children}</>;
  return (
    <span className={`tip-wrap tip-${position}${className ? ` ${className}` : ""}`}>
      <span className="tip-bubble" role="presentation" aria-hidden="true">
        {label}
      </span>
      {children}
    </span>
  );
}
