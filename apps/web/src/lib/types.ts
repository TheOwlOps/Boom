/**
 * Shared contract types for the Boom web UI.
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
