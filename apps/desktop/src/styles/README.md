# effects.css — Usage Guide

Pure-CSS effect library for the Boom web client. Dark-theme first, no
dependencies, no JavaScript required. Import it from your global stylesheet:

```css
@import "./styles/effects.css";
```

## Classes

### 1. `.typing-reveal` — character-by-character reveal for streamed AI text

Wrap each character in a `<span>` and set `--i` per character for the stagger:

```html
<p class="typing-reveal"><span>H</span><span>i</span></p>
<script>
  el.querySelectorAll('.typing-reveal > span')
    .forEach((s, i) => s.style.setProperty('--i', i));
</script>
```

Add `.streaming` to show a blinking caret while tokens are still arriving.

### 2. `.mesh-bg` + `.mesh-blob` — animated gradient mesh background

Place behind content (`.mesh-bg` is `position: absolute`, `z-index: 0`):

```html
<div class="mesh-bg" aria-hidden="true">
  <span class="mesh-blob mesh-blob--a"></span>
  <span class="mesh-blob mesh-blob--b"></span>
  <span class="mesh-blob mesh-blob--c"></span>
</div>
```

20s slow drift on the compositor (transform/opacity only). Keep to 3–4 blobs.

### 3. `.msg-enter` — message entrance (slide-up + fade, 300ms ease-out)

Add when a message mounts. Use `.msg-enter--user` / `.msg-enter--ai`
for directional slide-in from right/left:

```html
<div class="msg-enter msg-enter--ai">…</div>
```

### 4. `.thinking-dots` — thinking indicator (3 staggered pulsing dots)

```html
<span class="thinking-dots" role="status" aria-label="Thinking">
  <i></i><i></i><i></i>
</span>
```

Dots pulse with 140ms stagger (breathing wave). Use
`.thinking-dots--bars` for a waveform variant.

### 5. `.glow-hover` — hover glow for buttons/cards

Subtle lift + shadow + colored glow (default indigo). Accents:
`.glow-hover--green`, `.glow-hover--pink`, `.glow-hover--amber`.
Override the tint per element with `--glow-color`.

```html
<button class="glow-hover">Send</button>
```

### 6. `.tab-fade` / `.tab-fade--active` / `.tab-fade--once` — tab transitions

Two-state approach: add `.tab-fade` to the panel, then `.tab-fade--active`
when the tab becomes visible:

```html
<div class="tab-fade tab-fade--active">…</div>
```

For a one-shot play-on-mount, use `.tab-fade--once` instead.

### 7. `.skeleton-shimmer` — loading placeholder shimmer

A diagonal highlight sweeps across the element (moved via `transform`
on `::after`, compositor-only). Size it with inline styles:

```html
<div class="skeleton-shimmer" style="height: 14px; border-radius: 6px;"></div>
```

### 8. Custom scrollbar — global, no class needed

Thin dark scrollbars apply to the whole app automatically
(`::-webkit-scrollbar` + Firefox `scrollbar-width`/`scrollbar-color`).

## Accessibility

`@media (prefers-reduced-motion: reduce)` disables every animation and
transition in this file and forces elements into their end state.

## Performance

All animations touch only `transform`, `opacity` and `filter` — never
`width`/`height`/`margin` — so they stay on the compositor and avoid
layout thrash.
