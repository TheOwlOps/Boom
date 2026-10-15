# Boom — Danh mục Skills

Agent Skills của Boom nằm trong `skills/` — mỗi skill là 1 thư mục chứa `SKILL.md` (frontmatter `description` + hướng dẫn chi tiết). Agent khám phá skill qua tool `list_skills`, đọc chi tiết bằng `view_skill`, và cài thêm bằng `install_skill`.

> **Nguồn xác thực:** quét `skills/**/SKILL.md` trên `main` (commit `f4ed006`).
> **Tổng số: 80 skills** (mỗi file `SKILL.md` = 1 skill). Thư mục `skills/index-cache` hiện chưa có `SKILL.md` nên không tính.

| Nhóm | Số skills |
|------|-----------|
| Lập trình & chất lượng code | 17 |
| Reverse engineering (REA) | 4 |
| DevOps, deploy & vận hành | 7 |
| Lên lịch & thông báo | 2 |
| Bảo mật & dữ liệu | 3 |
| Nghiên cứu & thông tin | 6 |
| Tài liệu văn phòng | 9 |
| Kết nối dịch vụ | 13 |
| Theo dõi giá | 1 |
| Sáng tạo & media | 12 |
| Autonomous agents | 5 |
| Web recovery | 1 |

---

## 1. Lập trình & chất lượng code (17)

| Skill | Mô tả |
|-------|-------|
| `ast-refactor` | Refactor code bằng AST và structural pattern matching. |
| `code-review` | Review tĩnh code Python: complexity, dead code, bare except, print, TODO (wrap `codereview.py` của Boom). |
| `refactor-assistant` | Trợ lý refactor an toàn: rename symbol, extract function, move symbol bằng AST thật, luôn dry-run preview diff trước khi apply. |
| `test-writer` | Sinh test pytest từ AST: phân tích function, suy luận giá trị mẫu, sinh happy path + edge cases (None, rỗng, biên). |
| `api-designer` | Thiết kế REST API từ mô tả: sinh OpenAPI 3.0 spec (paths, schemas, errors) + route stubs FastAPI/Flask, có validate chuẩn REST. |
| `software-development/codebase-inspection` | Inspect codebase bằng pygount: LOC, ngôn ngữ, tỉ lệ. |
| `software-development/dogfood` | Exploratory QA web app: tìm bug, thu thập evidence, viết report. |
| `software-development/github` | GitHub qua `gh` CLI: PR, issue, review, repo, auth. |
| `software-development/hermes-agent-skill-authoring` | Viết SKILL.md trong repo: frontmatter và cấu trúc chuẩn. |
| `software-development/inspecting-hermes-desktop-dom` | Đọc live Hermes desktop DOM/CSS qua CDP. |
| `software-development/node-inspect-debugger` | Debug Node.js qua `--inspect` + Chrome DevTools Protocol CLI. |
| `software-development/python-debugpy` | Debug Python: pdb REPL + debugpy remote (DAP). |
| `software-development/requesting-code-review` | Review trước commit: security scan, quality gates, auto-fix. |
| `software-development/simplify-code` | Dọn code mới thay đổi gần đây bằng 4 agent song song. |
| `software-development/spike` | Thí nghiệm throwaway để validate ý tưởng trước khi build. |
| `software-development/systematic-debugging` | Debug tìm root cause theo 4 pha: hiểu bug trước khi sửa. |
| `software-development/test-driven-development` | TDD: ép RED-GREEN-REFACTOR, viết test trước code. |

---

## 2. Reverse engineering — REA (4)

Bộ skill từ `morluto/rea` (commit `3135db3`), dùng cho điều tra artifact.

| Skill | Mô tả |
|-------|-------|
| `rea/reverse-engineer-anything` | Reverse engineer target native, managed, Electron/JS, packaged, firmware, browser bằng REA; giải thích feature, so sánh version, decompile, hoặc hướng dẫn tái dựng. |
| `rea/rea-tool-design` | Thiết kế/thay đổi investigation tool của REA, contract CLI/MCP, provider capability và Evidence semantics. |
| `rea/rea-ci-tests-audit` | Audit CI và test suite của REA: correctness, fixture drift, false pass, performance đo được. |
| `rea/rea-changelog-update` | Chuẩn bị/viết lại changelog release và GitHub release notes của REA từ Git history, kèm lời cảm ơn contributor đã verify. |

