"""Pre-deploy checklist tự động cho Boom (skill deployer).

Chạy 6 nhóm kiểm tra trước khi deploy:
  1. tests_pass        — chạy test suite, phải pass hết
  2. no_secrets        — quét source code tìm secret/key/token dính cứng
  3. version_bumped    — version hiện tại phải mới hơn git tag gần nhất
  4. changelog_updated — CHANGELOG phải được cập nhật trong commit gần nhất
  5. env_documented    — mọi biến môi trường code dùng đều có trong .env.example
  6. backup_plan       — có script backup/rollback hoặc tài liệu backup

Kết quả: list dict {check, status: pass|fail|warn, detail}.

Chỉ dùng stdlib.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Hằng số
# ---------------------------------------------------------------------------

# Mẫu secret thường gặp: key/token dính cứng trong code.
# Trừ comment chỉ ví dụ (SKIP) — để tránh false positive khi dev ghi chú.
_SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub token"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
    (r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
     "hardcoded credential"),
]

# Thư mục/file bỏ qua khi quét secret
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".tox"}
_SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
              ".tar", ".gz", ".lock"}

# Biến môi trường mà code dùng: os.getenv("X") / os.environ["X"]
_ENV_USAGE_PATTERN = re.compile(
    r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|os\.environ(?:\[['\"]([A-Z][A-Z0-9_]*)['\"]|\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"])"
)

_VERSION_FILES = ("pyproject.toml", "package.json", "VERSION", "setup.py")


# ---------------------------------------------------------------------------
# Helper nội bộ
# ---------------------------------------------------------------------------

def _ket_qua(check, status, detail):
    """Dựng 1 mục kết quả chuẩn."""
    return {"check": check, "status": status, "detail": detail}


def _chay_lenh(cmd, cwd, timeout=120):
    """Chạy lệnh shell, trả về (returncode, stdout+stderr rút gọn)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), shell=True, capture_output=True,
            text=True, timeout=timeout)
        out = (proc.stdout + proc.stderr)[-2000:]  # chỉ giữ tail cho gọn
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout sau %ds" % timeout
    except OSError as exc:
        return 127, "không chạy được lệnh: %s" % exc


def _la_git_repo(root):
    return (Path(root) / ".git").is_dir()


# ---------------------------------------------------------------------------
# 6 nhóm check
# ---------------------------------------------------------------------------

def check_tests_pass(project_dir, test_command="python3 -m pytest -q"):
    """Chạy test suite; pass khi returncode == 0."""
    rc, out = _chay_lenh(test_command, project_dir)
    if rc == 0:
        return _ket_qua("tests_pass", "pass",
                        "test pass: %s" % _tom_tat_test(out))
    return _ket_qua("tests_pass", "fail",
                    "test FAIL (rc=%d): %s" % (rc, _tom_tat_test(out)))


def _tom_tat_test(out):
    """Lấy dòng summary cuối cùng của output test (vd '42 passed')."""
    dong = [d.strip() for d in out.splitlines() if d.strip()]
    return dong[-1][:120] if dong else "(không có output)"


def check_no_secrets(project_dir):
    """Quét source tìm secret dính cứng. Phát hiện -> fail."""
    root = Path(project_dir)
    phat_hien = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in _SKIP_EXTS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for i, dong in enumerate(text.splitlines(), start=1):
            if "SKIP" in dong or "example" in dong.lower():
                continue  # bỏ qua dòng ví dụ minh họa
            for pattern, mo_ta in _SECRET_PATTERNS:
                if re.search(pattern, dong):
                    phat_hien.append(
                        "%s:%d (%s)" % (p.relative_to(root), i, mo_ta))
                    break  # mỗi dòng chỉ báo 1 lần
        if len(phat_hien) >= 20:
            break  # đủ bằng chứng, dừng sớm
    if phat_hien:
        return _ket_qua("no_secrets", "fail",
                        "phát hiện %d vị trí nghi có secret: %s"
                        % (len(phat_hien), "; ".join(phat_hien[:5])))
    return _ket_qua("no_secrets", "pass", "không phát hiện secret trong code")


def _doc_version(root):
    """Đọc version hiện tại từ version file; trả về None nếu không có."""
    for ten in _VERSION_FILES:
        p = root / ten
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if ten == "pyproject.toml":
            m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']',
                          text, re.MULTILINE)
            if m:
                return m.group(1)
        elif ten == "package.json":
            m = re.search(r'"version"\s*:\s*"([^"]+)"', text)
            if m:
                return m.group(1)
        else:  # VERSION, setup.py
            m = re.search(r"(\d+\.\d+\.\d+)", text)
            if m:
                return m.group(1)
    return None


def check_version_bumped(project_dir):
    """Version hiện tại phải khác/mới hơn tag gần nhất (dạng vX.Y.Z).

    Không có git hoặc không có tag -> warn (không chặn, nhưng nhắc nhở).
    """
    root = Path(project_dir)
    version = _doc_version(root)
    if version is None:
        return _ket_qua("version_bumped", "warn",
                        "không tìm thấy version trong %s" % _VERSION_FILES)
    if not _la_git_repo(root):
        return _ket_qua("version_bumped", "warn",
                        "không phải git repo, không đối chiếu được tag")
    rc, out = _chay_lenh("git describe --tags --abbrev=0", root)
    if rc != 0:
        return _ket_qua("version_bumped", "warn",
                        "không có git tag để đối chiếu (version hiện tại %s)"
                        % version)
    tag = out.strip().lstrip("v")
    if tag == version:
        return _ket_qua("version_bumped", "fail",
                        "version %s TRÙNG với tag gần nhất %s — chưa bump"
                        % (version, out.strip()))
    return _ket_qua("version_bumped", "pass",
                    "version hiện tại %s khác tag gần nhất %s"
                    % (version, out.strip()))


