/**
 * BotAvatar — per-bot animated avatar, Grokbot-style.
 *
 * Unlike the Boomi mascot (the app brand), each bot gets its own identity:
 * a rounded-square tile with a deterministic accent color derived from the
 * bot id, the bot's emoji centered, and subtle CSS motion:
 *   - online:   gentle float + soft accent glow
 *   - thinking: pulsing glow (bot is working)
 *   - offline:  desaturated, no motion
 *   - idle:     static, no glow
 *
 * Accent colors are the ONE place color is allowed on top of the monochrome
 * theme — mirroring Grokbot, where bot icons are colorful but the UI is not.
 * All motion is pure CSS keyframes (GPU-cheap transforms/opacity only) and
 * disabled under `prefers-reduced-motion`.
 *
 * Fallback: no emoji → first letter of the bot name on the accent tile.
 */
import "./BotAvatar.css";

export type BotAvatarStatus = "online" | "offline" | "thinking" | "idle";

interface BotAvatarProps {
  /** Stable bot id — hashed into a deterministic accent hue. */
  botId: string;
  /** Bot display name — aria-label + fallback initial. */
  name: string;
  /** Bot emoji (bot.avatar_emoji). Falls back to name initial. */
  emoji?: string;
  /** Pixel size of the square tile. Defaults to 36. */
  size?: number;
  /** Motion/glow variant. Defaults to "idle". */
  status?: BotAvatarStatus;
  /** When false, render statically (no CSS animation). Defaults to true. */
  animated?: boolean;
  className?: string;
}

/** djb2 hash → hue 0..359. Deterministic per bot id. */
export function botHue(botId: string): number {
  let h = 5381;
  for (let i = 0; i < botId.length; i++) {
    h = ((h << 5) + h + botId.charCodeAt(i)) | 0;
  }
  return Math.abs(h) % 360;
}

/** Accent gradient stops for a bot id. Exported for reuse (e.g. presence dots). */
export function botAccent(botId: string): { from: string; to: string; glow: string } {
  const hue = botHue(botId);
  return {
    from: `hsl(${hue}, 78%, 62%)`,
    to: `hsl(${(hue + 28) % 360}, 72%, 44%)`,
    glow: `hsla(${hue}, 85%, 60%, 0.45)`,
  };
}

export function BotAvatar({
  botId,
  name,
  emoji,
  size = 36,
  status = "idle",
  animated = true,
  className = "",
}: BotAvatarProps) {
  const accent = botAccent(botId);
  const motion = animated && status !== "offline" && status !== "idle" ? status : "static";
  const cls = [
    "bot-avatar",
    `bot-avatar--${motion}`,
    status === "offline" ? "bot-avatar--offline" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  const style: React.CSSProperties = {
    width: size,
    height: size,
    fontSize: Math.max(12, Math.round(size * 0.52)),
    ["--bot-from" as string]: accent.from,
    ["--bot-to" as string]: accent.to,
    ["--bot-glow" as string]: accent.glow,
  };
  const glyph = (emoji ?? "").trim() || name.trim().charAt(0).toUpperCase() || "B";

  return (
    <span className={cls} style={style} role="img" aria-label={`${name} avatar`}>
      <span className="bot-avatar-glyph" aria-hidden="true">
        {glyph}
      </span>
    </span>
  );
}
