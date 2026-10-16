"""Boom feeds module: realtime WebSocket feeds + RSS news feeds."""

from .feeds import BinancePriceFeed, RSSNewsFeed, fetch_rss, parse_rss
from .websocket_feed import (
    Feed,
    WebSocketFeed,
    WebSocketFeedError,
    _require_backend,
)

__all__ = [
    "BinancePriceFeed",
    "RSSNewsFeed",
    "fetch_rss",
    "parse_rss",
    "Feed",
    "WebSocketFeed",
    "WebSocketFeedError",
    "_require_backend",
]
