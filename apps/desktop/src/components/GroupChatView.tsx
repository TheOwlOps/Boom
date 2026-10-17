/**
 * GroupChatView — full-page chat for a bot group.
 *
 * Renders in the main view (like ChatView) when a group is selected in the
 * sidebar. Team-chat style: stacked-avatar header, tc-msg rows with the
 * speaking bot's avatar + name, pill composer with a "Send as" selector.
 *
 * Sending fans out to every member via POST /api/bot-groups/:id/message.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  RefreshCw,
  SendHorizonal,
  Trash2,
  User,
  UserMinus,
  UserPlus,
} from "lucide-react";
import { formatFullTime, formatSmartTime } from "../lib/formatTime";
import {
  type BotGroup,
  type BotRef,
  type GroupMessage,
  apiGetGroupDetail,
  apiDeleteGroup,
  apiAddBotToGroup,
  apiRemoveBotFromGroup,
  apiMessageGroup,
  getBotsRef,
} from "../lib/botGroups";
import { BotAvatar } from "./BotAvatar";
import { BoomiEmptyState } from "./BoomiEmptyState";
import { DeleteGroupModal, MemberAvatarStack } from "./BotGroupManager";
import { ThinkingDots } from "./ThinkingDots";
import "../styles/effects.css";
import "../styles/teamchat.css";

type Props = {
  groupId: string;
  /** Deselect the group (back to the 1:1 chat view). */
  onClose(): void;
  /** Called after the group is deleted. */
  onDeleted(id: string): void;
  /** Refresh the sidebar group list (member changes, renames…). */
  onChanged?(): void;
};

const NEAR_BOTTOM_PX = 80;
const MAX_INPUT_PX = 200;