---

## 3. DevOps, deploy & vận hành (7)

| Skill | Mô tả |
|-------|-------|
| `devops/sdlc-review` | Review Kanban handoff và route outcome đã verify. |
| `deployer` | Checklist pre-deploy tự động: tests pass, không secret, version bumped, CHANGELOG, env vars, backup plan. |
| `env-setup` | Kiểm tra môi trường dev: version Python, package đã cài, biến môi trường bắt buộc, port trống. Output chuẩn [{check, status, fix_command}]. |
| `git-workflow` | Git workflow helper: `smart_commit()` tự sinh commit message từ diff, `branch_status()` xem trạng thái nhánh, `pr_checklist()` kiểm tra trước khi tạo PR. |
| `log-analyzer` | Phân tích file log, đếm lỗi và tìm pattern chậm. |
| `backup-manager` | Tạo, liệt kê, khôi phục và dọn backup tar.gz. |
| `perf-optimizer` | Phân tích bottleneck hiệu năng bằng AST: vòng lặp lồng O(n²), I/O trong loop, nối chuỗi +=, N+1 query, thiếu cache. |

---

## 4. Lên lịch & thông báo (2)

| Skill | Mô tả |
|-------|-------|
| `cron-manager` | Quản lý cron jobs: add/list/remove (lưu JSON) + validate cron expression, hỗ trợ @daily/@hourly và dạng 5 trường. |
| `notify-hub` | Gửi thông báo đa kênh qua webhook URL từ env. |

---

## 5. Bảo mật & dữ liệu (3)

| Skill | Mô tả |
|-------|-------|
| `security-audit` | Kiểm toán bảo mật code Python: hardcoded secret, hàm nguy hiểm (eval/exec/os.system), SQL injection, thiếu validate input. |
| `data-validator` | Validate JSON/CSV theo schema, type và format. |
| `db-helper` | Wrap SQLite an toàn: query/insert/migrate với whitelist bảng, parameterized query, chặn DROP và DELETE/UPDATE thiếu WHERE. |

---

## 6. Nghiên cứu & thông tin (6)

| Skill | Mô tả |
|-------|-------|
| `research/arxiv` | Tìm paper arXiv theo keyword, author, category hoặc ID. |
| `research/competitor-news-monitor` | Theo dõi tin tức quan trọng của các công ty chỉ định; digest có trích dẫn. |
| `research/grounded-citations` | Neo câu trả lời và tài liệu vào nguồn trích dẫn có thể verify. |
| `research/llm-wiki` | Xây/truy vấn knowledge base markdown liên kết (theo LLM Wiki của Karpathy). |
| `doc-researcher` | Lập kế hoạch nghiên cứu cho 1 topic: câu hỏi cần trả lời, nguồn cần tra, template outline markdown để agent điền. |
| `feature-watch` | Theo dõi hệ sinh thái AI agent, báo tính năng/release mới có trích dẫn. |

---

## 7. Tài liệu văn phòng (9)

| Skill | Mô tả |
|-------|-------|
| `productivity/docx` | Tạo, đọc, sửa, template và review file Word .docx. |
| `productivity/pdf` | File PDF: tạo, đọc, merge, fill form, OCR, sửa text. |
| `productivity/xlsx` | Tạo, đọc, sửa workbook Excel .xlsx và CSV. |
| `productivity/powerpoint` | Tạo, đọc, sửa deck .pptx bằng python-pptx. |
| `productivity/notion` | Notion API + CLI `ntn`: pages, databases, markdown, Workers. |
| `productivity/meeting-action-items` | Biến meeting notes thành decision, owner, ticket có trích dẫn. |
| `productivity/document-to-action-items` | Trích obligation, deadline, task có trích dẫn từ tài liệu. |
| `productivity/weekly-review-planning` | Weekly reset: commitments, việc kẹt, kế hoạch tuần tới. |
| `note-taking/obsidian` | Đọc, tìm, tạo và sửa note trong Obsidian vault. |

---

## 8. Kết nối dịch vụ (13)

