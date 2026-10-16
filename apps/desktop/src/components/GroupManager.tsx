/**
 * GroupManager — manage groups/channels registered in Boom.
 *
 * Reads GET /api/groups (backed by agent/src/business/groups.py GroupManager),
 * and exposes per-group actions:
 *   - mute / unmute  -> POST /api/groups/mute   {channel, group_id, muted}
 *   - edit policy    -> POST /api/groups/policy  {channel, group_id, policy}
 *
 * All responses are validated at runtime (unknown + narrowing, same pattern
 * as lib/api.ts); malformed shapes throw instead of rendering garbage.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bell,
  BellOff,
  RefreshCw,
  Search,
  ShieldCheck,
  Users,
  X,
} from "lucide-react";
import { API_BASE } from "../lib/api";
import { EmptyState } from "./EmptyState";

const REFRESH_MS = 30_000;

/* ------------------------------------------------------------------ types */

/** Command policy attached to a group (see business/groups.py). */
export interface GroupPolicy {
  allowed_commands: string[];
  denied_commands: string[];
  require_approval: string[];
}

/** One group row from GET /api/groups. `muted` is derived from settings.muted. */
export interface Group {
  channel: string;
  group_id: string;
  name: string;
  member_count: number;
  message_count: number;
  muted: boolean;
  last_active: string | null;
  policy: GroupPolicy;
}

/* ------------------------------------------------------- runtime validation */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function reqString(v: unknown, field: string): string {
  if (typeof v !== "string") throw new Error(`malformed response: ${field} must be a string`);
  return v;
}

function reqNumber(v: unknown, field: string): number {
  if (typeof v !== "number" || Number.isNaN(v))
    throw new Error(`malformed response: ${field} must be a number`);
  return v;
}

function nullableString(v: unknown, field: string): string | null {
  return v === null || v === undefined ? null : reqString(v, field);
}

/** Lenient string-list parser: non-array or non-string entries are dropped. */
function stringList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string");
}

/** Policy is best-effort: missing/invalid keys fall back to empty lists. */
function parsePolicy(v: unknown): GroupPolicy {
  if (!isRecord(v)) return { allowed_commands: [], denied_commands: [], require_approval: [] };
  return {
    allowed_commands: stringList(v.allowed_commands),
    denied_commands: stringList(v.denied_commands),
    require_approval: stringList(v.require_approval),
  };
}

function parseGroup(v: unknown): Group {
  if (!isRecord(v)) throw new Error("malformed response: group entry");
  const settings = isRecord(v.settings) ? v.settings : {};
  return {
    channel: reqString(v.channel, "group.channel"),
    group_id: reqString(v.group_id, "group.group_id"),
    name: reqString(v.name, "group.name"),
    member_count: reqNumber(v.member_count, "group.member_count"),
    message_count: reqNumber(v.message_count, "group.message_count"),
    muted: settings.muted === true,
    last_active: nullableString(v.last_active, "group.last_active"),
    policy: parsePolicy(v.policy),
  };
}

function parseGroupList(v: unknown): Group[] {
  if (!isRecord(v) || !Array.isArray(v.groups))
    throw new Error("malformed response: /api/groups");
  return v.groups.map(parseGroup);
}

/** Server error convention: {"error": "..."} or {"ok": false, "error": "..."}. */
async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body: unknown = (await res.json()) as unknown;
    if (isRecord(body) && typeof body.error === "string") return body.error;
    if (isRecord(body) && body.ok === false && typeof body.error === "string") return body.error;
  } catch {
    /* body was not JSON */
  }
  return `HTTP ${res.status} ${res.statusText}`.trim();
}

function throwIfNotOk(res: Response, body: unknown): void {
  if (isRecord(body) && body.ok === false) {
    throw new Error(typeof body.error === "string" ? body.error : "request failed");
  }
}

async function postJson(path: string, payload: unknown): Promise<unknown> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error(`malformed response: ${path} did not return JSON`);
  }
  if (!res.ok) throw new Error(await readErrorMessage(res));
  throwIfNotOk(res, body);
  return body;
}

/* ------------------------------------------------------------------ API */

