import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../lib/api";

interface TerminalLogProps {
  /** Dòng log từ props (dùng khi chưa có /api/logs). */
  logs?: string[];
  /** Chu kỳ poll /api/logs (ms). Đặt 0 hoặc âm để tắt poll. Mặc định 2000. */
  pollIntervalMs?: number;
  /** Giữ tối đa bao nhiêu dòng để DOM không phình. Mặc định 1000. */
  maxLines?: number;
}

const DEFAULT_POLL_MS = 2_000;
const DEFAULT_MAX_LINES = 1_000;

/** Rút gọn body /api/logs về string[]: chấp nhận {lines: []} hoặc mảng trần. */
function toLines(body: unknown): string[] {
  const arr = Array.isArray(body) ? body : (body as { lines?: unknown }).lines;
  if (!Array.isArray(arr)) return [];
  return arr.filter((l): l is string => typeof l === "string");
}

/**
 * Terminal log: khung đen, font mono, tự cuộn xuống cuối.
 * Poll /api/logs theo pollIntervalMs; luôn render thêm logs từ props.
 */
export default function TerminalLog({ logs = [], pollIntervalMs = DEFAULT_POLL_MS, maxLines = DEFAULT_MAX_LINES }: TerminalLogProps) {
  const [polled, setPolled] = useState<string[]>([]);
  const boxRef = useRef<HTMLDivElement>(null);

  // Poll /api/logs; endpoint chưa có thì im lặng giữ nguyên.
  useEffect(() => {
    if (pollIntervalMs <= 0) return;
    let cancelled = false;
    const seen = new Set<string>();
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/logs`);
        if (!res.ok || cancelled) return;
        const fresh = toLines(await res.json()).filter((l) => !seen.has(l));
        if (fresh.length === 0) return;
        fresh.forEach((l) => seen.add(l));
        setPolled((prev) => [...prev, ...fresh].slice(-maxLines));
      } catch { /* API chưa có — component vẫn render logs từ props */ }
    };
    void load();
    const id = setInterval(() => void load(), pollIntervalMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [pollIntervalMs, maxLines]);

  const lines = [...logs, ...polled].slice(-maxLines);

  // Auto-scroll xuống dòng mới nhất.
  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  return (
    <div
      ref={boxRef}
      className="h-80 overflow-y-auto rounded-lg border border-neutral-800 bg-black p-3 font-mono text-xs leading-5"
      aria-label="Terminal log"
    >
      {lines.length === 0 ? (
        <span className="text-neutral-600">// chưa có log — chờ /api/logs hoặc truyền props logs</span>
      ) : (
        lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-words text-neutral-300">
            <span className="mr-2 select-none text-neutral-700">{String(i + 1).padStart(4, " ")}</span>
            {line}
          </div>
        ))
      )}
    </div>
  );
}
