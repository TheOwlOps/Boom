/// <reference types="vite/client" />
/**
 * Thin HTTP client for the Boom gateway API (`crates/boom-core (Rust)`, see API_CONTRACT.md).
 * No retries, no caching — failures surface honestly.
 * Every response is validated at runtime (unknown + narrowing); wrong shapes throw.
 * Transport failures (network errors, timeouts) and non-2xx responses throw
 * `BackendUnreachableError` — components should detect it with
 * `isBackendUnreachableError()` and render a fallback instead of crashing.
 */
import type {
  Bot, BotDraft, ChannelState, ChannelStatus, ChatReply, ChatRequest, ChatStatus, DagNode, DagNodeStatus, Health, Session,
} from './types';
import type { MarketplaceItem } from '../data/marketplace';

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:18080';
/** Default per-request timeout: fail fast when the backend is not reachable. */
const DEFAULT_TIMEOUT_MS = 10_000;
/** Chat (SSE) needs a longer budget — the model streams for a while. */
const CHAT_TIMEOUT_MS = 120_000;

/**
 * Base message for every backend-outage failure. The desktop app spawns the
 * backend itself, so the fix is a restart — not a shell command.
 */
export const BACKEND_UNREACHABLE_MESSAGE =
  'Boom backend is not running. Please restart the app — it starts automatically.';

/**
 * Help text for backend-outage failures. The frontend always runs inside
 * Tauri now; kept as a function so existing imports keep working.
 */
export function getBackendHelpText(): string {
  return BACKEND_UNREACHABLE_MESSAGE;
}

/**
 * Thrown for every failure to get a usable response from the backend:
 * network errors (connection refused, DNS), request timeouts, and
 * non-2xx HTTP responses. Catch it with `isBackendUnreachableError()`.
 */
export class BackendUnreachableError extends Error {
  /** Extra context (HTTP status line, timeout length) — informational only. */
  readonly detail?: string;

  constructor(detail?: string) {
    const base = getBackendHelpText();
    super(detail ? `${base} (${detail})` : base);
    this.name = 'BackendUnreachableError';
    this.detail = detail;
  }
}

/** Type guard so components can react to backend-outage failures specifically. */
export function isBackendUnreachableError(err: unknown): err is BackendUnreachableError {
  return err instanceof BackendUnreachableError;
}

/**
 * Fetch with an AbortController timeout.
 * Transport failures are translated into BackendUnreachableError:
 * - network errors (backend down, connection refused, DNS failure)
 * - AbortError raised by our own timeout
 */
async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      throw new BackendUnreachableError(`request timed out after ${timeoutMs / 1000}s`);
    }
    throw new BackendUnreachableError(err instanceof Error ? err.message : String(err));
  } finally {
    clearTimeout(timer);
  }
}

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

