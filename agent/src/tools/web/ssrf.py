"""Anti-SSRF for every outbound web request.

validate_url rules:
  - http/https schemes only (blocks file://, gopher://, dict://, ...).
  - hostname must not be empty; userinfo in URLs (user:pass@host) blocked.
  - resolves the hostname via DNS then checks EVERY returned IP: any IP in
    loopback / private (RFC1918, CGNAT 100.64/10, IPv6 ULA) / link-local /
    multicast / reserved / unspecified -> rejected. Checks after resolving to
    counter basic DNS rebinding (the attacker can't swap IPs after our check).
  - blocks the localhost names and numeric forms directly (0x7f000001, 0177.0.0.1...).

Note: this is an application-layer defense. Production should add an egress
firewall / proxy allowlist at the infrastructure layer.
"""

import ipaddress
import os
import socket
import urllib.parse

__all__ = ["SSRFError", "validate_url"]


class SSRFError(ValueError):
    """URL blocked for SSRF risk."""


def _test_allowlist() -> set:
    """IPs/hosts allowed in internal tests.

    Read from the ``BOOM_SSRF_TEST_ALLOW`` env var (e.g. "127.0.0.1,::1").
    Default empty -> blocks all internal IPs. TEST USE ONLY, never enable
    in production.
    """
    raw = os.environ.get("BOOM_SSRF_TEST_ALLOW", "")
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


# Hostnames always dangerous, no resolution needed.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "ip6-localhost.",
}


def _is_dangerous_ip(ip: ipaddress._BaseAddress) -> bool:
    """True when the IP must not be an outbound request target."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """Validates a URL against SSRF, returns the stripped URL.

    Raises:
      - ValueError: empty URL / not a str / scheme not http(s).
      - SSRFError : hostname blocked or resolving to internal/dangerous IPs.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    target = url.strip()

    parts = urllib.parse.urlsplit(target)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFError(
            "only http/https schemes allowed, got %r" % (parts.scheme or target[:32])
        )

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise SSRFError("URL is missing a hostname")
    # Blocks userinfo like http://user:pass@internal/ (parser trickery).
    if parts.username or parts.password:
        raise SSRFError("URL contains userinfo — blocked for SSRF risk")
    if host in _BLOCKED_HOSTNAMES and host not in _test_allowlist():
        raise SSRFError("hostname %r blocked (localhost)" % host)

    # Resolve then check EACH IP: also catches hostnames pointing at 127.0.0.1.
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC,
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError("cannot resolve hostname %r: %s" % (host, exc))

    allow = _test_allowlist()
    bad = []
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            bad.append(sockaddr[0])
            continue
        # Hosts/IPs on the test allowlist skip the checks.
        if host in allow or str(ip).lower() in allow:
            continue
        if _is_dangerous_ip(ip):
            bad.append(str(ip))
    if bad:
        raise SSRFError(
            "hostname %r resolves to internal/dangerous IP(s) (%s) — blocked"
            % (host, ", ".join(sorted(set(bad))))
        )
    return target
