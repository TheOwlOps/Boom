/**
 * Marketplace — browse and install plugins / bots from a curated catalog.
 *
 * Full-screen overlay modal:
 *   - backdrop blur, closes on ESC, backdrop click, or the X button
 *   - header: "Marketplace" title + Plugins|Bots segmented tabs + close button
 *   - live search (name + description), category filter chips
 *   - Featured section first (only when no filter/search active), then one
 *     section per category with a "View all" shortcut
 *   - cards: emoji icon, bold name, 2-line description, Add/Added button
 *   - Bots tab: same layout, "Use" buttons instead of Add
 *
 * Drop-in for `apps/web/src/components/`. Fully controlled: the parent owns
 * `open`, `items`, `categories`, `bots` and `installedIds`; this component
 * only manages transient UI state (tab, search, active chip).
 *
 * Types: `MarketplaceItem` is defined here (compatible with a future
 * `../data/marketplace` module shape) and re-exported for consumers.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search, X } from "lucide-react";

/* ------------------------------------------------------------------ types */

/** A single installable entry in the marketplace catalog. */
export interface MarketplaceItem {
  id: string;
  name: string;
  icon: string;
  description: string;
  category: string;
  /** Highlighted on the Featured shelf when true. */
  featured?: boolean;
  /** Extra metadata hooks (author, version, …) — intentionally loose but typed. */
  kind?: string;
}

/** Minimal summary of a bot entry, used by the Bots tab. */
export interface BotSummary {
  id: string;
  name: string;
  icon: string;
  description: string;
}

export interface MarketplaceProps {
  /** Controls visibility; modal renders nothing when false. */
  open: boolean;
  /** Called when the modal should close (X, ESC, backdrop click). */
  onClose: () => void;
  /** Called when the user clicks Add on a plugin card. */
  onAdd: (item: MarketplaceItem) => void;
  /** Called when the user clicks Use on a bot card. Optional. */
  onUseBot?: (id: string) => void;
  /** Ids already installed/added — cards render as "Added ✓" (disabled). */
  installedIds: Set<string> | string[];
  /** Plugin catalog entries. */
  items: MarketplaceItem[];
  /** Dynamic categories rendered as filter chips (after All/Featured). */
  categories: string[];
  /** Bot entries for the Bots tab. */
  bots?: BotSummary[];
}

type Tab = "plugins" | "bots";

const CHIP_ALL = "All";
const CHIP_FEATURED = "Featured";

/** Normalize installedIds so both Set and array inputs work uniformly. */
function normalizeInstalled(ids: Set<string> | string[]): Set<string> {
  return ids instanceof Set ? ids : new Set(ids);
}

/* ------------------------------------------------------------------ card */

