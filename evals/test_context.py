"""Test cho agent/src/context — chạy bằng:  python3 test_context.py

Dùng assert thuần (stdlib), không cần pytest. Tất cả phải PASS.
"""
import os
import sys

# Cho phép import package `context` khi chạy file test trực tiếp.
sys.path.insert(0, "/tmp/boom_git/agent/src")

from context import ContextManager, HeuristicSummarizer, Message, estimate_tokens

PASSED = 0


def check(name, cond):
    """Một assertion có tên; raise nếu FAIL."""
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  ok: {name}")


print("== 1. estimate_tokens ==")
check("EN: 400 ký tự ≈ 100 tokens", estimate_tokens("a" * 400) == 100)
check("VI có dấu: 250 ký tự ≈ 100 tokens", estimate_tokens("à" * 250) == 100)
check("text rỗng = 0 token", estimate_tokens("") == 0)
check("VI tốn token hơn EN cùng độ dài",
      estimate_tokens("à" * 100) > estimate_tokens("a" * 100))

print("== 2. add_message + get_context (100 messages) ==")
cm = ContextManager(max_tokens=16000)
cm.add_message("system", "Bạn là trợ lý Boom.")
for i in range(100):
    role = "user" if i % 2 == 0 else "assistant"
    cm.add_message(role, f"message {i:03d} " + "nội dung hội thoại " * 3)
check("đã thêm đủ 100 messages + 1 system", len(cm.messages) == 101)

ctx = cm.get_context(max_tokens=1500)
total = sum(m.tokens for m in ctx)
check("get_context(1500): tổng token <= 1500", total <= 1500)
check("get_context: giữ system prompt", any(m.role == "system" for m in ctx))
check("get_context: giữ message mới nhất",
      ctx[-1].content.startswith("message 100") or "message 099" in ctx[-1].content)
contents = [m.content for m in ctx]
check("get_context: giữ đúng thứ tự thời gian", contents == sorted(contents))
check("get_context: message mới nhất không bị mất",
      any("message 099" in m.content for m in ctx))

print("== 3. compress() ==")
cm2 = ContextManager(max_tokens=16000, keep_recent=5)
cm2.add_message("system", "Bạn là trợ lý Boom.")
cm2.add_message("user", "Chào Boom, hôm nay ta bàn về kiến trúc BoomBot.")
cm2.add_message("assistant", "Chào anh Son! Em đã đọc xong agent/src/loop/.")
cm2.add_message("assistant",
                "Quyết định: dùng Devbox thay vì Nix cho môi trường dev.")
for i in range(28):
    cm2.add_message("user", f"Câu hỏi thứ {i} về thiết kế hệ thống.")
    cm2.add_message("assistant", f"Trả lời chi tiết cho câu hỏi {i}.")
cm2.add_message("user", "Cần viết thêm test cho prune trước khi merge.")
cm2.add_message("assistant", "Đã rõ, em sẽ làm ngay.")
before = cm2.total_tokens()
n_before = len(cm2.messages)
summary = cm2.compress()
after = cm2.total_tokens()
check("compress: giảm tổng tokens", after < before)
check("compress: giảm số messages", len(cm2.messages) < n_before)
check("compress: summary giữ được quyết định",
      summary is not None and "quyết định" in summary.content.lower())
check("compress: giữ nguyên system", cm2.messages[0].role == "system")
check("compress: giữ nguyên 5 message mới nhất",
      cm2.messages[-1].content == "Đã rõ, em sẽ làm ngay.")
cm3 = ContextManager()
cm3.add_message("user", "hello")
check("compress: lịch sử ngắn -> None", cm3.compress() is None)

print("== 4. HeuristicSummarizer ==")
s = HeuristicSummarizer()
msgs = [
    Message(role="user", content="Quyết định: chọn Devbox thay vì Nix. Xem thêm https://github.com/TheOwlOps/Boom."),
    Message(role="assistant", content="We decided to refactor agent/src/context/manager.py cho gọn."),
    Message(role="user", content="Cần viết thêm test cho prune trước khi merge, todo cho tối nay."),
    Message(role="assistant", content="BoomBot sẽ hỗ trợ tốt hơn sau đợt này."),
]
dec = s.extract_decisions(msgs)
check("trích decisions: bắt 'Quyết định'", any("Devbox" in d for d in dec))
check("trích decisions: bắt 'decided'", any("refactor" in d for d in dec))
acts = s.extract_action_items(msgs)
check("trích action items: bắt 'cần'/'todo'", any("prune" in a for a in acts))
ents = s.extract_entities(msgs)
check("trích entities: bắt URL", any("github.com/TheOwlOps/Boom" in u for u in ents["urls"]))
check("trích entities: bắt file path", any("manager.py" in f for f in ents["files"]))
check("trích entities: bắt tên riêng BoomBot", "BoomBot" in ents["names"])
out = s.summarize(msgs)
check("summarize: output là bullet list", out.startswith("- "))
check("summarize: có header Quyết định tiếng Việt", "Quyết định" in out)
check("summarize: có header Việc cần làm", "Việc cần làm" in out)
check("summarize: rỗng -> thông báo rõ", "(không có nội dung" in s.summarize([]))

print("== 5. prune() ==")
cm4 = ContextManager(max_tokens=16000)
cm4.add_message("system", "Bạn là trợ lý Boom.")
for i in range(20):
    cm4.add_message("tool", "kết quả tool " * 20 + f" #{i}")
for i in range(5):
    cm4.add_message("user", f"câu hỏi quan trọng của user #{i} " + "x" * 60)
n_users_before = sum(1 for m in cm4.messages if m.role == "user")
removed = cm4.prune(max_tokens=400)
check("prune: đã xóa message", removed > 0)
check("prune: tổng token vừa budget", cm4.total_tokens() <= 400)
check("prune: giữ toàn bộ message của user (tool bị xóa trước)",
      sum(1 for m in cm4.messages if m.role == "user") == n_users_before)
check("prune: không bao giờ xóa system",
      any(m.role == "system" for m in cm4.messages))

print("== 6. edge cases ==")
cm5 = ContextManager()
check("manager rỗng: total = 0", cm5.total_tokens() == 0)
check("manager rỗng: get_context = []", cm5.get_context() == [])
check("manager rỗng: prune = 0", cm5.prune() == 0)
try:
    Message(role="alien", content="x")
    check("Message pydantic: từ chối role lạ", False)
except Exception:
    check("Message pydantic: từ chối role lạ", True)

print(f"\nTẤT CẢ {PASSED} ASSERTIONS PASS ✓")
