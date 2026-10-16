import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// Order matters: design tokens first, then shared animation vocabulary,
// then semantic component styles (which reference both).
import "./tokens/theme.css";
import "./tokens/animations.css";
import "./tokens/components.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