/** GET `path`, validate the JSON body with `parse`. Failures throw BackendUnreachableError. */
async function getJson<T>(path: string, parse: (v: unknown) => T): Promise<T> {
  const res = await fetchWithTimeout(`${API_BASE}${path}`, {}, DEFAULT_TIMEOUT_MS);
  if (!res.ok) throw new BackendUnreachableError(await readErrorMessage(res));
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

/**
 * Normalize a raw backend channel state to a ChannelState. Accepts the new
 * contract's `status` string ("connected" | "disconnected" | "not-configured" |
 * "unsupported", separators may be `-` or `_`, case-insensitive) and falls
 * back to the legacy `connected` boolean. Anything unrecognized becomes
 * 'unknown' — never throws, so one bad entry can't blank the whole list.
 */
function parseChannelState(v: unknown): ChannelState {
  if (typeof v === 'string') {
    switch (v.trim().toLowerCase().replace(/[_ ]/g, '-')) {
      case 'connected': return 'connected';
      case 'disconnected': return 'disconnected';
      case 'not-configured': case 'notconfigured': case 'unconfigured': return 'not-configured';
      case 'unsupported': return 'unsupported';
    }
  }
  if (typeof v === 'boolean') return v ? 'connected' : 'disconnected';
  return 'unknown';
}

/**
 * Strip placeholder/dead-text the old backend used to leak through `detail`
 * (e.g. "gateway daemon not wired"). Those strings must never reach the UI —
 * the UI renders its own honest state labels instead.
 */
const PLACEHOLDER_DETAILS = [/gateway daemon not wired/i];
function cleanChannelDetail(detail: string | null): string | null {
  if (detail === null) return null;
  const trimmed = detail.trim();
  if (trimmed === '' || PLACEHOLDER_DETAILS.some((re) => re.test(trimmed))) return null;
  return trimmed;
}

function parseChannelEntry(c: unknown): ChannelStatus {
  if (!isRecord(c)) throw new Error('malformed response: channel entry');
  const name = reqString(c.name ?? c.id, 'channel.name');
  const id = typeof c.id === 'string' && c.id.trim() !== '' ? c.id : name;
  const state = 'status' in c ? parseChannelState(c.status) : parseChannelState(c.connected);
  return {
    id,
    name,
    state,
    detail: cleanChannelDetail(nullableString(c.detail ?? null, 'channel.detail')),
    // Legacy compat: mirror the old boolean so older consumers keep working.
    connected: state === 'connected' || (typeof c.connected === 'boolean' ? c.connected : false),
  };
}

/**
 * Parse GET /api/channels (new contract) and GET /api/channels/status
 * (legacy contract): { channels: [...] }.
 */
function parseChannels(v: unknown): ChannelStatus[] {
  if (!isRecord(v) || !Array.isArray(v.channels)) throw new Error('malformed response: /api/channels');
  return v.channels.map(parseChannelEntry);
}

/** Result of POST /api/channels/:id/test. */
export interface ChannelTestResult {
  ok: boolean;
  message: string | null;
}

function parseChannelTestResult(v: unknown): ChannelTestResult {
  if (!isRecord(v)) throw new Error('malformed response: /api/channels/:id/test');
  const ok = typeof v.ok === 'boolean' ? v.ok : typeof v.success === 'boolean' ? v.success : true;
  const raw = typeof v.message === 'string' ? v.message : typeof v.detail === 'string' ? v.detail : null;
  return { ok, message: cleanChannelDetail(raw) };
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
/**
 * Fetch the channel list. Tries the new contract (GET /api/channels) first
 * and falls back to the legacy GET /api/channels/status during the backend
 * transition — both are parsed by the same tolerant parser.
 */
export async function getChannels(): Promise<ChannelStatus[]> {
  try {
    return await getJson('/api/channels', parseChannels);
  } catch (firstErr) {
    try {
      return await getJson('/api/channels/status', parseChannels);
    } catch {
      throw firstErr; // surface the new-contract error when both fail
    }
  }
}

/**
 * Save credentials and connect a channel.
 * Backend contract: POST /api/channels/:id/connect, body { token? }.
 * Non-2xx responses throw BackendUnreachableError carrying the server's
 * real `{"error"}` message — callers should display it verbatim.
 */
export function connectChannel(id: string, token?: string): Promise<void> {
  const payload: Record<string, string> = {};
  if (token !== undefined && token.trim() !== '') payload.token = token.trim();
  return sendJson('POST', `/api/channels/${encodeURIComponent(id)}/connect`, () => undefined, payload);
}

/** Disconnect a channel: POST /api/channels/:id/disconnect. */
export function disconnectChannel(id: string): Promise<void> {
  return sendJson('POST', `/api/channels/${encodeURIComponent(id)}/disconnect`, () => undefined);
}

/** Send a test message through a channel: POST /api/channels/:id/test. */
export function testChannel(id: string): Promise<ChannelTestResult> {
  return sendJson('POST', `/api/channels/${encodeURIComponent(id)}/test`, parseChannelTestResult);
}

/* ---------------- Messenger Facebook OAuth ------------------------------ */

export interface MessengerOAuthConfigStatus {
  configured: boolean;
  app_id: string | null;
}

function parseMessengerOAuthConfig(v: unknown): MessengerOAuthConfigStatus {
  if (!isRecord(v)) throw new Error('malformed response: messenger oauth config');
  return {
    configured: v.configured === true,
    app_id: typeof v.app_id === 'string' ? v.app_id : null,
  };
}

/** GET /api/channels/messenger/oauth/config — app_id is public, the secret is never returned. */
export function getMessengerOAuthConfig(): Promise<MessengerOAuthConfigStatus> {
  return getJson('/api/channels/messenger/oauth/config', parseMessengerOAuthConfig);
}

/** POST /api/channels/messenger/oauth/config — save the Facebook App credentials. */
export function saveMessengerOAuthConfig(appId: string, appSecret: string): Promise<void> {
  return sendJson('POST', '/api/channels/messenger/oauth/config', () => undefined, {
    app_id: appId.trim(),
    app_secret: appSecret,
  });
}

/** GET /api/channels/messenger/oauth/start → the Facebook Login URL to open in the browser. */
export async function getMessengerOAuthUrl(): Promise<string> {
  const v = await getJson<unknown>('/api/channels/messenger/oauth/start', (x) => x);
  if (!isRecord(v) || typeof v.url !== 'string' || !v.url) {
    throw new Error('malformed response: messenger oauth start');
  }
  return v.url;
}
export function getDag(): Promise<DagNode[]> { return getJson('/api/dag/status', parseDag); }

/* ---------------- Custom bots (Grok-style) -------------------------------- */

function parseBot(v: unknown): Bot {
  if (!isRecord(v)) throw new Error('malformed response: bot entry');
  return {
    id: reqString(v.id, 'bot.id'),
    name: reqString(v.name, 'bot.name'),
    description: typeof v.description === 'string' ? v.description : '',
    system_prompt: typeof v.system_prompt === 'string' ? v.system_prompt : '',
    avatar_emoji: typeof v.avatar_emoji === 'string' ? v.avatar_emoji : '',
    allowed_tools: isStringArray(v.allowed_tools) ? v.allowed_tools : [],
    denied_tools: isStringArray(v.denied_tools) ? v.denied_tools : [],
    channel_bindings: isStringArray(v.channel_bindings) ? v.channel_bindings : [],
    is_active: v.is_active === true,
  };
}

/** Parse `GET /api/bots` -> `{bots: Bot[]}`. */
function parseBots(v: unknown): Bot[] {
  if (!isRecord(v) || !Array.isArray(v.bots)) throw new Error('malformed response: /api/bots');
  return v.bots.map(parseBot);
}

/** Parse `{bot: Bot}` bodies returned by POST/PUT /api/bots[/:id]. */
function parseBotBody(v: unknown, path: string): Bot {
  if (!isRecord(v) || !isRecord(v.bot)) throw new Error(`malformed response: ${path}`);
  return parseBot(v.bot);
}

/**
 * JSON request with a body (POST/PUT/DELETE). Validates the response with
 * `parse`. Failures throw BackendUnreachableError. Mirrors getJson conventions.
 */
async function sendJson<T>(method: 'POST' | 'PUT' | 'DELETE', path: string, parse: (v: unknown) => T, payload?: unknown): Promise<T> {
  const res = await fetchWithTimeout(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  }, DEFAULT_TIMEOUT_MS);
  if (!res.ok) throw new BackendUnreachableError(await readErrorMessage(res));
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error(`malformed response: ${path} did not return JSON`);
  }
  return parse(body);
}

