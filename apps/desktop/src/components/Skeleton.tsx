/**
 * Skeleton — reusable skeleton loading primitives (monochrome shimmer).
 *
 * - Skeleton       : generic shimmer block (any size, optional circle/radius)
 * - SkeletonText   : paragraph-style line stack with a shorter last line
 * - SkeletonAvatar : circular avatar placeholder
 * - SkeletonCard   : marketplace-style card placeholder (icon + title +
 *                    description lines + action button)
 *
 * Shimmer visuals live in ../styles/loading.css (imported by main.tsx):
 * CSS keyframe `skeleton-shimmer` (1.5s gradient sweep, grays only) with a
 * prefers-reduced-motion fallback to a static gray. Blocks are
 * aria-hidden; the region that owns them carries role="status"/aria-busy.
 */
import type { CSSProperties } from "react";

/* ------------------------------------------------------------------ */
/* Skeleton — generic shimmer block                                   */
/* ------------------------------------------------------------------ */

export interface SkeletonProps {
  /** CSS width (number = px). */
  width?: number | string;
  /** CSS height (number = px). */
  height?: number | string;
  /** Border radius in px; overrides the default 6px from .sk-shimmer. */
  radius?: number;
  /** Render as a circle (e.g. avatar). */
  circle?: boolean;
  className?: string;
  style?: CSSProperties;
}

export function Skeleton({
  width = "100%",
  height = 14,
  radius,
  circle = false,
  className,
  style,
}: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={`sk-shimmer${className ? ` ${className}` : ""}`}
      style={{
        display: "block",
        width,
        height,
        borderRadius: circle ? "999px" : radius,
        ...style,
      }}
    />
  );
}

/* ------------------------------------------------------------------ */
/* SkeletonText — line stack                                          */
/* ------------------------------------------------------------------ */

export interface SkeletonTextProps {
  /** Number of shimmer lines. */
  lines?: number;
  /** Per-line widths (number = px, string = CSS). Defaults to full width
      with a shorter (62%) last line for a natural paragraph shape. */
  widths?: Array<number | string>;
  /** Gap between lines in px. */
  gap?: number;
  /** Line height in px. */
  lineHeight?: number;
  /** Accessible label for the status region. */
  label?: string;
  className?: string;
  style?: CSSProperties;
}

export function SkeletonText({
  lines = 3,
  widths,
  gap = 10,
  lineHeight = 14,
  label = "Loading",
  className,
  style,
}: SkeletonTextProps) {
  const rows = Array.from({ length: Math.max(1, lines) }, (_, i) => i);

  return (
    <div
      className={`sk-stack${className ? ` ${className}` : ""}`}
      style={{ gap, ...style }}
      role="status"
      aria-busy="true"
      aria-label={label}
    >
      {rows.map((i) => {
        const last = i === rows.length - 1;
        return (
          <Skeleton
            key={i}
            width={widths?.[i] ?? (last ? "62%" : "100%")}
            height={lineHeight}
            // 0.03s stagger keeps long lists calm (see DESIGN_CONTRACT.md)
            style={{ animationDelay: `${i * 0.03}s` }}
          />
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* SkeletonAvatar — circular avatar placeholder                       */
/* ------------------------------------------------------------------ */

export interface SkeletonAvatarProps {
  /** Diameter in px. */
  size?: number;
  className?: string;
  style?: CSSProperties;
}

export function SkeletonAvatar({ size = 40, className, style }: SkeletonAvatarProps) {
  return (
    <Skeleton
      circle
      width={size}
      height={size}
      className={className}
      style={style}
    />
  );
}

/* ------------------------------------------------------------------ */
/* SkeletonCard — marketplace card placeholder                        */
/* ------------------------------------------------------------------ */

export interface SkeletonCardProps {
  /** Description text lines under the title. */
  lines?: number;
  className?: string;
  style?: CSSProperties;
}

/**
 * Mirrors the PluginCard/BotCard layout (icon block, title, description
 * lines, action button) so the skeleton grid matches the loaded grid.
 */
export function SkeletonCard({ lines = 2, className, style }: SkeletonCardProps) {
  const rows = Array.from({ length: Math.max(1, lines) }, (_, i) => i);

  return (
    <div
      aria-hidden="true"
      className={`flex items-start gap-4 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4${
        className ? ` ${className}` : ""
      }`}
      style={style}
    >
      {/* icon block */}
      <Skeleton width={52} height={52} radius={10} />
      {/* title + description */}
      <div className="min-w-0 flex-1">
        <Skeleton width="40%" height={16} />
        <div className="mt-2 flex flex-col gap-2">
          {rows.map((i) => (
            <Skeleton
              key={i}
              width={i === rows.length - 1 ? "62%" : "100%"}
              height={12}
            />
          ))}
        </div>
      </div>
      {/* action button */}
      <Skeleton width={64} height={36} radius={8} />
    </div>
  );
}
