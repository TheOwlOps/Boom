/**
 * ThinkingDots — 3 bouncing accent dots + label, for assistant
 * "thinking" state. Spec: DESIGN_CONTRACT.md — 1.2s loop,
 * staggered 0.15s, dot-bounce keyframe in tokens/animations.css.
 */
interface ThinkingDotsProps {
  label?: string;
}

export function ThinkingDots({ label = 'Thinking' }: ThinkingDotsProps) {
  return (
    <span
      className="fade-in"
      role="status"
      aria-label={label}
      style={{ display: 'inline-flex', alignItems: 'center', gap: 10 }}
    >
      <span className="dot-row" aria-hidden="true">
        <span className="dot-bounce" />
        <span className="dot-bounce" />
        <span className="dot-bounce" />
      </span>
      <span style={{ fontSize: 13, color: 'var(--muted)' }}>{label}</span>
    </span>
  );
}
