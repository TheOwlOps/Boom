/**
 * ShortcutHint — tiny monochrome `<kbd>`-styled hint for keyboard shortcuts.
 * Example: <ShortcutHint keys={["⌘", "K"]} label="Command palette" />
 */
export function ShortcutHint({
  keys,
  label,
}: {
  keys: string[];
  label?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="inline-flex items-center gap-0.5" aria-hidden="true">
        {keys.map((k) => (
          <kbd
            key={k}
            className="rounded border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-[10px] leading-none text-[var(--muted)]"
          >
            {k}
          </kbd>
        ))}
      </span>
      {label && (
        <span className="text-[11px] text-[var(--muted)]">{label}</span>
      )}
    </span>
  );
}
