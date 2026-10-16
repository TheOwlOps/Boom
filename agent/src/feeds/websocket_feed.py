"""WebSocket client framework for Boom's realtime feeds.

Prefers the repo's own `tools/ws_client.py` (stdlib-only, real RFC6455, no
extra lib needed). Falls back to the `websockets` lib when not importable;
raises a clear error when neither exists.

Every WS feed runs in its own thread; the `on_message` callback is invoked
from that thread.
Auto-reconnect: disconnect -> backoff 1s, 2s, 4s ... up to 60s, retried
indefinitely unless `max_retries` is set.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

# --- WS backend choice: repo ws_client first, websockets lib second, else raise ---
_WS_BACKEND = None  # "repo" | "websockets"
_WS_IMPORT_ERROR = None

try:
    from tools.ws_client import WebSocket as _RepoWebSocket, WSError as _RepoWSError
    _WS_BACKEND = "repo"
except ImportError as e:  # when run outside agent/src (not yet on sys.path)
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
    """WebSocketFeed error (no WS backend, connect failure, ...)."""


def _require_backend() -> str:
    """Returns the available backend's name, or raises a clear error."""
    if _WS_BACKEND is None:
        raise WebSocketFeedError(
            "No WebSocket backend available. "
            "Option 1: place this module under agent/src/feeds/ of the Boom repo "
            "to use the bundled tools/ws_client.py (recommended). "
            f"Original import error: {_WS_IMPORT_ERROR}. "
            "Option 2: pip install websockets."
        )
    return _WS_BACKEND


class _WebsocketsAdapter:
    """Wraps the `websockets` lib to the same minimal interface as the repo WebSocket.

    Not yet tested against the real lib (dev env doesn't have it);
    the default backend remains the repo's tools/ws_client.py.
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
    """Default factory: makes a real WS connection with the available backend."""
    backend = _require_backend()
    if backend == "repo":
        return _RepoWebSocket.connect(url, timeout=timeout)
    return _WebsocketsAdapter.connect(url, timeout=timeout)


class Feed:
    """Lightweight base class for every feed: start/close/is_running + assignable on_message."""

    def __init__(self, on_message: Optional[Callable] = None):
        self.on_message: Optional[Callable] = on_message
        self._running = False
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    def _emit(self, msg) -> None:
        """Invokes the callback safely: a callback error never kills the feed thread."""
        cb = self.on_message
        if cb is None:
            return
        try:
            cb(msg)
        except Exception:
            pass  # a user callback failing is ignored, the feed keeps running

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class WebSocketFeed(Feed):
    """WebSocket client running in its own thread, auto-reconnects on network drops.

    Args:
        url: ws:// or wss:// URL.
        on_message: callback(message) — message is str (text) or bytes (binary).
        on_error: callback(exception) on connect/recv errors (before each retry).
        max_retries: max reconnect attempts; None = retry forever.
        backoff_base: seconds to wait on the first retry (default 1s per spec).
        backoff_max: backoff ceiling (default 60s per spec).
        _connect_factory: test hook — a (url) -> ws-like object function
            with send_text/recv/close. Defaults to the real WS backend.
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
        # Check the backend at init time so failures surface early and clearly.
        self._connect_factory = _connect_factory or _default_connect
        if _connect_factory is None:
            _require_backend()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending_subscribe: Optional[str] = None
        self.connected = False
        self.reconnect_count = 0  # reconnect counter (handy for monitor/test)

    # -- spec API -----------------------------------------------------------
    def connect(self) -> None:
        """Connects WS once (blocking). Raises on failure."""
        with self._lock:
            if self._ws is not None:
                return  # already connected
            ws = self._connect_factory(self.url)
            self._ws = ws
            self.connected = True
            # Send any pending subscribe message (when subscribe() ran before connect)
            if self._pending_subscribe is not None:
                ws.send_text(self._pending_subscribe)
                self._pending_subscribe = None

    def subscribe(self, message) -> None:
        """Sends a subscribe message. Held back and sent on connect when not yet connected.

        message can be a dict (json.dumps'd) or a str.
        """
        text = json.dumps(message) if isinstance(message, dict) else str(message)
        with self._lock:
            if self._ws is not None and self.connected:
                self._ws.send_text(text)
            else:
                self._pending_subscribe = text

    def start(self) -> None:
        """Runs the connect/recv/reconnect loop in its own (daemon) thread."""
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name=f"wsfeed-{id(self)}", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Closes cleanly: stops the thread, closes the socket."""
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

    # -- internal loop --------------------------------------------------------
    def _run_loop(self) -> None:
        import socket as _socket  # local import to avoid extra module-top deps

        retries = 0
        backoff = self.backoff_base
        while not self._stop.is_set():
            try:
                self.connect()
                retries = 0
                backoff = self.backoff_base
                self._recv_loop(_socket)
            except Exception as e:
                # Clean up the broken socket before retrying
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
                                f"exceeded {self.max_retries} retries, stopping feed {self.url}"))
                        except Exception:
                            pass
                    break
                if self.on_error:
                    try:
                        self.on_error(e)
                    except Exception:
                        pass
                self.reconnect_count += 1
                # Backoff 1s, 2s, 4s ... up to 60s; the stop event lets close()
                # interrupt the wait immediately
                if self._stop.wait(timeout=min(backoff, self.backoff_max)):
                    break
                backoff = min(backoff * 2, self.backoff_max)
        self._running = False

    def _recv_loop(self, _socket) -> None:
        """Receives messages until disconnect. Returning = dropped connection -> raise to retry."""
        while not self._stop.is_set():
            with self._lock:
                ws = self._ws
            if ws is None:
                raise WebSocketFeedError("lost connection (ws=None)")
            try:
                msg = ws.recv(timeout=1.0)
            except _socket.timeout:
                continue  # silent peer -> keep waiting, not an error
            if msg is None:
                raise WebSocketFeedError("server closed the connection (close frame)")
            self._emit(msg)
