// MessageParts.tsx — chat message rendering primitives for Boom desktop UI.
// Staged: message-parts/components/MessageParts.tsx  |  Mirrors: apps/desktop/src/components/MessageParts.tsx
// Integration: pnpm add react-markdown remark-gfm
// Animation classes (keyframes live in shared animations.css, NOT defined here):
//   msg-appear (message mount), toolcall-pulse (tool running state)
// Team-chat skin: row/avatar/name/time classes live in ../styles/teamchat.css
//   (prefix `tc-`). MessageBubble renders content only — the row chrome
//   (avatar, sender name, timestamp) is owned by ChatView.
// Security: no dangerouslySetInnerHTML. react-markdown runs without rehype-raw,
// so raw HTML in model output is escaped, never injected.

import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Check, CheckCircle2, Copy, Loader2, RotateCcw, XCircle } from 'lucide-react';
import type { ChatMessage } from '../lib/types';

const MONO = "'JetBrains Mono', monospace";
const BODY = "'Work Sans', sans-serif";

const ghostBtn: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 6, background: 'transparent',
  border: '1px solid var(--border)', borderRadius: 8, padding: '4px 10px',
  fontSize: 12, fontFamily: BODY, color: 'var(--muted)', cursor: 'pointer',
};

/**
 * Code-block background resolves through --im-code-bg so older skins can
 * darken code inside bot messages; falls back to --surface-2 everywhere
 * else (callers that never set it).
 */
export function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      const ta = document.createElement('textarea'); // fallback for non-secure contexts
      ta.value = code;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    setCopied(true);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1500);
  }, [code]);

  return (
    <div style={{ background: 'var(--im-code-bg, var(--surface-2))', border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden', margin: '8px 0' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 12px', borderBottom: '1px solid var(--border)' }}>
        <span style={{ fontSize: 12, fontFamily: MONO, color: 'var(--muted)' }}>{lang ?? 'code'}</span>
        <button
          type="button" onClick={copy}
          aria-label={copied ? 'Copied to clipboard' : 'Copy code'}
          style={{ ...ghostBtn, color: copied ? 'var(--success)' : 'var(--muted)', borderColor: copied ? 'var(--success)' : 'var(--border)' }}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre style={{ margin: 0, padding: 12, overflowX: 'auto', fontFamily: MONO, fontSize: 13, lineHeight: 1.6, color: 'var(--text)' }}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

// react-markdown overrides: fenced blocks -> CodeBlock, inline code -> mono chip, external links -> new tab.
const mdComponents: Components = {
  pre({ children }) { return <>{children}</>; }, // CodeBlock renders its own <pre>
  code({ className, children }) {
    const match = /language-([\w-]+)/.exec(className ?? '');
    const raw = String(children ?? '').replace(/\n$/, '');
    if (!match && !raw.includes('\n')) {
      return (
        <code className={className} style={{ fontFamily: MONO, fontSize: '0.85em', background: 'var(--im-code-bg, var(--surface-2))', padding: '2px 6px', borderRadius: 6 }}>
          {children}
        </code>
      );
    }
    return <CodeBlock code={raw} lang={match?.[1]} />;
  },
  a({ href, children }) {
    return <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)' }}>{children}</a>;
  },
};

/**
 * Team-chat message content (no chrome).
 *  - user: plain pre-wrapped text.
 *  - assistant: markdown rendered inline.
 * The row (avatar, sender name, timestamp) is rendered by ChatView.
 */
export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';
  return isUser ? (
    <div className="tc-user-text">{message.text}</div>
  ) : (
    <div className="tc-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
        {message.text}
      </ReactMarkdown>
    </div>
  );
}

type ToolStatus = 'running' | 'done' | 'error';

export function ToolCallCard({ name, status, detail }: { name: string; status: ToolStatus; detail?: string }) {
  const icon = status === 'running'
    ? <Loader2 size={14} className="toolcall-pulse" style={{ color: 'var(--warning)', flexShrink: 0 }} />
    : status === 'done'
      ? <CheckCircle2 size={14} style={{ color: 'var(--success)', flexShrink: 0 }} />
      : <XCircle size={14} style={{ color: 'var(--danger)', flexShrink: 0 }} />;
  const label = status === 'running' ? 'Running' : status === 'done' ? 'Done' : 'Failed';

  return (
    <div role="status" aria-label={`Tool ${name}: ${label}`}
      style={{ display: 'flex', alignItems: 'center', gap: 8, background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 8, padding: '6px 10px', margin: '6px 0', fontSize: 13, fontFamily: BODY, color: 'var(--muted)', maxWidth: '100%' }}>
      {icon}
      <span style={{ fontFamily: MONO, color: 'var(--text)', flexShrink: 0 }}>{name}</span>
      <span style={{ flexShrink: 0 }}>{label}</span>
      {detail && <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{detail}</span>}
    </div>
  );
}

export function ErrorCard({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert"
      style={{ display: 'flex', alignItems: 'center', gap: 10, background: 'rgba(239, 68, 68, 0.08)', border: '1px solid rgba(239, 68, 68, 0.35)', borderRadius: 10, padding: '10px 14px', margin: '8px 0' }}>
      <XCircle size={16} style={{ color: 'var(--danger)', flexShrink: 0 }} />
      <span style={{ flex: 1, fontSize: 14, fontFamily: BODY, lineHeight: 1.5, color: 'var(--text)' }}>{message}</span>
      {onRetry && (
        <button type="button" onClick={onRetry}
          style={{ ...ghostBtn, borderColor: 'rgba(239, 68, 68, 0.5)', color: 'var(--danger)', fontSize: 13, padding: '6px 12px', flexShrink: 0 }}>
          <RotateCcw size={13} />
          Retry
        </button>
      )}
    </div>
  );
}
