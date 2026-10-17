/**
 * BotGroupManager — create and manage groups of bots, and message them.
 *
 * A bot group is a named set of bots that can be addressed together:
 * sending a message to a group fans out to every member, and replies
 * are shown per bot. Groups are conversation spaces where multiple
 * bot personas can weigh in on the same thread.
 *
 * Endpoints (backed by the gateway bot-groups routes):
 *   POST   /api/bot-groups               {name, bot_ids[]} -> 201 {ok, group}
 *   GET    /api/bot-groups               -> {groups: [...], count}
 *   GET    /api/bot-groups/:id           -> {ok, group, messages: [...]}
 *   DELETE /api/bot-groups/:id           -> {ok: true}
 *   POST   /api/bot-groups/:id/bots      {bot_id} -> {ok, group}
 *   DELETE /api/bot-groups/:id/bots/:bot -> {ok, group}
 *   POST   /api/bot-groups/:id/message   {text, from_bot_id?} -> {ok, replies: [...]}
 *
 * All responses are validated at runtime (unknown + narrowing, same pattern
 * as BotManager / lib/api.ts); malformed shapes throw instead of rendering
 * garbage.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  MessageSquare,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  UserMinus,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { API_BASE } from "../lib/api";
import { formatFullTime, formatSmartTime } from "../lib/formatTime";
import { BoomiEmptyState } from "./BoomiEmptyState";
import { BotAvatar } from "./BotAvatar";

/* ------------------------------------------------------------------ types */

/** One bot group. */
export interface BotGroup {
  id: string;
  name: string;
  bot_ids: string[];
  created_at: string;
}

/** One message in a group conversation. */
export interface GroupMessage {
  id: string;
  bot_id: string;
  bot_name: string;
  text: string;
  created_at: string;
}

/** One reply from a group member after sending a group message. */
export interface GroupReply {
  bot_id: string;
  bot_name: string;
  reply: string;
}

/** Slim bot reference: only what the group UI needs. */
export interface BotRef {
  id: string;
  name: string;
  avatar_emoji: string;
}

/* ------------------------------------------------------- runtime validation */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function reqString(v: unknown, field: string): string {
  if (typeof v !== "string") throw new Error(`malformed response: ${field} must be a string`);
  return v;
}

function optString(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Lenient string-list parser: non-array or non-string entries are dropped. */
function stringList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string");
}

function parseGroup(v: unknown): BotGroup {
  if (!isRecord(v)) throw new Error("malformed response: bot group entry");
  return {
    id: reqString(v.id, "group.id"),
    name: reqString(v.name, "group.name"),
    bot_ids: stringList(v.bot_ids),
    created_at: optString(v.created_at),
  };
}

function parseGroupMessage(v: unknown): GroupMessage {
  if (!isRecord(v)) throw new Error("malformed response: group message entry");
  return {
    id: reqString(v.id, "message.id"),
    bot_id: optString(v.bot_id),
    bot_name: optString(v.bot_name),
    text: reqString(v.text, "message.text"),
    created_at: optString(v.created_at),
  };
}

function parseGroupReply(v: unknown): GroupReply {
  if (!isRecord(v)) throw new Error("malformed response: group reply entry");
  return {
    bot_id: reqString(v.bot_id, "reply.bot_id"),
    bot_name: optString(v.bot_name),
    reply: reqString(v.reply, "reply.reply"),
  };
}

function parseBotRef(v: unknown): BotRef {
  if (!isRecord(v)) throw new Error("malformed response: bot entry");
  return {
    id: reqString(v.id, "bot.id"),
    name: reqString(v.name, "bot.name"),
    avatar_emoji: optString(v.avatar_emoji),
  };
}

/** Server error convention: {"error": "..."} or {"ok": false, "error": "..."}. */
async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body: unknown = (await res.json()) as unknown;
    if (isRecord(body) && typeof body.error === "string") return body.error;
  } catch {
    /* body was not JSON */
  }
  return `HTTP ${res.status} ${res.statusText}`.trim();
}

async function parseJsonBody(res: Response, path: string): Promise<unknown> {
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error(`malformed response: ${path} did not return JSON`);
  }
  if (!res.ok) throw new Error(await readErrorMessage(res));
  if (isRecord(body) && body.ok === false) {
    throw new Error(typeof body.error === "string" ? body.error : "request failed");
  }
  return body;
}

/* ------------------------------------------------------------- api helpers */

