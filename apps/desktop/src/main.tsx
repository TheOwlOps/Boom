import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// Order matters: design tokens first, then shared animation vocabulary,
// then semantic component styles (which reference both).
import "./tokens/theme.css";
import "./tokens/animations.css";
import "./tokens/components.css";
// Micro-interactions: additive press/hover/entrance polish that
// intentionally overrides the base transitions above.
import "./styles/micro-interactions.css";
// Loading skeletons: shimmer keyframes for skeleton placeholders.
import "./styles/loading.css";
// Fine polish: scrollbars, selection, tooltip base (theme-aware).
import "./styles/polish.css";
// Accessibility: focus rings, skip link, reduced-motion guard.
import "./styles/accessibility.css";
// Responsive polish LAST: additive media queries that override the
// above (plus Tailwind utilities) only below their breakpoints.
import "./styles/responsive.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
