/// <reference types="vite/client" />
/**
 * Thin HTTP client for the Boom gateway API (`gateway/api.py`, see API_CONTRACT.md).
 * No retries, no caching — failures surface honestly.
 * Every response is validated at runtime (unknown + narrowing); wrong shapes throw.
 * HTTP errors throw `Error` with the server's `{"error"}` message when present.
 */
import type {
  ChannelStatus, ChatReply, ChatRequest, ChatStatus, DagNode, DagNodeStatus, Health, Session,
} from './types';

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:18080';
const CHAT_TIMEOUT_MS = 120_000;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}
function isStringArray(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((d): d is string => typeof d === 'string');
}
function reqString(v: unknown, field: string): string {
  if (typeof v !== 'string') throw new Error(`malformed response: ${field} must be a string`);
  return v;
}
function nullableString(v: unknown, field: string): string | null {
  return v === null ? null : reqString(v, field);
}

/** Extract the server's `{"error"}` message for a failed response. */
async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (isRecord(body) && typeof body.error === 'string') return body.error;
  } catch { /* body was not JSON */ }
  return `HTTP ${res.status} ${res.statusText}`.trim();
}

/** GET `path`, validate the JSON body with `parse`, throw on HTTP/shape errors. */
async function getJson<T>(path: string, parse: (v: unknown) => T): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(await readErrorMessage(res));
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error(`malformed response: ${path} did not return JSON`);
  }
  return parse(body);
}

function parseErrors(v: unknown, field: string): Record<string, string> {
  if (!isRecord(v)) throw new Error(`malformed response: ${field} must be an object`);
  const out: Record<string, string> = {};
  for (const [k, e] of Object.entries(v)) {
    if (typeof e !== 'string') throw new Error(`malformed response: ${field}["${k}"] must be a string`);
    out[k] = e;
  }
  return out;
}

function parseHealth(v: unknown): Health {
  if (!isRecord(v)) throw new Error('malformed response: /api/health');
  return { status: reqString(v.status, 'status'), version: reqString(v.version, 'version'), model: reqString(v.model, 'model') };
}

function parseSessions(v: unknown): Session[] {
  if (!isRecord(v) || !Array.isArray(v.sessions)) throw new Error('malformed response: /api/sessions');
  return v.sessions.map((s: unknown) => {
    if (!isRecord(s)) throw new Error('malformed response: session entry');
    return {
      id: reqString(s.id, 'session.id'),
      title: nullableString(s.title, 'session.title'),
      updated_at: nullableString(s.updated_at, 'session.updated_at'),
    };
  });
}

function parseChannels(v: unknown): ChannelStatus[] {
  if (!isRecord(v) || !Array.isArray(v.channels)) throw new Error('malformed response: /api/channels/status');
  return v.channels.map((c: unknown) => {
    if (!isRecord(c)) throw new Error('malformed response: channel entry');
    if (typeof c.connected !== 'boolean') throw new Error('malformed response: channel.connected must be a boolean');
    return { name: reqString(c.name, 'channel.name'), connected: c.connected, detail: nullableString(c.detail, 'channel.detail') };
  });
}

function parseDagStatus(v: unknown): DagNodeStatus {
  const s = reqString(v, 'dag node.status');
  switch (s) {
    case 'idle': case 'running': case 'completed': case 'failed': case 'blocked': return s;
    default: throw new Error(`malformed response: unknown dag node status "${s}"`);
  }
}

function parseDag(v: unknown): DagNode[] {
  if (!isRecord(v) || !Array.isArray(v.nodes)) throw new Error('malformed response: /api/dag/status');
  return v.nodes.map((n: unknown) => {
    if (!isRecord(n)) throw new Error('malformed response: dag node entry');
    if (!isStringArray(n.dependencies)) throw new Error('malformed response: dag node.dependencies must be string[]');
    return {
      id: reqString(n.id, 'dag node.id'),
      name: reqString(n.name, 'dag node.name'),
      status: parseDagStatus(n.status),
      dependencies: n.dependencies,
    };
  });
}