| Skill | Mô tả |
|-------|-------|
| `productivity/airtable` | Airtable REST API qua curl: records CRUD, filter, upsert. |
| `productivity/box` | Box: quản lý file cloud, chia sẻ, tìm kiếm, metadata. |
| `productivity/google-workspace` | Gmail, Calendar, Drive, Docs, Sheets qua CLI `gws` hoặc Python. |
| `productivity/maps` | Geocode, POI, route, timezone qua OpenStreetMap/OSRM. |
| `productivity/teams-meeting-pipeline` | Tóm tắt meeting Teams, job replay, Graph subscriptions. |
| `email/email-inbox-triage` | Triage inbox: ưu tiên thread, draft reply an toàn. |
| `email/himalaya` | Email IMAP/SMTP từ terminal qua Himalaya CLI. |
| `social-media/xurl` | X/Twitter qua `xurl` CLI: tìm post, đăng bài, DM, media. |
| `media/youtube-content` | Transcript YouTube thành summary, thread, blog. |
| `apple/apple-notes` | Quản lý Apple Notes qua `memo` CLI: tạo, tìm, sửa. |
| `apple/apple-reminders` | Apple Reminders qua `remindctl`: add, list, complete. |
| `apple/findmy` | Theo dõi thiết bị Apple/AirTag qua FindMy.app trên macOS. |
| `apple/imessage` | Gửi/nhận iMessage/SMS qua CLI `imsg` trên macOS. |

---

## 9. Theo dõi giá (1)

| Skill | Mô tả |
|-------|-------|
| `productivity/product-price-monitor` | Theo dõi giá sản phẩm, chuyến bay, listing; alert khi chạm target. |

---

## 10. Sáng tạo & media (12)

| Skill | Mô tả |
|-------|-------|
| `creative/architecture-diagram` | Vẽ sơ đồ kiến trúc/cloud/infra dạng SVG dark-theme xuất HTML. |
| `creative/ascii-video` | ASCII video: chuyển video/audio thành MP4/GIF ASCII màu. |
| `creative/baoyu-infographic` | Infographic: 21 layout × 21 style. |
| `creative/claude-design` | Thiết kế HTML artifact một lần (landing, deck, prototype). |
| `creative/design-md` | Soạn/validate/export file spec token DESIGN.md của Google. |
| `creative/humanizer` | Humanize text: loại AI-ism, thêm giọng thật. |
| `creative/manim-video` | Animation Manim CE: video toán/thuật toán kiểu 3Blue1Brown. |
| `creative/p5js` | Sketch p5.js: generative art, shader, interactive, 3D. |
| `creative/popular-web-designs` | 54 design system thật (Stripe, Linear, Vercel) dạng HTML/CSS. |
| `creative/songwriting-and-ai-music` | Kỹ thuật sáng tác nhạc và prompt nhạc AI Suno. |
| `media/gif-search` | Tìm/tải GIF từ Tenor qua curl + jq. |
| `media/songsee` | Spectrogram/đặc trưng audio (mel, chroma, MFCC) qua CLI. |

---

## 11. Autonomous agents (5)

Ủy quyền coding cho các coding agent CLI khác.

| Skill | Mô tả |
|-------|-------|
| `autonomous-ai-agents/claude-code` | Ủy quyền coding cho Claude Code CLI (feature, PR). |
| `autonomous-ai-agents/codex` | Ủy quyền coding cho OpenAI Codex CLI (feature, PR). |
| `autonomous-ai-agents/opencode` | Ủy quyền coding cho OpenCode CLI (feature, PR review). |
| `autonomous-ai-agents/hermes-agent` | Dùng, cấu hình, theme, mở rộng và điều phối Hermes Agent. |
| `autonomous-ai-agents/computer-use` | Điều khiển desktop background-first; escalate khi có signal. |

---

## 12. Web recovery (1)

| Skill | Mô tả |
|-------|-------|
| `web/blocked-page-recovery` | Dùng khi fetch thất bại: 403/429, paywall, WAF, tường bot. |

---

*Cập nhật 2026-10-16 từ `main`. Số liệu quét thật từ `skills/**/SKILL.md` — không ước lượng. Mô tả lấy từ field `description:` trong frontmatter của từng SKILL.md.*
