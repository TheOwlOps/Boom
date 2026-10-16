import { useMemo, useState } from "react";
import { API_BASE } from "../lib/api";
import { TOOL_CATALOG, type ToolDef } from "../data/toolCatalog";

/** Group tools by function category. */
const GROUPS = ["filesystem", "terminal", "git", "web", "code", "security", "db"] as const;

/** Tool trial-run modal: enter JSON args, calls POST /api/tools/run. */
function RunModal({ tool, onClose }: { tool: ToolDef; onClose(): void }) {
  const [args, setArgs] = useState("{}");
  const [result, setResult] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const run = async () => {
    setRunning(true);
    setResult(null);
    try {
      const parsed: unknown = JSON.parse(args); // surface JSON errors immediately instead of sending garbage
      const res = await fetch(`${API_BASE}/api/tools/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: tool.name, args: parsed }),
      });
      const body: unknown = await res.json().catch(() => null);
      setResult(res.ok ? JSON.stringify(body, null, 2) : `Error ${res.status}: ${JSON.stringify(body)}`);
    } catch (err) {
      setResult(err instanceof Error ? `Error: ${err.message}` : "Unknown error");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="w-full max-w-lg rounded-lg bg-neutral-900 p-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-1 font-semibold text-neutral-100">Run: {tool.name}</h3>
        <p className="mb-3 text-sm text-neutral-400">{tool.description}</p>
        <label className="mb-1 block text-xs text-neutral-400">Args (JSON)</label>
        <textarea
          value={args}
          onChange={(e) => setArgs(e.target.value)}
          rows={5}
          spellCheck={false}
          className="mb-3 w-full rounded border border-neutral-700 bg-neutral-950 p-2 font-mono text-sm text-neutral-200"
        />
        {result !== null && (
          <pre className="mb-3 max-h-48 overflow-auto rounded bg-neutral-950 p-2 font-mono text-xs text-neutral-300">
            {result}
          </pre>
        )}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300">
            Close
          </button>
          <button
            onClick={() => void run()}
            disabled={running}
            className="rounded bg-white px-3 py-1.5 text-sm text-black hover:bg-zinc-200 disabled:opacity-50"
          >
            {running ? "Running…" : "Execute"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Tool manager: list by group, search by name, run each tool. */
export default function ToolManager() {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<ToolDef | null>(null);

  // Filter by tool name (case-insensitive).
  const filtered = useMemo(
    () => TOOL_CATALOG.filter((t) => t.name.toLowerCase().includes(query.trim().toLowerCase())),
    [query],
  );

  return (
    <div>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search tools by name…"
        className="mb-4 w-full rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200 placeholder:text-neutral-600"
      />
      {GROUPS.map((group) => {
        const tools = filtered.filter((t) => t.group === group);
        return (
          <section key={group} className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-neutral-500">
              {group} ({tools.length})
            </h3>
            {tools.length === 0 ? (
              <p className="text-sm italic text-neutral-600">No tools in this group yet.</p>
            ) : (
              <ul className="divide-y divide-neutral-800 rounded-lg border border-neutral-800">
                {tools.map((t) => (
                  <li key={t.name} className="flex items-center gap-3 px-3 py-2">
                    <code className="font-mono text-sm text-zinc-300">{t.name}</code>
                    <span className="flex-1 text-sm text-neutral-400">{t.description}</span>
                    <button
                      onClick={() => setActive(t)}
                      className="rounded border border-neutral-700 px-2.5 py-1 text-xs text-neutral-200 hover:bg-neutral-800"
                    >
                      Run
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
      {active && <RunModal tool={active} onClose={() => setActive(null)} />}
    </div>
  );
}
