/**
 * BotAvatar — per-bot blob avatar, Grok-style.
 *
 * Design: a soft rounded blob (circle) with two tilted oval eyes, drawn as
 * inline SVG — no image assets, no dependencies. Each bot is deterministically
 * assigned one of 8 variants (color + signature motion) from its id hash:
 *
 *   ghost  white blob, black eyes    — idle: breathe (scale pulse)
 *   ink    black blob, white eyes    — idle: rock (rotate ±4°)
 *   slate  gray blob, dark eyes      — idle: wiggle (quick small rotate)
 *   peach  pastel peach blob        — idle: jelly (squish scaleX/scaleY)
 *   mint   pastel mint blob         — idle: bob (translateY float)
 *   sky    pastel blue blob         — idle: sway (translateX drift)
 *   lav    pastel purple blob       — idle: puff (deep breathe)
 *   lemon  pastel yellow blob       — idle: nod (tilt + dip)
 *
 * Status layers on top of the variant motion:
 *   online:   variant motion + soft tinted glow
 *   thinking: body rocks + eyes dart side to side
 *   offline:  desaturated, no motion
 *   idle:     variant motion, no glow
 *
 * Interaction: hovering the avatar makes it hop, widens the eyes, and the
 * eyes follow the cursor within a few px. All motion is transform/opacity
 * only and disabled under `prefers-reduced-motion`.
 *
 * Props interface is unchanged from the previous (gradient tile) version,
 * so all existing call sites keep working.
 */
import { useRef } from "react";
import "./BotAvatar.css";

export type BotAvatarStatus = "online" | "offline" | "thinking" | "idle";

/** The 8 blob variants, in hash order. */
export const BOT_AVATAR_VARIANTS = [
  "ghost",
  "ink",
  "slate",
  "peach",
  "mint",
  "sky",
  "lav",
  "lemon",
] as const;

export type BotAvatarVariant = (typeof BOT_AVATAR_VARIANTS)[number];

interface BotAvatarProps {
  /** Stable bot id — hashed into a deterministic variant. */
  botId: string;
  /** Bot display name — aria-label. */
  name: string;
  /** Bot emoji (bot.avatar_emoji). Kept for API compat; the blob design
   *  does not render emoji — identity comes from the variant. */
  emoji?: string;
  /** Pixel size of the avatar. Defaults to 36. */
  size?: number;
  /** Motion/glow variant. Defaults to "idle". */
  status?: BotAvatarStatus;
  /** When false, render statically (no CSS animation). Defaults to true. */
  animated?: boolean;
  className?: string;
}

/** djb2 hash → hue 0..359. Deterministic per bot id. Kept for compat. */
export function botHue(botId: string): number {
  let h = 5381;
  for (let i = 0; i < botId.length; i++) {
    h = ((h << 5) + h + botId.charCodeAt(i)) | 0;
  }
  return Math.abs(h) % 360;
}

/** Deterministic blob variant for a bot id. */
export function botVariant(botId: string): BotAvatarVariant {
  return BOT_AVATAR_VARIANTS[botHue(botId) % BOT_AVATAR_VARIANTS.length];
}

/** Accent stops for a bot id. Kept for compat (unused by the blob design). */
export function botAccent(botId: string): { from: string; to: string; glow: string } {
  const hue = botHue(botId);
  return {
    from: `hsl(${hue}, 78%, 62%)`,
    to: `hsl(${(hue + 28) % 360}, 72%, 44%)`,
    glow: `hsla(${hue}, 85%, 60%, 0.45)`,
  };
}

/** Max px the eyes travel toward the cursor. */
const EYE_TRAVEL = 5;

export function BotAvatar({
  botId,
  name,
  emoji: _emoji,
  size = 36,
  status = "idle",
  animated = true,
  className = "",
}: BotAvatarProps) {
  void _emoji;
  const variant = botVariant(botId);
  const ref = useRef<HTMLSpanElement>(null);

  const motion =
    !animated || status === "offline" ? "static" : status === "thinking" ? "thinking" : variant;

  const cls = [
    "blob-avatar",
    `blob-avatar--${variant}`,
    `blob-avatar--${motion}`,
    status === "offline" ? "blob-avatar--offline" : "",
    status === "online" ? "blob-avatar--glow" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  /** Eyes follow the cursor within EYE_TRAVEL px of center. */
  const trackCursor = (e: React.MouseEvent) => {
    const el = ref.current;
    if (!el || !animated || status === "offline") return;
    const r = el.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2);
    const dy = e.clientY - (r.top + r.height / 2);
    const dist = Math.hypot(dx, dy) || 1;
    const mag = Math.min(EYE_TRAVEL, dist / 6);
    el.style.setProperty("--ex", `${((dx / dist) * mag).toFixed(1)}px`);
    el.style.setProperty("--ey", `${((dy / dist) * mag).toFixed(1)}px`);
  };

  const resetEyes = () => {
    const el = ref.current;
    if (!el) return;
    el.style.removeProperty("--ex");
    el.style.removeProperty("--ey");
  };

  return (
    <span
      ref={ref}
      className={cls}
      style={{ width: size, height: size }}
      role="img"
      aria-label={`${name} avatar`}
      onMouseMove={trackCursor}
      onMouseLeave={resetEyes}
    >
      <span className="blob-avatar-body" aria-hidden="true">
        <svg className="blob-avatar-face" viewBox="0 0 64 64" aria-hidden="true">
          <g className="blob-eyes-follow">
            <g className="blob-eyes-dart">
              <ellipse className="blob-eye blob-eye--left" cx="24" cy="27" rx="6.5" ry="9.5" />
              <ellipse className="blob-eye blob-eye--right" cx="41" cy="26" rx="6.5" ry="9.5" />
            </g>
          </g>
        </svg>
      </span>
    </span>
  );
}
