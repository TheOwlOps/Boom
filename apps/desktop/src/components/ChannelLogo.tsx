/**
 * ChannelLogo — official brand logos for chat channels.
 *
 * SVG paths for Telegram / Discord / Messenger / Slack / WhatsApp / LINE
 * sourced from Simple Icons (CC0). Zalo / Zalo Personal / Feishu have no
 * Simple Icons entry, so they are simplified originals drawn for Boom
 * (blue rounded square + wordmark / geometric mark) — no trademarked
 * artwork copied.
 *
 * Rendered in brand colors by default; pass `colored={false}` for a
 * monochrome currentColor fallback that adapts to light/dark theme.
 */

import { useId } from "react";
import type { ReactNode } from "react";

interface ChannelLogoProps {
  channelId: string;
  className?: string;
  /** Use official brand colors (default true). False = currentColor monochrome. */
  colored?: boolean;
  size?: number;
}

/* ------------------------------------------------------------------ */
/* Official Simple Icons (CC0) paths                                   */
/* ------------------------------------------------------------------ */

const TELEGRAM_PATH =
  "M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z";

const DISCORD_PATH =
  "M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z";

const MESSENGER_PATH =
  "M.001 11.639C.001 4.949 5.241 0 12.001 0S24 4.95 24 11.639c0 6.689-5.24 11.638-12 11.638-1.21 0-2.38-.16-3.47-.46a.96.96 0 00-.64.05l-2.39 1.05a.96.96 0 01-1.35-.85l-.07-2.14a.97.97 0 00-.32-.68A11.39 11.389 0 01.002 11.64zm8.32-2.19l-3.52 5.6c-.35.53.32 1.139.82.75l3.79-2.87c.26-.2.6-.2.87 0l2.8 2.1c.84.63 2.04.4 2.6-.48l3.52-5.6c.35-.53-.32-1.13-.82-.75l-3.79 2.87c-.25.2-.6.2-.86 0l-2.8-2.1a1.8 1.8 0 00-2.61.48z";

/* Slack: 4 sub-path groups — blue (left), red (top), green (right), yellow (bottom). */
const SLACK_BLUE =
  "M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z";
const SLACK_RED =
  "M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z";
const SLACK_GREEN =
  "M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z";
const SLACK_YELLOW =
  "M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z";

const WHATSAPP_PATH =
  "M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z";

const LINE_PATH =
  "M19.365 9.863c.349 0 .63.285.63.631 0 .345-.281.63-.63.63H17.61v1.125h1.755c.349 0 .63.283.63.63 0 .344-.281.629-.63.629h-2.386c-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.63-.63h2.386c.346 0 .627.285.627.63 0 .349-.281.63-.63.63H17.61v1.125h1.755zm-3.855 3.016c0 .27-.174.51-.432.596-.064.021-.133.031-.199.031-.211 0-.391-.09-.51-.25l-2.443-3.317v2.94c0 .344-.279.629-.631.629-.346 0-.626-.285-.626-.629V8.108c0-.27.173-.51.43-.595.06-.023.136-.033.194-.033.195 0 .375.104.495.254l2.462 3.33V8.108c0-.345.282-.63.63-.63.345 0 .63.285.63.63v4.771zm-5.741 0c0 .344-.282.629-.631.629-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.63-.63.346 0 .628.285.628.63v4.771zm-2.466.629H4.917c-.345 0-.63-.285-.63-.629V8.108c0-.345.285-.63.63-.63.348 0 .63.285.63.63v4.141h1.756c.348 0 .629.283.629.63 0 .344-.282.629-.629.629M24 10.314C24 4.943 18.615.572 12 .572S0 4.943 0 10.314c0 4.811 4.27 8.842 10.035 9.608.391.082.923.258 1.058.59.12.301.079.766.038 1.08l-.164 1.02c-.045.301-.24 1.186 1.049.645 1.291-.539 6.916-4.078 9.436-6.975C23.176 14.393 24 12.458 24 10.314";

/* ------------------------------------------------------------------ */
/* Brand colors                                                        */
/* ------------------------------------------------------------------ */

const BRAND: Record<string, string> = {
  telegram: "#229ED9",
  discord: "#5865F2",
  whatsapp: "#25D366",
  line: "#06C755",
  zalo: "#0068FF",
  "zalo-personal": "#0068FF",
  feishu: "#3370FF",
};

const SLACK_COLORS = ["#36C5F0", "#E01E5A", "#2EB67D", "#ECB22E"];
const SLACK_PATHS = [SLACK_BLUE, SLACK_RED, SLACK_GREEN, SLACK_YELLOW];

/* Messenger official gradient: blue -> purple -> pink/red */
const MESSENGER_STOPS = ["#0099FF", "#A033FF", "#FF5280"];

