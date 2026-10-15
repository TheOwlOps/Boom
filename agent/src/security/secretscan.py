"""Quet secret truoc khi commit/push.

Module nay giup phat hien som cac secret bi lot vao source code
(API key, token, private key, mat khau...) de chan truoc khi push len git.

Chi dung thu vien chuan (stdlib), khong phu thuoc ben ngoai.
"""

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# PATTERNS: danh sach (ten_loai, regex, nhom_chua_secret)
# - nhom_chua_secret = 0 nghia la ca match chinh la secret
# - nhom_chua_secret = 1 nghia la group 1 cua regex la gia tri secret
# ---------------------------------------------------------------------------
PATTERNS = [
    # AWS Access Key ID: bat dau bang AKIA + 16 ky tu hoa/so
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), 0),
    # GitHub token: ghp_ / gho_ / ghu_ / ghs_ / ghr_ + chuoi dai
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}"), 0),
    # npm token: npm_ + chuoi dai
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{36,}"), 0),
    # Telegram bot token: <bot_id>:<35 ky tu>
    ("telegram_bot_token", re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"), 0),
    # Private key PEM: dong header -----BEGIN ... PRIVATE KEY-----
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"), 0),
    # password = "..." (gia tri tu 8 ky tu tro len, trong ngoac kep/don)
    ("password_assign", re.compile(
        r"(?i)\bpassword\s*[:=]\s*[\"']([^\"']{8,})[\"']"), 1),
    # api_key = "..." / api_key: "..." (cho phep khong co ngoac)
    ("api_key", re.compile(
        r"(?i)\bapi[_-]?key\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+]{8,})[\"']?"), 1),
    # api_secret = "..." / api_secret: "..."
    ("api_secret", re.compile(
        r"(?i)\bapi[_-]?secret\s*[:=]\s*[\"']?([A-Za-z0-9_\-./+]{8,})[\"']?"), 1),
]

# ---------------------------------------------------------------------------
# Whitelist: cac dau hieu cho thay dong nay KHONG phai secret that
# ---------------------------------------------------------------------------
# Marker cho phep danh dau dong test/vi du:  api_key = "fake"  # nosec-test
NOSEC_MARKER = "# nosec-test"


def _is_whitelisted(line):
    """Tra ve True neu dong nay nam trong whitelist (bo qua, khong bao loi)."""
    low = line.lower()
    # Doc secret tu bien moi truong -> an toan
    if "os.environ.get" in line or "os.getenv" in line:
        return True
    # Placeholder da che
    if "***" in line:
        return True
    if "<redacted>" in low:
        return True
    # xxx / XXX la gia tri gia mau
    if re.search(r"x{3,}", low):
        return True
    # Marker danh dau dong test/vi du
    if NOSEC_MARKER in line:
        return True
    return False


def _mask(secret):
    """Che 80% gia tri: chi giu lai 4 ky tu dau, phan con lai thanh '*'."""
    if len(secret) <= 4:
        return "****"
    return secret[:4] + "*" * (len(secret) - 4)


def _scan_line(line, line_no):
    """Quet mot dong, tra ve list finding cho dong do."""
    findings = []
    if _is_whitelisted(line):
        return findings
    for name, pattern, group in PATTERNS:
        for match in pattern.finditer(line):
            secret = match.group(group)
            findings.append({
                "line": line_no,
                "type": name,
                "preview": _mask(secret),
            })
    return findings


def scan_file(path):
    """Quet mot file, tra ve [{line, type, preview}].

    - File nhi phan (chua byte NUL) hoac doc loi -> bo qua, tra ve [].
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return []
    if "\x00" in text:
        return []  # file nhi phan
    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        findings.extend(_scan_line(line, i))
    return findings


def scan_dir(root, exclude=None):
    """Quet de quy mot thu muc, tra ve [{path, line, type, preview}].

    - exclude: ten thu muc bo qua (mac dinh: .git, node_modules,
      __pycache__, .venv).
    """
    if exclude is None:
        exclude = [".git", "node_modules", "__pycache__", ".venv"]
    exclude = set(exclude)
    root = Path(root)
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Loai bo thu muc exclude ngay tai cho (khong di sau vao)
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            for item in scan_file(fpath):
                item["path"] = str(fpath.relative_to(root))
                findings.append(item)
    return findings


def scan_diff(diff_text):
    """Quet mot unified diff, chi xet cac dong duoc THEM MOI.

    - Dong them moi bat dau bang '+' (bo qua dong header '+++').
    - 'line' la so dong trong file moi (tinh tu hunk header @@).
    - Tra ve [{line, type, preview}].
    """
    findings = []
    new_line = 0
    for raw in diff_text.splitlines():
        # Hunk header: @@ -a,b +c,d @@ -> lay so dong bat dau cua file moi
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            new_line = int(m.group(1))
            continue
        if raw.startswith("+++"):
            continue  # header ten file, khong phai noi dung
        if raw.startswith("+"):
            findings.extend(_scan_line(raw[1:], new_line))
            new_line += 1
        elif raw.startswith("-"):
            continue  # dong bi xoa: khong tang so dong file moi
        else:
            # Dong context (bat dau bang ' ' hoac dong trong): van dem dong
            if new_line:
                new_line += 1
    return findings


def has_secrets(target):
    """Tra ve True neu co secret.

    - target co the la: list findings, duong dan file, hoac duong dan thu muc.
    """
    if isinstance(target, list):
        return len(target) > 0
    p = Path(target)
    if p.is_dir():
        return len(scan_dir(p)) > 0
    return len(scan_file(p)) > 0


def to_markdown(findings):
    """Render findings thanh markdown tieng Viet."""
    if not findings:
        return "## Ket qua quet secret\n\nKhong phat hien secret nao. An toan de commit/push.\n"
    lines = [
        "## Ket qua quet secret",
        "",
        "Phat hien **%d** vi tri nghi ngo chua secret. Vui long kiem tra va xoa truoc khi commit/push." % len(findings),
        "",
        "| File | Dong | Loai | Xem truoc (da che) |",
        "| ---- | ---- | ---- | ------------------ |",
    ]
    for f in findings:
        fpath = f.get("path", "")
        lines.append("| %s | %s | `%s` | `%s` |" % (
            fpath, f["line"], f["type"], f["preview"]))
    lines += [
        "",
        "> Luu y: doc secret tu `os.environ.get(...)` hoac dung placeholder",
        "> (`***`, `xxx`, `<redacted>`, `# nosec-test`) se khong bi bao loi.",
        "",
    ]
    return "\n".join(lines)


def main(argv):
    """CLI don gian: python3 secretscan.py <file|thu_muc> [--diff-file <file.diff>]."""
    if len(argv) < 2:
        print("Dung: python3 secretscan.py <file|thu_muc> [--diff-file <file.diff>]")
        return 2
    target = Path(argv[1])
    if "--diff-file" in argv:
        idx = argv.index("--diff-file")
        diff_text = Path(argv[idx + 1]).read_text(encoding="utf-8", errors="replace")
        findings = scan_diff(diff_text)
    elif target.is_dir():
        findings = scan_dir(target)
    else:
        findings = scan_file(target)
        for f in findings:
            f["path"] = str(target)
    print(to_markdown(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