async function getGroups(): Promise<BotGroup[]> {
  const res = await fetch(`${API_BASE}/api/bot-groups`);
  const body = await parseJsonBody(res, "GET /api/bot-groups");
  if (!isRecord(body) || !Array.isArray(body.groups)) {
    throw new Error("malformed response: GET /api/bot-groups");
  }
  return body.groups.map(parseGroup);
}

async function getBotsRef(): Promise<BotRef[]> {
  const res = await fetch(`${API_BASE}/api/bots`);
  const body = await parseJsonBody(res, "GET /api/bots");
  if (!isRecord(body) || !Array.isArray(body.bots)) {
    throw new Error("malformed response: GET /api/bots");
  }
  return body.bots.map(parseBotRef);
}

async function apiCreateGroup(name: string, botIds: string[]): Promise<BotGroup> {
  const res = await fetch(`${API_BASE}/api/bot-groups`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, bot_ids: botIds }),
  });
  const body = await parseJsonBody(res, "POST /api/bot-groups");
  if (!isRecord(body) || !isRecord(body.group)) {
    throw new Error("malformed response: POST /api/bot-groups");
  }
  return parseGroup(body.group);
}

async function apiGetGroupDetail(id: string): Promise<{ group: BotGroup; messages: GroupMessage[] }> {
  const res = await fetch(`${API_BASE}/api/bot-groups/${encodeURIComponent(id)}`);
  const body = await parseJsonBody(res, "GET /api/bot-groups/:id");
  if (!isRecord(body) || !isRecord(body.group) || !Array.isArray(body.messages)) {
    throw new Error("malformed response: GET /api/bot-groups/:id");
  }
  return { group: parseGroup(body.group), messages: body.messages.map(parseGroupMessage) };
}

async function apiDeleteGroup(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/bot-groups/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  await parseJsonBody(res, "DELETE /api/bot-groups/:id");
}

async function apiAddBotToGroup(groupId: string, botId: string): Promise<BotGroup> {
  const res = await fetch(`${API_BASE}/api/bot-groups/${encodeURIComponent(groupId)}/bots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bot_id: botId }),
  });
  const body = await parseJsonBody(res, "POST /api/bot-groups/:id/bots");
  if (!isRecord(body) || !isRecord(body.group)) {
    throw new Error("malformed response: POST /api/bot-groups/:id/bots");
  }
  return parseGroup(body.group);
}

async function apiRemoveBotFromGroup(groupId: string, botId: string): Promise<BotGroup> {
  const res = await fetch(
    `${API_BASE}/api/bot-groups/${encodeURIComponent(groupId)}/bots/${encodeURIComponent(botId)}`,
    { method: "DELETE" },
  );
  const body = await parseJsonBody(res, "DELETE /api/bot-groups/:id/bots/:bot_id");
  if (!isRecord(body) || !isRecord(body.group)) {
    throw new Error("malformed response: DELETE /api/bot-groups/:id/bots/:bot_id");
  }
  return parseGroup(body.group);
}

async function apiMessageGroup(groupId: string, text: string, fromBotId?: string): Promise<GroupReply[]> {
  const res = await fetch(`${API_BASE}/api/bot-groups/${encodeURIComponent(groupId)}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fromBotId ? { text, from_bot_id: fromBotId } : { text }),
  });
  const body = await parseJsonBody(res, "POST /api/bot-groups/:id/message");
  if (!isRecord(body) || !Array.isArray(body.replies)) {
    throw new Error("malformed response: POST /api/bot-groups/:id/message");
  }
  return body.replies.map(parseGroupReply);
}

/* ------------------------------------------------------------ shared modal */

const inputCls =
  "w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)] focus:border-[var(--muted)] focus:outline-none";

