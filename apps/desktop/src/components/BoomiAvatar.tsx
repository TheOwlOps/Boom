// BoomiAvatar.tsx — the Boomi owl mascot as a reusable avatar.
// The webp lives in `apps/desktop/public/boomi-mascot.webp`; Tauri/Vite
// serves `public/` at the root, hence the absolute src.
import "./BoomiAvatar.css";

export type BoomiStatus = "idle" | "thinking" | "error" | "success";

interface BoomiAvatarProps {
  /** Pixel size of the (square) avatar. Defaults to 32. */
  size?: number;
  /** Visual status variant; defaults to "idle". */
  status?: BoomiStatus;
  alt?: string;
  className?: string;
}

/** Public-dir asset path — keep in sync with apps/desktop/public/. */
export const BOOMI_MASCOT_SRC = "/boomi-mascot.webp";

export function BoomiAvatar({
  size = 32,
  status = "idle",
  alt = "Boomi",
  className = "",
}: BoomiAvatarProps) {
  const cls = `boomi-avatar boomi-status-${status}${className ? ` ${className}` : ""}`;
  return (
    <img
      src={BOOMI_MASCOT_SRC}
      alt={alt}
      width={size}
      height={size}
      draggable={false}
      className={cls}
      style={{ width: size, height: size }}
    />
  );
}
