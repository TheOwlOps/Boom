"""Khung WebSocket client cho realtime feed của Boom.

Ưu tiên dùng `tools/ws_client.py` có sẵn của repo (stdlib-only, RFC6455 thật,
không cần cài thêm lib). Nếu không import được thì thử lib `websockets`;
không có cái nào thì raise lỗi rõ ràng.

Mọi feed WS chạy trong thread riêng; callback `on_message` được gọi từ thread đó.
Auto-reconnect: disconnect -> backoff 1s, 2s, 4s ... tối đa 60s, thử lại vô hạn
trừ khi đặt `max_retries`.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

# --- Chọn backend WS: repo ws_client trước, websockets lib sau, không có thì raise ---
_WS_BACKEND = None  # "repo" | "websockets"
_WS_IMPORT_ERROR = None

try:
    from tools.ws_client import WebSocket as _RepoWebSocket, WSError as _RepoWSError
    _WS_BACKEND = "repo"
except ImportError as e:  # khi chạy ngoài agent/src (chưa thêm vào sys.path)
    _RepoWebSocket = None
    _RepoWSError = None
    _WS_IMPORT_ERROR = e
    try:
        import websockets  # type: ignore
        import asyncio
        _WS_BACKEND = "websockets"
    except ImportError:
        _WS_BACKEND = None


class WebSocketFeedError(Exception):
    """Lỗi của WebSocketFeed (không có backend WS, connect fail, ...)."""


def _require_backend() -> str:
    """Trả về tên backend khả dụng, hoặc raise lỗi rõ ràng."""
    if _WS_BACKEND is None:
        raise WebSocketFeedError(
            "Không có WebSocket backend nào khả dụng. "
            "Cách 1: đặt module này vào agent/src/feeds/ của repo Boom để dùng "
            "tools/ws_client.py có sẵn (khuyến nghị). "
            f"Lỗi import gốc: {_WS_IMPORT_ERROR}. "
            "Cách 2: pip install websockets."
        )
    return _WS_BACKEND


class _WebsocketsAdapter:
    """Bọc `websockets` lib cho cùng interface tối thiểu như repo WebSocket.

    Chưa được test với lib thật (môi trường dev không có lib này);
    backend mặc định vẫn là tools/ws_client.py của repo.
    """

    def __init__(self, ws):
        self._ws = ws
        self.closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 30.0) -> "_WebsocketsAdapter":
        async def _do():
            return await websockets.connect(url, open_timeout=timeout)  # type: ignore
        ws = asyncio.run(_do())
        return cls(ws)

    def send_text(self, text: str) -> None:
        asyncio.run(self._ws.send(text))

    def recv(self, timeout: Optional[float] = None):
        async def _do():
            try:
                return await asyncio.wait_for(self._ws.recv(), timeout or 30.0)
            except asyncio.TimeoutError:
                import socket as _socket
                raise _socket.timeout("recv timeout")
        return asyncio.run(_do())

    def close(self) -> None:
        async def _do():
            await self._ws.close()
        try:
            asyncio.run(_do())
        finally:
            self.closed = True


def _default_connect(url: str, timeout: float = 30.0):
    """Factory mặc định: kết nối WS thật bằng backend khả dụng."""
    backend = _require_backend()
    if backend == "repo":
        return _RepoWebSocket.connect(url, timeout=timeout)
    return _WebsocketsAdapter.connect(url, timeout=timeout)


class Feed:
    """Base class nhẹ cho mọi feed: start/close/is_running + on_message gán được."""

    def __init__(self, on_message: Optional[Callable] = None):
        self.on_message: Optional[Callable] = on_message
        self._running = False
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    def _emit(self, msg) -> None:
        """Gọi callback an toàn: lỗi ở callback không giết thread feed."""
        cb = self.on_message
        if cb is None:
            return
        try:
            cb(msg)
        except Exception:
            pass  # callback của user lỗi thì bỏ qua, feed vẫn chạy tiếp

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class WebSocketFeed(Feed):
    """WebSocket client chạy trong thread riêng, tự reconnect khi rớt mạng.

    Args:
        url: ws:// hoặc wss:// URL.
        on_message: callback(message) — message là str (text) hoặc bytes (binary).
        on_error: callback(exception) khi connect/recv lỗi (trước mỗi lần retry).
        max_retries: số lần reconnect tối đa; None = thử lại vô hạn.
        backoff_base: giây chờ lần retry đầu (mặc định 1s theo spec).
        backoff_max: trần backoff (mặc định 60s theo spec).
        _connect_factory: hook cho test — hàm (url) -> ws-like object
            có send_text/recv/close. Mặc định dùng backend WS thật.
    """

    def __init__(
        self,
        url: str,
        on_message: Optional[Callable] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        max_retries: Optional[int] = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        _connect_factory=None,
    ):
        super().__init__(on_message)
        self.url = url
        self.on_error = on_error
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # Kiểm tra backend ngay từ lúc khởi tạo để lỗi hiện sớm, rõ ràng.
        self._connect_factory = _connect_factory or _default_connect
        if _connect_factory is None:
            _require_backend()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending_subscribe: Optional[str] = None
        self.connected = False
        self.reconnect_count = 0  # đếm số lần đã reconnect (tiện cho monitor/test)

    # -- API theo spec -------------------------------------------------
    def connect(self) -> None:
        """Kết nối WS một lần (blocking). Raise nếu thất bại."""
        with self._lock:
            if self._ws is not None:
                return  # đã connect rồi
            ws = self._connect_factory(self.url)
            self._ws = ws
            self.connected = True
            # Gửi subscribe message còn treo (nếu subscribe() gọi trước khi connect)
            if self._pending_subscribe is not None:
                ws.send_text(self._pending_subscribe)
                self._pending_subscribe = None

    def subscribe(self, message) -> None:
        """Gửi subscribe message. Nếu chưa connect thì giữ lại, gửi ngay khi connect xong.

        message có thể là dict (tự json.dumps) hoặc str.
        """
        text = json.dumps(message) if isinstance(message, dict) else str(message)
        with self._lock:
            if self._ws is not None and self.connected:
                self._ws.send_text(text)
            else:
                self._pending_subscribe = text

    def start(self) -> None:
        """Chạy vòng lặp connect/recv/reconnect trong thread riêng (daemon)."""
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name=f"wsfeed-{id(self)}", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Đóng sạch: dừng thread, đóng socket."""
        self._stop.set()
        with self._lock:
            ws, self._ws = self._ws, None
            self.connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        th = self._thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=10)
        self._running = False

    # -- Vòng lặp nội bộ ------------------------------------------------
    def _run_loop(self) -> None:
        import socket as _socket  # import local để tránh phụ thuộc thừa ở module top

        retries = 0
        backoff = self.backoff_base
        while not self._stop.is_set():
            try:
                self.connect()
                retries = 0
                backoff = self.backoff_base
                self._recv_loop(_socket)
            except Exception as e:
                # Dọn socket hỏng trước khi retry
                with self._lock:
                    ws, self._ws = self._ws, None
                    self.connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if self._stop.is_set():
                    break
                retries += 1
                if self.max_retries is not None and retries > self.max_retries:
                    if self.on_error:
                        try:
                            self.on_error(WebSocketFeedError(
                                f"quá {self.max_retries} lần retry, dừng feed {self.url}"))
                        except Exception:
                            pass
                    break
                if self.on_error:
                    try:
                        self.on_error(e)
                    except Exception:
                        pass
                self.reconnect_count += 1
                # Backoff 1s, 2s, 4s ... tối đa 60s; stop event để close() ngắt chờ ngay
                if self._stop.wait(timeout=min(backoff, self.backoff_max)):
                    break
                backoff = min(backoff * 2, self.backoff_max)
        self._running = False

    def _recv_loop(self, _socket) -> None:
        """Nhận message tới khi disconnect. Hết hàm = rớt kết nối -> raise để retry."""
        while not self._stop.is_set():
            with self._lock:
                ws = self._ws
            if ws is None:
                raise WebSocketFeedError("mất kết nối (ws=None)")
            try:
                msg = ws.recv(timeout=1.0)
            except _socket.timeout:
                continue  # peer im lặng -> chờ tiếp, không tính là lỗi
            if msg is None:
                raise WebSocketFeedError("server đóng kết nối (close frame)")
            self._emit(msg)