export function listBots(): Promise<Bot[]> {
  return getJson('/api/bots', parseBots);
}

export function createBot(draft: BotDraft): Promise<Bot> {
  return sendJson('POST', '/api/bots', (v) => parseBotBody(v, 'POST /api/bots'), draft);
}

export function updateBot(id: string, draft: BotDraft): Promise<Bot> {
  return sendJson('PUT', `/api/bots/${encodeURIComponent(id)}`, (v) => parseBotBody(v, 'PUT /api/bots/:id'), draft);
}

export function deleteBot(id: string): Promise<void> {
  return sendJson('DELETE', `/api/bots/${encodeURIComponent(id)}`, () => undefined);
}

export function setActiveBot(id: string): Promise<void> {
  return sendJson('POST', '/api/bots/active', () => undefined, { bot_id: id });
}

/* ---------------- Marketplace ------------------------------------------- */

function parseMarketplaceKind(v: unknown): MarketplaceItem['kind'] {
  if (v === 'plugin' || v === 'tool' || v === 'bot') return v;
  throw new Error('malformed response: marketplace item.kind must be "plugin", "tool" or "bot"');
}

function parseMarketplaceItem(v: unknown): MarketplaceItem {
  if (!isRecord(v)) throw new Error('malformed response: marketplace item entry');
  return {
    id: reqString(v.id, 'marketplace item.id'),
    name: reqString(v.name, 'marketplace item.name'),
    icon: reqString(v.icon, 'marketplace item.icon'),
    description: reqString(v.description, 'marketplace item.description'),
    category: reqString(v.category, 'marketplace item.category'),
    featured: v.featured === true ? true : undefined,
    kind: parseMarketplaceKind(v.kind),
  };
}

