"""Test cho 4 skills batch D: log-analyzer, backup-manager, notify-hub, data-validator.

Chạy: python3 test_skills_d.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "skills-archived"
sys.path.insert(0, str(BASE / "log-analyzer"))
sys.path.insert(0, str(BASE / "backup-manager"))
sys.path.insert(0, str(BASE / "notify-hub"))
sys.path.insert(0, str(BASE / "data-validator"))

from analyzer import analyze_log  # noqa: E402
from backup import (  # noqa: E402
    create_backup,
    list_backups,
    prune,
    restore,
    verify_backup,
)
from notify import broadcast, send  # noqa: E402
from validator import validate_csv, validate_json  # noqa: E402

PASSED = 0


def check(name: str, cond: bool) -> None:
    """Một assertion: đúng thì đếm PASS, sai thì raise ngay."""
    global PASSED
    assert cond, f"FAIL: {name}"
    PASSED += 1
    print(f"  PASS: {name}")


def tmp() -> Path:
    d = Path("/tmp/skills_d_test")
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_log_analyzer() -> None:
    print("== log-analyzer ==")
    log = tmp() / "app.log"
    log.write_text(
        "2026-10-16 02:00:01 INFO server started pid=1234\n"
        "2026-10-16 02:00:02 WARN disk usage high\n"
        "2026-10-16 02:00:03 ERROR connection refused db host=db1 req_id=abc123\n"
        "2026-10-16 02:00:04 ERROR connection refused db host=db1 req_id=def456\n"
        "2026-10-16 02:00:05 WARNING timeout after 3000 ms on /api/slow\n"
        "2026-10-16 02:00:06 CRITICAL out of memory\n"
        "2026-10-16 02:00:07 Traceback (most recent call last):\n"
        "2026-10-16 02:00:08   File \"main.py\", line 10, in <module>\n"
        "2026-10-16 02:00:09 INFO query took 2500 ms\n",
        encoding="utf-8",
    )
    r = analyze_log(log, slow_threshold_ms=1000)
    check("đếm đúng tổng số dòng", r["total_lines"] == 9)
    check("đếm đúng errors (ERROR x2 + CRITICAL)", r["errors"] == 3)
    check("đếm đúng warnings (WARN + WARNING)", r["warnings"] == 2)
    check("gom nhóm lỗi trùng (2 connection refused -> 1 bucket, count=2)",
          any(e["count"] == 2 and "connection refused" in e["message"] for e in r["top_errors"]))
    check("phát hiện stack trace", r["stack_traces"] == 1)
    check("phát hiện slow ops (3000ms + 2500ms)",
          len(r["slow_ops"]) == 2 and all(s["duration_ms"] >= 1000 for s in r["slow_ops"]))
    check("slow op ghi đúng số dòng", any(s["line"] == 5 for s in r["slow_ops"]))


def test_backup_manager() -> None:
    print("== backup-manager ==")
    src = tmp() / "data_src"
    (src / "sub").mkdir(parents=True, exist_ok=True)
    (src / "a.txt").write_text("hello")
    (src / "sub" / "b.txt").write_text("world")
    bdir = tmp() / "backups"
    if bdir.exists():
        for p in bdir.iterdir():
            p.unlink()

    info = create_backup(src, bdir)
    check("tạo backup thành công + verified", info["verified"] is True)
    check("file backup tồn tại và có checksum", Path(info["path"]).is_file() and len(info["sha256"]) == 64)
    check("verify_backup độc lập đúng", verify_backup(info["path"]) is True)

    items = list_backups(bdir)
    check("list_backups thấy 1 bản, có checksum", len(items) == 1 and items[0]["has_checksum"])

    rdir = tmp() / "restored"
    if rdir.exists():
        import shutil
        shutil.rmtree(rdir)
    res = restore(info["path"], rdir)
    check("restore giải nén đúng nội dung",
          (rdir / "data_src" / "a.txt").read_text() == "hello"
          and (rdir / "data_src" / "sub" / "b.txt").read_text() == "world")

    # Tạo thêm 2 bản để test prune
    time.sleep(1.1)
    create_backup(src, bdir)
    time.sleep(1.1)
    create_backup(src, bdir)
    pr = prune(bdir, keep_n=2)
    check("prune giữ đúng 2 bản mới nhất, xoá 1 bản",
          len(pr["kept"]) == 2 and len(pr["removed"]) == 1 and len(list_backups(bdir)) == 2)


def test_notify_hub() -> None:
    print("== notify-hub ==")
    # Đảm bảo không có env nào set -> fail graceful
    for k in ("TELEGRAM_WEBHOOK", "DISCORD_WEBHOOK", "SLACK_WEBHOOK", "GENERIC_WEBHOOK"):
        os.environ.pop(k, None)

    r = send("telegram", "hello")
    check("thiếu env -> ok=False, không raise", r["ok"] is False and "TELEGRAM_WEBHOOK" in r["error"])
    r = send("carrier-pigeon", "hello")
    check("kênh lạ -> ok=False với message rõ", r["ok"] is False and "không hỗ trợ" in r["error"])

    # Webhook URL không tồn tại -> lỗi mạng nhưng vẫn fail graceful
    os.environ["GENERIC_WEBHOOK"] = "http://127.0.0.1:9/nope"
    r = send("webhook", "hello")
    check("lỗi kết nối -> ok=False, không raise", r["ok"] is False and r["error"])
    del os.environ["GENERIC_WEBHOOK"]

    results = broadcast("test", channels=["telegram", "discord"])
    check("broadcast trả kết quả từng kênh", len(results) == 2 and all("channel" in x for x in results))
    check("broadcast không raise khi mọi kênh lỗi", all(x["ok"] is False for x in results))


def test_data_validator() -> None:
    print("== data-validator ==")
    schema = {
        "name": {"type": "str", "required": True},
        "email": {"type": "str", "required": True, "format": "email"},
        "age": {"type": "int", "required": False},
        "site": {"type": "str", "required": False, "format": "url"},
        "dob": {"type": "str", "required": False, "format": "date"},
    }
    good = {"name": "Son", "email": "a@b.com", "age": 30,
            "site": "https://example.com", "dob": "2000-01-15"}
    r = validate_json(good, schema)
    check("JSON hợp lệ -> valid=True", r["valid"] is True and r["errors"] == [])

    bad = {"email": "not-an-email", "age": "30", "site": "notaurl", "dob": "2026-13-99"}
    r = validate_json(bad, schema)
    check("thiếu required + sai format -> valid=False", r["valid"] is False)
    fields = {e["field"] for e in r["errors"]}
    check("báo đúng các field lỗi (name, email, age, site, dob)",
          {"name", "email", "age", "site", "dob"} <= fields)

    r = validate_json("not-a-dict", schema)
    check("data không phải dict -> invalid", r["valid"] is False)

    csv_path = tmp() / "users.csv"
    csv_path.write_text("name,email,age\nSon,a@b.com,30\nAn,b@c.com,25\n", encoding="utf-8")
    r = validate_csv(csv_path, ["name", "email", "age"])
    check("CSV hợp lệ -> valid=True, rows=2", r["valid"] is True and r["rows"] == 2)

    bad_csv = tmp() / "bad.csv"
    bad_csv.write_text("name,email\nSon,a@b.com,30\n", encoding="utf-8")
    r = validate_csv(bad_csv, ["name", "email", "age"])
    check("CSV thiếu cột + lệch số cột -> invalid", r["valid"] is False and len(r["errors"]) >= 2)

    r = validate_csv("/tmp/khong-ton-tai-xyz.csv", ["a"])
    check("CSV không tồn tại -> invalid", r["valid"] is False)


if __name__ == "__main__":
    test_log_analyzer()
    test_backup_manager()
    test_notify_hub()
    test_data_validator()
    print(f"\nTẤT CẢ {PASSED} ASSERTIONS PASS")
