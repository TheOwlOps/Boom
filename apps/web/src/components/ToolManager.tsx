import { useMemo, useState } from "react";
import { API_BASE } from "../lib/api";
import { TOOL_CATALOG, type ToolDef } from "../data/toolCatalog";

/** Nhóm tool theo danh mục chức năng. */
const GROUPS = ["filesystem", "terminal", "git", "web", "code", "security", "db"] as const;

/** Modal chạy thử tool: nhập args JSON, gọi POST /api/tools/run. */
function RunModal({ tool, onClose }: { tool: ToolDef; onClose(): void }) {
  const [args, setArgs] = useState("{}");
  const [result, setResult] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const run = async () => {
    setRunning(true);
    setResult(null);
    try {
      const parsed: unknown = JSON.parse(args); // báo lỗi JSON ngay, không gửi bậy
      const res = await fetch(`${API_BASE}/api/tools/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: tool.name, args: parsed }),
      });
      const body: unknown = await res.json().catch(() => null);
      setResult(res.ok ? JSON.stringify(body, null, 2) : `Lỗi ${res.status}: ${JSON.stringify(body)}`);
    } catch (err) {
      setResult(err instanceof Error ? `Lỗi: ${err.message}` : "Lỗi không xác định");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="w-full max-w-lg rounded-lg bg-neutral-900 p-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-1 font-semibold text-neutral-100">Chạy thử: {tool.name}</h3>
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
            Đóng
          </button>
          <button
            onClick={() => void run()}
            disabled={running}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {running ? "Đang chạy…" : "Thực thi"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Quản lý tools: liệt kê theo nhóm, tìm kiếm theo tên, chạy thử từng tool. */
export default function ToolManager() {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<ToolDef | null>(null);

  // Lọc theo tên tool (không phân biệt hoa thường).
  const filtered = useMemo(
    () => TOOL_CATALOG.filter((t) => t.name.toLowerCase().includes(query.trim().toLowerCase())),
    [query],
  );

  return (
    <div>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Tìm tool theo tên…"
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
              <p className="text-sm italic text-neutral-600">Chưa có tool nào trong nhóm này.</p>
            ) : (
              <ul className="divide-y divide-neutral-800 rounded-lg border border-neutral-800">
                {tools.map((t) => (
                  <li key={t.name} className="flex items-center gap-3 px-3 py-2">
                    <code className="font-mono text-sm text-blue-300">{t.name}</code>
                    <span className="flex-1 text-sm text-neutral-400">{t.description}</span>
                    <button
                      onClick={() => setActive(t)}
                      className="rounded border border-neutral-700 px-2.5 py-1 text-xs text-neutral-200 hover:bg-neutral-800"
                    >
                      Chạy thử
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