/** One plugin card: big emoji icon, name, 2-line description, Add/Added. */
function PluginCard(props: {
  item: MarketplaceItem;
  added: boolean;
  onAdd: (item: MarketplaceItem) => void;
}) {
  const { item, added, onAdd } = props;
  return (
    <div className="flex items-start gap-4 rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 transition-colors hover:border-neutral-700">
      <div className="text-4xl leading-none" aria-hidden="true">
        {item.icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="font-semibold text-neutral-100">{item.name}</div>
        <p className="mt-1 line-clamp-2 text-sm text-neutral-400">{item.description}</p>
      </div>
      <button
        type="button"
        disabled={added}
        onClick={() => onAdd(item)}
        className={
          added
            ? "shrink-0 rounded-lg bg-emerald-600/15 px-4 py-2 text-sm font-medium text-emerald-400"
            : "shrink-0 rounded-lg bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 transition-colors hover:bg-white"
        }
      >
        {added ? "Added ✓" : "Add"}
      </button>
    </div>
  );
}

/** One bot card: same layout, "Use" action instead of Add. */
function BotCard(props: {
  bot: BotSummary;
  onUse?: (id: string) => void;
}) {
  const { bot, onUse } = props;
  return (
    <div className="flex items-start gap-4 rounded-xl border border-neutral-800 bg-neutral-900/60 p-4 transition-colors hover:border-neutral-700">
      <div className="text-4xl leading-none" aria-hidden="true">
        {bot.icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="font-semibold text-neutral-100">{bot.name}</div>
        <p className="mt-1 line-clamp-2 text-sm text-neutral-400">{bot.description}</p>
      </div>
      <button
        type="button"
        onClick={() => onUse?.(bot.id)}
        className="shrink-0 rounded-lg bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 transition-colors hover:bg-white"
      >
        Use
      </button>
    </div>
  );
}

/* --------------------------------------------------------------- sections */

/** A titled section with an optional "View all" shortcut. */
function Section(props: {
  title: string;
  onViewAll?: () => void;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-neutral-300">
          {props.title}
        </h3>
        {props.onViewAll && (
          <button
            type="button"
            onClick={props.onViewAll}
            className="text-sm text-neutral-400 transition-colors hover:text-neutral-100"
          >
            View all
          </button>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">{props.children}</div>
    </section>
  );
}

/* -------------------------------------------------------------- component */

/**
 * Marketplace modal. Rendered (and state active) only while `open` is true.
 */
export function Marketplace(props: MarketplaceProps) {
  const { open, onClose, onAdd, onUseBot, installedIds, items, categories, bots } = props;

  const [tab, setTab] = useState<Tab>("plugins");
  const [query, setQuery] = useState("");
  const [chip, setChip] = useState<string>(CHIP_ALL);
  const dialogRef = useRef<HTMLDivElement>(null);

  /* --- ESC to close ----------------------------------------------------- */
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  /* --- body scroll lock while open -------------------------------------- */
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open ]);

  /* --- reset transient UI state each time the modal opens ---------------- */
  useEffect(() => {
    if (open) {
      setTab("plugins");
      setQuery("");
      setChip(CHIP_ALL);
    }
  }, [open]);

  /* --- backdrop click closes only when clicking the backdrop itself ------ */
  const onBackdropMouseDown = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose]
  );

  /* --- filtering --------------------------------------------------------- */
  const normalized = useMemo(() => query.trim().toLowerCase(), [query]);

  const matchesSearch = useCallback(
    (name: string, description: string): boolean =>
      normalized.length === 0 ||
      name.toLowerCase().includes(normalized) ||
      description.toLowerCase().includes(normalized),
    [normalized]
  );

  /** Items after applying the active chip + search filter. */
  const filteredPlugins = useMemo(() => {
    return items.filter((item) => {
      const inChip =
        chip === CHIP_ALL ||
        (chip === CHIP_FEATURED ? item.featured === true : item.category === chip);
      return inChip && matchesSearch(item.name, item.description);
    });
  }, [items, chip, matchesSearch]);

  const filteredBots = useMemo(() => {
    return (bots ?? []).filter((bot) => matchesSearch(bot.name, bot.description));
  }, [bots, matchesSearch]);

  /** Group filtered plugins by category, in the order of the categories prop. */
  const groupedPlugins = useMemo(() => {
    const order = new Map<string, number>();
    categories.forEach((c, i) => order.set(c, i));
    const groups = new Map<string, MarketplaceItem[]>();
    for (const item of filteredPlugins) {
      const list = groups.get(item.category);
      if (list) list.push(item);
      else groups.set(item.category, [item]);
    }
    return [...groups.entries()].sort((a, b) => {
      const oa = order.has(a[0]) ? (order.get(a[0]) as number) : Number.MAX_SAFE_INTEGER;
      const ob = order.has(b[0]) ? (order.get(b[0]) as number) : Number.MAX_SAFE_INTEGER;
      return oa - ob;
    });
  }, [filteredPlugins, categories]);

  /** Featured shelf shows only when nothing narrows the list. */
  const showFeatured =
    tab === "plugins" && chip === CHIP_ALL && normalized.length === 0;
  const featuredItems = useMemo(
    () => filteredPlugins.filter((item) => item.featured === true),
    [filteredPlugins]
  );

  const installed = useMemo(() => normalizeInstalled(installedIds), [installedIds]);
  const chips = useMemo(
    () => [CHIP_ALL, CHIP_FEATURED, ...categories],
    [categories]
  );

  if (!open) return null;

  const searchPlaceholder = tab === "plugins" ? "Search plugins" : "Search bots";

  return (
    <div
      className="marketplace-overlay fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onMouseDown={onBackdropMouseDown}
      role="presentation"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Marketplace"
        className="marketplace-dialog flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-neutral-800 bg-neutral-900/95 text-neutral-100 shadow-2xl"
      >
        {/* --- header --------------------------------------------------- */}
        <div className="flex items-center justify-between gap-4 border-b border-neutral-800 px-6 py-4">
          <h2 className="text-lg font-bold text-neutral-100">Marketplace</h2>
          <div className="flex items-center gap-3">
            {/* segmented tabs */}
            <div
              role="tablist"
              aria-label="Marketplace tabs"
              className="flex rounded-lg border border-neutral-800 bg-neutral-900 p-1"
            >
              {(["plugins", "bots"] as Tab[]).map((t) => (
                <button
                  key={t}
                  role="tab"
                  aria-selected={tab === t}
                  type="button"
                  onClick={() => setTab(t)}
                  className={
                    tab === t
                      ? "rounded-md bg-neutral-700 px-4 py-1.5 text-sm font-medium text-neutral-100"
                      : "rounded-md px-4 py-1.5 text-sm font-medium text-neutral-400 transition-colors hover:text-neutral-200"
                  }
                >
                  {t === "plugins" ? "Plugins" : "Bots"}
                </button>
              ))}
            </div>
            {/* close */}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close marketplace"
              className="rounded-lg p-2 text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
            >
              <X size={20} />
            </button>
          </div>
        </div>

        {/* --- search + chips ------------------------------------------- */}
        <div className="border-b border-neutral-800 px-6 py-4">
          <div className="relative">
            <Search
              size={18}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500"
            />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              className="w-full rounded-lg border border-neutral-800 bg-neutral-900 py-2.5 pl-10 pr-4 text-sm text-neutral-100 placeholder:text-neutral-500 focus:border-neutral-600 focus:outline-none"
            />
          </div>
          {tab === "plugins" && (
            <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label="Categories">
              {chips.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setChip(c)}
                  aria-pressed={chip === c}
                  className={
                    chip === c
                      ? "rounded-full bg-neutral-100 px-4 py-1.5 text-sm font-medium text-neutral-900"
                      : "rounded-full border border-neutral-800 bg-neutral-900 px-4 py-1.5 text-sm text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200"
                  }
                >
                  {c}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* --- content --------------------------------------------------- */}
        <div className="flex-1 space-y-8 overflow-y-auto px-6 py-6">
          {tab === "plugins" ? (
            <>
              {showFeatured && featuredItems.length > 0 && (
                <Section title="Featured">
                  {featuredItems.map((item) => (
                    <PluginCard
                      key={item.id}
                      item={item}
                      added={installed.has(item.id)}
                      onAdd={onAdd}
                    />
                  ))}
                </Section>
              )}
              {groupedPlugins.map(([category, list]) => (
                <Section
                  key={category}
                  title={category}
                  onViewAll={
                    chip === category ? undefined : () => setChip(category)
                  }
                >
                  {list.map((item) => (
                    <PluginCard
                      key={item.id}
                      item={item}
                      added={installed.has(item.id)}
                      onAdd={onAdd}
                    />
                  ))}
                </Section>
              ))}
              {filteredPlugins.length === 0 && (
                <p className="py-12 text-center text-sm text-neutral-500">
                  No plugins found. Try a different search or category.
                </p>
              )}
            </>
          ) : (
            <>
              {filteredBots.length > 0 ? (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  {filteredBots.map((bot) => (
                    <BotCard key={bot.id} bot={bot} onUse={onUseBot} />
                  ))}
                </div>
              ) : (
                <p className="py-12 text-center text-sm text-neutral-500">
                  No bots found. Try a different search.
                </p>
              )}
            </>
          )}
        </div>
      </div>

      {/* modal open animation: fade + scale */}
      <style>{`
        .marketplace-overlay { animation: marketplace-fade-in 160ms ease-out; }
        .marketplace-dialog { animation: marketplace-pop-in 200ms cubic-bezier(0.16, 1, 0.3, 1); }
        @keyframes marketplace-fade-in {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        @keyframes marketplace-pop-in {
          from { opacity: 0; transform: scale(0.96) translateY(8px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
        @media (prefers-reduced-motion: reduce) {
          .marketplace-overlay, .marketplace-dialog { animation: none; }
        }
      `}</style>
    </div>
  );
}

export default Marketplace;
