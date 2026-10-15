"""Minimal RFC6455 WebSocket client over `socket`. stdlib only, TLS via `ssl`.

Only what the Chrome DevTools Protocol needs: an opening handshake, masked
client->server frames, unmasked server->client frames, FIN + opcodes (text,
binary, ping -> pong, close, continuation for fragmented messages) and the
7-bit / 16-bit / 64-bit payload lengths. `wss://` wraps the socket in an
`ssl.SSLContext`, which is what the Discord gateway needs.

Unit-testable without a browser: everything here takes a plain socket, so a
test can drive it against a socket.socketpair() or a local listener.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from typing import Optional, Tuple, Union

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(Exception):
    """Handshake or framing failure."""


def parse_ws_url(url: str) -> Tuple[str, int, str]:
    """ws://host:port/path -> (host, port, path). wss:// defaults to port 443."""
    from urllib.parse import urlsplit

    parts = urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        raise WSError(f"not a ws:// or wss:// URL: {url!r}")
    if not parts.hostname:
        raise WSError(f"{scheme}:// URL has no host: {url!r}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.hostname, parts.port or (443 if scheme == "wss" else 80), path


def encode_frame(
    payload: Union[bytes, bytearray, str],
    opcode: int = OP_TEXT,
    fin: bool = True,
    mask: bool = True,
) -> bytes:
    """One RFC6455 frame. Clients must mask; `mask=False` is for servers."""
    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    n = len(data)
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    mask_bit = 0x80 if mask else 0x00
    if n < 126:
        head.append(mask_bit | n)
    elif n < 1 << 16:
        head.append(mask_bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mask_bit | 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + data
    key = os.urandom(4)
    head += key
    return bytes(head) + bytes(b ^ key[i & 3] for i, b in enumerate(data))


def decode_frame(read) -> Tuple[bool, int, bytes, bool]:
    """Read one frame with `read(n)` (exactly n bytes).

    Returns (fin, opcode, payload, was_masked).
    """
    b0, b1 = read(2)
    length = b1 & 0x7F
    masked = bool(b1 & 0x80)
    if length == 126:
        length = struct.unpack("!H", read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read(8))[0]
    key = read(4) if masked else b""
    payload = read(length) if length else b""
    if masked:
        payload = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
    return bool(b0 & 0x80), b0 & 0x0F, payload, masked


class FrameReader:
    """Buffered `read(n)` over a socket, for decode_frame."""

    def __init__(self, sock: socket.socket, timeout: Optional[float] = None):
        self.sock = sock
        self.timeout = timeout
        self.buf = b""
        self.closed = False

    def read(self, n: int) -> bytes:
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                self.closed = True
                raise WSError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class WebSocket:
    """Blocking client socket speaking RFC6455."""

    def __init__(self, sock: socket.socket, timeout: float = 30.0):
        self.sock = sock
        self.timeout = timeout
        self.reader = FrameReader(sock, timeout)
        self.closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 30.0,
                context: Optional[ssl.SSLContext] = None) -> "WebSocket":
        """Open a socket and hand it to the server. Passing `context` is how a test trusts its
        own certificate; leaving it None verifies against the system CAs."""
        host, port, path = parse_ws_url(url)
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if url.lower().startswith("wss://"):
                sock = (context or ssl.create_default_context()).wrap_socket(
                    sock, server_hostname=host
                )
            sock.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            head, leftover = _read_handshake(sock)
            lines = head.decode("latin-1").split("\r\n")
            if len(lines) < 2 or " 101" not in lines[0]:
                raise WSError(f"handshake rejected: {lines[0][:120]!r}")
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            expect = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != expect:
                raise WSError(f"bad Sec-WebSocket-Accept: {headers.get('sec-websocket-accept')!r}")
        except Exception:
            sock.close()
            raise
        ws = cls(sock, timeout)
        ws.reader.buf = leftover  # a fast peer may already have sent frames
        return ws

    def send(self, payload: Union[bytes, str, bytearray], opcode: int = OP_TEXT, fin: bool = True) -> None:
        if self.closed:
            raise WSError("send on a closed websocket")
        self.sock.sendall(encode_frame(payload, opcode, fin, mask=True))

    def send_text(self, text: str) -> None:
        self.send(text, OP_TEXT)

    def send_binary(self, data: bytes) -> None:
        self.send(data, OP_BINARY)

    def ping(self, payload: bytes = b"") -> None:
        self.send(payload, OP_PING)

    def recv(self, timeout: Optional[float] = None) -> Optional[Union[str, bytes]]:
        """Next data message (fragments reassembled). Answers pings. None on close.

        A silent peer raises socket.timeout instead of hanging the caller forever.
        """
        if self.closed:
            return None
        self.reader.timeout = self.timeout if timeout is None else timeout
        chunks = []
        data_opcode = None
        while True:
            fin, opcode, payload, _ = decode_frame(self.reader.read)
            if opcode == OP_CLOSE:
                self._send_close(payload)
                return None
            if opcode == OP_PING:
                try:
                    self.send(payload, OP_PONG)
                except (OSError, WSError):
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY) and data_opcode is None:
                data_opcode = opcode
                chunks = [payload]
            elif opcode == OP_CONT and data_opcode is not None:
                chunks.append(payload)
            else:
                raise WSError(f"unexpected websocket opcode {opcode:#x}")
            if fin:
                data = b"".join(chunks)
                return data.decode("utf-8") if data_opcode == OP_TEXT else data

    def _send_close(self, payload: bytes = b"") -> None:
        self.closed = True
        try:
            self.sock.sendall(encode_frame(payload[:125], OP_CLOSE, mask=True))
        except OSError:
            pass

    def close(self) -> None:
        if not self.closed:
            self._send_close()
        try:
            self.sock.close()
        except OSError:
            pass
        self.closed = True


def _read_handshake(sock: socket.socket) -> Tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSError("connection closed during handshake")
        buf += chunk
        if len(buf) > 65536:
            raise WSError("handshake response too large")
    head, _, leftover = buf.partition(b"\r\n\r\n")
    return head, leftover
