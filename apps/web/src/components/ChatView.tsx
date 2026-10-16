import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { PanelRight, SendHorizonal, RotateCcw, MessageCircle, Copy, Check, Bot } from "lucide-react";
import { listBots, postChat, setActiveBot } from "../lib/api";
import type { Bot as BotInfo, ChatMessage, ChatRequest, Strategy } from "../lib/types";
import { MessageBubble } from "./MessageParts";
import { ThinkingDots } from "./ThinkingDots";
import "../styles/effects.css";

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

type Props = {
  sessionId: string | null;
  onTogglePanel(): void;
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

// Inline layout matching MessageBubble's assistant row (kept local so the
// typing placeholder looks identical to the final markdown bubble).
const typingRowStyle: CSSProperties = {
  display: "flex",
  gap: 10,
  justifyContent: "flex-start",
  padding: "6px 0",
};
const typingAvatarStyle: CSSProperties = {
  width: 28,
  height: 28,
  borderRadius: 999,
  background: "var(--accent)",
  flexShrink: 0,
  marginTop: 2,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};
const typingBodyStyle: CSSProperties = { maxWidth: "85%", padding: "10px 4px" };

export function ChatView({ sessionId, onTogglePanel }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [lastStrategy, setLastStrategy] = useState<Strategy | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  // Custom bots (Grok-style): roster + active bot picker.
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

  const listRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Load this session's messages whenever the session changes.
  useEffect(() => {
    setMessages([]);
    setError(null);
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
      } catch (err) {
        setError(err instanceof Error ? err.message : "Request failed");
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

  const title = sessionId ? `Session ${sessionId.slice(0, 8)}…` : "New conversation";
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
      <header className="chat-header">
        {/* Active-bot picker: avatar + name, click to switch. */}
        <div className="bot-picker">
          <button
            type="button"
            className="bot-picker-button"
            onClick={() => setBotMenuOpen((v) => !v)}
            aria-haspopup="listbox"
            aria-expanded={botMenuOpen}
            aria-label={activeBot ? `Active bot: ${activeBot.name}. Switch bot` : "Switch bot"}
            disabled={bots.length === 0}
          >
            <span className="bot-avatar" aria-hidden="true">
              {activeBot ? activeBot.avatar_emoji : "🤖"}
            </span>
            <span className="bot-name">{activeBot ? activeBot.name : "Default bot"}</span>
            <span className="bot-caret" aria-hidden="true">▾</span>
          </button>

          {botMenuOpen && bots.length > 0 && (
            <ul className="bot-menu" role="listbox" aria-label="Select bot">
              {bots.map((bot) => (
                <li key={bot.id} role="option" aria-selected={bot.id === activeBotId}>
                  <button
                    type="button"
                    className={`bot-menu-item${bot.id === activeBotId ? " is-active" : ""}`}
                    onClick={() => void switchBot(bot.id)}
                  >
                    <span className="bot-avatar" aria-hidden="true">{bot.avatar_emoji}</span>
                    <span className="bot-name">{bot.name}</span>
                    {bot.id === activeBotId && (
                      <span className="bot-check" aria-hidden="true">✓</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <h2 className="chat-title">{title}</h2>
        {lastStrategy && (
          <span className="strategy-badge" title="Strategy used for the last reply">
            {lastStrategy}
          </span>
        )}
        {botError && (
          <span className="bot-error" role="alert" title={botError}>
            Bot unavailable
          </span>
        )}
        <button
          type="button"
          className="icon-button"
          onClick={onTogglePanel}
          aria-label="Toggle side panel"
        >
          <PanelRight size={18} />
        </button>
      </header>

      <div className="chat-messages" ref={listRef} onScroll={handleScroll} role="log" aria-live="polite">
        {messages.length === 0 && !sending && (
          <div className="empty-state">
            <MessageCircle size={28} className="empty-icon" />
            <p>No messages yet — say hello</p>
          </div>
        )}

        {messages.map((msg) => {
          const isTyping = msg.id === typingId;
          const copied = copiedId === msg.id;
          return (
            <div key={msg.id} className="msg-row">
              {/* Hover actions: Copy on every message, Regenerate only on the
                  latest assistant reply. CSS reveals them on .msg-row:hover. */}
              <div className="msg-actions" role="toolbar" aria-label="Message actions">
                <button
                  type="button"
                  className={`msg-action-btn${copied ? " is-copied" : ""}`}
                  onClick={() => void copyText(msg.id, msg.text)}
                  aria-label={copied ? "Copied to clipboard" : "Copy message"}
                >
                  {copied ? <Check size={13} /> : <Copy size={13} />}
                  {copied ? "Copied" : "Copy"}
                </button>
                {msg.role === "assistant" && msg.id === lastAssistantId && !isTyping && (
                  <button
                    type="button"
                    className="msg-action-btn"
                    onClick={regenerate}
                    disabled={sending}
                    aria-label="Regenerate reply"
                  >
                    <RotateCcw size={13} />
                    Regenerate
                  </button>
                )}
              </div>

              {isTyping ? (
                // Typing placeholder: plain text word reveal; full markdown
                // renders via MessageBubble once the reveal finishes.
                <div className="msg-appear" style={typingRowStyle}>
                  <div aria-hidden="true" style={typingAvatarStyle}>
                    <Bot size={16} color="#fff" />
                  </div>
                  <div style={typingBodyStyle}>
                    <TypingText text={msg.text} wordsShown={revealedWords} />
                  </div>
                </div>
              ) : (
                <div className="msg-markdown">
                  <MessageBubble message={msg} />
                </div>
              )}
            </div>
          );
        })}

        {phase && (
          <div className="thinking-row">
            <ThinkingDots />
            <span className="thinking-label">{phase}…</span>
          </div>
        )}

        {error && (
          <div className="error-card" role="alert">
            <p className="error-text">{error}</p>
            <button type="button" className="retry-button" onClick={retryLast} disabled={sending}>
              <RotateCcw size={14} /> Retry
            </button>
          </div>
        )}

        {/* Scroll sentinel: smooth auto-scroll target (see scrollToBottomSmooth). */}
        <div ref={bottomRef} aria-hidden="true" />
      </div>

      {/* Typing indicator: visible near the composer while waiting for a reply. */}
      {sending && (
        <div className="typing-indicator" role="status" aria-live="polite">
          <span className="typing-dots" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>{phase ? `${phase}…` : "bot đang trả lời…"}</span>
        </div>
      )}

      <form
        className="chat-composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          ref={inputRef}
          className="composer-input"
          rows={1}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={sending ? "bot đang trả lời…" : "Message Boom…"}
          disabled={sending}
          aria-label="Message input"
        />
        <button type="submit" className="send-button" disabled={!canSend} aria-label="Send message">
          <SendHorizonal size={18} />
        </button>
      </form>
    </section>
  );
}
