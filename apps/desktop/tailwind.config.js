/* ------------------------------------------------------------------
 * Staged mirror of `apps/web/tailwind.config.js` (repo currently has none).
 * Tailwind v3.4 — colors read from the CSS variables in tokens/theme.css
 * so switching [data-theme] repaints the whole UI without a rebuild.
 * ------------------------------------------------------------------ */

const withAlpha = (name) => `rgb(var(--${name}-rgb) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: withAlpha("bg"),
        surface: withAlpha("surface"),
        "surface-2": withAlpha("surface-2"),
        border: "var(--border)",
        text: withAlpha("text"),
        muted: withAlpha("muted"),
        accent: withAlpha("accent"),
        "accent-hover": withAlpha("accent-hover"),
        success: withAlpha("success"),
        warning: withAlpha("warning"),
        danger: withAlpha("danger"),
      },
      fontFamily: {
        display: "var(--font-display)",
        body: "var(--font-body)",
        mono: "var(--font-mono)",
      },
      borderRadius: {
        card: "var(--radius-card)",
        control: "var(--radius-control)",
        pill: "var(--radius-pill)",
      },
      maxWidth: {
        chat: "768px",
      },
    },
  },
  plugins: [],
};