/** Parse `GET /api/marketplace` -> `{items: MarketplaceItem[]}`. */
function parseMarketplace(v: unknown): MarketplaceItem[] {
  if (!isRecord(v) || !Array.isArray(v.items)) throw new Error('malformed response: /api/marketplace');
  return v.items.map(parseMarketplaceItem);
}

/**
 * Parse `GET /api/marketplace/installed`.
 * Accepts `{installed: string[]}` (primary) or `{ids: string[]}`.
 */
function parseMarketplaceInstalled(v: unknown): string[] {
  if (!isRecord(v)) throw new Error('malformed response: /api/marketplace/installed');
  if (isStringArray(v.installed)) return v.installed;
  if (isStringArray(v.ids)) return v.ids;
  throw new Error('malformed response: /api/marketplace/installed must carry an installed/ids string[]');
}

export function getMarketplace(): Promise<MarketplaceItem[]> {
  return getJson('/api/marketplace', parseMarketplace);
}

export function getMarketplaceInstalled(): Promise<string[]> {
  return getJson('/api/marketplace/installed', parseMarketplaceInstalled);
}

export function installMarketplaceItem(id: string): Promise<void> {
  return sendJson('POST', '/api/marketplace/install', () => undefined, { id });
}

export function uninstallMarketplaceItem(id: string): Promise<void> {
  return sendJson('POST', '/api/marketplace/uninstall', () => undefined, { id });
}

/* ---------------- Provider setup wizard ----------------------------------- */

/**
 * Backend contract (crates/boom-core (Rust), extends webui API_CONTRACT.md):
 *   POST /api/providers/config -> { provider_id, api_key, model, base_url? } -> { ok: true }
 *   POST /api/providers/test   -> { provider_id } -> { ok: boolean, message: string }
 *
 * Keys are only ever sent to the backend; the client never persists them
 * (localStorage included).
 */
export interface ProviderConfigDraft {
  /** API key for the provider; omit when the provider needs no key (Ollama). */
  apiKey?: string;
  /** Model id the wizard picked for this provider. */
  model?: string;
  /** Custom base URL (OpenAI-compatible endpoints only). */
  baseUrl?: string;
}

function parseOkFlag(v: unknown, path: string): void {
  if (!isRecord(v) || v.ok !== true) throw new Error(`malformed response: ${path}`);
}

/** Save the wizard's provider choice (writes ~/.boom/providers.json 0600). */
export function saveProviderConfig(providerId: string, draft: ProviderConfigDraft): Promise<void> {
  return sendJson('POST', '/api/providers/config',
    (v) => parseOkFlag(v, 'POST /api/providers/config'),
    {
      provider_id: providerId,
      api_key: draft.apiKey,
      model: draft.model,
      base_url: draft.baseUrl,
    });
}

export interface ProviderTestResult {
  ok: boolean;
  message: string | null;
}

function parseProviderTest(v: unknown): ProviderTestResult {
  if (!isRecord(v)) throw new Error('malformed response: POST /api/providers/test');
  if (typeof v.ok !== 'boolean') throw new Error('malformed response: POST /api/providers/test — ok must be a boolean');
  return { ok: v.ok, message: typeof v.message === 'string' ? v.message : null };
}

/**
 * Test the currently saved config for `providerId`.
 * Call `saveProviderConfig()` first — the backend tests what it has stored.
 */
export function testProviderConnection(providerId: string): Promise<ProviderTestResult> {
  return sendJson('POST', '/api/providers/test', parseProviderTest, { provider_id: providerId });
}

/* ---------------- Provider-not-configured detection --------------------- */

/**
 * Exact `error` string the gateway returns from POST /api/chat when the
 * backend runs degraded because no AI provider key is configured
 * (`self._error(503, "agent brain not configured")` in crates/boom-core (Rust) —
 * see `build_real_api`). `readErrorMessage()` keeps it intact on
 * `BackendUnreachableError.detail`.
 */
export const PROVIDER_NOT_CONFIGURED_ERROR = 'agent brain not configured';

