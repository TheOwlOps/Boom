"""Quản lý nhiều feed: thêm/bớt, start/stop hàng loạt, fan-out message.

Fan-out: 1 message từ feed tên X -> gọi TẤT CẢ callback đã đăng ký qua on(X, cb).
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional


class FeedManager:
    """Quản lý tập feed theo tên.

    Cách dùng:
        mgr = FeedManager()
        mgr.add_feed("btc", BinancePriceFeed())
        mgr.on("btc", lambda m: print("A:", m))
        mgr.on("btc", lambda m: print("B:", m))   # fan-out: cả A và B đều nhận
        mgr.start_all()
        ...
        mgr.stop_all()
    """

    def __init__(self):
        self._feeds: Dict[str, object] = {}
        self._callbacks: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    # -- Quản lý feed ---------------------------------------------------
    def add_feed(self, name: str, feed) -> None:
        """Thêm feed. Tự bọc on_message của feed để fan-out về các callback đã đăng ký."""
        with self._lock:
            if name in self._feeds:
                raise ValueError(f"feed {name!r} đã tồn tại (dùng remove_feed trước)")
            original = getattr(feed, "on_message", None)

            def _dispatch(msg, _name=name, _orig=original):
                # 1. callback gốc của feed (nếu có lúc add) vẫn được gọi
                if _orig is not None:
                    try:
                        _orig(msg)
                    except Exception:
                        pass
                # 2. fan-out tới mọi callback đăng ký qua on(name, cb)
                for cb in list(self._callbacks.get(_name, [])):
                    try:
                        cb(msg)
                    except Exception:
                        pass  # 1 callback lỗi không chặn các callback khác

            try:
                feed.on_message = _dispatch
            except AttributeError:
                raise TypeError(
                    f"feed {name!r} không có thuộc tính on_message gán được "
                    "(feed phải kế thừa Feed từ websocket_feed.py)"
                )
            self._feeds[name] = feed
            self._callbacks.setdefault(name, [])

    def remove_feed(self, name: str) -> None:
        """Gỡ feed: stop feed rồi xóa khỏi manager."""
        with self._lock:
            feed = self._feeds.pop(name, None)
            self._callbacks.pop(name, None)
        if feed is not None:
            try:
                feed.close()
            except Exception:
                pass

    def on(self, name: str, callback: Callable) -> None:
        """Đăng ký callback nhận mọi message từ feed `name` (fan-out)."""
        with self._lock:
            if name not in self._feeds:
                raise KeyError(f"chưa có feed {name!r} (add_feed trước)")
            self._callbacks[name].append(callback)

    def off(self, name: str, callback: Callable) -> bool:
        """Hủy 1 callback. Trả True nếu tìm thấy và xóa."""
        with self._lock:
            cbs = self._callbacks.get(name, [])
            if callback in cbs:
                cbs.remove(callback)
                return True
            return False

    # -- Điều khiển hàng loạt -------------------------------------------
    def start_all(self) -> None:
        """Start tất cả feed. Feed nào lỗi start thì bỏ qua, các feed khác vẫn chạy."""
        for name, feed in list(self._feeds.items()):
            try:
                feed.start()
            except Exception:
                pass

    def stop_all(self) -> None:
        """Stop tất cả feed."""
        for name, feed in list(self._feeds.items()):
            try:
                feed.close()
            except Exception:
                pass

    # -- Trạng thái -------------------------------------------------------
    def get_status(self) -> Dict[str, str]:
        """{tên_feed: 'running' | 'stopped'} cho từng feed."""
        status = {}
        for name, feed in self._feeds.items():
            try:
                running = bool(getattr(feed, "is_running", False))
            except Exception:
                running = False
            status[name] = "running" if running else "stopped"
        return status

    def __len__(self) -> int:
        return len(self._feeds)

    def __contains__(self, name: str) -> bool:
        return name in self._feeds
