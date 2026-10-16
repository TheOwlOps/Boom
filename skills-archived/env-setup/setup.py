#!/usr/bin/env python3
"""Skill env-setup: kiểm tra môi trường dev trước khi chạy/build.

Các check:
  - check_python(min_version): version Python có đủ mới không
  - check_deps(packages): các package Python đã cài chưa (import được không)
  - check_env_vars(names): các biến môi trường bắt buộc đã set chưa
  - check_ports(ports): các port có còn trống không (thử bind)

Output chuẩn: list[CheckResult] với schema {check, status, fix_command}
để agent đọc và tự sửa theo gợi ý.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import socket
import sys
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

# Trạng thái check: ok = đạt, fail = lỗi cần sửa, warn = cảnh báo (chưa dùng nhiều)
Status = Literal["ok", "fail", "warn"]


class CheckResult(BaseModel):
    """Kết quả 1 mục kiểm tra môi trường."""

    check: str  # tên mục kiểm tra, vd: "dep:pydantic"
    status: Status  # "ok" | "fail" | "warn"
    detail: str = ""  # chi tiết ngắn gọn
    fix_command: str = ""  # lệnh gợi ý để sửa (rỗng nếu đã ok)


class EnvConfig(BaseModel):
    """Cấu hình 1 đợt kiểm tra môi trường."""

    min_python: str = "3.10"
    deps: list[str] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)


def _parse_version(s: str) -> tuple[int, int, int]:
    """Parse '3.10' -> (3, 10, 0); bỏ qua hậu tố lạ như 'rc1'."""
    nums: list[int] = []
    for p in s.strip().split("."):
        m = re.match(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def check_python(min_version: str = "3.10") -> CheckResult:
    """Kiểm tra version Python hiện tại có >= min_version không."""
    want = _parse_version(min_version)
    cur = sys.version_info[:3]
    ok = cur >= want
    cur_s = ".".join(map(str, cur))
    return CheckResult(
        check=f"Python >= {min_version}",
        status="ok" if ok else "fail",
        detail=f"đang dùng Python {cur_s}",
        fix_command="" if ok else (
            f"Cài Python {min_version}+ từ python.org, "
            f"hoặc: sudo apt install python{min_version}"
        ),
    )


def check_deps(packages: Iterable[str]) -> list[CheckResult]:
    """Kiểm tra từng package có import được không (đã cài chưa)."""
    results: list[CheckResult] = []
    for pkg in packages:
        pkg = pkg.strip()
        if not pkg:
            continue
        found = importlib.util.find_spec(pkg) is not None
        results.append(CheckResult(
            check=f"dep:{pkg}",
            status="ok" if found else "fail",
            detail="đã cài, import được" if found else "chưa cài / không import được",
            fix_command="" if found else f"pip install {pkg}",
        ))
    return results


def check_env_vars(names: Iterable[str]) -> list[CheckResult]:
    """Kiểm tra các biến môi trường bắt buộc đã được set (và không rỗng)."""
    results: list[CheckResult] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        value = os.environ.get(name)
        ok = bool(value)
        results.append(CheckResult(
            check=f"env:{name}",
            status="ok" if ok else "fail",
            detail="đã set" if ok else "chưa set hoặc rỗng",
            fix_command="" if ok else f"export {name}=<giá-trị>  # hoặc thêm vào ~/.bashrc",
        ))
    return results


def check_ports(ports: Iterable[int]) -> list[CheckResult]:
    """Kiểm tra port còn trống không bằng cách thử bind 127.0.0.1.

    Bind được -> port trống (ok). Bind lỗi -> đang bị tiến trình khác chiếm.
    """
    results: list[CheckResult] = []
    for port in ports:
        port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Thử bind: thành công nghĩa là chưa ai dùng port này
            sock.bind(("127.0.0.1", port))
            free = True
        except OSError:
            free = False
        finally:
            sock.close()
        results.append(CheckResult(
            check=f"port:{port}",
            status="ok" if free else "fail",
            detail="còn trống" if free else "đang bị tiến trình khác chiếm",
            fix_command="" if free else (
                f"lsof -ti :{port} | xargs kill -9  # hoặc đổi sang port khác"
            ),
        ))
    return results


def run_all(config: EnvConfig) -> list[CheckResult]:
    """Chạy toàn bộ check theo cấu hình, trả về list[CheckResult]."""
    results: list[CheckResult] = [check_python(config.min_python)]
    results.extend(check_deps(config.deps))
    results.extend(check_env_vars(config.env_vars))
    results.extend(check_ports(config.ports))
    return results


def all_ok(results: Iterable[CheckResult]) -> bool:
    """True khi mọi check đều ok (không có fail/warn)."""
    return all(r.status == "ok" for r in results)


def results_to_json(results: Iterable[CheckResult]) -> str:
    """Serialize kết quả ra JSON (đúng schema {check, status, fix_command})."""
    return json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="env-setup: kiểm tra môi trường dev")
    ap.add_argument("--min-python", default="3.10", help="version Python tối thiểu")
    ap.add_argument("--dep", action="append", default=[], help="package cần có (lặp lại được)")
    ap.add_argument("--env", action="append", default=[], help="biến môi trường bắt buộc (lặp lại được)")
    ap.add_argument("--port", action="append", default=[], type=int, help="port cần trống (lặp lại được)")
    ap.add_argument("--json", action="store_true", help="in JSON thay vì bảng")
    args = ap.parse_args(argv)

    config = EnvConfig(
        min_python=args.min_python, deps=args.dep, env_vars=args.env, ports=args.port,
    )
    results = run_all(config)

    if args.json:
        print(results_to_json(results))
    else:
        # Bảng text gọn để đọc nhanh trên terminal
        print(f"{'CHECK':<28} {'STATUS':<6} FIX")
        print("-" * 70)
        for r in results:
            print(f"{r.check:<28} {r.status:<6} {r.fix_command or '-'}")
        failed = [r.check for r in results if r.status != "ok"]
        if failed:
            print(f"\nChưa đạt {len(failed)}/{len(results)} mục.")
    return 0 if all_ok(results) else 1


if __name__ == "__main__":
    sys.exit(main())
