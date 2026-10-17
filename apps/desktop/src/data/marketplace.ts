/**
 * Marketplace catalog for the Boom Marketplace modal.
 *
 * Data sources:
 * - plugin items: real manifests from `plugins/` (plugin.json / plugin.yaml)
 *   and plugin READMEs in the Boom repo.
 * - tool items: real entries from TOOL_CATALOG (src/data/toolCatalog.ts).
 * - bot items: channel integrations served by boom-core (Rust).
 */

export interface MarketplaceItem {
  id: string;
  name: string;
  icon: string; // emoji
  description: string; // 1-2 lines, English
  category: string;
  featured?: boolean;
  kind: "plugin" | "tool" | "bot";
}

export interface BotSummary {
  id: string;
  name: string;
  icon: string;
  description: string;
}

export const MARKETPLACE_CATEGORIES: string[] = [
  "Productivity",
  "Developer Tools",
  "Communication",
  "AI & Data",
  "Business",
];

export const MARKETPLACE_ITEMS: MarketplaceItem[] = [
  // ------------------------- Productivity -------------------------
  {
    id: "gmail",
    name: "Gmail",
    icon: "📧",
    description: "Search, read, draft, and manage emails natively with OAuth2.",
    category: "Productivity",
    featured: true,
    kind: "tool",
  },
  {
    id: "calendar",
    name: "Calendar",
    icon: "📅",
    description: "Create, list, and delete events on your primary calendar.",
    category: "Productivity",
    featured: true,
    kind: "tool",
  },
  {
    id: "drive",
    name: "Google Drive",
    icon: "💾",
    description: "Search, list, upload, and download Google Drive files.",
    category: "Productivity",
    kind: "tool",
  },
  {
    id: "google-meet",
    name: "Google Meet",
    icon: "🎙️",
    description: "Join Meet calls, transcribe, speak, and handle follow-up work.",
    category: "Productivity",
    kind: "plugin",
  },
  {
    id: "scheduler",
    name: "Scheduler",
    icon: "⏰",
    description: "Schedule prompts on cron expressions with delivery to channels.",
    category: "Productivity",
    kind: "tool",
  },
  {
    id: "goals",
    name: "Goals",
    icon: "🎯",
    description: "Track long-term goals with timestamped progress logs.",
    category: "Productivity",
    kind: "tool",
  },
  {
    id: "memory",
    name: "Long-term Memory",
    icon: "🧠",
    description: "Durable memory with full-text search, providers, and scope resolution.",
    category: "Productivity",
    featured: true,
    kind: "plugin",
  },
  {
    id: "digest",
    name: "Briefing Digest",
    icon: "📰",
    description: "Render proactive briefing markdown from assembled sections.",
    category: "Productivity",
    kind: "tool",
  },

  // ------------------------- Developer Tools -------------------------
  {
    id: "github",
    name: "GitHub",
    icon: "🐙",
    description: "Read issues and PRs, search code, and fetch repo files.",
    category: "Developer Tools",
    featured: true,
    kind: "tool",
  },
  {
    id: "code-review",
    name: "Code Review",
    icon: "🔍",
    description: "Static review: complexity, dead code, TODOs, Markdown report.",
    category: "Developer Tools",
    kind: "tool",
  },
  {
    id: "git-toolkit",
    name: "Git Toolkit",
    icon: "🌿",
    description: "Status, diff, log, and safe commits with secret scanning.",
    category: "Developer Tools",
    kind: "tool",
  },
  {
    id: "terminal",
    name: "Terminal",
    icon: "💻",
    description: "Run shell commands with timeouts and background job tracking.",
    category: "Developer Tools",
    kind: "tool",
  },
  {
    id: "security-guidance",
    name: "Security Guidance",
    icon: "🛡️",
    description:
      "Appends security warnings to file writes on dangerous patterns (Apache-2.0).",
    category: "Developer Tools",
    kind: "plugin",
  },
  {
    id: "secret-scan",
    name: "Secret Scanner",
    icon: "🔑",
    description: "Scan files and directories for leaked API keys and tokens.",
    category: "Developer Tools",
    kind: "tool",
  },
  {
    id: "disk-cleanup",
    name: "Disk Cleanup",
    icon: "🧹",
    description: "Auto-track and clean ephemeral files via plugin hooks.",
    category: "Developer Tools",
    kind: "plugin",
  },
  {
    id: "kanban",
    name: "Kanban",
    icon: "📋",
    description: "Visual task board with dashboard and systemd integration.",
    category: "Developer Tools",
    kind: "plugin",
  },

  // ------------------------- Communication -------------------------
  {
    id: "telegram",
    name: "Telegram",
    icon: "✈️",
    description: "Chat with Boom on Telegram; approve actions via buttons.",
    category: "Communication",
    featured: true,
    kind: "bot",
  },
  {
    id: "discord",
    name: "Discord",
    icon: "🎮",
    description: "Chat with Boom on Discord servers and channels.",
    category: "Communication",
    kind: "bot",
  },
  {
    id: "slack",
    name: "Slack",
    icon: "💬",
    description: "Chat with Boom inside Slack workspaces.",
    category: "Communication",
    kind: "bot",
  },
  {
    id: "whatsapp",
    name: "WhatsApp",
    icon: "📱",
    description: "Chat with Boom on WhatsApp.",
    category: "Communication",
    kind: "bot",
  },
  {
    id: "teams-pipeline",
    name: "Teams Pipeline",
    icon: "👥",
    description: "Transcript-first meeting summaries with durable runtime state.",
    category: "Communication",
    kind: "plugin",
  },
  {
    id: "broadcast",
    name: "Broadcast",
    icon: "📢",
    description: "Send one message to many channel targets, rate-limit friendly.",
    category: "Communication",
    kind: "tool",
  },

  // ------------------------- AI & Data -------------------------
  {
    id: "web-search",
    name: "Web Search",
    icon: "🔎",
    description: "Search the web with deep, cross-verified research and extraction.",
    category: "AI & Data",
    featured: true,
    kind: "tool",
  },
  {
    id: "web-providers",
    name: "Web Providers",
    icon: "🌐",
    description: "Bundled search providers: Brave, DDG, Exa, Firecrawl, and more.",
    category: "AI & Data",
    kind: "plugin",
  },
  {
    id: "ocr",
    name: "OCR",
    icon: "👁️",
    description: "Extract text from images with local Tesseract OCR.",
    category: "AI & Data",
    kind: "tool",
  },
  {
    id: "image-gen",
    name: "Image Generation",
    icon: "🎨",
    description: "Generate images via OpenAI, Fal, DeepInfra, and more providers.",
    category: "AI & Data",
    featured: true,
    kind: "plugin",
  },
  {
    id: "video-gen",
    name: "Video Generation",
    icon: "🎬",
    description: "Generate video via Fal, DeepInfra, and xAI providers.",
    category: "AI & Data",
    kind: "plugin",
  },
  {
    id: "browser",
    name: "Browser Automation",
    icon: "🌍",
    description: "Headless browsing: navigate, click, fill, and screenshot.",
    category: "AI & Data",
    featured: true,
    kind: "plugin",
  },
  {
    id: "news",
    name: "News Headlines",
    icon: "🗞️",
    description: "Latest headlines and keyword search from VnExpress RSS.",
    category: "AI & Data",
    kind: "tool",
  },
  {
    id: "deep-think",
    name: "Deep Think",
    icon: "🧩",
    description: "Multi-angle analysis with source reliability scoring.",
    category: "AI & Data",
    kind: "tool",
  },

  // ------------------------- Business -------------------------
  {
    id: "shop-tracker",
    name: "Shop Tracker",
    icon: "🛒",
    description: "Search products and prices on Shopee, Tiki, and Lazada.",
    category: "Business",
    kind: "tool",
  },
  {
    id: "captcha",
    name: "CAPTCHA Solver",
    icon: "🔲",
    description: "Solve image CAPTCHAs via the 2captcha service.",
    category: "Business",
    kind: "tool",
  },
  {
    id: "audit",
    name: "Audit Log",
    icon: "📜",
    description: "Search the append-only audit trail of who did what, when.",
    category: "Business",
    kind: "tool",
  },
  {
    id: "access-control",
    name: "Access Control",
    icon: "🔐",
    description: "Role-based access control with custom roles and checks.",
    category: "Business",
    kind: "tool",
  },
  {
    id: "triggers",
    name: "Event Triggers",
    icon: "⚡",
    description: "Gmail, price, news, and webhook triggers with cooldown.",
    category: "Business",
    kind: "tool",
  },
];

/** Items flagged as featured (at least 6). */
export function getFeaturedItems(): MarketplaceItem[] {
  return MARKETPLACE_ITEMS.filter((item) => item.featured === true);
}

/** Items in one category. */
export function getItemsByCategory(cat: string): MarketplaceItem[] {
  return MARKETPLACE_ITEMS.filter((item) => item.category === cat);
}
