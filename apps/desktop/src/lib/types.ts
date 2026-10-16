/**
 * Shared contract types for the Boom desktop UI.
 * Shapes mirror `gateway/api.py` responses (see API_CONTRACT.md).
 * Do not add fields — every property must exist on the wire.
 */

export interface Health {
  status: string;
  version: string;
  model: string;
}

export type Strategy = 'auto' | 'react' | 'dag' | 'mass';

export interface ChatRequest {
  prompt: string;
  session_id?: string;
  strategy?: Strategy;
  bot_id?: string;
}

/** One custom bot (Grok-style). `allowed_tools: []` means "all tools". */
export interface Bot {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  avatar_emoji: string;
  allowed_tools: string[];
  denied_tools: string[];
  channel_bindings: string[];
  is_active: boolean;
}

/** Writable fields for create/update (id is server-assigned). */
export interface BotDraft {
  name: string;
  description: string;
  system_prompt: string;
  avatar_emoji: string;
  allowed_tools: string[];
  denied_tools: string[];
  channel_bindings: string[];
}

export interface ChatReply {
  reply: string;
  session_id: string;
  strategy: string;
  errors: Record<string, string>;
}

export interface Session {
  id: string;
  title: string | null;
  updated_at: string | null;
}

export interface ChannelStatus {
  name: string;
  connected: boolean;
  detail: string | null;
}

export type DagNodeStatus = 'idle' | 'running' | 'completed' | 'failed' | 'blocked';

export interface DagNode {
  id: string;
  name: string;
  status: DagNodeStatus;
  dependencies: string[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  strategy?: string;
  ts: number;
}

/** Payload of the SSE `event: status` emitted by POST /api/chat. */
export interface ChatStatus {
  phase: string;
}
