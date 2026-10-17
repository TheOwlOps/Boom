import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SendHorizonal, RotateCcw, Copy, Check, KeyRound, X } from "lucide-react";
import { listBots, postChat, setActiveBot, isProviderNotConfiguredError } from "../lib/api";
import type { Bot as BotInfo, ChatMessage, ChatRequest, Strategy } from "../lib/types";
import { formatSmartTime, formatFullTime } from "../lib/formatTime";
import { MessageBubble } from "./MessageParts";
import { BoomiAvatar } from "./BoomiAvatar";
import { BotAvatar } from "./BotAvatar";
import { BoomiEmptyState } from "./BoomiEmptyState";
import { BotThinking } from "./BotThinking";
import { Skeleton } from "./Skeleton";
import "../styles/effects.css";
import "../styles/teamchat.css"; // tc-header only; messages use grokchat.css
import "../styles/grokchat.css";

// CLIENT-SIDE ANIMATION (NOT live token streaming): the backend returns the
// full reply at once (see API_CONTRACT.md `POST /api/chat`), and this reveals
// it word-by-word as a typewriter visual effect. The newest word gets the
// `.typing-word` CSS class (fade/slide in, see ../styles/effects.css) and a
// blinking `.typing-caret` trails the end until the reveal finishes.
// True token streaming is a v2 backend item.
const TYPEWRITER_MS_PER_WORD = 60;
const NEAR_BOTTOM_PX = 80;
const MAX_INPUT_PX = 200; // composer auto-resize cap
const COPY_FEEDBACK_MS = 1500;

/**
 * Skeleton bubble widths for the history-loading state: alternating
 * bot/user rows with varied widths so the placeholder reads like a
 * real conversation.
 */
const HISTORY_SKELETON_WIDTHS = ["52%", "34%", "61%", "28%", "47%"];

/** Bot-switching surface exposed to parents (e.g. the sidebar) via onBotApi. */
export interface BotApi {
  bots: BotInfo[];
  activeBotId: string | null;
  switchBot: (id: string) => Promise<void>;
}

type Props = {
  sessionId: string | null;
  /**
   * Kept for API compatibility with older callers (App.tsx passes it).
   * The team-chat header no longer renders a panel toggle button; the prop
   * is accepted and ignored.
   */
  onTogglePanel(): void;
  /**
   * Opens the Settings view (defaults to the Providers section, which holds
   * the full provider configuration). Optional so older callers keep
   * compiling — when absent, the provider prompt still renders, just
   * without the shortcut button.
   */
  onOpenSettings?: () => void;
  /**
   * Optional hook for parents that own bot selection (e.g. a sidebar):
   * receives { bots, activeBotId, switchBot } whenever it changes, so the
   * parent can render its own picker and switch bots without the in-header
   * dropdown this component used to have.
   */
  onBotApi?: (api: BotApi) => void;
  /**
   * True while the session's message history is loading — renders
   * skeleton message bubbles instead of the empty state. Defaults to
   * false; no history fetch exists yet (see the sessionId effect).
   */
  loadingHistory?: boolean;
};

