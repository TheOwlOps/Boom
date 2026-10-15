"""Test cho secretscan.py — chay bang: python3 test_secretscan.py.

Dung secret GIA MAU de kiem tra, khong co secret that nao o day.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_REPO = Path(__file__).resolve().parents[1]
_sys_src = str(_REPO / "agent" / "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)


from security.secretscan import (
    PATTERNS,
    scan_file,
    scan_dir,
    scan_diff,
    has_secrets,
    to_markdown,
    _mask,
)

PASSED = 0


def check(cond, msg):
    """Mot assertion: dem so luong PASS, raise neu FAIL."""
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS: " + msg)


def write_tmp(content, suffix=".py"):
    """Ghi noi dung ra file tam, tra ve duong dan."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_patterns_du_chat():
    # 1: du so luong pattern yeu cau (>= 7 loai)
    check(len(PATTERNS) >= 7, "PATTERNS co >= 7 loai (thuc te %d)" % len(PATTERNS))


def test_bat_6_loai_secret():
    cases = [
        ("aws_access_key", 'key = "AKIAIOSFODNN7EXAMPLE"\n'),
        ("github_token", 'token = "ghp_' + "a" * 36 + '"\n'),
        ("github_token_gho", 'token = "gho_' + "b" * 36 + '"\n'),
        ("npm_token", 'token = "npm_' + "c" * 36 + '"\n'),
        ("telegram_bot_token", 'bot = "12345678:AA' + "d" * 33 + '"\n'),
        ("private_key", "-----BEGIN RSA PRIVATE KEY-----\n"),
        ("password_assign", 'password = "supersecret123"\n'),
        ("api_key", 'api_key = "sk-live-abcdef123456"\n'),
        ("api_secret", 'api_secret = "s3cr3t-value-xyz-789"\n'),
    ]
    for name, content in cases:
        p = write_tmp(content)
        try:
            findings = scan_file(p)
            # 2..10: moi loai secret gia deu bi bat
            check(len(findings) >= 1, "bat duoc secret loai %s" % name)
        finally:
            os.unlink(p)


def test_preview_che_80_phan_tram():
    secret = "AKIAIOSFODNN7EXAMPLE"
    masked = _mask(secret)
    # 11: chi hien 4 ky tu dau
    check(masked == "AKIA" + "*" * 16, "preview che dung: %s" % masked)
    p = write_tmp('key = "%s"\n' % secret)
    try:
        findings = scan_file(p)
        # 12: preview trong finding khong lo toan bo secret
        check(secret not in findings[0]["preview"], "preview khong lo secret that")
        # 13: preview van giu 4 ky tu dau de nhan dien
        check(findings[0]["preview"].startswith("AKIA"), "preview giu 4 ky tu dau")
    finally:
        os.unlink(p)


def test_khong_false_positive():
    safe_lines = [
        ('API_KEY = os.environ.get("API_KEY")\n', "os.environ.get"),
        ('API_KEY = os.getenv("API_KEY")\n', "os.getenv"),
        ('token = "***"\n', "*** placeholder"),
        ('key = "xxx"\n', "xxx placeholder"),
        ('secret = "<redacted>"\n', "<redacted>"),
        ('password = "realpassword123"  # nosec-test\n', "# nosec-test marker"),
        ('password = "short"\n', "password ngan hon 8 ky tu"),
        ('print("hello world")\n', "dong thuong khong secret"),
    ]
    for content, label in safe_lines:
        p = write_tmp(content)
        try:
            findings = scan_file(p)
            # 14..21: cac truong hop an toan khong bi bao loi
            check(findings == [], "khong false positive: %s" % label)
        finally:
            os.unlink(p)


