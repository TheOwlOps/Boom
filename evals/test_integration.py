#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integration test cho full pipeline của Boom (TheOwlOps/Boom, commit bf171b9).

Chạy:  python3 test_integration.py
Yêu cầu: repo local read-only tại /tmp/boom_git, Python 3.12+, httpx, pydantic
         (pydantic được khai báo trong agent/pyproject.toml + devbox.json;
          nếu máy chưa có, cài: pip install --break-system-packages pydantic)

Pipeline được test:
  AgentEnvironment.__init__  ->  registry self._tools  ->  gọi tool qua
  entry["func"] (read_file / write_file / patch_file / shell_run)  ->
  verify kết quả + error handling có cấu trúc (không raise lung tung).

Test KHÔNG sửa repo: mọi file temp nằm trong thư mục tạm riêng.
"""

import sys
import tempfile
import traceback
from pathlib import Path

# Repo local read-only; chỉ đọc, không sửa.
REPO = Path("/tmp/boom_git")
sys.path.insert(0, str(REPO / "agent" / "src"))

from environment import AgentEnvironment  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    """Một assertion có đếm số. Ghi PASS/FAIL ra stdout, tiếp tục chạy."""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    print("== Boom integration test: full pipeline ==\n")

    # ------------------------------------------------------------------
    # 1. Khởi tạo environment
    # ------------------------------------------------------------------
    print("-- 1. Khởi tạo AgentEnvironment --")
    tmp_root = Path(tempfile.mkdtemp(prefix="boom_integration_"))
    env = AgentEnvironment(root_dir=tmp_root)

    check("env là instance của AgentEnvironment", isinstance(env, AgentEnvironment))
    check("root_dir trỏ đúng thư mục tạm", env.root_dir == tmp_root, str(env.root_dir))

    # ------------------------------------------------------------------
    # 2. Registry tools
    # ------------------------------------------------------------------
    print("\n-- 2. Registry tools --")
    tools = env._tools
    check("env._tools là dict", isinstance(tools, dict))
    check("số tools > 0", len(tools) > 0, f"đếm được: {len(tools)}")
    # Theo memory: 131 -> 139 -> 160 tools sau các đợt overnight; đặt ngưỡng
    # an toàn 100 để bắt regression lớn mà không giòn theo từng commit.
    check("số tools >= 100 (không bị rụng registry)", len(tools) >= 100,
          f"đếm được: {len(tools)}")

    for name in ("read_file", "write_file", "patch_file", "shell_run"):
        check(f"tool '{name}' có trong registry", name in tools)

    for name in ("read_file", "write_file", "patch_file", "shell_run"):
        entry = tools[name]
        check(f"'{name}'.func callable", callable(entry.get("func")))
        schema = entry.get("schema")
        check(f"'{name}'.schema là dict có 'properties'",
              isinstance(schema, dict) and isinstance(schema.get("properties"), dict))
        check(f"'{name}'.entry['name'] khớp key", entry.get("name") == name)

    w_schema = tools["write_file"]["schema"]
    check("schema write_file yêu cầu path + content",
          set(w_schema.get("required", [])) >= {"path", "content"})

    # ------------------------------------------------------------------
    # 3. Pipeline filesystem thật: write -> read -> patch -> read
    # ------------------------------------------------------------------
    print("\n-- 3. Pipeline filesystem (write/read/patch) --")
    target = tmp_root / "work" / "hello.txt"
    content = "dòng 1: BOOM-INTEGRATION-OK\n" \
              "dòng 2: pipeline test\n" \
              "dòng 3: tiếng Việt có dấu\n"

    w = tools["write_file"]["func"](path=str(target), content=content)
    check("write_file trả success=True", w.get("success") is True, str(w))
    check("write_file báo đúng bytes_written",
          w.get("bytes_written") == len(content.encode("utf-8")))

    r = tools["read_file"]["func"](path=str(target))
    check("read_file đọc lại được nội dung đã ghi",
          "BOOM-INTEGRATION-OK" in r.get("content", ""))
    check("read_file báo đúng total_lines == 3", r.get("total_lines") == 3,
          f"total_lines={r.get('total_lines')}")
    check("file thật tồn tại trên đĩa", target.exists())

    # Phân trang offset/limit
    r2 = tools["read_file"]["func"](path=str(target), offset=2, limit=1)
    check("read_file offset=2,limit=1 trả đúng 1 dòng số 2",
          r2.get("content", "").startswith("2|"),
          repr(r2.get("content", ""))[:40])

    # Patch
    p = tools["patch_file"]["func"](path=str(target),
                                    old_string="BOOM-INTEGRATION-OK",
                                    new_string="BOOM-PATCHED")
    check("patch_file success + đúng 1 match",
          p.get("success") is True and p.get("matches") == 1, str(p))

    r3 = tools["read_file"]["func"](path=str(target))
    check("đọc lại sau patch thấy nội dung mới",
          "BOOM-PATCHED" in r3.get("content", "") and
          "BOOM-INTEGRATION-OK" not in r3.get("content", ""))

    # Error handling có cấu trúc, không raise
    try:
        missing = tools["read_file"]["func"](path=str(tmp_root / "khong-ton-tai.txt"))
        structured = isinstance(missing, dict) and "error" in missing
    except Exception as exc:  # không được raise lung tung
        structured = False
        print(f"       (unexpected raise: {type(exc).__name__}: {exc})")
    check("read_file file không tồn tại -> dict có 'error', không raise", structured)

    try:
        p_bad = tools["patch_file"]["func"](path=str(target),
                                            old_string="chuỗi-không-tồn-tại-xyz",
                                            new_string="x")
        patch_structured = isinstance(p_bad, dict) and p_bad.get("success") is False
    except Exception:
        patch_structured = False
    check("patch_file old_string không khớp -> success=False có cấu trúc",
          patch_structured)

    # ------------------------------------------------------------------
    # 4. Tool shell an toàn
    # ------------------------------------------------------------------
    print("\n-- 4. Pipeline shell (shell_run) --")
    s = tools["shell_run"]["func"](cmd=["echo", "boom-pipeline-ok"])
    check("shell_run echo -> ok=True", s.get("ok") is True, str(s)[:120])
    check("stdout chứa 'boom-pipeline-ok'",
          "boom-pipeline-ok" in s.get("stdout", ""))

    # Lệnh lỗi -> dict có cấu trúc, không raise
    try:
        bad = tools["shell_run"]["func"](cmd=["boom_binary_khong_ton_tai_xyz"])
        bad_structured = (isinstance(bad, dict) and bad.get("ok") is False
                          and {"returncode", "stdout", "stderr"} <= set(bad))
    except Exception:
        bad_structured = False
    check("shell_run binary không tồn tại -> ok=False, dict đủ khóa", bad_structured)

    # Lệnh nguy hiểm bị chặn -> không raise, không thực thi
    try:
        danger = tools["shell_run"]["func"](cmd="rm -rf /")
        blocked = (isinstance(danger, dict) and danger.get("ok") is False
                   and danger.get("returncode") == -1)
    except Exception:
        blocked = False
    check("shell_run 'rm -rf /' bị chặn (ok=False, returncode=-1), không raise",
          blocked)

    # ------------------------------------------------------------------
    # 5. Meta tool: search_tools
    # ------------------------------------------------------------------
    print("\n-- 5. Meta tool search_tools --")
    check("tool 'search_tools' có trong registry", "search_tools" in tools)
    # search_tools trả về {"keyword","count","tools"} -- "tools" là list các hit.
    found = tools["search_tools"]["func"](keyword="read_file")
    names = [t.get("name") for t in found.get("tools", [])]
    check("search_tools('read_file') tìm thấy 'read_file' (exact name match)",
          "read_file" in names, f"tìm được {found.get('count')} hits")

    empty_kw = tools["search_tools"]["func"](keyword="")
    check("search_tools keyword rỗng -> dict có 'error'",
          isinstance(empty_kw, dict) and "error" in empty_kw)

    # ------------------------------------------------------------------
    # Tổng kết
    # ------------------------------------------------------------------
    print(f"\n== KẾT QUẢ: {PASS} PASS, {FAIL} FAIL, tổng {PASS + FAIL} assertions ==")
    print(f"   tools đếm được trong registry: {len(tools)}")
    print(f"   thư mục test tạm: {tmp_root}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
