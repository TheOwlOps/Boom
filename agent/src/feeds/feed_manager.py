"""Manages many feeds: add/remove, bulk start/stop, message fan-out.

Fan-out: 1 message from feed named X -> calls EVERY callback registered via on(X, cb).
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional


class FeedManager:
    """Manages a named set of feeds.

    Usage:
        mgr = FeedManager()
        mgr.add_feed("btc", BinancePriceFeed())
        mgr.on("btc", lambda m: print("A:", m))
        mgr.on("btc", lambda m: print("B:", m))   # fan-out: both A and B receive
        mgr.start_all()
        ...
        mgr.stop_all()
    """

    def __init__(self):
        self._feeds: Dict[str, object] = {}
        self._callbacks: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    # -- feed management ------------------------------------------------
    def add_feed(self, name: str, feed) -> None:
        """Adds a feed. Wraps the feed's on_message for fan-out to registered callbacks."""
        with self._lock:
            if name in self._feeds:
                raise ValueError(f"feed {name!r} already exists (remove_feed first)")
            original = getattr(feed, "on_message", None)

            def _dispatch(msg, _name=name, _orig=original):
                # 1. the feed's original callback (if set at add time) still runs
                if _orig is not None:
                    try:
                        _orig(msg)
                    except Exception:
                        pass
                # 2. fan-out to every callback registered via on(name, cb)
                for cb in list(self._callbacks.get(_name, [])):
                    try:
                        cb(msg)
                    except Exception:
                        pass  # one bad callback doesn't block the others

            try:
                feed.on_message = _dispatch
            except AttributeError:
                raise TypeError(
                    f"feed {name!r} has no assignable on_message attribute "
                    "(feeds must subclass Feed from websocket_feed.py)"
                )
            self._feeds[name] = feed
            self._callbacks.setdefault(name, [])

    def remove_feed(self, name: str) -> None:
        """Removes a feed: stops the feed, then drops it from the manager."""
        with self._lock:
            feed = self._feeds.pop(name, None)
            self._callbacks.pop(name, None)
        if feed is not None:
            try:
                feed.close()
            except Exception:
                pass

    def on(self, name: str, callback: Callable) -> None:
        """Registers a callback receiving every message from feed `name` (fan-out)."""
        with self._lock:
            if name not in self._feeds:
                raise KeyError(f"no feed {name!r} yet (add_feed first)")
            self._callbacks[name].append(callback)

    def off(self, name: str, callback: Callable) -> bool:
        """Unregisters one callback. Returns True when found and removed."""
        with self._lock:
            cbs = self._callbacks.get(name, [])
            if callback in cbs:
                cbs.remove(callback)
                return True
            return False

    # -- bulk control -----------------------------------------------------
    def start_all(self) -> None:
        """Starts every feed. A feed that fails to start is skipped, others still run."""
        for name, feed in list(self._feeds.items()):
            try:
                feed.start()
            except Exception:
                pass

    def stop_all(self) -> None:
        """Stops every feed."""
        for name, feed in list(self._feeds.items()):
            try:
                feed.close()
            except Exception:
                pass

    # -- status -----------------------------------------------------------
    def get_status(self) -> Dict[str, str]:
        """{feed_name: 'running' | 'stopped'} for each feed."""
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
