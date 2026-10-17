/**
 * botGroups.ts — shared API client for bot groups.
 *
 * Extracted from components/BotGroupManager.tsx so both the sidebar list
 * and the full-page GroupChatView can share one implementation.
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
 * All responses are validated at runtime (unknown + narrowing); malformed
 * shapes throw instead of rendering garbage.
 */
import { API_BASE } from "./api";

/* ------------------------------------------------------------------ types */

/** One bot group. */
export interface BotGroup {
  id: string;
  name: string;
  bot_ids: string[];
  chief_bot_id?: string | null;
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

/** Last-message preview for a sidebar group row. */
export interface GroupPreview {
  groupId: string;
  text: string;
  created_at: string;
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

export function parseGroup(v: unknown): BotGroup {
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

export async function getGroups(): Promise<BotGroup[]> {
  const res = await fetch(`${API_BASE}/api/bot-groups`);
  const body = await parseJsonBody(res, "GET /api/bot-groups");
  if (!isRecord(body) || !Array.isArray(body.groups)) {
    throw new Error("malformed response: GET /api/bot-groups");
  }
  return body.groups.map(parseGroup);
}

export async function getBotsRef(): Promise<BotRef[]> {
  const res = await fetch(`${API_BASE}/api/bots`);
  const body = await parseJsonBody(res, "GET /api/bots");
  if (!isRecord(body) || !Array.isArray(body.bots)) {
    throw new Error("malformed response: GET /api/bots");
  }
  return body.bots.map(parseBotRef);
}

export async function apiCreateGroup(name: string, botIds: string[]): Promise<BotGroup> {
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

export async function apiGetGroupDetail(id: string): Promise<{ group: BotGroup; messages: GroupMessage[] }> {
  const res = await fetch(`${API_BASE}/api/bot-groups/${encodeURIComponent(id)}`);
  const body = await parseJsonBody(res, "GET /api/bot-groups/:id");
  if (!isRecord(body) || !isRecord(body.group) || !Array.isArray(body.messages)) {
    throw new Error("malformed response: GET /api/bot-groups/:id");
  }
  return { group: parseGroup(body.group), messages: body.messages.map(parseGroupMessage) };
}

export async function apiDeleteGroup(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/bot-groups/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  await parseJsonBody(res, "DELETE /api/bot-groups/:id");
}

export async function apiAddBotToGroup(groupId: string, botId: string): Promise<BotGroup> {
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

export async function apiRemoveBotFromGroup(groupId: string, botId: string): Promise<BotGroup> {
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

export async function apiSetGroupChief(groupId: string, botId: string): Promise<BotGroup> {
  const res = await fetch(
    `${API_BASE}/api/bot-groups/${encodeURIComponent(groupId)}/chief`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bot_id: botId }),
    },
  );
  const body = await parseJsonBody(res, "PUT /api/bot-groups/:id/chief");
  if (!isRecord(body) || !isRecord(body.group)) {
    throw new Error("malformed response: PUT /api/bot-groups/:id/chief");
  }
  return parseGroup(body.group);
}

export async function apiMessageGroup(groupId: string, text: string, fromBotId?: string): Promise<GroupReply[]> {
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

/**
 * Last-message preview for one group. Returns null when the group has no
 * messages or the detail fetch fails — callers render a member-based
 * fallback instead.
 */
export async function getGroupPreview(groupId: string): Promise<GroupPreview | null> {
  try {
    const { messages } = await apiGetGroupDetail(groupId);
    if (messages.length === 0) return null;
    const last = messages[messages.length - 1];
    return { groupId, text: last.text, created_at: last.created_at };
  } catch {
    return null;
  }
}
