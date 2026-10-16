"""Research plan framework cho Boom (skill doc-researcher).

Nhận topic -> sinh research plan có cấu trúc:
  - phân rã topic thành các câu hỏi cần trả lời (theo 6 góc nhìn chuẩn)
  - gợi ý loại nguồn cần tra cho từng câu hỏi
  - tổng hợp thành outline markdown kèm template TODO để agent điền

KHÔNG gọi web thật: đây là framework để agent nghiên cứu có hệ thống,
agent sẽ tự tra cứu và điền kết quả vào template.

Chỉ dùng stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Khung câu hỏi chuẩn (6 góc nhìn áp dụng cho mọi topic kỹ thuật)
# ---------------------------------------------------------------------------

# Mỗi góc nhìn: (id, câu hỏi mẫu, vì sao cần hỏi, loại nguồn gợi ý)
_GOC_NHIN = [
    ("what",
     "Topic này là gì? Định nghĩa chính xác và phạm vi của nó?",
     "Tránh hiểu sai từ đầu — định nghĩa sai thì mọi so sánh sau đều lệch.",
     ["official-docs", "wikipedia", "glossary"]),
    ("why",
     "Vấn đề gì mà topic này giải quyết? Không dùng nó thì sao?",
     "Xác định động lực thật sự; nhiều công nghệ 'hay' nhưng không cần thiết.",
     ["official-docs", "blog", "case-study"]),
    ("how",
     "Nó hoạt động như thế nào ở mức kiến trúc? Các thành phần chính?",
     "Hiểu cơ chế để đánh giá trade-off, không chỉ tin benchmark quảng cáo.",
     ["official-docs", "paper", "source-code", "tech-talk"]),
    ("compare",
     "Các lựa chọn thay thế là gì? So sánh theo tiêu chí nào (hiệu năng, "
     "chi phí, độ phức tạp, cộng đồng, license)?",
     "Quyết định tốt cần ít nhất 2-3 lựa chọn đặt cạnh nhau.",
     ["benchmark", "blog", "community", "source-code"]),
    ("risk",
     "Rủi ro/limitations đã biết là gì? Có gotcha nào cộng đồng hay gặp?",
     "Docs chính thức thường giấu nhược điểm; cộng đồng nói thật hơn.",
     ["community", "issue-tracker", "blog", "postmortem"]),
    ("practice",
     "Best practice khi áp dụng vào dự án thật? Có ví dụ production nào?",
     "Khoảng cách từ tutorial tới production rất lớn.",
     ["case-study", "blog", "source-code", "tech-talk"]),
]

# Mô tả loại nguồn để agent biết tìm ở đâu
_MO_TA_NGUON = {
    "official-docs": "Tài liệu chính thức của dự án (docs site, README)",
    "paper": "Paper/blog kỹ thuật sâu, RFC, design doc",
    "blog": "Blog kỹ thuật, bài phân tích độc lập",
    "source-code": "Source code / repo của dự án",
    "community": "Thảo luận cộng đồng (Reddit, HN, Discord, Stack Overflow)",
    "issue-tracker": "GitHub Issues / bug tracker — xem lỗi thật người dùng gặp",
    "benchmark": "Benchmark độc lập, có phương pháp rõ ràng",
    "case-study": "Case study triển khai production",
    "tech-talk": "Talk/video hội thảo kỹ thuật",
    "postmortem": "Postmortem sự cố liên quan",
    "wikipedia": "Định nghĩa nền tảng, thuật ngữ",
    "glossary": "Thuật ngữ chuyên ngành",
}

# Số câu hỏi thêm theo depth (ngoài 6 câu hỏi góc nhìn chuẩn)
_CAU_HOI_THEM_THEO_DEPTH = {
    "quick": 0,      # chỉ 6 câu hỏi chuẩn, trả lời ngắn gọn
    "standard": 2,   # + 2 câu hỏi đào sâu
    "deep": 4,       # + 4 câu hỏi đào sâu
}

_CAU_HOI_DAO_SAU = [
    ("cost", "Chi phí tổng sở hữu (TCO) gồm những gì? Có chi phí ẩn nào?",
     "Quyết định kỹ thuật sai vì bỏ qua chi phí là lỗi phổ biến.",
     ["blog", "case-study", "official-docs"]),
    ("migrate", "Nếu đã dùng giải pháp cũ, migration path ra sao? Có khó không?",
     "Chi phí chuyển đổi đôi khi lớn hơn lợi ích của giải pháp mới.",
     ["official-docs", "blog", "community"]),
    ("future", "Roadmap/lộ trình phát triển của topic này thế nào? Có nguy cơ "
               "bị bỏ rơi không?",
     "Chọn công nghệ chết = nợ kỹ thuật tương lai.",
     ["official-docs", "community", "issue-tracker"]),
    ("security", "Có vấn đề bảo mật nào đã biết? Mô hình trust ra sao?",
     "Bảo mật là tiêu chí loại trừ, không phải tiêu chí cộng điểm.",
     ["official-docs", "issue-tracker", "paper"]),
]


@dataclass
class CauHoi:
    """Một câu hỏi trong research plan."""
    id: str
    question: str
    why: str
    suggested_sources: list = field(default_factory=list)


@dataclass
class ResearchPlan:
    """Kế hoạch nghiên cứu hoàn chỉnh cho 1 topic."""
    topic: str
    goal: str
    depth: str
    questions: list = field(default_factory=list)

    def to_dict(self):
        return {
            "topic": self.topic,
            "goal": self.goal,
            "depth": self.depth,
            "questions": [
                {"id": q.id, "question": q.question, "why": q.why,
                 "suggested_sources": q.suggested_sources}
                for q in self.questions
            ],
            "outline_markdown": render_outline(self),
            "source_checklist": build_source_checklist(self),
        }


def build_research_plan(topic, goal="", depth="standard", questions=None):
    """Dựng research plan từ topic.

    - topic: chủ đề nghiên cứu (bắt buộc, không rỗng)
    - goal: mục tiêu cuối cùng (vd 'chọn giải pháp để triển khai')
    - depth: 'quick' | 'standard' | 'deep'
    - questions: câu hỏi có sẵn của user, được merge vào đầu plan
    """
    if not topic or not topic.strip():
        raise ValueError("topic không được rỗng")
    if depth not in _CAU_HOI_THEM_THEO_DEPTH:
        raise ValueError("depth phải là một trong %s"
                         % sorted(_CAU_HOI_THEM_THEO_DEPTH))

    ds_cau_hoi = []
    # 1) Câu hỏi user đưa sẵn (ưu tiên cao nhất)
    for i, q in enumerate(questions or [], start=1):
        ds_cau_hoi.append(CauHoi(
            id="user-%d" % i,
            question=q.strip(),
            why="Câu hỏi do người dùng đặt ra trực tiếp.",
            suggested_sources=["official-docs", "community", "blog"],
        ))
    # 2) 6 câu hỏi theo góc nhìn chuẩn
    for gid, mau, why, nguon in _GOC_NHIN:
        ds_cau_hoi.append(CauHoi(
            id=gid,
            question=mau,
            why=why,
            suggested_sources=list(nguon),
        ))
    # 3) Câu hỏi đào sâu theo depth
    so_them = _CAU_HOI_THEM_THEO_DEPTH[depth]
    for gid, mau, why, nguon in _CAU_HOI_DAO_SAU[:so_them]:
        ds_cau_hoi.append(CauHoi(
            id="deep-" + gid,
            question=mau,
            why=why,
            suggested_sources=list(nguon),
        ))

    return ResearchPlan(topic=topic.strip(),
                        goal=goal.strip() or "(chưa đặt mục tiêu cụ thể)",
                        depth=depth,
                        questions=ds_cau_hoi)


def build_source_checklist(plan):
    """Checklist nguồn theo loại: {loai_nguon: [id câu hỏi cần tra]}."""
    checklist = {}
    for q in plan.questions:
        for nguon in q.suggested_sources:
            checklist.setdefault(nguon, []).append(q.id)
    # Sắp xếp để output ổn định, dễ đọc
    return {k: checklist[k] for k in sorted(checklist)}


def render_outline(plan):
    """Render research plan thành outline markdown kèm template TODO.

    Agent sẽ điền kết quả nghiên cứu vào các mục '> TODO:'.
    """
    dong = []
    dong.append("# Research: %s" % plan.topic)
    dong.append("")
    dong.append("- **Mục tiêu:** %s" % plan.goal)
    dong.append("- **Độ sâu:** %s" % plan.depth)
    dong.append("")
    dong.append("## Câu hỏi nghiên cứu")
    dong.append("")
    for q in plan.questions:
        dong.append("### %s. %s" % (q.id, q.question))
        dong.append("")
        dong.append("_Vì sao hỏi:_ %s" % q.why)
        dong.append("")
        dong.append("_Nguồn gợi ý:_ %s"
                    % ", ".join(_MO_TA_NGUON.get(s, s)
                                for s in q.suggested_sources))
        dong.append("")
        dong.append("> TODO: điền kết quả nghiên cứu + link nguồn tại đây.")
        dong.append("")
    dong.append("## Tổng hợp & khuyến nghị")
    dong.append("")
    dong.append("> TODO: tóm tắt phát hiện chính (3-5 gạch đầu dòng).")
    dong.append("")
    dong.append("> TODO: khuyến nghị cuối cùng ứng với mục tiêu đã đặt.")
    dong.append("")
    dong.append("## Nguồn tham khảo")
    dong.append("")
    dong.append("> TODO: liệt kê mọi nguồn đã tra, kèm đánh giá độ tin cậy "
                "(cao/trung bình/thấp).")
    dong.append("")
    return "\n".join(dong)


if __name__ == "__main__":
    import sys
    import json
    topic = " ".join(sys.argv[1:]) or "So sánh Redis vs Valkey cho cache session"
    plan = build_research_plan(topic, goal="chọn 1 giải pháp để triển khai")
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
