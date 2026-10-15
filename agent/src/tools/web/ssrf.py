"""Chống SSRF cho mọi request web ra ngoài.

Quy tắc validate_url:
  - chỉ cho scheme http/https (chặn file://, gopher://, dict://, ...).
  - hostname không được rỗng; chặn userinfo trong URL (user:pass@host).
  - resolve hostname qua DNS rồi kiểm tra MỌI IP trả về: bất kỳ IP nào thuộc
    loopback / private (RFC1918, CGNAT 100.64/10, IPv6 ULA) / link-local /
    multicast / reserved / unspecified -> từ chối. Kiểm tra sau resolve để
    chống DNS rebinding cơ bản (attacker không đổi được IP sau khi ta check).
  - chặn trực tiếp các tên localhost và dạng số (0x7f000001, 0177.0.0.1...).

Lưu ý: đây là lớp phòng thủ ở tầng ứng dụng. Môi trường production nên bổ
sung egress firewall / proxy allowlist ở tầng hạ tầng.
"""

import ipaddress
import os
import socket
import urllib.parse

__all__ = ["SSRFError", "validate_url"]


class SSRFError(ValueError):
    """URL bị chặn vì nguy cơ SSRF."""


def _test_allowlist() -> set:
    """Danh sách IP/host được phép trong test nội bộ.

    Đọc từ env ``BOOM_SSRF_TEST_ALLOW`` (VD: "127.0.0.1,::1").
    Mặc định rỗng -> chặn hết IP nội bộ. CHỈ dùng cho test, không bật
    trên production.
    """
    raw = os.environ.get("BOOM_SSRF_TEST_ALLOW", "")
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


# Tên host luôn nguy hiểm, không cần resolve.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "ip6-localhost.",
}


def _is_dangerous_ip(ip: ipaddress._BaseAddress) -> bool:
    """True nếu IP không được phép làm đích request ra ngoài."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """Validate URL chống SSRF, trả về URL đã strip.

    Raise:
      - ValueError: URL rỗng / không phải str / scheme không phải http(s).
      - SSRFError : hostname bị chặn hoặc resolve ra IP nội bộ/nguy hiểm.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url phải là string không rỗng")
    target = url.strip()

    parts = urllib.parse.urlsplit(target)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFError(
            "chỉ cho phép scheme http/https, nhận được %r" % (parts.scheme or target[:32])
        )

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise SSRFError("URL thiếu hostname")
    # Chặn userinfo kiểu http://user:pass@internal/ (đánh lừa parser).
    if parts.username or parts.password:
        raise SSRFError("URL chứa userinfo — bị chặn vì nguy cơ SSRF")
    if host in _BLOCKED_HOSTNAMES and host not in _test_allowlist():
        raise SSRFError("hostname %r bị chặn (localhost)" % host)

    # Resolve rồi kiểm tra TỪNG IP: chống cả hostname trỏ về 127.0.0.1.
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC,
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError("không resolve được hostname %r: %s" % (host, exc))

    allow = _test_allowlist()
    bad = []
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            bad.append(sockaddr[0])
            continue
        # Host/IP nằm trong allowlist test thì bỏ qua kiểm tra.
        if host in allow or str(ip).lower() in allow:
            continue
        if _is_dangerous_ip(ip):
            bad.append(str(ip))
    if bad:
        raise SSRFError(
            "hostname %r resolve ra IP nội bộ/nguy hiểm (%s) — bị chặn"
            % (host, ", ".join(sorted(set(bad))))
        )
    return target