def check_changelog_updated(project_dir, n_commits=5):
    """CHANGELOG phải thay đổi trong n_commits gần nhất (hoặc file có
    mục Unreleased). Không có CHANGELOG -> warn."""
    root = Path(project_dir)
    changelog = None
    for ten in ("CHANGELOG.md", "CHANGELOG", "ChangeLog.md", "NEWS.md"):
        if (root / ten).is_file():
            changelog = ten
            break
    if changelog is None:
        return _ket_qua("changelog_updated", "warn",
                        "không tìm thấy file CHANGELOG")
    try:
        text = (root / changelog).read_text(encoding="utf-8")
    except OSError:
        return _ket_qua("changelog_updated", "warn",
                        "không đọc được %s" % changelog)
    if re.search(r"(?im)^##\s*\[?unreleased\]?", text):
        return _ket_qua("changelog_updated", "pass",
                        "%s có mục Unreleased" % changelog)
    if _la_git_repo(root):
        rc, out = _chay_lenh(
            "git log -%d --name-only --pretty=format:" % n_commits, root)
        if rc == 0 and changelog in out:
            return _ket_qua("changelog_updated", "pass",
                            "%s được cập nhật trong %d commit gần nhất"
                            % (changelog, n_commits))
        return _ket_qua("changelog_updated", "warn",
                        "%s KHÔNG thay đổi trong %d commit gần nhất"
                        % (changelog, n_commits))
    return _ket_qua("changelog_updated", "pass",
                    "tìm thấy %s (không phải git repo, bỏ qua check git)"
                    % changelog)


def _thu_thap_env_dung(root):
    """Tập hợp biến môi trường mà code Python/JS dùng."""
    bien = set()
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in (".py", ".js", ".ts", ".sh"):
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _ENV_USAGE_PATTERN.finditer(text):
            ten = m.group(1) or m.group(2) or m.group(3)
            if ten:
                bien.add(ten)
    return bien


def check_env_documented(project_dir):
    """Mọi biến môi trường code dùng phải có trong .env.example (hoặc README).

    Thiếu -> fail (deploy sẽ thiếu config).
    """
    root = Path(project_dir)
    bien_dung = _thu_thap_env_dung(root)
    if not bien_dung:
        return _ket_qua("env_documented", "pass",
                        "code không dùng biến môi trường nào")
    tai_lieu = ""
    for ten in (".env.example", ".env.sample", "README.md", "docs/ENV.md"):
        p = root / ten
        if p.is_file():
            try:
                tai_lieu += p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                pass
    thieu = sorted(b for b in bien_dung if b not in tai_lieu)
    if thieu:
        return _ket_qua("env_documented", "fail",
                        "thiếu document cho %d biến: %s"
                        % (len(thieu), ", ".join(thieu)))
    return _ket_qua("env_documented", "pass",
                    "%d/%d biến môi trường đã được document"
                    % (len(bien_dung), len(bien_dung)))


def check_backup_plan(project_dir):
    """Có script backup/rollback hoặc tài liệu backup. Không có -> warn."""
    root = Path(project_dir)
    tim_thay = []
    for p in root.rglob("*"):
        ten = p.name.lower()
        if "backup" in ten or "rollback" in ten:
            if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
                tim_thay.append(str(p.relative_to(root)))
        if len(tim_thay) >= 5:
            break
    if tim_thay:
        return _ket_qua("backup_plan", "pass",
                        "tìm thấy: %s" % ", ".join(tim_thay[:3]))
    return _ket_qua("backup_plan", "warn",
                    "không tìm thấy script/tài liệu backup hoặc rollback")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_checklist(project_dir, test_command="python3 -m pytest -q"):
    """Chạy toàn bộ pre-deploy checklist. Trả về list {check, status, detail}."""
    return [
        check_tests_pass(project_dir, test_command),
        check_no_secrets(project_dir),
        check_version_bumped(project_dir),
        check_changelog_updated(project_dir),
        check_env_documented(project_dir),
        check_backup_plan(project_dir),
    ]


def deploy_blocked(results):
    """True nếu có bất kỳ check nào fail -> KHÔNG được deploy."""
    return any(r["status"] == "fail" for r in results)


if __name__ == "__main__":
    import sys
    import json
    thu_muc = sys.argv[1] if len(sys.argv) > 1 else "."
    lenh_test = sys.argv[2] if len(sys.argv) > 2 else "python3 -m pytest -q"
    ket_qua = run_checklist(thu_muc, lenh_test)
    print(json.dumps(ket_qua, ensure_ascii=False, indent=2))
    if deploy_blocked(ket_qua):
        print("\nBLOCKED: có check FAIL — không deploy.", file=sys.stderr)
        sys.exit(1)
    print("\nOK: không có check fail.")
