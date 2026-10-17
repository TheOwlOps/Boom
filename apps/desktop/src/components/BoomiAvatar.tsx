// BoomiAvatar.tsx — the Boomi owl mascot as a reusable avatar.
// Static webp + animated mp4s live in `apps/desktop/public/`; Tauri/Vite
// serves `public/` at the root, hence the absolute srcs.
import { useState } from "react";
import "./BoomiAvatar.css";

export type BoomiStatus = "idle" | "thinking" | "error" | "success" | "happy" | "talking" | "waving" | "sleeping";

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
/** Thinking animation variants — cycled randomly for variety. */
const BOOMI_THINKING_VIDEOS = [
  "/boomi-thinking.mp4",
  "/boomi-thinking-deep.mp4",
  "/boomi-thinking-curious.mp4",
  "/boomi-thinking-compute.mp4",
  "/boomi-thinking-eureka.mp4",
];
const BOOMI_VIDEOS: Record<string, string> = {
  idle: "/boomi-idle.mp4",
  happy: "/boomi-happy.mp4",
  talking: "/boomi-talking.mp4",
  waving: "/boomi-waving.mp4",
  sleeping: "/boomi-sleeping.mp4",
  // success maps to happy animation
  success: "/boomi-happy.mp4",
};

export function BoomiAvatar({
  size = 32,
  status = "idle",
  animated = true,
  alt = "Boomi",
  className = "",
}: BoomiAvatarProps) {
  const cls = `boomi-avatar boomi-status-${status}${className ? ` ${className}` : ""}`;
  const style = { width: size, height: size };

  // Monogram fallback when the asset (video or image) fails to load —
  // renders a monochrome disc with the first initial instead of a broken icon.
  const [failed, setFailed] = useState(false);
  if (failed) {
    const initial = (alt.trim().charAt(0) || "B").toUpperCase();
    return (
      <div
        className={`avatar-fallback${className ? ` ${className}` : ""}`}
        style={{ ...style, fontSize: Math.max(12, Math.round(size * 0.45)) }}
        aria-label={alt}
        role="img"
      >
        {initial}
      </div>
    );
  }

  // Animated video for lively states (like Muse's live avatar).
  // Thinking cycles randomly through 5 variants for variety.
  // Falls back to static image for error or when animated=false.
  let videoSrc: string | null = null;
  if (animated) {
    if (status === "thinking") {
      const idx = Math.floor(Math.random() * BOOMI_THINKING_VIDEOS.length);
      videoSrc = BOOMI_THINKING_VIDEOS[idx];
    } else {
      videoSrc = BOOMI_VIDEOS[status] ?? null;
    }
  }

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
        onError={() => setFailed(true)}
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
      onError={() => setFailed(true)}
    />
  );
}
