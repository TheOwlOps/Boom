// BoomiAvatar.tsx — the Boomi owl mascot as a reusable avatar.
// Static webp + animated mp4s live in `apps/desktop/public/`; Tauri/Vite
// serves `public/` at the root, hence the absolute srcs.
import "./BoomiAvatar.css";

export type BoomiStatus = "idle" | "thinking" | "error" | "success";

interface BoomiAvatarProps {
  /** Pixel size of the (square) avatar. Defaults to 32. */
  size?: number;
  /** Visual status variant; defaults to "idle". */
  status?: BoomiStatus;
  /** When true, use animated video instead of static image. Defaults to true for idle/thinking. */
  animated?: boolean;
  alt?: string;
  className?: string;
}

/** Public-dir asset paths — keep in sync with apps/desktop/public/. */
export const BOOMI_MASCOT_SRC = "/boomi-mascot.webp";
const BOOMI_IDLE_VIDEO = "/boomi-idle.mp4";
const BOOMI_THINKING_VIDEO = "/boomi-thinking.mp4";

export function BoomiAvatar({
  size = 32,
  status = "idle",
  animated = true,
  alt = "Boomi",
  className = "",
}: BoomiAvatarProps) {
  const cls = `boomi-avatar boomi-status-${status}${className ? ` ${className}` : ""}`;
  const style = { width: size, height: size };

  // Animated video for idle/thinking states (like Muse's live avatar).
  // Falls back to static image for error/success or when animated=false.
  const videoSrc =
    status === "thinking" ? BOOMI_THINKING_VIDEO : status === "idle" ? BOOMI_IDLE_VIDEO : null;

  if (animated && videoSrc) {
    return (
      <video
        src={videoSrc}
        autoPlay
        loop
        muted
        playsInline
        width={size}
        height={size}
        className={cls}
        style={{ ...style, objectFit: "cover", borderRadius: "50%" }}
        aria-label={alt}
      />
    );
  }

  return (
    <img
      src={BOOMI_MASCOT_SRC}
      alt={alt}
      width={size}
      height={size}
      draggable={false}
      className={cls}
      style={style}
    />
  );
}