function uid(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Split text into word chunks, keeping trailing whitespace per chunk. */
function splitWords(text: string): string[] {
  return text.match(/\S+\s*/g) ?? [];
}

/**
 * Plain-text renderer used while an assistant message is "typing".
 * Markdown is applied only after the reveal finishes (via MessageBubble),
 * so partially-revealed markup never renders broken mid-stream.
 */
function TypingText({ text, wordsShown }: { text: string; wordsShown: number }) {
  const chunks = useMemo(() => splitWords(text), [text]);
  const visible = chunks.slice(0, wordsShown);
  return (
    <div className="typing-plain" aria-label="Assistant reply, still typing">
      {visible.map((chunk, i) =>
        i === visible.length - 1 ? (
          <span key={i} className="typing-word">
            {chunk}
          </span>
        ) : (
          <span key={i}>{chunk}</span>
        ),
      )}
      <span className="typing-caret" aria-hidden="true" />
    </div>
  );
}

export function ChatView({ sessionId, onTogglePanel, onOpenSettings, onBotApi, loadingHistory = false }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [lastStrategy, setLastStrategy] = useState<Strategy | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Gentle, non-blocking prompt shown when the user sends a message before
  // any AI provider is configured (see the catch in `send`). It never blocks
  // sending — the message still goes through once a key is added in Settings.
  const [noProvider, setNoProvider] = useState(false);

  // Typewriter reveal state: id of the assistant message being revealed and
  // how many word chunks are currently visible.
  const [typingId, setTypingId] = useState<string | null>(null);
  const [revealedWords, setRevealedWords] = useState(0);

  // Copy-to-clipboard feedback: id of the message showing "Copied".
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copyTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  // Custom bots (Grok-style): roster + active bot. The in-header dropdown
  // picker is gone (the sidebar owns selection now) but the roster logic
  // is unchanged, and switchBot is exposed to parents via onBotApi.
  const [bots, setBots] = useState<BotInfo[]>([]);
  const [activeBotId, setActiveBotId] = useState<string | null>(() => {
    try {
      return localStorage.getItem("boom.activeBotId");
    } catch {
      return null;
    }
  });
  const [botMenuOpen, setBotMenuOpen] = useState(false);
  const [botError, setBotError] = useState<string | null>(null);

  // Load the bot roster once. The server's is_active flag wins over the
  // localStorage hint; localStorage only seeds the first paint.
  useEffect(() => {
    let cancelled = false;
    listBots()
      .then((list) => {
        if (cancelled) return;
        setBots(list);
        const serverActive = list.find((b) => b.is_active) ?? null;
        if (serverActive) {
          setActiveBotId(serverActive.id);
          try {
            localStorage.setItem("boom.activeBotId", serverActive.id);
          } catch {
            /* storage unavailable — state still works in memory */
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          // Honest degradation: chat keeps working without bot selection.
          setBotError(err instanceof Error ? err.message : "Failed to load bots");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const activeBot: BotInfo | null = bots.find((b) => b.id === activeBotId) ?? null;

  const switchBot = useCallback(
    async (id: string) => {
      if (id === activeBotId) {
        setBotMenuOpen(false);
        return;
      }
      setBotError(null);
      try {
        await setActiveBot(id);
        setActiveBotId(id);
        try {
          localStorage.setItem("boom.activeBotId", id);
        } catch {
          /* storage unavailable — state still works in memory */
        }
      } catch (err) {
        setBotError(err instanceof Error ? err.message : "Failed to switch bot");
      } finally {
        setBotMenuOpen(false);
      }
    },
    [activeBotId]
  );

  // Expose bot control to the parent (sidebar picker). onBotApi is optional;
  // when absent, everything above still works exactly as before.
  useEffect(() => {
    onBotApi?.({ bots, activeBotId, switchBot });
  }, [onBotApi, bots, activeBotId, switchBot]);

  const listRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Load this session's messages whenever the session changes.
  useEffect(() => {
    setMessages([]);
    setError(null);
    setNoProvider(false);
    setLastStrategy(null);
    setTypingId(null);
    // NOTE: history fetch goes through lib/api (e.g. GET /api/sessions detail
    // or the loader the api-client worker provides); no fake messages here.
  }, [sessionId]);

  const handleScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    nearBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  }, []);

  const scrollToBottomSmooth = useCallback(() => {
    // Smooth-scroll the sentinel into view; respects the user's scroll
    // position because callers check nearBottomRef first.
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, []);

  const followInstant = useCallback(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  // Auto-scroll on new messages / phase changes: smooth, and only when the
  // user is already near the bottom — reading old messages never gets yanked.
  useEffect(() => {
    if (nearBottomRef.current) scrollToBottomSmooth();
  }, [messages.length, phase, scrollToBottomSmooth]);

  // While the typewriter reveals words, follow instantly instead of smooth:
  // a smooth animation per word would queue up and lag behind the reveal.
  useEffect(() => {
    if (typingId && nearBottomRef.current) followInstant();
  }, [revealedWords, typingId, followInstant]);

  // CLIENT-SIDE word-by-word reveal: see the comment block at the top of file.
  // This is NOT live token streaming — the full reply already arrived.
  useEffect(() => {
    if (!typingId) return;
    const typingMsg = messages.find((m) => m.id === typingId);
    if (!typingMsg) {
      setTypingId(null);
      return;
    }
    const totalWords = splitWords(typingMsg.text).length;
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      setRevealedWords(totalWords); // motion off → show instantly
      setTypingId(null);
      return;
    }
    setRevealedWords(0);
    const timer = window.setInterval(() => {
      setRevealedWords((n) => Math.min(n + 1, totalWords));
    }, TYPEWRITER_MS_PER_WORD);
    return () => window.clearInterval(timer);
  }, [typingId, messages]);

  useEffect(() => {
    if (!typingId) return;
    const typingMsg = messages.find((m) => m.id === typingId);
    if (typingMsg && revealedWords >= splitWords(typingMsg.text).length) {
      setTypingId(null);
    }
  }, [typingId, messages, revealedWords]);

  const autoresize = useCallback((el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_PX)}px`;
  }, []);

  const send = useCallback(
    async (promptText?: string) => {
      const prompt = (promptText ?? input).trim();
      if (!prompt || sending) return;
      setError(null);

      const userMsg: ChatMessage = { id: uid(), role: "user", text: prompt, ts: Date.now() };
      setMessages((prev) => [...prev, userMsg]);
      setInput("");
      if (inputRef.current) autoresize(inputRef.current);
      setSending(true);
      setPhase("thinking");

      try {
        const req: ChatRequest = { prompt };
        if (sessionId) req.session_id = sessionId;
        if (activeBot) req.bot_id = activeBot.id;
        const res = await postChat(req, (st) => setPhase(st.phase));

        const reply: ChatMessage = { id: uid(), role: "assistant", text: res.reply, ts: Date.now() };
        setMessages((prev) => [...prev, reply]);
        setLastStrategy(
          (["auto", "react", "dag", "mass"] as const).includes(res.strategy as Strategy)
            ? (res.strategy as Strategy)
            : null,
        );
        setTypingId(reply.id); // start the typewriter reveal animation
        setNoProvider(false); // a reply proves a provider is configured
      } catch (err) {
        if (isProviderNotConfiguredError(err)) {
          // The backend is up but no AI provider key is configured (HTTP 503
          // "agent brain not configured"). Show the gentle inline prompt
          // instead of the scary backend-unreachable error card.
          setNoProvider(true);
        } else {
          setError(err instanceof Error ? err.message : "Request failed");
        }
      } finally {
        setSending(false);
        setPhase(null);
      }
    },
    [input, sending, sessionId, activeBot, autoresize]
  );

  const retryLast = useCallback(() => {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser) void send(lastUser.text);
  }, [messages, send]);

  /**
   * Regenerate the latest assistant reply: drop it (and anything after the
   * last user message), then re-send the same prompt.
   */
  const regenerate = useCallback(() => {
    if (sending) return;
    const lastUserIdx = messages.map((m) => m.role).lastIndexOf("user");
    if (lastUserIdx < 0) return;
    const prompt = messages[lastUserIdx].text;
    setMessages((prev) => {
      const idx = prev.map((m) => m.role).lastIndexOf("user");
      return idx < 0 ? prev : prev.slice(0, idx + 1);
    });
    void send(prompt);
  }, [messages, sending, send]);

  const copyText = useCallback(async (id: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for non-secure contexts (same approach as MessageParts).
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopiedId(id);
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopiedId(null), COPY_FEEDBACK_MS);
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    autoresize(e.target);
  };

  // `lastStrategy` is still tracked (set in `send`) but the team-chat
  // header is minimal — no strategy badge rendered. Read here to keep the
  // state intentionally alive for future use / debugging.
  void lastStrategy;
  // `onTogglePanel` kept for prop compatibility only (no toggle button in
  // the team-chat header); `botMenuOpen` state is driven by switchBot only.
  void onTogglePanel;

  const canSend = !sending && input.trim().length > 0;

  // Id of the newest assistant message — the only one that gets Regenerate.
  const lastAssistantId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return messages[i].id;
    }
    return null;
  }, [messages]);

  return (
    <section className="chat-view" aria-label="Chat">
      {/* Team-chat header: avatar + bot name + Online status.
          No bot-picker dropdown (sidebar owns selection via onBotApi),
          no panel toggle button. */}
      <header className="tc-header">
        {activeBot ? (
          <BotAvatar
            botId={activeBot.id}
            name={activeBot.name}
            emoji={activeBot.avatar_emoji}
            size={36}
            status="online"
          />
        ) : (
          <BoomiAvatar size={36} alt="Boomi" />
        )}
        <div className="tc-header-meta">
          <div className="tc-header-name">{activeBot ? activeBot.name : "Boomi"}</div>
          <div className="tc-header-status">
            <span className="tc-online-dot" aria-hidden="true" />
            <span>Online</span>
            {botError && (
              <span className="tc-header-note" role="alert" title={botError}>
                · bots unavailable
              </span>
            )}
          </div>
        </div>
      </header>

      <div className="gc-scroll" ref={listRef} onScroll={handleScroll} role="log" aria-live="polite" aria-label="Chat messages">
      <div className="gc-column">
        {/* History loading: bubble-shaped skeletons. */}
        {loadingHistory && messages.length === 0 && (
          <div role="status" aria-busy="true" aria-label="Loading conversation history">
            {HISTORY_SKELETON_WIDTHS.map((w, i) => (
              <div key={i} className={`gc-skel-row ${i % 2 ? "gc-skel-row-user" : "gc-skel-row-bot"}`} aria-hidden="true" style={{ marginBottom: 20 }}>
                <Skeleton width={w} height={i % 3 === 2 ? 72 : 48} style={{ borderRadius: 22 }} />
              </div>
            ))}
          </div>
        )}

        {messages.length === 0 && !sending && !loadingHistory && (
          <BoomiEmptyState
            animation="waving"
            size={112}
            title="Hi, I'm Boomi"
            description="No messages yet — say hello to start a conversation."
          />
        )}

        {messages.map((msg) => {
          const isTyping = msg.id === typingId;
          const copied = copiedId === msg.id;
          const isUser = msg.role === "user";
          const ts = formatSmartTime(msg.ts);
          const fullTs = formatFullTime(msg.ts);
          return (
            <div key={msg.id} className={`gc-row ${isUser ? "gc-row-user" : "gc-row-bot"}`}>
              {/* Bot avatar on the left (iMessage/Grok style). */}
              {!isUser && (
                <div className="gc-avatar" aria-hidden="true">
                  {isTyping ? (
                    <BoomiAvatar size={34} status="thinking" alt="Boomi is typing" />
                  ) : activeBot ? (
                    <BotAvatar
                      botId={activeBot.id}
                      name={activeBot.name}
                      emoji={activeBot.avatar_emoji}
                      size={34}
                      status="online"
                    />
                  ) : (
                    <BoomiAvatar size={34} alt="Boomi" />
                  )}
                </div>
              )}
              <div className="gc-main">
                <div className={`gc-bubble ${isUser ? "gc-bubble-user" : "gc-bubble-bot"}`}>
                  {isTyping ? (
                    <TypingText text={msg.text} wordsShown={revealedWords} />
                  ) : (
                    <MessageBubble message={msg} />
                  )}
                </div>
                <div className="gc-meta">
                  {ts && (
                    <span className="gc-time" title={fullTs}>
                      {ts}
                    </span>
                  )}
                  <div className="gc-actions" role="toolbar" aria-label="Message actions">
                    <button
                      type="button"
                      className={`gc-action-btn${copied ? " is-copied" : ""}`}
                      onClick={() => void copyText(msg.id, msg.text)}
                      aria-label={copied ? "Copied to clipboard" : "Copy message"}
                      title={copied ? "Copied" : "Copy"}
                    >
                      {copied ? <Check size={13} /> : <Copy size={13} />}
                    </button>
                    {msg.role === "assistant" && msg.id === lastAssistantId && !isTyping && (
                      <button
                        type="button"
                        className="gc-action-btn"
                        onClick={regenerate}
                        disabled={sending}
                        aria-label="Regenerate reply"
                        title="Regenerate"
                      >
                        <RotateCcw size={13} />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </div>
          );
        })}

        {/* Bot working: bouncing avatar + dancing dots + playful status. */}
        {phase && (
          <BotThinking
            phase={phase}
            botId={activeBot?.id ?? null}
            botName={activeBot?.name ?? null}
            botEmoji={activeBot?.avatar_emoji ?? null}
          />
        )}

        {noProvider && (
          <div className="gc-card" role="status">
            <KeyRound size={16} aria-hidden="true" style={{ color: "var(--muted)", flexShrink: 0 }} />
            <p className="gc-card-text">
              No AI provider configured — add an API key in Settings so Boom can reply.
            </p>
            {onOpenSettings && (
              <button
                type="button"
                className="gc-card-btn"
                onClick={onOpenSettings}
              >
                Open Settings
              </button>
            )}
            <button
              type="button"
              className="gc-card-dismiss"
              onClick={() => setNoProvider(false)}
              aria-label="Dismiss notification"
            >
              <X size={14} />
            </button>
          </div>
        )}

        {error && (
          <div className="gc-card gc-card-error" role="alert">
            <p className="gc-card-text">{error}</p>
            <button type="button" className="gc-card-btn" onClick={retryLast} disabled={sending}>
              <RotateCcw size={14} /> Retry
            </button>
          </div>
        )}

        {/* Scroll sentinel: smooth auto-scroll target (see scrollToBottomSmooth). */}
        <div ref={bottomRef} aria-hidden="true" />
      </div>
      </div>

      {/* Slim "replying…" strip above the composer while waiting. */}
      {sending && (
        <div className="gc-sending" role="status" aria-live="polite">
          <span className="gc-dots" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>{phase ? `${phase}…` : "Boom is replying…"}</span>
        </div>
      )}

      <div className="gc-composer-wrap">
        <div className="gc-composer-inner">
          <form
            className="gc-composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <textarea
              ref={inputRef}
              className="gc-input"
              rows={1}
              value={input}
              onChange={handleInput}
              onKeyDown={handleKeyDown}
              placeholder={sending ? "Boom is replying…" : "Message Boom…"}
              disabled={sending}
              aria-label="Message input"
            />
            <button type="submit" className="gc-send" disabled={!canSend} aria-label="Send message">
              <SendHorizonal size={17} />
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}