/* ------------------------------------------------------------------ */
/* Simplified originals (no Simple Icons entry)                        */
/* ------------------------------------------------------------------ */

/** Zalo: blue rounded square + white "Zalo" wordmark. */
function ZaloMark({ colored }: { colored: boolean }): ReactNode {
  return (
    <>
      <rect
        x="1.5"
        y="1.5"
        width="21"
        height="21"
        rx="5"
        fill={colored ? BRAND.zalo : "none"}
        stroke={colored ? "none" : "currentColor"}
        strokeWidth={colored ? 0 : 1.6}
      />
      <text
        x="12"
        y="15.6"
        textAnchor="middle"
        fontSize="7"
        fontWeight={800}
        fontFamily="system-ui, -apple-system, sans-serif"
        fill={colored ? "#ffffff" : "currentColor"}
        letterSpacing="-0.2"
      >
        Zalo
      </text>
    </>
  );
}

/** Zalo Personal: Zalo mark + small person badge at bottom-right. */
function ZaloPersonalMark({ colored }: { colored: boolean }): ReactNode {
  const blue = colored ? BRAND["zalo-personal"] : "currentColor";
  return (
    <>
      <rect x="1.5" y="1.5" width="21" height="21" rx="5" fill={blue} />
      <text
        x="11"
        y="15.4"
        textAnchor="middle"
        fontSize="6.4"
        fontWeight={800}
        fontFamily="system-ui, -apple-system, sans-serif"
        fill="#ffffff"
        letterSpacing="-0.2"
      >
        Zalo
      </text>
      {/* person badge */}
      <circle cx="18.6" cy="18.4" r="4.4" fill="#ffffff" />
      <circle cx="18.6" cy="17.2" r="1.5" fill={blue} />
      <path
        d="M15.9 21.6c.3-1.7 1.4-2.6 2.7-2.6s2.4.9 2.7 2.6"
        fill="none"
        stroke={blue}
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </>
  );
}

/** Feishu: simplified geometric paper-plane mark in Feishu blue. */
function FeishuMark({ colored }: { colored: boolean }): ReactNode {
  const fill = colored ? BRAND.feishu : "currentColor";
  return (
    <path
      d="M3.2 11.8 20.8 3.6l-6.9 17.2-3.1-7.3-7.6-1.7zm7.9 1.6l2 4.7 4.4-11-9.4 4.5 3 1.8z"
      fill={fill}
    />
  );
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export default function ChannelLogo({
  channelId,
  className,
  colored = true,
  size = 18,
}: ChannelLogoProps) {
  const id = channelId.toLowerCase();
  const gradId = useId();
  const mono = !colored;

  let body: ReactNode = null;

  switch (id) {
    case "telegram":
      body = <path d={TELEGRAM_PATH} fill={mono ? "currentColor" : BRAND.telegram} />;
      break;
    case "discord":
      body = <path d={DISCORD_PATH} fill={mono ? "currentColor" : BRAND.discord} />;
      break;
    case "messenger":
      body = mono ? (
        <path d={MESSENGER_PATH} fill="currentColor" />
      ) : (
        <>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
              {MESSENGER_STOPS.map((c, i) => (
                <stop key={c} offset={i / (MESSENGER_STOPS.length - 1)} stopColor={c} />
              ))}
            </linearGradient>
          </defs>
          <path d={MESSENGER_PATH} fill={`url(#${gradId})`} />
        </>
      );
      break;
    case "slack":
      body = (
        <>
          {SLACK_PATHS.map((d, i) => (
            <path key={i} d={d} fill={mono ? "currentColor" : SLACK_COLORS[i]} />
          ))}
        </>
      );
      break;
    case "whatsapp":
      body = <path d={WHATSAPP_PATH} fill={mono ? "currentColor" : BRAND.whatsapp} />;
      break;
    case "line":
      body = <path d={LINE_PATH} fill={mono ? "currentColor" : BRAND.line} />;
      break;
    case "zalo":
      body = <ZaloMark colored={colored} />;
      break;
    case "zalo-personal":
      body = <ZaloPersonalMark colored={colored} />;
      break;
    case "feishu":
      body = <FeishuMark colored={colored} />;
      break;
    default:
      return null;
  }

  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={className}
      style={{ width: size, height: size, flexShrink: 0 }}
    >
      {body}
    </svg>
  );
}

/** Channel display names for accessibility. */
export const CHANNEL_LABELS: Record<string, string> = {
  telegram: "Telegram",
  discord: "Discord",
  messenger: "Messenger",
  slack: "Slack",
  whatsapp: "WhatsApp",
  line: "LINE",
  zalo: "Zalo",
  "zalo-personal": "Zalo Personal",
  feishu: "Feishu",
};