function uid(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function GroupChatView({ groupId, onClose, onDeleted, onChanged }: Props) {
  const [group, setGroup] = useState<BotGroup | null>(null);
  const [bots, setBots] = useState<BotRef[]>([]);
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [fromBotId, setFromBotId] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [membersOpen, setMembersOpen] = useState(false);
  const [addBotId, setAddBotId] = useState("");
  const [adding, setAdding] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const listRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const botById = useMemo(() => new Map(bots.map((b) => [b.id, b])), [bots]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [detail, refs] = await Promise.all([apiGetGroupDetail(groupId), getBotsRef()]);
      setGroup(detail.group);
      setMessages(detail.messages);
      setBots(refs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load group");
    } finally {
      setLoading(false);
    }
  }, [groupId]);

  useEffect(() => {
    setMessages([]);
    setGroup(null);
    setError(null);
    setInput("");
    setFromBotId("");
    setMembersOpen(false);
    void load();
  }, [groupId, load]);

  const handleScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    nearBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  }, []);

  // Follow new messages only when already near the bottom.
  useEffect(() => {
    if (nearBottomRef.current) bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, sending]);

  const autoresize = useCallback((el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_PX)}px`;
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;
    setSendError(null);
    setSending(true);
    const nowIso = new Date().toISOString();
    const userEntry: GroupMessage = {
      id: uid(),
      bot_id: fromBotId,
      bot_name: fromBotId ? (botById.get(fromBotId)?.name ?? "You") : "You",
      text,
      created_at: nowIso,
    };
    setMessages((prev) => [...prev, userEntry]);
    setInput("");
    if (inputRef.current) autoresize(inputRef.current);
    try {
      const replies = await apiMessageGroup(groupId, text, fromBotId || undefined);
      const replyEntries: GroupMessage[] = replies.map((r) => ({
        id: uid(),
        bot_id: r.bot_id,
        bot_name: r.bot_name,
        text: r.reply,
        created_at: new Date().toISOString(),
      }));
      setMessages((prev) => [...prev, ...replyEntries]);
      onChanged?.();
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Send failed");
    } finally {
      setSending(false);
    }
  }, [input, sending, groupId, fromBotId, botById, autoresize, onChanged]);

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

  const addBot = async () => {
    if (!addBotId || adding) return;
    setAdding(true);
    try {
      const updated = await apiAddBotToGroup(groupId, addBotId);
      setGroup(updated);
      setAddBotId("");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add bot");
    } finally {
      setAdding(false);
    }
  };

  const removeBot = async (botId: string) => {
    if (removingId) return;
    setRemovingId(botId);
    try {
      const updated = await apiRemoveBotFromGroup(groupId, botId);
      setGroup(updated);
      if (fromBotId === botId) setFromBotId("");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove bot");
    } finally {
      setRemovingId(null);
    }
  };

  const members = useMemo(
    () =>
      (group?.bot_ids ?? [])
        .map((id) => botById.get(id))
        .filter((b): b is BotRef => b !== undefined),
    [group, botById],
  );
  const addableBots = useMemo(
    () => bots.filter((b) => !(group?.bot_ids ?? []).includes(b.id)),
    [bots, group],
  );

  const canSend = !sending && input.trim().length > 0;

  return (
    <section className="chat-view" aria-label={`Group chat ${group?.name ?? ""}`}>
      {/* Header: stacked member avatars + group name + member count. */}
      <header className="tc-header">
        <MemberAvatarStack bots={members} size={36} />
        <div className="tc-header-meta">
          <div className="tc-header-name">{group?.name ?? "Group"}</div>
          <div className="tc-header-status">
            <span className="tc-online-dot" aria-hidden="true" />
            <span>
              {members.length} member{members.length === 1 ? "" : "s"}
            </span>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={() => setMembersOpen((v) => !v)}
            aria-expanded={membersOpen}
            aria-label="Toggle members panel"
            className="flex h-9 items-center gap-1 rounded-full px-2.5 text-[13px] text-[var(--muted)] transition-colors hover:bg-[var(--surface)] hover:text-[var(--text)]"
          >
            Members
            <ChevronDown size={14} className={`transition-transform ${membersOpen ? "rotate-180" : ""}`} />
          </button>
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            aria-label="Delete group"
            title="Delete group"
            className="flex h-9 w-9 items-center justify-center rounded-full text-[var(--muted)] transition-colors hover:bg-[var(--surface)] hover:text-[var(--text)]"
          >
            <Trash2 size={16} />
          </button>
        </div>
      </header>

      {/* Collapsible members panel. */}
      {membersOpen && group && (
        <div className="shrink-0 border-b border-[var(--border)] bg-[var(--surface)] px-5 py-3">
          {members.length === 0 ? (
            <p className="text-sm italic text-[var(--muted)]">No members yet — add a bot below.</p>
          ) : (
            <ul className="mb-2 space-y-1">
              {members.map((m) => (
                <li key={m.id} className="flex items-center gap-2">
                  <BotAvatar botId={m.id} name={m.name} emoji={m.avatar_emoji} size={28} status="idle" />
                  <span className="flex-1 text-sm text-[var(--text)]">{m.name}</span>
                  <button
                    onClick={() => void removeBot(m.id)}
                    disabled={removingId !== null}
                    aria-label={`Remove ${m.name} from group`}
                    title={`Remove ${m.name}`}
                    className="rounded p-1 text-[var(--muted)] hover:text-[var(--text)] disabled:opacity-50"
                  >
                    {removingId === m.id ? (
                      <RefreshCw size={14} className="animate-spin" />
                    ) : (
                      <UserMinus size={14} />
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {addableBots.length > 0 && (
            <div className="flex gap-2">
              <select
                value={addBotId}
                onChange={(e) => setAddBotId(e.target.value)}
                aria-label="Bot to add"
                className="flex-1 rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-sm text-[var(--text)]"
              >
                <option value="">Add a bot…</option>
                {addableBots.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
              <button
                onClick={() => void addBot()}
                disabled={!addBotId || adding}
                className="flex items-center gap-1 rounded bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
              >
                {adding ? "Adding…" : (
                  <>
                    <UserPlus size={14} /> Add
                  </>
                )}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Messages. */}
      <div className="chat-messages tc-list" ref={listRef} onScroll={handleScroll} role="log" aria-live="polite" aria-label="Group messages">
        {loading && (
          <div role="status" aria-busy="true" aria-label="Loading group messages">
            {[0, 1, 2].map((i) => (
              <div key={i} className="tc-msg" aria-hidden="true">
                <div className="h-9 w-9 animate-pulse rounded-full bg-[var(--surface-2)]" />
                <div className="tc-msg-main" style={{ gap: 8 }}>
                  <div className="h-3 w-24 animate-pulse rounded bg-[var(--surface-2)]" />
                  <div className="h-10 w-3/4 animate-pulse rounded bg-[var(--surface-2)]" />
                </div>
              </div>
            ))}
          </div>
        )}

        {!loading && error && (
          <div className="error-card" role="alert">
            <p className="error-text">{error}</p>
            <button type="button" className="retry-button" onClick={() => void load()}>
              <RefreshCw size={14} /> Retry
            </button>
          </div>
        )}

        {!loading && !error && messages.length === 0 && (
          <BoomiEmptyState
            animation="waving"
            size={112}
            title={group ? `Welcome to ${group.name}` : "Group chat"}
            description="No messages yet — say hello and every member will reply in its own voice."
          />
        )}

        {!loading && !error && messages.map((m) => {
          const isYou = m.bot_id === "" || m.bot_name === "You";
          const ref = botById.get(m.bot_id);
          const ts = formatSmartTime(m.created_at);
          const fullTs = formatFullTime(m.created_at);
          return (
            <div key={m.id} className="msg-row">
              <div className="msg-appear tc-msg">
                <div className="tc-msg-avatar">
                  {isYou ? (
                    <span className="tc-user-chip" aria-hidden="true">
                      <User size={18} />
                    </span>
                  ) : (
                    <BotAvatar
                      botId={m.bot_id}
                      name={m.bot_name}
                      emoji={ref?.avatar_emoji}
                      size={36}
                      status="online"
                    />
                  )}
                </div>
                <div className="tc-msg-main">
                  <div className="tc-msg-head">
                    <span className="tc-msg-name">{isYou ? "You" : m.bot_name}</span>
                    {ts && (
                      <span className="tc-msg-time" title={fullTs} aria-label={`Sent at ${fullTs}`}>
                        {ts}
                      </span>
                    )}
                  </div>
                  <div className="tc-msg-body">
                    <p className="whitespace-pre-wrap break-words">{m.text}</p>
                  </div>
                </div>
              </div>
            </div>
          );
        })}

        {sending && (
          <div className="tc-thinking">
            <div className="tc-msg-avatar">
              <MemberAvatarStack bots={members} size={36} />
            </div>
            <div className="tc-thinking-main">
              <div className="tc-msg-head">
                <span className="tc-msg-name">{group?.name ?? "Group"}</span>
              </div>
              <ThinkingDots label="Members are replying" />
            </div>
          </div>
        )}

        <div ref={bottomRef} aria-hidden="true" />
      </div>

      {sendError && (
        <div className="mx-4 mb-2 rounded border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)]" role="alert">
          {sendError}
        </div>
      )}

      {/* Composer: send-as selector + pill input. */}
      <form
        className="tc-composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        {members.length > 0 && (
          <select
            value={fromBotId}
            onChange={(e) => setFromBotId(e.target.value)}
            aria-label="Send as"
            title="Send the message as a specific member"
            className="max-w-32 shrink-0 rounded-full border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)]"
          >
            <option value="">Everyone</option>
            {members.map((m) => (
              <option key={m.id} value={m.id}>
                As {m.name}
              </option>
            ))}
          </select>
        )}
        <textarea
          ref={inputRef}
          className="tc-input"
          rows={1}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={sending ? "Members are replying…" : `Message ${group?.name ?? "group"}…`}
          disabled={sending}
          aria-label="Message input"
        />
        <button type="submit" className="tc-send" disabled={!canSend} aria-label="Send message">
          <SendHorizonal size={18} />
        </button>
      </form>

      {confirmDelete && group && (
        <DeleteGroupModal
          group={group}
          onClose={() => setConfirmDelete(false)}
          onDeleted={(id) => {
            setConfirmDelete(false);
            onDeleted(id);
          }}
        />
      )}
    </section>
  );
}

export type { Props as GroupChatViewProps };
