/**
 * Skeleton — shimmer placeholder rows for loading panels.
 * Spec: DESIGN_CONTRACT.md — gradient sweep 1.5s, aria-busy.
 */
import { useMemo } from 'react';

interface SkeletonProps {
  /** number of shimmer rows */
  lines?: number;
  /** aria label for screen readers */
  label?: string;
}

export function Skeleton({ lines = 3, label = 'Loading' }: SkeletonProps) {
  const rows = useMemo(() => Array.from({ length: lines }, (_, i) => i), [lines]);

  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={label}
      style={{ display: 'flex', flexDirection: 'column', gap: 10 }}
    >
      {rows.map((i) => (
        <div
          key={i}
          className="skeleton-shimmer skeleton-row"
          style={{
            // shorten the last row for a natural paragraph shape
            width: i === rows.length - 1 ? '62%' : '100%',
            // 0.03s stagger keeps long lists calm
            animationDelay: `${i * 0.03}s`,
          }}
        >
          &nbsp;
        </div>
      ))}
    </div>
  );
}
