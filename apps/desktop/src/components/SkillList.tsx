import { useEffect, useState } from "react";
import { API_BASE } from "../lib/api";

interface Skill {
  name: string;
  description: string;
  /** Relative path within the repo, e.g. skills/code-review/SKILL.md */
  path: string;
}

/**
 * List of real skills in the repo (scanned from skills/ on 2026-10-16).
 * Used as a fallback when /api/skills is unavailable.
 */
const FALLBACK_SKILLS: Skill[] = [
  { name: "api-designer", description: "Design REST APIs: generate OpenAPI 3.0 + route stubs", path: "skills/api-designer/SKILL.md" },
  { name: "apple", description: "Apple ecosystem integration", path: "skills/apple/SKILL.md" },
  { name: "ast-refactor", description: "Refactor code with AST and pattern matching", path: "skills/ast-refactor/SKILL.md" },
  { name: "autonomous-ai-agents", description: "Autonomous AI agent templates", path: "skills/autonomous-ai-agents/SKILL.md" },
  { name: "code-review", description: "Static Python code review: complexity, dead code, bare except", path: "skills/code-review/SKILL.md" },
  { name: "creative", description: "Content creation assistance", path: "skills/creative/SKILL.md" },
  { name: "deployer", description: "Pre-deploy checklist: tests, secrets, version, changelog", path: "skills/deployer/SKILL.md" },
  { name: "devops", description: "DevOps tasks", path: "skills/devops/SKILL.md" },
  { name: "doc-researcher", description: "Research planning for a topic + outline template", path: "skills/doc-researcher/SKILL.md" },
  { name: "email", description: "Email handling", path: "skills/email/SKILL.md" },
  { name: "feature-watch", description: "Track new features/releases of the AI agent ecosystem", path: "skills/feature-watch/SKILL.md" },
  { name: "index-cache", description: "Cached index for fast lookups", path: "skills/index-cache/SKILL.md" },
  { name: "media", description: "Media processing", path: "skills/media/SKILL.md" },
  { name: "note-taking", description: "Note-taking", path: "skills/note-taking/SKILL.md" },
  { name: "perf-optimizer", description: "Find performance bottlenecks with AST", path: "skills/perf-optimizer/SKILL.md" },
  { name: "productivity", description: "Productivity tools", path: "skills/productivity/SKILL.md" },
  { name: "rea", description: "Reverse-engineer anything (rea skill pack)", path: "skills/rea/SKILL.md" },
  { name: "refactor-assistant", description: "Safe refactoring: rename, extract, move with AST (dry-run)", path: "skills/refactor-assistant/SKILL.md" },
  { name: "research", description: "In-depth topic research", path: "skills/research/SKILL.md" },
  { name: "security-audit", description: "Python code security audit: secrets, eval, SQLi", path: "skills/security-audit/SKILL.md" },
  { name: "social-media", description: "Social media management", path: "skills/social-media/SKILL.md" },
  { name: "software-development", description: "Software development", path: "skills/software-development/SKILL.md" },
  { name: "test-writer", description: "Generate pytest tests from AST", path: "skills/test-writer/SKILL.md" },
  { name: "web", description: "Web tools", path: "skills/web/SKILL.md" },
];

/** Validate the raw shape of a skill returned from the API. */
function isSkill(v: unknown): v is Skill {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return typeof o.name === "string" && typeof o.description === "string" && typeof o.path === "string";
}

/** SKILL.md viewer modal: try fetching from the API, otherwise tell the user to open the file in the repo. */
function ReadmeModal({ skill, onClose }: { skill: Skill; onClose(): void }) {
  const [content, setContent] = useState<string>("Loading…");

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/skills/${encodeURIComponent(skill.name)}/readme`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body: unknown = await res.json();
        const text = (body as { content?: unknown }).content;
        if (cancelled) return;
        setContent(typeof text === "string" ? text : "The API returned no content.");
      })
      .catch(() => {
        if (!cancelled) setContent(`No API for reading SKILL.md yet.\n\nOpen the file in the repo: ${skill.path}\n\n${skill.description}`);
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
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

/** Skill list: read from /api/skills, fall back to the repo-scanned list when the API is unavailable. */
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
        if (!Array.isArray(arr) || !arr.every(isSkill)) throw new Error("unexpected shape");
        if (!cancelled) {
          setSkills(arr);
          setUsingApi(true);
        }
      })
      .catch(() => { /* keep the fallback, stay quiet */ });
    return () => { cancelled = true; };
  }, []);

  return (
    <div>
      {!usingApi && (
        <p className="mb-3 text-xs text-neutral-500">
          No /api/skills yet — showing static data scanned from the repo ({FALLBACK_SKILLS.length} skills).
        </p>
      )}
      <ul className="grid gap-3 sm:grid-cols-2">
        {skills.map((s) => (
          <li key={s.name} className="rounded-lg border border-neutral-800 bg-neutral-950 p-3">
            <div className="mb-1 flex items-center justify-between">
              <code className="font-mono text-sm font-semibold text-zinc-300">{s.name}</code>
              <button
                onClick={() => setViewing(s)}
                className="rounded border border-neutral-700 px-2.5 py-1 text-xs text-neutral-200 hover:bg-neutral-800"
              >
                View SKILL.md
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
