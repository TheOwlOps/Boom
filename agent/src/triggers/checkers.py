"""Trigger condition checkers for the Boom agent.

A checker answers one question — "did this trigger fire right now?" — given
the trigger definition and an injected context of callables. Checkers are
stateless and side-effect free: they never store state, touch disk, or call
the network themselves. All data comes from the ``ctx`` callables injected
by the runner, so every checker below runs "for real" as soon as the runner
wires the real callables in.

Import root is agent/src, so this lives at agent/src/triggers/checkers.py
and is imported as ``from triggers.checkers import check_trigger``.
Stdlib only — no imports at all.

Supported kinds (see ``VALID_KINDS`` in models.py):
    gmail_new    - fire when a new Gmail message matches a query
    price_below  - fire when a product price drops below a threshold
    keyword_news - fire when a new headline matches a keyword
    webhook      - always fires (the runner calls this on an incoming webhook)

Newness convention (ID-BASED):
    For ``gmail_new`` and ``keyword_news``, "new" is decided by comparing the
    newest item's id against ``trigger.last_fired_at``:

    - First run (``last_fired_at`` is None): fires when there is >= 1 item.
    - Later runs: fires only when the newest item's id differs from
      ``last_fired_at``.

    After a run fires, the runner MUST call ``mark_fired`` storing the newest
    item's id into ``last_fired_at`` (as a string). Ids are compared as
    strings, so int/str mismatches can never cause a false fire.

    Callables must return items newest-first (``items[0]`` is the newest):
    Gmail's list order and RSS feeds both satisfy this. Date-based comparison
    is deliberately NOT used — dates from different sources have unreliable
    formats and timezones, while ids are stable.

Expected ctx callables (runner responsibility — checkers only call them):
    gmail_search(query) -> list of dicts, each with "id"
        (real shape on main: GmailConnector.gmail_search returns
        {"messages": [...], "resultSizeEstimate": ...}; each message row has
        id/threadId/snippet/from/subject/date — the runner unwraps the list,
        but this module also tolerates the {"messages": [...]} wrapper)
    get_price(url) -> float | None
        (runner wires this, e.g. from TikiConnector.tiki_product's "price"
        field, which on main is a real VND number via the public API v2)
    news_search(keyword) -> list of dicts, each with "url"/"link"/"id"
        (real shape on main: NewsConnector.news_search returns
        {"keyword": ..., "headlines": [...], "total": ...}; each headline has
        title/link/pubDate/summary — same unwrapping tolerance applies)
"""


def check_trigger(trigger, ctx):
    """Check one trigger. Returns {"fired": bool, "detail": str}.

    Never raises: any failure (bad kind, missing ctx callable, network error
    inside an injected callable, malformed config...) becomes
    {"fired": False, "detail": "error: ..."}.
    """
    try:
        kind = getattr(trigger, "kind", None)
        if kind == "gmail_new":
            return _check_gmail_new(trigger, ctx)
        if kind == "price_below":
            return _check_price_below(trigger, ctx)
        if kind == "keyword_news":
            return _check_keyword_news(trigger, ctx)
        if kind == "webhook":
            return _check_webhook(trigger, ctx)
        return {"fired": False, "detail": "error: unknown kind %r" % (kind,)}
    except Exception as exc:  # never raise — see docstring
        return {"fired": False, "detail": "error: %s" % (exc,)}


# ------------------------------------------------------------------ helpers


def _config_of(trigger, kind):
    config = getattr(trigger, "config", None)
    if not isinstance(config, dict):
        raise ValueError("%s: trigger.config must be a dict" % (kind,))
    return config


def _require_callable(ctx, name):
    if not isinstance(ctx, dict):
        raise ValueError(
            "ctx must be a dict of callables, got %s" % type(ctx).__name__
        )
    fn = ctx.get(name)
    if fn is None:
        raise ValueError("ctx is missing required callable %r" % (name,))
    if not callable(fn):
        raise ValueError("ctx[%r] is not callable" % (name,))
    return fn


