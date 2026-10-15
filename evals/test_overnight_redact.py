# -*- coding: utf-8 -*-
"""Test cho agent/src/security/redact.py — chay bang python3 thuan (stdlib only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
_REPO = Path(__file__).resolve().parents[1]
_sys_src = str(_REPO / "agent" / "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)


from security.redact import redact_text, redact_dict, redact_args  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


# --- 5 loai secret trong text ------------------------------------------------
GITHUB = "ghp_" + "a" * 36
NPM = "npm_" + "b" * 36
TELE = "123456789:AAH" + "c" * 32
AWS = "AKIA" + "D" * 16
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBsecretdata\n-----END RSA PRIVATE KEY-----"

t1 = redact_text(f"token {GITHUB} het")
check("github bi che", GITHUB not in t1)
check("github giu prefix + label", t1.startswith("token ghp_****[REDACTED:github]"))

t2 = redact_text(f"npm token {NPM} day")
check("npm bi che", NPM not in t2 and "[REDACTED:npm]" in t2)

t3 = redact_text(f"bot {TELE} online")
check("telegram bi che", TELE not in t3 and "[REDACTED:telegram]" in t3)

t4 = redact_text(f"key {AWS} dung")
check("aws bi che", AWS not in t4 and "[REDACTED:aws]" in t4)

t5 = redact_text(f"key:\n{PEM}\nhet")
check("private key bi che", "MIIBsecretdata" not in t5 and "[REDACTED:privatekey]" in t5)

t6 = redact_text("login password=Sup3rSecret123 done")
check("password= bi che", "Sup3rSecret123" not in t6)
check("password= giu key + label", "password=Sup3****[REDACTED:credential]" in t6)

t7 = redact_text("cfg api_key: xyz-999-abc")
check("api_key: bi che", "xyz-999-abc" not in t7 and "[REDACTED:credential]" in t7)

t8 = redact_text("khong co secret gi o day")
check("text sach giu nguyen", t8 == "khong co secret gi o day")

t9 = redact_text(f"nhieu secret: {GITHUB} va {NPM}")
check("nhieu secret cung che het",
      GITHUB not in t9 and NPM not in t9
      and "[REDACTED:github]" in t9 and "[REDACTED:npm]" in t9)

# --- redact_dict: nested ------------------------------------------------------
d = {
    "user": "son",
    "password": "matkhau123",
    "config": {
        "api_key": "sk-live-999",
        "nested": [{"auth_token": "tok_abcdef", "ok": 1}],
    },
    "note": f"debug {GITHUB}",
    "count": 42,
}
r = redact_dict(d)
check("dict: password bi che", r["password"] != "matkhau123" and "matk****" in r["password"])
check("dict nested: api_key bi che", r["config"]["api_key"] != "sk-live-999")
check("dict nested list: auth_token bi che", r["config"]["nested"][0]["auth_token"] != "tok_abcdef")
check("dict: secret trong string thuong bi che", GITHUB not in r["note"])
check("dict: key thuong giu nguyen", r["user"] == "son" and r["count"] == 42)
check("dict goc khong bi sua", d["password"] == "matkhau123")

d2 = {"SECRET_KEY": "abcXYZ", "PublicInfo": "hello"}
r2 = redact_dict(d2)
check("dict: key hoa SECRET_KEY van che (case-insensitive)", r2["SECRET_KEY"] != "abcXYZ")

# --- redact_args --------------------------------------------------------------
a = redact_args("gmail_send", {"to": "a@b.com", "token": "secrettoken1", "body": "hi"})
check("redact_args che token", a["token"] != "secrettoken1" and "[REDACTED" in a["token"])
check("redact_args giu field thuong", a["to"] == "a@b.com" and a["body"] == "hi")
check("redact_args None -> {}", redact_args("x", None) == {})

# --- input la: khong crash -----------------------------------------------------
check("None -> ''", redact_text(None) == "")
check("int -> str", redact_text(12345) == "12345")
check("redact_dict(None) khong crash", redact_dict(None) is None)
check("redact_dict(int) khong crash", redact_dict(7) == 7)
check("redact_dict(list) che tung phan tu",
      redact_dict(["ok", f"k {NPM}"]) == ["ok", f"k npm_****[REDACTED:npm]"])
check("redact_dict key khong phai str khong crash",
      redact_dict({1: "v", (2,): "w"}) == {1: "v", (2,): "w"})
check("redact_args voi args la string", redact_args("t", "plain") == {"_value": "plain"})
check("tuple trong dict duoc xu ly", redact_dict({"t": ("a", "b")}) == {"t": ("a", "b")})

print(f"\n== {PASS} PASS, {FAIL} FAIL ==")
sys.exit(1 if FAIL else 0)