/**
 * True when `err` is the backend honestly reporting that chat cannot run
 * because no AI provider is configured (HTTP 503). The chat view uses this
 * to show a gentle "open Settings" prompt instead of the generic
 * backend-unreachable error.
 */
export function isProviderNotConfiguredError(err: unknown): err is BackendUnreachableError {
  return (
    isBackendUnreachableError(err) &&
    err.detail === PROVIDER_NOT_CONFIGURED_ERROR
  );
}

/* ---------------- Zalo Personal ----------------------------------------- */

/**
 * Backend contract (crates/boom-core (Rust), extends webui API_CONTRACT.md):
 *   GET  /api/channels/zalo-personal/status -> { connected: boolean, phone_masked: string | null }
 *   POST /api/channels/zalo-personal/login  -> { phone, password } -> { connected: boolean, phone_masked: string | null }
 *   POST /api/channels/zalo-personal/logout -> { ok: true }
 *
 * Zalo Personal uses an unofficial API — credentials are only ever sent over
 * this call; the client never stores them (localStorage included).
 */
export interface ZaloPersonalStatus {
  /** Whether the personal Zalo account is currently connected. */
  connected: boolean;
  /** Masked phone number (e.g. "09***1234"), or null when not connected. */
  phoneMasked: string | null;
}

/**
 * Parse GET /api/channels/zalo-personal/status and
 * POST /api/channels/zalo-personal/login responses.
 * `phone_masked` is optional — absent/null means "not masked by the backend".
 */
function parseZaloPersonalStatus(v: unknown, path: string): ZaloPersonalStatus {
  if (!isRecord(v)) throw new Error(`malformed response: ${path}`);
  if (typeof v.connected !== 'boolean') throw new Error(`malformed response: ${path} — connected must be a boolean`);
  return {
    connected: v.connected,
    phoneMasked: v.phone_masked === undefined ? null : nullableString(v.phone_masked, 'zalo-personal.phone_masked'),
  };
}

export function zaloPersonalStatus(): Promise<ZaloPersonalStatus> {
  return getJson('/api/channels/zalo-personal/status', (v) =>
    parseZaloPersonalStatus(v, 'GET /api/channels/zalo-personal/status'));
}

export function zaloPersonalLogin(phone: string, password: string): Promise<ZaloPersonalStatus> {
  return sendJson('POST', '/api/channels/zalo-personal/login',
    (v) => parseZaloPersonalStatus(v, 'POST /api/channels/zalo-personal/login'),
    { phone, password });
}

export function zaloPersonalLogout(): Promise<void> {
  return sendJson('POST', '/api/channels/zalo-personal/logout', () => undefined);
}

/* ---------------- Ollama ----------------------------------------------- */

/**
 * Backend contract (crates/boom-core (Rust), extends webui API_CONTRACT.md):
 *   GET /api/providers/ollama/status -> { running: boolean, models: string[] }
 *
 * Always answers 200 — `running: false` means Ollama is not installed,
 * not running, or unreachable on localhost:11434.
 */
export interface OllamaStatus {
  /** Whether the local Ollama daemon answered. */
  running: boolean;
  /** Installed model names (e.g. ["llama3.1", "codellama:7b"]). */
  models: string[];
}

function parseOllamaStatus(v: unknown): OllamaStatus {
  const path = 'GET /api/providers/ollama/status';
  if (!isRecord(v)) throw new Error(`malformed response: ${path}`);
  if (typeof v.running !== 'boolean') throw new Error(`malformed response: ${path} — running must be a boolean`);
  if (!isStringArray(v.models)) throw new Error(`malformed response: ${path} — models must be string[]`);
  return { running: v.running, models: v.models };
}

export function getOllamaStatus(): Promise<OllamaStatus> {
  return getJson('/api/providers/ollama/status', parseOllamaStatus);
}

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

/**
 * POST /api/chat (SSE stream). Chat keeps its own 120s budget — a 10s default
 * would kill legitimate long model responses. Network/timeout/HTTP failures
 * throw BackendUnreachableError (the timeout detail says "after 120s").
 */
export async function postChat(req: ChatRequest, onStatus?: (s: ChatStatus) => void): Promise<ChatReply> {
  const res = await fetchWithTimeout(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(req),
  }, CHAT_TIMEOUT_MS);
  if (!res.ok) throw new BackendUnreachableError(await readErrorMessage(res));
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
}
