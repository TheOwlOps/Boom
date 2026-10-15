"""Test cho GroupManager. Chạy: python3 test_groups.py"""
import os
import sqlite3
import tempfile
import threading

from groups import GroupManager


def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # để GroupManager tự tạo DB mới
    return GroupManager(path)


passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("PASS: %s" % name)
    else:
        failed += 1
        print("FAIL: %s" % name)


# --- register ---
gm = fresh()
r = gm.register_group("telegram", "-1001", "Sales VN", {"timezone": "Asia/Ho_Chi_Minh"})
check("register ok", r.get("ok") is True and r["group"]["name"] == "Sales VN")
check("register settings lưu", r["group"]["settings"] == {"timezone": "Asia/Ho_Chi_Minh"})

r2 = gm.register_group("telegram", "-1001", "Tên khác")
check("trùng (channel, group_id) báo lỗi", "error" in r2)
check("trùng không đè name cũ", gm.get_group_stats("telegram", "-1001")["name"] == "Sales VN")

r3 = gm.register_group("discord", "-1001", "Discord Team")
check("khác channel cùng group_id vẫn ok", r3.get("ok") is True)

check("register channel rỗng báo lỗi", "error" in gm.register_group("", "g", "n"))
check("register group_id rỗng báo lỗi", "error" in gm.register_group("telegram", " ", "n"))
check("register name rỗng báo lỗi", "error" in gm.register_group("telegram", "g2", ""))
check("register settings không phải dict báo lỗi",
      "error" in gm.register_group("telegram", "g2", "n", settings="x"))

# --- unregister ---
check("unregister ok", gm.unregister_group("discord", "-1001").get("ok") is True)
check("unregister lại báo lỗi", "error" in gm.unregister_group("discord", "-1001"))

# --- list_groups ---
gm.register_group("telegram", "-1002", "Support")
allg = gm.list_groups()
check("list tất cả", len(allg) == 2 and all(g["message_count"] == 0 for g in allg))
tg = gm.list_groups("telegram")
check("list lọc theo channel", len(tg) == 2 and all(g["channel"] == "telegram" for g in tg))
check("list channel trống", gm.list_groups("zalo") == [])

# --- record_message ---
gm2 = fresh()
r = gm2.record_message("telegram", "-2001")
check("record_message group mới tự tạo", r.get("ok") is True
      and r["group"]["message_count"] == 1
      and r["group"]["last_active"] is not None)
gm2.record_message("telegram", "-2001")
gm2.record_message("telegram", "-2001")
st = gm2.get_group_stats("telegram", "-2001")
check("record_message tăng count", st["message_count"] == 3)
check("tên mặc định của group tự tạo", st["name"] == "telegram:-2001")
check("get_group_stats group không tồn tại → None",
      gm2.get_group_stats("telegram", "nope") is None)
check("record_message input rỗng báo lỗi", "error" in gm2.record_message(" ", "g"))

# --- set_member_count ---
check("set_member_count ok", gm2.set_member_count("telegram", "-2001", 42) == {"ok": True, "member_count": 42})
check("member_count trong stats", gm2.get_group_stats("telegram", "-2001")["member_count"] == 42)
check("set_member_count số âm báo lỗi", "error" in gm2.set_member_count("telegram", "-2001", -1))
check("set_member_count float báo lỗi", "error" in gm2.set_member_count("telegram", "-2001", 1.5))
check("set_member_count group chưa đăng ký báo lỗi",
      "error" in gm2.set_member_count("telegram", "nope", 5))

# --- policy ---
gm3 = fresh()
gm3.register_group("telegram", "-3001", "G1")
check("policy mặc định rỗng", gm3.get_group_policy("telegram", "-3001") == {})
check("is_command_allowed không policy → True",
      gm3.is_command_allowed("telegram", "-3001", "broadcast") is True)
check("is_command_allowed group lạ → True (fail-open)",
      gm3.is_command_allowed("telegram", "nope", "broadcast") is True)

ok = gm3.set_group_policy("telegram", "-3001",
                          {"allowed_commands": ["help", "report"],
                           "require_approval": ["broadcast"]})
check("set_group_policy ok", ok.get("ok") is True)
check("get_group_policy roundtrip",
      gm3.get_group_policy("telegram", "-3001")["allowed_commands"] == ["help", "report"])
check("command trong allowlist → True",
      gm3.is_command_allowed("telegram", "-3001", "help") is True)
check("command chuẩn hoá '/HELP' → True",
      gm3.is_command_allowed("telegram", "-3001", "/HELP") is True)
check("command ngoài allowlist → False",
      gm3.is_command_allowed("telegram", "-3001", "broadcast") is False)
check("command rỗng → False",
      gm3.is_command_allowed("telegram", "-3001", "  ") is False)

gm3.set_group_policy("telegram", "-3001", {"denied_commands": ["spam"]})
check("denied_commands ưu tiên cao nhất → False",
      gm3.is_command_allowed("telegram", "-3001", "spam") is False)
check("không allowlist + không deny → True",
      gm3.is_command_allowed("telegram", "-3001", "help") is True)

check("set_group_policy không phải dict báo lỗi",
      "error" in gm3.set_group_policy("telegram", "-3001", ["x"]))
check("set_group_policy group chưa đăng ký báo lỗi",
      "error" in gm3.set_group_policy("telegram", "nope", {}))
check("get_group_policy group lạ → {}",
      gm3.get_group_policy("telegram", "nope") == {})

# --- thread-safety cơ bản ---
gm4 = fresh()
gm4.register_group("telegram", "-4001", "Race")
def worker():
    for _ in range(50):
        gm4.record_message("telegram", "-4001")
threads = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
check("4 thread x 50 message = 200",
      gm4.get_group_stats("telegram", "-4001")["message_count"] == 200)

# --- persistence qua restart ---
path = tempfile.mktemp(suffix=".db")
gm5a = GroupManager(path)
gm5a.register_group("zalo", "z1", "Zalo Shop")
gm5a.set_group_policy("zalo", "z1", {"allowed_commands": ["order"]})
gm5b = GroupManager(path)
st = gm5b.get_group_stats("zalo", "z1")
check("dữ liệu giữ sau restart", st is not None and st["name"] == "Zalo Shop")
check("policy giữ sau restart", gm5b.is_command_allowed("zalo", "z1", "order") is True)
os.unlink(path)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
