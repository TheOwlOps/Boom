# Boom Web UI (`apps/web`)

Grokbot-style clean chat UI for the Boom agent: sidebar sessions, chat with
typewriter reveal, right inspector panel (DAG graph + channel status), settings
drawer (theme / font size / backend health). Dark-first design tokens.

Spec sources: `~/workspace/boom-mods/webui/DESIGN_CONTRACT.md` (design) and
`~/workspace/boom-mods/webui/API_CONTRACT.md` (backend contract).

## Run it

Two processes — the Python gateway API and the Vite dev server.

**1. Backend** — from the repo root:

```bash
python -m gateway.api
# listens on http://127.0.0.1:18080 (override with BOOM_API_PORT)
```

No auth in v1 — the server binds to `127.0.0.1` only (localhost).
CORS allows `http://localhost:5173` (Vite) and `http://localhost:1420` (Tauri).

**2. Frontend** — from `apps/web`:

```bash
npm install
npm run dev     # http://localhost:5173
```

The UI talks to the gateway at `VITE_API_BASE` (default
`http://localhost:18080`):

```bash
VITE_API_BASE=http://127.0.0.1:18080 npm run dev
```

Production build:

```bash
npm run build   # tsc && vite build -> dist/
```

## Architecture

```
apps/web/
├── index.html                  # Google Fonts (Outfit / Work Sans / JetBrains Mono)
├── tailwind.config.js          # colors read CSS vars -> theme switch needs no rebuild
├── postcss.config.js
├── vite.config.ts
├── src/
│   ├── main.tsx                # CSS import order: theme.css -> animations.css -> components.css
│   ├── App.tsx                 # 3-column shell + drawers (<1024px / <768px) + settings overlay
│   ├── tokens/
│   │   ├── theme.css           # design tokens: colors, fonts, radii, base styles (@tailwind)
│   │   ├── animations.css      # ONE shared animation file (keyframes + utility classes)
│   │   └── components.css      # semantic classes the components use (chat-view, sidebar, ...)
│   ├── lib/
│   │   ├── types.ts            # wire shapes mirroring gateway/api.py responses
│   │   ├── api.ts              # validated HTTP client (unknown + narrowing, no retries)
│   │   └── useTheme.ts         # client-side theme (dark/light/system, localStorage)
│   └── components/
│       ├── ChatView.tsx        # messages + composer; CLIENT-SIDE typewriter reveal
│       ├── MessageParts.tsx    # MessageBubble (markdown), ToolCallCard, ErrorCard, CodeBlock
│       ├── Sidebar.tsx         # sessions list, search, collapse, channel dots
│       ├── DagViewer.tsx       # live DAG via @xyflow/react, polls /api/dag/status every 3s
│       ├── DagViewer.css       # node status colors + transitions
│       ├── ChannelMonitor.tsx  # full list + compact dots variant, 30s refresh
│       ├── SettingsPanel.tsx   # model (read-only), theme, font size, backend health test
│       ├── EmptyState.tsx      # shared empty-state primitive (icon + title + hint + action)
│       ├── Skeleton.tsx        # shimmer placeholder rows
│       └── ThinkingDots.tsx    # 3 bouncing dots "thinking" indicator
```

Backend: `gateway/api.py` (stdlib only: `http.server.ThreadingHTTPServer`),
tests in `evals/test_gateway_api.py` (live HTTP against a fake brain/daemon).

## New packages

- **`react-markdown` + `remark-gfm`** — render assistant markdown (GFM tables,
  task lists). No `rehype-raw`: raw HTML in model output is escaped, never
  injected. Code fences render via `CodeBlock` with a copy button.
- **`@xyflow/react`** — DAG graph in the inspector panel, required by
  DESIGN_CONTRACT (live graph from `/api/dag/status`).

Everything else is CSS + existing deps (no GSAP/framer-motion per contract).

## Honest-behavior notes

- **Typewriter reveal is a client-side animation, not live tokens.** The
  backend returns the full reply at once (`message` SSE event carries it);
  `ChatView` reveals it ~15ms/char. True token streaming is a v2 backend item.
- **No fake data anywhere.** Empty sessions/channels/DAG show empty states;
  errors show inline cards with retry — never invented rows.
- `POST /api/chat` turns are serialized server-side; a second concurrent turn
  gets `429 "agent busy"`, surfaced in the chat error card.
- `GET /api/sessions` returns `[]` until the loop exposes a listing API.
- `GET /api/dag/status` returns `{"nodes": []}` ("No DAG run yet") until the
  Brain records last-run state.