export async function getGroups(): Promise<Group[]> {
  const res = await fetch(`${API_BASE}/api/groups`);
  if (!res.ok) throw new Error(await readErrorMessage(res));
  let body: unknown;
  try {
    body = (await res.json()) as unknown;
  } catch {
    throw new Error("malformed response: /api/groups did not return JSON");
  }
  return parseGroupList(body);
}

export async function setGroupMuted(
  channel: string,
  group_id: string,
  muted: boolean,
): Promise<void> {
  await postJson("/api/groups/mute", { channel, group_id, muted });
}

export async function setGroupPolicy(
  channel: string,
  group_id: string,
  policy: GroupPolicy,
): Promise<void> {
  await postJson("/api/groups/policy", { channel, group_id, policy });
}

/* ------------------------------------------------------------------ icons */

/** Channel -> emoji, matching the backend channel adapters. */
const CHANNEL_EMOJI: Record<string, string> = {
  telegram: "📨",
  discord: "🎮",
  slack: "💬",
  zalo: "💙",
  line: "💚",
  whatsapp: "📞",
  messenger: "💭",
  feishu: "📘",
};

function channelEmoji(channel: string): string {
  return CHANNEL_EMOJI[channel.toLowerCase()] ?? "🏷️";
}

/* --------------------------------------------------------------- sub views */

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading groups" className="flex flex-col gap-2">
      {[0, 1, 2].map((i) => (
        <div key={i} className="skeleton-shimmer h-20 rounded-lg bg-neutral-800" />
      ))}
    </div>
  );
}

function ErrorCard({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="flex items-center justify-between gap-2 rounded-lg border border-neutral-800 bg-zinc-950/40 px-4 py-3 text-sm text-neutral-200"
    >
      <span className="truncate" title={message}>
        {message || "Couldn't reach backend"}
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-xs text-neutral-200 hover:bg-neutral-700"
      >
        <RefreshCw size={13} />
        Retry
      </button>
    </div>
  );
}