function parseChatReply(v: unknown): ChatReply {
  if (!isRecord(v)) throw new Error('malformed response: /api/chat');
  return {
    reply: reqString(v.reply, 'reply'),
    session_id: reqString(v.session_id, 'session_id'),
    strategy: reqString(v.strategy, 'strategy'),
    errors: parseErrors(v.errors, 'errors'),
  };
}

export function getHealth(): Promise<Health> { return getJson('/api/health', parseHealth); }
export function listSessions(): Promise<Session[]> { return getJson('/api/sessions', parseSessions); }
export function getChannels(): Promise<ChannelStatus[]> { return getJson('/api/channels/status', parseChannels); }
export function getDag(): Promise<DagNode[]> { return getJson('/api/dag/status', parseDag); }

interface SseState { text: string | null; sessionId: string; strategy: string; errors: Record<string, string> }

/** Handle one parsed SSE event. Returns true on terminal `done`; throws on `error`. */
function handleSseEvent(name: string, data: unknown, onStatus: ((s: ChatStatus) => void) | undefined, st: SseState): boolean {
  if (name === 'status') {
    if (isRecord(data) && typeof data.phase === 'string') onStatus?.({ phase: data.phase });
  } else if (name === 'message') {
    if (isRecord(data) && typeof data.text === 'string') st.text = data.text;
  } else if (name === 'done') {
    if (isRecord(data)) {
      if (typeof data.session_id === 'string') st.sessionId = data.session_id;
      if (typeof data.strategy === 'string') st.strategy = data.strategy;
      if (isRecord(data.errors)) st.errors = parseErrors(data.errors, 'errors');
    }
    return true;
  } else if (name === 'error') {
    throw new Error(isRecord(data) && typeof data.error === 'string' ? data.error : 'agent error');
  }
  return false;
}

/** Consume an SSE stream and resolve a ChatReply from its events. */
async function readSseReply(res: Response, req: ChatRequest, onStatus?: (s: ChatStatus) => void): Promise<ChatReply> {
  if (!res.body) throw new Error('server closed the stream');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const st: SseState = { text: null, sessionId: '', strategy: '', errors: {} };
  let eventName = '';
  let dataLines: string[] = [];
  let buffer = '';
  let finished = false;

  const dispatch = (): void => {
    const raw = dataLines.join('\n');
    dataLines = [];
    const name = eventName;
    eventName = '';
    if (name === '') return;
    let data: unknown;
    try {
      data = raw === '' ? undefined : (JSON.parse(raw) as unknown);
    } catch {
      throw new Error('malformed SSE event data');
    }
    if (handleSseEvent(name, data, onStatus, st)) finished = true;
  };

  try {
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const rawLine of lines) {
        const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
        if (line === '') dispatch();
        else if (line.startsWith('event:')) eventName = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.startsWith('data: ') ? line.slice(6) : line.slice(5));
        /* `: comments` and other lines are ignored */
      }
    }
    dispatch(); // trailing event without a final blank line
  } finally {
    try { await reader.cancel(); } catch { /* already closed */ }
    reader.releaseLock();
  }

  if (st.text === null && !finished) throw new Error('stream ended before the reply completed');
  return {
    reply: st.text ?? '',
    session_id: st.sessionId || req.session_id || '',
    strategy: st.strategy || req.strategy || 'auto',
    errors: st.errors,
  };
}

export async function postChat(req: ChatRequest, onStatus?: (s: ChatStatus) => void): Promise<ChatReply> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(req),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(await readErrorMessage(res));
    if ((res.headers.get('content-type') ?? '').includes('text/event-stream')) {
      return await readSseReply(res, req, onStatus);
    }
    let body: unknown;
    try {
      body = (await res.json()) as unknown;
    } catch {
      throw new Error('malformed response: /api/chat did not return JSON');
    }
    return parseChatReply(body);
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') throw new Error('chat request timed out after 120s');
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
