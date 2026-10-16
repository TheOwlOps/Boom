import { useCallback, useEffect, useRef, useState } from "react";
import { PanelRight, SendHorizonal, RotateCcw, MessageCircle } from "lucide-react";
import { listBots, postChat, setActiveBot } from "../lib/api";
import type { Bot, ChatMessage, ChatRequest, Strategy } from "../lib/types";
import { MessageBubble } from "./MessageParts";
import { ThinkingDots } from "./ThinkingDots";

// CLIENT-SIDE ANIMATION (NOT live token streaming): the backend returns the
// full reply at once (see API_CONTRACT.md `POST /api/chat`), and this reveals
// it character-by-character (~15ms/char) purely as a typewriter visual effect.
// True token streaming is a v2 backend item.
const TYPEWRITER_MS_PER_CHAR = 15;
const NEAR_BOTTOM_PX = 80;

type Props = {
  sessionId: string | null;
  onTogglePanel(): void;
};

function uid(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function ChatView({ sessionId, onTogglePanel }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [lastStrategy, setLastStrategy] = useState<Strategy | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Typewriter reveal state: id of the assistant message being revealed.
  const [typingId, setTypingId] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(0);

  // Custom bots (Grok-style): roster + active bot picker.
  const [bots, setBots] = useState<Bot[]>([]);
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

  const activeBot: Bot | null = bots.find((b) => b.id === activeBotId) ?? null;

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

  const scrollToBottom = useCallback(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  const handleScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    nearBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  }, []);

  // Auto-scroll on new content — only when the user is already near the
  // bottom, so reading old messages never gets yanked away.
  useEffect(() => {
    if (nearBottomRef.current) scrollToBottom();
  }, [messages, revealed, phase, scrollToBottom]);

  // CLIENT-SIDE typewriter reveal: see the comment block at the top of file.
  // This is NOT live token streaming — the full reply already arrived.
  useEffect(() => {
    if (!typingId) return;
    const typingMsg = messages.find((m) => m.id === typingId);
    if (!typingMsg) {
      setTypingId(null);
      return;
    }
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      setRevealed(typingMsg.text.length); // motion off → show instantly
      setTypingId(null);
      return;
    }
    setRevealed(0);
    const timer = window.setInterval(() => {
      setRevealed((n) => Math.min(n + 1, typingMsg.text.length));
    }, TYPEWRITER_MS_PER_CHAR);
    return () => window.clearInterval(timer);
  }, [typingId, messages]);

  useEffect(() => {
    if (!typingId) return;
    const typingMsg = messages.find((m) => m.id === typingId);
    if (typingMsg && revealed >= typingMsg.text.length) setTypingId(null);
  }, [typingId, messages, revealed]);

  const send = useCallback(
    async (promptText?: string) => {
      const prompt = (promptText ?? input).trim();
      if (!prompt || sending) return;
      setError(null);

      const userMsg: ChatMessage = { id: uid(), role: "user", text: prompt, ts: Date.now() };
      setMessages((prev) => [...prev, userMsg]);
      setInput("");
      if (inputRef.current) inputRef.current.style.height = "auto";
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
    [input, sending, sessionId, activeBot]
  );

  const retryLast = useCallback(() => {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser) void send(lastUser.text);
  }, [messages, send]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  const title = sessionId ? `Session ${sessionId.slice(0, 8)}…` : "New conversation";
  const canSend = !sending && input.trim().length > 0;

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
          const display =
            msg.id === typingId ? { ...msg, text: msg.text.slice(0, revealed) } : msg;
          return <MessageBubble key={msg.id} message={display} />;
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
      </div>

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
          placeholder="Message Boom…"
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