def _as_item_list(result, name):
    """Normalize a search callable's return to a list of item dicts.

    Contract: the callable returns a list. The real connector tools on main
    return {"messages": [...]} / {"headlines": [...]} wrappers, which are
    tolerated here (the runner may wire either the raw tool or an unwrapping
    adapter). {"error": ...} is always surfaced as an error, never as
    silence.
    """
    if isinstance(result, dict):
        if "error" in result:
            raise ValueError("%s failed: %s" % (name, result["error"]))
        for key in ("messages", "headlines"):
            if isinstance(result.get(key), list):
                return result[key]
        raise ValueError(
            "%s returned a dict without a 'messages'/'headlines' list: %s"
            % (name, sorted(result))
        )
    if not isinstance(result, list):
        raise ValueError(
            "%s must return a list, got %s" % (name, type(result).__name__)
        )
    return result


def _newest_id(items, name, id_keys):
    """Id of the newest item (items[0]), as a string. Raises if unusable."""
    newest = items[0]
    if not isinstance(newest, dict):
        raise ValueError("%s item is not a dict: %r" % (name, newest))
    for key in id_keys:
        value = newest.get(key)
        if value:
            return str(value), newest
    raise ValueError(
        "%s item is missing any of %s: %r" % (name, "/".join(id_keys), newest)
    )


# ------------------------------------------------------------------ checkers


def _check_gmail_new(trigger, ctx):
    kind = "gmail_new"
    gmail_search = _require_callable(ctx, "gmail_search")
    query = _config_of(trigger, kind).get("query")
    if not (query or "").strip():
        raise ValueError("%s: config needs a non-empty 'query'" % (kind,))
    items = _as_item_list(gmail_search(query), "gmail_search")
    if not items:
        return {"fired": False, "detail": "no messages matching query"}
    new_id, newest = _newest_id(items, "gmail_search", ("id",))
    last = getattr(trigger, "last_fired_at", None)
    if last is None:
        return {
            "fired": True,
            "detail": "first check: %d message(s), newest id=%s"
            % (len(items), new_id),
        }
    if new_id != str(last):
        return {
            "fired": True,
            "detail": "new message id=%s (prev=%s), %d message(s), subject=%r"
            % (new_id, last, len(items), newest.get("subject", "")),
        }
    return {"fired": False, "detail": "no new messages (newest id=%s)" % (new_id,)}


def _check_price_below(trigger, ctx):
    kind = "price_below"
    get_price = _require_callable(ctx, "get_price")
    config = _config_of(trigger, kind)
    url = config.get("url")
    if not (url or "").strip():
        raise ValueError("%s: config needs a non-empty 'url'" % (kind,))
    raw_threshold = config.get("threshold")
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        raise ValueError(
            "%s: threshold %r is not a number" % (kind, raw_threshold)
        )
    price = get_price(url)
    if price is None:
        raise ValueError("%s: get_price(%r) returned None" % (kind, url))
    try:
        price = float(price)
    except (TypeError, ValueError):
        raise ValueError(
            "%s: get_price(%r) returned non-numeric %r" % (kind, url, price)
        )
    if price < threshold:
        return {
            "fired": True,
            "detail": "price %s < threshold %s: %s" % (price, threshold, url),
        }
    return {
        "fired": False,
        "detail": "price %s >= threshold %s: %s" % (price, threshold, url),
    }


def _check_keyword_news(trigger, ctx):
    kind = "keyword_news"
    news_search = _require_callable(ctx, "news_search")
    keyword = _config_of(trigger, kind).get("keyword")
    if not (keyword or "").strip():
        raise ValueError("%s: config needs a non-empty 'keyword'" % (kind,))
    items = _as_item_list(news_search(keyword), "news_search")
    if not items:
        return {"fired": False, "detail": "no headlines matching keyword"}
    # Real news items on main use "link"; "url"/"id" also accepted.
    new_id, newest = _newest_id(items, "news_search", ("url", "link", "id"))
    title = newest.get("title", "")
    last = getattr(trigger, "last_fired_at", None)
    if last is None:
        return {
            "fired": True,
            "detail": "first check: %d headline(s), newest id=%s %r"
            % (len(items), new_id, title),
        }
    if new_id != str(last):
        return {
            "fired": True,
            "detail": "new headline id=%s (prev=%s): %r" % (new_id, last, title),
        }
    return {"fired": False, "detail": "no new headlines (newest=%s)" % (new_id,)}


def _check_webhook(trigger, ctx):
    _ = trigger, ctx  # no config, no ctx needed — the call itself is the event
    return {"fired": True, "detail": "webhook invoked"}
