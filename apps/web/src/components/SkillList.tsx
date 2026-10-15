import { useEffect, useState } from "react";
import { API_BASE } from "../lib/api";

interface Skill {
  name: string;
  description: string;
  /** Đường dẫn tương đối trong repo, ví dụ skills/code-review/SKILL.md */
  path: string;
}

/**
 * Danh sách skills thật trong repo (quét từ skills/ ngày 2026-10-16).
 * Dùng làm fallback khi /api/skills chưa có.
 */
const FALLBACK_SKILLS: Skill[] = [
  { name: "api-designer", description: "Thiết kế REST API: sinh OpenAPI 3.0 + route stubs", path: "skills/api-designer/SKILL.md" },
  { name: "apple", description: "Tích hợp hệ sinh thái Apple", path: "skills/apple/SKILL.md" },
  { name: "ast-refactor", description: "Refactor code bằng AST và pattern matching", path: "skills/ast-refactor/SKILL.md" },
  { name: "autonomous-ai-agents", description: "Mẫu agent AI tự hành", path: "skills/autonomous-ai-agents/SKILL.md" },
  { name: "code-review", description: "Review tĩnh code Python: complexity, dead code, bare except", path: "skills/code-review/SKILL.md" },
  { name: "creative", description: "Hỗ trợ sáng tạo nội dung", path: "skills/creative/SKILL.md" },
  { name: "deployer", description: "Checklist pre-deploy: tests, secrets, version, changelog", path: "skills/deployer/SKILL.md" },
  { name: "devops", description: "Tác vụ DevOps", path: "skills/devops/SKILL.md" },
  { name: "doc-researcher", description: "Lập kế hoạch nghiên cứu topic + template outline", path: "skills/doc-researcher/SKILL.md" },
  { name: "email", description: "Xử lý email", path: "skills/email/SKILL.md" },
  { name: "feature-watch", description: "Theo dõi tính năng/release mới của hệ sinh thái AI agent", path: "skills/feature-watch/SKILL.md" },
  { name: "index-cache", description: "Cache index để tra cứu nhanh", path: "skills/index-cache/SKILL.md" },
  { name: "media", description: "Xử lý media", path: "skills/media/SKILL.md" },
  { name: "note-taking", description: "Ghi chú", path: "skills/note-taking/SKILL.md" },
  { name: "perf-optimizer", description: "Tìm bottleneck hiệu năng bằng AST", path: "skills/perf-optimizer/SKILL.md" },
  { name: "productivity", description: "Công cụ năng suất", path: "skills/productivity/SKILL.md" },
  { name: "rea", description: "Reverse-engineer anything (bộ skill rea)", path: "skills/rea/SKILL.md" },
  { name: "refactor-assistant", description: "Refactor an toàn: rename, extract, move bằng AST (dry-run)", path: "skills/refactor-assistant/SKILL.md" },
  { name: "research", description: "Nghiên cứu sâu chủ đề", path: "skills/research/SKILL.md" },
  { name: "security-audit", description: "Kiểm toán bảo mật code Python: secret, eval, SQLi", path: "skills/security-audit/SKILL.md" },
  { name: "social-media", description: "Quản lý mạng xã hội", path: "skills/social-media/SKILL.md" },
  { name: "software-development", description: "Phát triển phần mềm", path: "skills/software-development/SKILL.md" },
  { name: "test-writer", description: "Sinh test pytest từ AST", path: "skills/test-writer/SKILL.md" },
  { name: "web", description: "Công cụ web", path: "skills/web/SKILL.md" },
];

/** Kiểm tra shape thô của 1 skill trả về từ API. */
function isSkill(v: unknown): v is Skill {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return typeof o.name === "string" && typeof o.description === "string" && typeof o.path === "string";
}

/** Modal xem SKILL.md: thử lấy từ API, chưa có thì hướng dẫn mở file trong repo. */
function ReadmeModal({ skill, onClose }: { skill: Skill; onClose(): void }) {
  const [content, setContent] = useState<string>("Đang tải…");

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/skills/${encodeURIComponent(skill.name)}/readme`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body: unknown = await res.json();
        const text = (body as { content?: unknown }).content;
        if (cancelled) return;
        setContent(typeof text === "string" ? text : "API không trả về nội dung.");
      })
      .catch(() => {
        if (!cancelled) setContent(`Chưa có API đọc SKILL.md.\n\nMở file trong repo: ${skill.path}\n\n${skill.description}`);
      });
    return () => { cancelled = true; };
  }, [skill]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-lg bg-neutral-900 p-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-3 font-mono text-sm font-semibold text-neutral-100">SKILL.md — {skill.name}</h3>
        <pre className="flex-1 overflow-auto rounded bg-neutral-950 p-3 font-mono text-xs text-neutral-300">{content}</pre>
        <div className="mt-3 flex justify-end">
          <button onClick={onClose} className="rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300">
            Đóng
          </button>
        </div>
      </div>
    </div>
  );
}

/** Danh sách skills: đọc từ /api/skills, chưa có API thì dùng mock từ repo. */
export default function SkillList() {
  const [skills, setSkills] = useState<Skill[]>(FALLBACK_SKILLS);
  const [usingApi, setUsingApi] = useState(false);
  const [viewing, setViewing] = useState<Skill | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/skills`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body: unknown = await res.json();
        const arr = Array.isArray(body) ? body : (body as { skills?: unknown }).skills;
        if (!Array.isArray(arr) || !arr.every(isSkill)) throw new Error("shape lạ");
        if (!cancelled) {
          setSkills(arr);
          setUsingApi(true);
        }
      })
      .catch(() => { /* giữ fallback, không ồn ào */ });
    return () => { cancelled = true; };
  }, []);

  return (
    <div>
      {!usingApi && (
        <p className="mb-3 text-xs text-neutral-500">
          Chưa có /api/skills — đang hiển thị dữ liệu tĩnh quét từ repo (25 skills).
        </p>
      )}
      <ul className="grid gap-3 sm:grid-cols-2">
        {skills.map((s) => (
          <li key={s.name} className="rounded-lg border border-neutral-800 bg-neutral-950 p-3">
            <div className="mb-1 flex items-center justify-between">
              <code className="font-mono text-sm font-semibold text-green-300">{s.name}</code>
              <button
                onClick={() => setViewing(s)}
                className="rounded border border-neutral-700 px-2.5 py-1 text-xs text-neutral-200 hover:bg-neutral-800"
              >
                Xem SKILL.md
              </button>
            </div>
            <p className="text-sm text-neutral-400">{s.description}</p>
          </li>
        ))}
      </ul>
      {viewing && <ReadmeModal skill={viewing} onClose={() => setViewing(null)} />}
    </div>
  );
}