function StatusBadge({ muted }: { muted: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
        muted
          ? "bg-zinc-500/15 text-zinc-300"
          : "bg-zinc-500/15 text-zinc-300"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${muted ? "bg-zinc-400" : "bg-zinc-400"}`}
        aria-hidden
      />
      {muted ? "muted" : "active"}
    </span>
  );
}

function formatCount(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
}

/* ------------------------------------------------------------ policy modal */

/** Policy editor modal: toggle allow/deny command chips, add new commands. */
function PolicyModal({ group, onClose, onSaved }: {
  group: Group;
  onClose: () => void;
  onSaved: (policy: GroupPolicy) => void;
}) {
  const [allowed, setAllowed] = useState<string[]>(group.policy.allowed_commands);
  const [denied, setDenied] = useState<string[]>(group.policy.denied_commands);
  const [newAllowed, setNewAllowed] = useState("");
  const [newDenied, setNewDenied] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const removeFrom = (list: string[], cmd: string, set: (l: string[]) => void) =>
    set(list.filter((c) => c !== cmd));

  const addTo = (list: string[], raw: string, set: (l: string[]) => void, clear: () => void) => {
    const cmd = raw.trim().replace(/^\//, "").toLowerCase();
    if (!cmd || list.includes(cmd)) {
      clear();
      return;
    }
    set([...list, cmd]);
    clear();
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    const policy: GroupPolicy = {
      allowed_commands: allowed,
      denied_commands: denied,
      // Preserve require_approval untouched — this editor only manages allow/deny.
      require_approval: group.policy.require_approval,
    };
    try {
      await setGroupPolicy(group.channel, group.group_id, policy);
      onSaved(policy);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to save policy");
    } finally {
      setSaving(false);
    }
  };

  const chipRow = (
    label: string,
    list: string[],
    set: (l: string[]) => void,
    input: string,
    setInput: (s: string) => void,
    accent: string,
  ) => (
    <div>
      <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-neutral-400">
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {list.length === 0 && (
          <span className="text-xs text-neutral-600">none</span>
        )}
        {list.map((cmd) => (
          <button
            key={cmd}
            type="button"
            title={`Remove /${cmd}`}
            onClick={() => removeFrom(list, cmd, set)}
            className={`group inline-flex cursor-pointer items-center gap-1 rounded-md border px-2 py-1 font-mono text-xs ${accent}`}
          >
            /{cmd}
            <X size={12} className="opacity-40 group-hover:opacity-100" />
          </button>
        ))}
      </div>
      <div className="mt-2 flex gap-1.5">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") addTo(list, input, set, () => setInput(""));
          }}
          placeholder="add command…"
          className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1 font-mono text-xs text-neutral-200 placeholder:text-neutral-600 focus:border-neutral-500 focus:outline-none"
        />
        <button
          type="button"
          onClick={() => addTo(list, input, set, () => setInput(""))}
          className="shrink-0 cursor-pointer rounded-md border border-neutral-700 bg-neutral-800 px-2 py-1 text-xs text-neutral-300 hover:bg-neutral-700"
        >
          Add
        </button>
      </div>
    </div>
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Policy for ${group.name}`}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-lg bg-neutral-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-neutral-100">
              {channelEmoji(group.channel)} {group.name}
            </h3>
            <p className="mt-0.5 font-mono text-xs text-neutral-400">
              {group.channel} · {group.group_id}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="cursor-pointer rounded-md p-1 text-neutral-500 hover:bg-neutral-800 hover:text-neutral-300"
          >
            <X size={16} />
          </button>
        </div>

        <div className="mt-3 flex flex-1 flex-col gap-4 overflow-auto">
          {chipRow(
            "Allowed commands",
            allowed,
            setAllowed,
            newAllowed,
            setNewAllowed,
            "border-zinc-800 bg-zinc-950/50 text-zinc-300 hover:border-zinc-600",
          )}
          {chipRow(
            "Denied commands",
            denied,
            setDenied,
            newDenied,
            setNewDenied,
            "border-zinc-800 bg-zinc-950/50 text-zinc-300 hover:border-zinc-600",
          )}
          <p className="text-xs leading-relaxed text-neutral-400">
            Denied commands always win. When an allow-list is non-empty, only
            listed commands are permitted. require_approval is left unchanged.
          </p>
          {error && (
            <div role="alert" className="rounded-md bg-zinc-950/50 px-3 py-2 text-xs text-zinc-300">
              {error}
            </div>
          )}
        </div>

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="cursor-pointer rounded-md border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={saving}
            className="cursor-pointer rounded-md bg-neutral-100 px-3 py-1.5 text-sm font-medium text-neutral-900 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save policy"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ main */

export default function GroupManager() {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [channelFilter, setChannelFilter] = useState("all");
  const [policyGroup, setPolicyGroup] = useState<Group | null>(null);
  const [pendingMute, setPendingMute] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const keyOf = (g: Group) => `${g.channel}:${g.group_id}`;

  const load = useCallback(async () => {
    try {
      const list = await getGroups();
      setGroups(list);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "failed to load groups");
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  const channels = useMemo(() => {
    const set = new Set<string>();
    for (const g of groups ?? []) set.add(g.channel);
    return [...set].sort();
  }, [groups]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (groups ?? []).filter((g) => {
      if (channelFilter !== "all" && g.channel !== channelFilter) return false;
      if (!q) return true;
      return (
        g.name.toLowerCase().includes(q) || g.group_id.toLowerCase().includes(q)
      );
    });
  }, [groups, query, channelFilter]);

  const toggleMute = async (g: Group) => {
    const key = keyOf(g);
    setPendingMute(key);
    setActionError(null);
    try {
      await setGroupMuted(g.channel, g.group_id, !g.muted);
      setGroups((prev) =>
        prev?.map((x) => (keyOf(x) === key ? { ...x, muted: !g.muted } : x)) ?? prev,
      );
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "mute action failed");
    } finally {
      setPendingMute(null);
    }
  };

  const onPolicySaved = (g: Group, policy: GroupPolicy) => {
    const key = keyOf(g);
    setGroups((prev) =>
      prev?.map((x) => (keyOf(x) === key ? { ...x, policy } : x)) ?? prev,
    );
  };

  /* ------------------------------------------------------------ states */

  if (groups === null) {
    if (loadError) return <ErrorCard message={loadError} onRetry={load} />;
    return <ListSkeleton />;
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Header: search + channel filter + refresh */}
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-auto text-base font-semibold text-neutral-100">
          Groups{" "}
          <span className="ml-1 rounded-full bg-neutral-800 px-2 py-0.5 text-xs font-normal text-neutral-400">
            {filtered.length}
          </span>
        </h2>
        <div className="relative">
          <Search
            size={14}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-neutral-500"
          />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name or group id…"
            aria-label="Search groups"
            className="w-52 rounded-md border border-neutral-700 bg-neutral-900 py-1.5 pl-8 pr-2 text-sm text-neutral-200 placeholder:text-neutral-600 focus:border-neutral-500 focus:outline-none"
          />
        </div>
        <select
          value={channelFilter}
          onChange={(e) => setChannelFilter(e.target.value)}
          aria-label="Filter by channel"
          className="cursor-pointer rounded-md border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-300 focus:border-neutral-500 focus:outline-none"
        >
          <option value="all">All channels</option>
          {channels.map((c) => (
            <option key={c} value={c}>
              {channelEmoji(c)} {c}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={load}
          title="Refresh"
          aria-label="Refresh groups"
          className="cursor-pointer rounded-md border border-neutral-700 bg-neutral-900 p-1.5 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {actionError && (
        <div
          role="alert"
          className="rounded-md bg-zinc-950/50 px-3 py-2 text-xs text-zinc-300"
        >
          {actionError}
        </div>
      )}

      {loadError && groups.length > 0 && (
        <div className="rounded-md border border-neutral-800 px-3 py-2 text-xs text-neutral-400">
          Showing cached data — refresh failed: {loadError}
        </div>
      )}

      {/* Body */}
      {groups.length === 0 ? (
        <EmptyState
          icon={Users}
          title="No groups registered"
          hint="Groups appear here once a channel adapter registers them."
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No groups match"
          hint="Try a different search term or channel filter."
        />
      ) : (
        <div className="flex flex-col gap-2" role="list" aria-label="Groups">
          {filtered.map((g) => {
            const key = keyOf(g);
            const muting = pendingMute === key;
            return (
              <div
                key={key}
                role="listitem"
                className="flex items-center gap-3 rounded-lg border border-neutral-800 bg-neutral-900 px-4 py-3"
              >
                <span className="text-2xl" aria-hidden title={g.channel}>
                  {channelEmoji(g.channel)}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-medium text-neutral-100">
                      {g.name}
                    </span>
                    <StatusBadge muted={g.muted} />
                  </div>
                  <div className="mt-0.5 truncate font-mono text-xs text-neutral-400">
                    {g.channel} · {g.group_id}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-neutral-400">
                    <span title="Members">{formatCount(g.member_count)} members</span>
                    <span title="Messages">{formatCount(g.message_count)} messages</span>
                    {g.policy.denied_commands.length > 0 && (
                      <span className="text-zinc-400/80">
                        {g.policy.denied_commands.length} denied
                      </span>
                    )}
                    {g.last_active && <span>active {g.last_active}</span>}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => setPolicyGroup(g)}
                    title="View / edit command policy"
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-neutral-700 bg-neutral-800 px-2.5 py-1.5 text-xs text-neutral-300 hover:bg-neutral-700"
                  >
                    <ShieldCheck size={13} />
                    Policy
                  </button>
                  <button
                    type="button"
                    onClick={() => toggleMute(g)}
                    disabled={muting}
                    title={g.muted ? "Unmute group" : "Mute group"}
                    className={`inline-flex cursor-pointer items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-50 ${
                      g.muted
                        ? "border-zinc-800 bg-zinc-950/40 text-zinc-300 hover:bg-zinc-950/70"
                        : "border-zinc-800 bg-zinc-950/40 text-zinc-300 hover:bg-zinc-950/70"
                    }`}
                  >
                    {g.muted ? <Bell size={13} /> : <BellOff size={13} />}
                    {muting ? "…" : g.muted ? "Unmute" : "Mute"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {policyGroup && (
        <PolicyModal
          group={policyGroup}
          onClose={() => setPolicyGroup(null)}
          onSaved={(policy) => onPolicySaved(policyGroup, policy)}
        />
      )}
    </div>
  );
}