def test_scan_diff_chi_dong_them():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "index abc..def 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import os\n"
        '-api_key = "old-secret-value-111"\n'
        '+api_key = "new-secret-value-222"\n'
        " context = 1\n"
        '+password = "supersecret123"\n'
    )
    findings = scan_diff(diff)
    types = [f["type"] for f in findings]
    # 22: dong them moi co secret bi bat
    check("api_key" in types, "scan_diff bat secret tren dong them (+)")
    check("password_assign" in types, "scan_diff bat password tren dong them (+)")
    # 23: dong bi xoa (-) khong bi quet
    check(all("111" not in f["preview"] for f in findings),
          "scan_diff bo qua dong bi xoa (-)")
    # 24: so dong tinh dung theo file moi (dong +api_key o dong 2, +password o dong 4)
    by_type = {f["type"]: f["line"] for f in findings}
    check(by_type.get("api_key") == 2, "scan_diff danh so dong dung (api_key dong 2)")
    check(by_type.get("password_assign") == 4,
          "scan_diff danh so dong dung (password dong 4)")
    # 25: header +++ khong gay loi / khong bi quet nham
    check(all(f["line"] > 0 for f in findings), "scan_diff bo qua header +++")


def test_scan_dir_va_exclude():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app.py").write_text('api_key = "sk-live-abcdef123456"\n')
        # File chua secret nam trong cac thu muc phai exclude
        for d in [".git", "node_modules", "__pycache__", ".venv"]:
            sub = root / d
            sub.mkdir()
            (sub / "leak.py").write_text('password = "supersecret123"\n')
        findings = scan_dir(root)
        paths = [f["path"] for f in findings]
        # 26: tim thay secret o file thuong
        check(any("app.py" in p for p in paths), "scan_dir tim thay secret trong app.py")
        # 27: finding cua scan_dir co truong path
        check(all("path" in f for f in findings), "scan_dir gan truong path cho finding")
        # 28..31: cac thu muc exclude khong bi quet
        for d in [".git", "node_modules", "__pycache__", ".venv"]:
            check(all(d not in p for p in paths), "scan_dir bo qua thu muc %s" % d)


def test_has_secrets():
    p = write_tmp('token = "ghp_' + "a" * 36 + '"\n')
    q = write_tmp('print("sach")\n')
    try:
        # 32..33: has_secrets voi duong dan file
        check(has_secrets(p) is True, "has_secrets True khi file co secret")
        check(has_secrets(q) is False, "has_secrets False khi file sach")
        # 34..35: has_secrets voi list findings
        check(has_secrets(scan_file(p)) is True, "has_secrets True voi list findings")
        check(has_secrets([]) is False, "has_secrets False voi list rong")
    finally:
        os.unlink(p)
        os.unlink(q)


def test_to_markdown_tieng_viet():
    findings = [{"path": "app.py", "line": 3, "type": "api_key",
                 "preview": "sk-l" + "*" * 16}]
    md = to_markdown(findings)
    # 36: markdown tieng Viet, co bang
    check("Ket qua quet secret" in md, "to_markdown co tieu de tieng Viet")
    check("|" in md and "api_key" in md, "to_markdown render bang findings")
    check("os.environ.get" in md, "to_markdown nhac whitelist")
    # 37: khong co finding -> bao an toan
    check("Khong phat hien" in to_markdown([]), "to_markdown bao an toan khi sach")


def test_file_loi_khong_crash():
    # 38: file khong ton tai -> bo qua, tra ve [] (khong crash)
    check(scan_file("/tmp/khong-ton-tai-secretscan-xyz.py") == [],
          "scan_file file khong ton tai tra ve []")
    # 39: file nhi phan -> bo qua, tra ve []
    p = write_tmp("", suffix=".bin")
    try:
        with open(p, "wb") as f:
            f.write(b"\x00\x01\x02AKIA" + b"\xff" * 20)
        check(scan_file(p) == [], "scan_file bo qua file nhi phan")
    finally:
        os.unlink(p)


if __name__ == "__main__":
    test_patterns_du_chat()
    test_bat_6_loai_secret()
    test_preview_che_80_phan_tram()
    test_khong_false_positive()
    test_scan_diff_chi_dong_them()
    test_scan_dir_va_exclude()
    test_has_secrets()
    test_to_markdown_tieng_viet()
    test_file_loi_khong_crash()
    print("\nTONG: %d assertions PASS, 0 FAIL" % PASSED)