function ModalShell({
  label,
  maxWidth,
  onClose,
  children,
}: {
  label: string;
  maxWidth: string;
  onClose(): void;
  children: React.ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={label}
    >
      <div
        className={`max-h-[90vh] w-full overflow-y-auto rounded-lg bg-[var(--surface)] p-5 ${maxWidth}`}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------- new group modal */

function NewGroupModal({
  bots,
  onClose,
  onCreated,
}: {
  bots: BotRef[];
  onClose(): void;
  onCreated(group: BotGroup): void;
}) {
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const toggle = (botId: string) => {
    setSelected((prev) =>
      prev.includes(botId) ? prev.filter((b) => b !== botId) : [...prev, botId],
    );
  };

  const create = async () => {
    setFormError(null);
    if (!name.trim()) {
      setFormError("Group name is required.");
      return;
    }
    setSaving(true);
    try {
      const group = await apiCreateGroup(name.trim(), selected);
      onCreated(group);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalShell label="New bot group" maxWidth="max-w-md" onClose={onClose}>
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-semibold text-[var(--text)]">New Bot Group</h3>
        <button onClick={onClose} aria-label="Close" className="text-[var(--muted)] hover:text-[var(--text)]">
          <X size={18} />
        </button>
      </div>

      {formError && (
        <p className="mb-3 rounded border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] px-3 py-2 text-sm text-[var(--text)]">
          {formError}
        </p>
      )}

      <div className="mb-3">
        <label className="mb-1 block text-xs text-[var(--muted)]">Group name *</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Support Crew"
          autoFocus
          className={inputCls}
        />
      </div>

      <div className="mb-4">
        <label className="mb-1 block text-xs text-[var(--muted)]">
          Members <span className="text-[var(--muted)]">({selected.length} selected)</span>
        </label>
        {bots.length === 0 ? (
          <p className="rounded border border-[var(--border)] px-3 py-3 text-sm italic text-[var(--muted)]">
            No bots exist yet — create bots first, then group them.
          </p>
        ) : (
          <div className="max-h-56 overflow-y-auto rounded border border-[var(--border)]">
            <ul className="divide-y divide-[var(--border)]">
              {bots.map((b) => (
                <li key={b.id}>
                  <label className="flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-[color-mix(in_srgb,var(--surface-2)_50%,transparent)]">
                    <input
                      type="checkbox"
                      checked={selected.includes(b.id)}
                      onChange={() => toggle(b.id)}
                      className="h-4 w-4 accent-[var(--muted)]"
                    />
                    <BotAvatar botId={b.id} name={b.name} emoji={b.avatar_emoji} size={24} status="idle" />
                    <span className="text-sm text-[var(--text)]">{b.name}</span>
                  </label>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="flex justify-end gap-2">
        <button
          onClick={onClose}
          disabled={saving}
          className="rounded border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--text)] hover:bg-[var(--surface-2)] disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={() => void create()}
          disabled={saving}
          className="rounded bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
        >
          {saving ? "Creating…" : "Create group"}
        </button>
      </div>
    </ModalShell>
  );
}

/* -------------------------------------------------------- delete group modal */

function DeleteGroupModal({
  group,
  onClose,
  onDeleted,
}: {
  group: BotGroup;
  onClose(): void;
  onDeleted(id: string): void;
}) {
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const confirm = async () => {
    setDeleteError(null);
    setDeleting(true);
    try {
      await apiDeleteGroup(group.id);
      onDeleted(group.id);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <ModalShell label={`Delete group ${group.name}`} maxWidth="max-w-sm" onClose={onClose}>
      <h3 className="mb-2 font-semibold text-[var(--text)]">Delete group?</h3>
      <p className="mb-3 text-sm text-[var(--muted)]">
        Delete <span className="font-medium text-[var(--text)]">{group.name}</span> and its
        conversation history? The bots themselves are kept. This cannot be undone.
      </p>
      {deleteError && (
        <p className="mb-3 rounded border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] px-3 py-2 text-sm text-[var(--text)]">
          {deleteError}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          onClick={onClose}
          disabled={deleting}
          className="rounded border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--text)] hover:bg-[var(--surface-2)] disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={() => void confirm()}
          disabled={deleting}
          className="rounded bg-[var(--muted)] px-3 py-1.5 text-sm text-[var(--bg)] hover:bg-[color-mix(in_srgb,var(--muted)_70%,var(--text))] disabled:opacity-50"
        >
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </div>
    </ModalShell>
  );
}

/* ------------------------------------------------------------------ avatar */

function MemberAvatarStack({ bots, size = 28 }: { bots: BotRef[]; size?: number }) {
  const shown = bots.slice(0, 5);
  const extra = bots.length - shown.length;
  return (
    <div className="flex items-center">
      {shown.map((b, i) => (
        <span key={b.id} className="rounded-full ring-2 ring-[var(--surface)]" style={{ marginLeft: i === 0 ? 0 : -8 }}>
          <BotAvatar botId={b.id} name={b.name} emoji={b.avatar_emoji} size={size} status="idle" />
        </span>
      ))}
      {extra > 0 && (
        <span
          className="flex items-center justify-center rounded-full bg-[var(--surface-2)] text-xs text-[var(--text)] ring-2 ring-[var(--surface)]"
          style={{ width: size, height: size, marginLeft: -8 }}
        >
          +{extra}
        </span>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- group list */

function GroupListView({
  groups,
  bots,
  loading,
  error,
  onReload,
  onOpen,
  onNew,
  onDeleted,
}: {
  groups: BotGroup[];
  bots: BotRef[];
  loading: boolean;
  error: string | null;
  onReload(): void;
  onOpen(id: string): void;
  onNew(): void;
  onDeleted(id: string): void;
}) {
  const [confirmDelete, setConfirmDelete] = useState<BotGroup | null>(null);
  const botById = useMemo(() => new Map(bots.map((b) => [b.id, b])), [bots]);

  const header = (
    <div className="mb-4 flex items-center gap-2">
      <div className="flex flex-1 items-center gap-2 text-[var(--muted)]">
        <Users size={16} />
        <span className="text-sm">
          {groups.length === 0 ? "No groups" : `${groups.length} group${groups.length === 1 ? "" : "s"}`}
        </span>
      </div>
      <button
        onClick={onReload}
        aria-label="Reload groups"
        className="rounded border border-[var(--border)] px-2.5 py-2 text-[var(--text)] hover:bg-[var(--surface-2)]"
      >
        <RefreshCw size={14} />
      </button>
      <button
        onClick={onNew}
        className="flex items-center gap-1.5 rounded bg-[var(--accent)] px-3 py-2 text-sm text-[var(--bg)] hover:bg-[var(--accent-hover)]"
      >
        <Plus size={14} /> New Group
      </button>
    </div>
  );

  if (loading) {
    return (
      <div>
        {header}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="animate-pulse rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4">
              <div className="mb-2 h-4 w-1/2 rounded bg-[var(--surface-2)]" />
              <div className="h-7 w-24 rounded bg-[var(--surface-2)]" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div>
      {header}

      {error && (
        <div className="mb-4 flex items-center justify-between rounded border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] px-3 py-2">
          <p className="text-sm text-[var(--text)]">{error}</p>
          <button
            onClick={onReload}
            className="rounded border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface)_50%,transparent)]"
          >
            Retry
          </button>
        </div>
      )}

      {groups.length === 0 ? (
        <BoomiEmptyState
          animation="sleeping"
          title="No bot groups yet"
          description="Group bots together and message the whole crew at once — every member replies in its own voice."
          actionLabel="＋ New Group"
          onAction={onNew}
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {groups.map((g) => {
            const members = g.bot_ids
              .map((id) => botById.get(id))
              .filter((b): b is BotRef => b !== undefined);
            return (
              <article
                key={g.id}
                className="cursor-pointer rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4 hover:bg-[color-mix(in_srgb,var(--surface-2)_40%,transparent)]"
                onClick={() => onOpen(g.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onOpen(g.id);
                }}
                aria-label={`Open group ${g.name}`}
              >
                <div className="mb-2 flex items-start justify-between gap-2">
                  <h3 className="font-semibold text-[var(--text)]">{g.name}</h3>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmDelete(g);
                    }}
                    aria-label={`Delete group ${g.name}`}
                    title="Delete group"
                    className="rounded p-1 text-[var(--muted)] hover:bg-[color-mix(in_srgb,var(--bg)_50%,transparent)]"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
                <div className="mb-3 flex items-center gap-3">
                  <MemberAvatarStack bots={members} />
                  <span className="text-xs text-[var(--muted)]">
                    {g.bot_ids.length} member{g.bot_ids.length === 1 ? "" : "s"}
                  </span>
                </div>
                <p className="text-xs text-[var(--muted)]" title={formatFullTime(g.created_at)}>
                  Created {formatSmartTime(g.created_at) || "—"}
                </p>
              </article>
            );
          })}
        </div>
      )}

      {confirmDelete && (
        <DeleteGroupModal
          group={confirmDelete}
          onClose={() => setConfirmDelete(null)}
          onDeleted={(id) => {
            setConfirmDelete(null);
            onDeleted(id);
          }}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------- group detail */

function GroupDetailView({
  groupId,
  bots,
  onBack,
  onGroupDeleted,
}: {
  groupId: string;
  bots: BotRef[];
  onBack(): void;
  onGroupDeleted(id: string): void;
}) {
  const [group, setGroup] = useState<BotGroup | null>(null);
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [fromBotId, setFromBotId] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [addBotId, setAddBotId] = useState("");
  const [adding, setAdding] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const firstRender = useRef(true);

  const botById = useMemo(() => new Map(bots.map((b) => [b.id, b])), [bots]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const detail = await apiGetGroupDetail(groupId);
      setGroup(detail.group);
      setMessages(detail.messages);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load group");
    } finally {
      setLoading(false);
    }
  }, [groupId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Scroll to bottom on new messages (skip the initial load scroll jump).
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = async () => {
    setSendError(null);
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const replies = await apiMessageGroup(groupId, text, fromBotId || undefined);
      const now = new Date().toISOString();
      const userEntry: GroupMessage = {
        id: `local-${Date.now()}`,
        bot_id: fromBotId,
        bot_name: fromBotId ? (botById.get(fromBotId)?.name ?? "You") : "You",
        text,
        created_at: now,
      };
      const replyEntries: GroupMessage[] = replies.map((r, i) => ({
        id: `reply-${Date.now()}-${i}`,
        bot_id: r.bot_id,
        bot_name: r.bot_name,
        text: r.reply,
        created_at: now,
      }));
      setMessages((prev) => [...prev, userEntry, ...replyEntries]);
      setInput("");
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Send failed");
    } finally {
      setSending(false);
    }
  };

  const addBot = async () => {
    if (!addBotId || adding) return;
    setAdding(true);
    try {
      const updated = await apiAddBotToGroup(groupId, addBotId);
      setGroup(updated);
      setAddBotId("");
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove bot");
    } finally {
      setRemovingId(null);
    }
  };

  if (loading) {
    return (
      <div className="animate-pulse">
        <div className="mb-4 h-5 w-40 rounded bg-[var(--surface-2)]" />
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-12 w-3/4 rounded-lg bg-[var(--surface-2)]" />
          ))}
        </div>
      </div>
    );
  }

  if (error || !group) {
    return (
      <div>
        <button
          onClick={onBack}
          className="mb-4 flex items-center gap-1 text-sm text-[var(--muted)] hover:text-[var(--text)]"
        >
          <ChevronLeft size={16} /> Back to groups
        </button>
        <div className="rounded border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] px-3 py-4 text-center">
          <p className="mb-2 text-sm text-[var(--text)]">{error ?? "Group not found."}</p>
          <button
            onClick={() => void load()}
            className="rounded border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text)] hover:bg-[color-mix(in_srgb,var(--surface)_50%,transparent)]"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const members = group.bot_ids
    .map((id) => botById.get(id))
    .filter((b): b is BotRef => b !== undefined);
  const addableBots = bots.filter((b) => !group.bot_ids.includes(b.id));

  return (
    <div>
      {/* Header */}
      <div className="mb-4 flex items-center gap-2">
        <button
          onClick={onBack}
          aria-label="Back to groups"
          className="rounded border border-[var(--border)] p-1.5 text-[var(--text)] hover:bg-[var(--surface-2)]"
        >
          <ChevronLeft size={16} />
        </button>
        <div className="flex-1">
          <h3 className="font-semibold text-[var(--text)]">{group.name}</h3>
          <p className="text-xs text-[var(--muted)]">
            {group.bot_ids.length} member{group.bot_ids.length === 1 ? "" : "s"}
            {group.created_at && (
              <span title={formatFullTime(group.created_at)}>
                {" "}· created {formatSmartTime(group.created_at) || "—"}
              </span>
            )}
          </p>
        </div>
        <button
          onClick={() => setConfirmDelete(true)}
          aria-label={`Delete group ${group.name}`}
          title="Delete group"
          className="rounded border border-[var(--border)] px-2.5 py-1.5 text-xs text-[var(--muted)] hover:bg-[color-mix(in_srgb,var(--bg)_50%,transparent)]"
        >
          <Trash2 size={12} />
        </button>
      </div>

      {/* Members */}
      <div className="mb-4 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-xs font-medium text-[var(--muted)]">MEMBERS</span>
        </div>
        {members.length === 0 ? (
          <p className="mb-2 text-sm italic text-[var(--muted)]">No members yet — add a bot below.</p>
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
                  className="rounded p-1 text-[var(--muted)] hover:bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] disabled:opacity-50"
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

      {/* Messages */}
      <div className="mb-4 max-h-[40vh] min-h-40 overflow-y-auto rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3">
        {messages.length === 0 ? (
          <div className="flex h-40 flex-col items-center justify-center text-center">
            <MessageSquare size={24} className="mb-2 text-[var(--muted)]" />
            <p className="text-sm text-[var(--muted)]">No messages yet.</p>
            <p className="mt-1 text-xs text-[var(--muted)]">
              Say hello below — every member of the group will reply.
            </p>
          </div>
        ) : (
          <ul className="space-y-3">
            {messages.map((m) => {
              const isYou = m.bot_id === "" || m.bot_name === "You";
              const ref = botById.get(m.bot_id);
              return (
                <li key={m.id} className={`flex gap-2 ${isYou ? "flex-row-reverse" : ""}`}>
                  {!isYou && (
                    <BotAvatar
                      botId={m.bot_id}
                      name={m.bot_name}
                      emoji={ref?.avatar_emoji}
                      size={28}
                      status="idle"
                    />
                  )}
                  <div className={`max-w-[80%] ${isYou ? "text-right" : ""}`}>
                    <div className="mb-0.5 text-xs text-[var(--muted)]" title={formatFullTime(m.created_at)}>
                      {isYou ? "You" : m.bot_name}
                      {formatSmartTime(m.created_at) && ` · ${formatSmartTime(m.created_at)}`}
                    </div>
                    <div
                      className={`inline-block rounded-lg px-3 py-2 text-sm ${
                        isYou
                          ? "bg-[var(--accent)] text-[var(--bg)]"
                          : "bg-[var(--surface-2)] text-[var(--text)]"
                      }`}
                    >
                      <p className="whitespace-pre-wrap break-words">{m.text}</p>
                    </div>
                  </div>
                </li>
              );
            })}
            <div ref={bottomRef} />
          </ul>
        )}
      </div>

      {/* Composer */}
      {sendError && (
        <p className="mb-2 rounded border border-[var(--border)] bg-[color-mix(in_srgb,var(--bg)_50%,transparent)] px-3 py-2 text-sm text-[var(--text)]">
          {sendError}
        </p>
      )}
      <div className="flex gap-2">
        {members.length > 0 && (
          <select
            value={fromBotId}
            onChange={(e) => setFromBotId(e.target.value)}
            aria-label="Send as"
            title="Send the message as a specific member"
            className="max-w-36 rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-2 text-sm text-[var(--text)]"
          >
            <option value="">Everyone</option>
            {members.map((m) => (
              <option key={m.id} value={m.id}>
                As {m.name}
              </option>
            ))}
          </select>
        )}
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void send();
          }}
          placeholder={`Message ${group.name}…`}
          aria-label="Message the group"
          className="flex-1 rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] placeholder:text-[var(--muted)]"
        />
        <button
          onClick={() => void send()}
          disabled={sending || !input.trim()}
          aria-label="Send message"
          className="rounded bg-[var(--accent)] px-3 py-2 text-sm text-[var(--bg)] hover:bg-[var(--accent-hover)] disabled:opacity-50"
        >
          {sending ? <RefreshCw size={16} className="animate-spin" /> : <Send size={16} />}
        </button>
      </div>

      {confirmDelete && (
        <DeleteGroupModal
          group={group}
          onClose={() => setConfirmDelete(false)}
          onDeleted={(id) => {
            setConfirmDelete(false);
            onGroupDeleted(id);
          }}
        />
      )}
    </div>
  );
}

/* --------------------------------------------------------------- root manager */

/** Bot group manager: list of groups + per-group detail view. */
export default function BotGroupManager() {
  const [groups, setGroups] = useState<BotGroup[]>([]);
  const [bots, setBots] = useState<BotRef[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [g, b] = await Promise.all([getGroups(), getBotsRef()]);
      setGroups(g);
      setBots(b);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load bot groups");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleCreated = (group: BotGroup) => {
    setGroups((prev) => [...prev, group]);
    setCreating(false);
    setOpenId(group.id);
  };

  const handleDeleted = (id: string) => {
    setGroups((prev) => prev.filter((g) => g.id !== id));
    setOpenId((prev) => (prev === id ? null : prev));
  };

  return (
    <div>
      {openId ? (
        <GroupDetailView
          groupId={openId}
          bots={bots}
          onBack={() => {
            setOpenId(null);
            void load();
          }}
          onGroupDeleted={handleDeleted}
        />
      ) : (
        <GroupListView
          groups={groups}
          bots={bots}
          loading={loading}
          error={error}
          onReload={() => void load()}
          onOpen={setOpenId}
          onNew={() => setCreating(true)}
          onDeleted={handleDeleted}
        />
      )}

      {creating && (
        <NewGroupModal bots={bots} onClose={() => setCreating(false)} onCreated={handleCreated} />
      )}
    </div>
  );
}
