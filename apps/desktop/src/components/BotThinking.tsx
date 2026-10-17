/**
 * BotThinking — playful "bot is working" indicator for the bubble chat.
 *
 * Shows the bot avatar doing a happy bounce (CSS), three dancing dots,
 * and a rotating Vietnamese status line that gets funnier the longer
 * the wait: "Để em nghĩ chút..." → "Đang lục tung dữ liệu..." → ...
 *
 * Backend phases (when the SSE stream reports them) map to their own
 * lines; otherwise the elapsed-time rotation keeps things lively.
 * Monochrome, respects prefers-reduced-motion (see grokchat.css).
 */

import { useEffect, useState } from "react";
import { BoomiAvatar } from "./BoomiAvatar";
import { BotAvatar } from "./BotAvatar";

/** Backend phase → playful Vietnamese line. */
const PHASE_LINES: Record<string, string[]> = {
  thinking: [
    "Để em nghĩ chút...",
    "Đang suy nghĩ sâu sắc...",
    "Hmm, câu này hay đấy...",
  ],
  reasoning: [
    "Đang suy luận logic...",
    "Nối các ý lại với nhau...",
  ],
  tool_call: [
    "Đang dùng công cụ...",
    "Để em tra cứu giúp anh...",
  ],
  searching: [
    "Đang lục tung dữ liệu...",
    "Tìm kiếm trong biển thông tin...",
  ],
  writing: [
    "Đang viết câu trả lời...",
    "Sắp xong rồi...",
  ],
};

/** Fallback rotation when the backend only says "thinking" (or nothing). */
const FALLBACK_LINES = [
  "Để em nghĩ chút...",
  "Đang lục tung dữ liệu...",
  "Sắp xong rồi, chờ em tí...",
  "Hơi lâu chút... em đang cố hết sức!",
];

const ROTATE_MS = 4500;

interface BotThinkingProps {
  /** Backend phase string (e.g. "thinking"); defaults to elapsed rotation. */
  phase?: string | null;
  botId?: string | null;
  botName?: string | null;
  botEmoji?: string | null;
}

export function BotThinking({ phase, botId, botName, botEmoji }: BotThinkingProps) {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return; // static first line, no rotation
    }
    const timer = window.setInterval(() => setTick((t) => t + 1), ROTATE_MS);
    return () => window.clearInterval(timer);
  }, []);

  const pool =
    (phase && PHASE_LINES[phase]) ||
    (phase && phase !== "thinking" ? [`Đang ${phase}...`] : null) ||
    FALLBACK_LINES;
  const line = pool[tick % pool.length];

  return (
    <div className="gc-row gc-row-bot">
      <div className="gc-avatar gc-avatar-thinking" aria-hidden="true">
        {botId ? (
          <BotAvatar
            botId={botId}
            name={botName ?? "Bot"}
            emoji={botEmoji ?? undefined}
            size={34}
            status="thinking"
          />
        ) : (
          <BoomiAvatar size={34} status="thinking" alt="Boomi đang suy nghĩ" />
        )}
      </div>
      <div className="gc-main">
        <div className="gc-thinking-bubble" role="status" aria-live="polite">
          <span className="gc-dots" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="gc-thinking-label" key={line}>
            {line}
          </span>
        </div>
      </div>
    </div>
  );
}
