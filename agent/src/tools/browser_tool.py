"""Headless Chrome over the Chrome DevTools Protocol. stdlib only.

A minimal RFC6455 client (tools/ws_client.py) for the page socket, plain HTTP
for Chrome's /json endpoints, and the port read from the DevToolsActivePort
file Chrome writes into our temp profile. No browser discoverable -> the tools
return {"error": ...}; nothing here reports a success it did not observe.

Tabs are real targets: the session keeps the target id it is attached to, so a call
after a tab switch goes to that tab, and a tab closed under us falls back to another
rather than failing. Clicking and filling drive the page through the Input domain on
the element's own box, so what happened is what the page received, not a script guess.

Deliberately not implemented: TLS (wss://), network interception, element
screenshots (a click/fill reports the coordinates it acted on, and a wrong
selector is an error).
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from tools.ws_client import WSError, WebSocket

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_CHROME_NAMES = (
    "chrome",
    "chrome.exe",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "msedge",
    "msedge.exe",
)

_WINDOWS_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)


# Both return null when nothing matches, so a bad selector is an error and never a guess.
_CLICK_JS = """(() => {
  const el = document.querySelector(%s);
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height};
})()"""

_FILL_JS = """(() => {
  const el = document.querySelector(%s);
  if (!el) return null;
  el.scrollIntoView({block: 'center'});
  if (el.focus) el.focus();
  const text = %s;
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
              : el.tagName === 'INPUT' ? HTMLInputElement.prototype : null;
  const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) setter.set.call(el, text); else el.value = text;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {tag: el.tagName.toLowerCase(), value: el.value};
})()"""


class BrowserError(Exception):
    """No browser, launch failed, or a CDP/transport failure."""


def find_chrome() -> Optional[str]:
    """BOOM_CHROME_PATH first, then PATH, then the usual Windows install paths.

    An explicit but missing BOOM_CHROME_PATH means "no browser" rather than
    silence: a typo must not launch a browser the caller did not ask for.
    """
    override = os.environ.get("BOOM_CHROME_PATH")
    if override:
        return override if os.path.isfile(override) else None
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for raw in _WINDOWS_PATHS:
        path = os.path.expandvars(raw)
        if os.path.isfile(path):
            return path
    return None


def launch_chrome(executable: str, timeout: float = 20.0) -> Tuple[subprocess.Popen, Path, int]:
    """Headless Chrome with a temp profile and an ephemeral debug port.

    Returns (process, profile_dir, port). The port is whatever Chrome wrote to
    <user-data-dir>/DevToolsActivePort, never a hardcoded 9222.

    shortcut: stdout/stderr go to DEVNULL, so no reader thread sits on a pipe
    holding a buffer lock; capture output here only if launch diagnosis is needed.
    """
    profile = Path(tempfile.mkdtemp(prefix="boom-chrome-"))
    args = [
        executable,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BrowserError(f"chrome exited before opening a debug port (exit {proc.returncode})")
        try:
            lines = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        if lines and lines[0].strip().isdigit():
            return proc, profile, int(lines[0])
        time.sleep(0.05)
    proc.terminate()
    raise BrowserError(f"chrome wrote no DevToolsActivePort within {timeout}s")


def http_json(port: int, method: str, path: str, timeout: float = 10.0) -> Any:
    """One request to Chrome's plain-HTTP debugging endpoint."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            raise BrowserError(f"{method} {path} -> HTTP {response.status}: {body[:200]!r}")
        return json.loads(body.decode("utf-8", "replace")) if body else {}
    except OSError as exc:
        raise BrowserError(f"cannot reach the Chrome debug port 127.0.0.1:{port}: {exc}") from None
    finally:
        conn.close()


def _pick_page(targets: Any) -> Optional[str]:
    for target in targets if isinstance(targets, list) else []:
        if isinstance(target, dict) and target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    return None


def page_ws_url(port: int, timeout: float = 10.0) -> str:
    """Page socket URL: reuse the page we already have, else open one."""
    url = _pick_page(http_json(port, "GET", "/json/list", timeout))
    if url:
        return url
    try:  # modern Chrome rejects GET /json/new; PUT creates the target
        target = http_json(port, "PUT", "/json/new?url=about:blank", timeout)
        if isinstance(target, dict) and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    except BrowserError:
        pass
    info = http_json(port, "GET", "/json/version", timeout)
    browser_url = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
    if browser_url:
        cdp = CDPConnection(WebSocket.connect(browser_url, timeout))
        try:
            cdp.call("Target.createTarget", {"url": "about:blank"}, timeout)
        finally:
            cdp.close()
        url = _pick_page(http_json(port, "GET", "/json/list", timeout))
        if url:
            return url
    raise BrowserError("the browser exposes no page target to attach to")


class CDPConnection:
    """Id-matched CDP calls over one socket. Events are skipped; a CDP error raises."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._next_id = 0

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        self._next_id += 1
        wanted = self._next_id
        self.ws.send_text(json.dumps({"id": wanted, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(f"CDP {method} timed out after {timeout}s")
            try:
                raw = self.ws.recv(timeout=remaining)
            except socket.timeout:
                raise BrowserError(f"CDP {method} timed out after {timeout}s") from None
            if raw is None:
                raise BrowserError(f"CDP socket closed while waiting for {method}")
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != wanted:
                continue  # an event, or a response to something else
            if "error" in message:
                error = message["error"]
                detail = error.get("message", error) if isinstance(error, dict) else error
                raise BrowserError(f"CDP {method} failed: {detail}")
            return message.get("result") or {}

    def close(self) -> None:
        self.ws.close()


def _exception_text(details: Dict[str, Any]) -> str:
    return (details.get("exception") or {}).get("description") or details.get("text") or "page threw an exception"


def _kill_profile_holders(profile: Path) -> None:
    """Kill any browser still running against this temp profile (Windows only).

    shortcut: shells out to PowerShell CIM because taskkill cannot filter on a
    command line; use a job object instead if close ever runs hot.
    """
    if os.name != "nt":
        return
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{profile.name}*'}} | "
        "ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


class BrowserTool:
    """Headless Chrome session. Starts lazily on the first call and stays up.

    `port` attaches to a browser that is already listening on a debug port
    instead of launching one (how the offline eval drives this without Chrome).
    """

    def __init__(self, executable: Optional[str] = None, port: Optional[int] = None, timeout: float = 20.0):
        self.executable = executable
        self.port = port
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._profile: Optional[Path] = None
        self._page: Optional[CDPConnection] = None
        self._target: Optional[str] = None  # the tab this session is attached to

    # -- session ---------------------------------------------------------
    def _targets(self) -> List[Dict[str, Any]]:
        """Every page target the browser will let us attach to."""
        targets = http_json(self.port, "GET", "/json/list", self.timeout)
        if not isinstance(targets, list):
            return []
        return [
            t for t in targets
            if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")
        ]

    def _ensure_page(self) -> CDPConnection:
        if self._page is not None:
            return self._page
        if self.port is None:
            executable = self.executable or find_chrome()
            if not executable:
                raise BrowserError(
                    "no Chrome/Edge/Chromium found (set BOOM_CHROME_PATH to the executable)"
                )
            self._proc, self._profile, self.port = launch_chrome(executable, self.timeout)
        ws_url = ""
        for tab in self._targets():
            if self._target and tab.get("id") == self._target:
                ws_url = tab["webSocketDebuggerUrl"]
                break
        if not ws_url:
            # No tab chosen, or the one we were on is gone: first page, else a new one.
            self._target = None
            ws_url = page_ws_url(self.port, self.timeout)
        self._target = self._target or ws_url.rsplit("/", 1)[-1]
        self._page = CDPConnection(WebSocket.connect(ws_url, self.timeout))
        return self._page

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        """CDP call; a dead transport drops the page socket so the next call reconnects."""
        try:
            return self._ensure_page().call(method, params, timeout)
        except (WSError, OSError) as exc:
            self._drop_page()
            raise BrowserError(f"CDP {method}: {exc}") from None

    def _drop_page(self) -> None:
        if self._page is not None:
            self._page.close()
            self._page = None

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        """Runtime.evaluate with returnByValue; raises when the page threw."""
        result = self.call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout
        )
        if result.get("exceptionDetails"):
            raise BrowserError(_exception_text(result["exceptionDetails"]))
        return (result.get("result") or {}).get("value")

    def _wait_loaded(self, timeout: float = 10.0) -> str:
        """Page.navigate returns before the page finishes; poll readyState so the
        next eval/screenshot does not race a half-loaded document."""
        state = ""
        deadline = time.monotonic() + timeout
        while True:
            state = self.evaluate("document.readyState") or ""
            if state == "complete" or time.monotonic() >= deadline:
                return state
            time.sleep(0.1)

    # -- actions ---------------------------------------------------------
    def goto_url(self, url: str) -> Dict[str, Any]:
        try:
            result = self.call("Page.navigate", {"url": url}, self.timeout + 10.0)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if result.get("errorText"):
            return {"error": f"navigation failed: {result['errorText']}"}
        try:
            self._wait_loaded()
        except Exception:
            pass  # page_info below still reports the document's real state
        info = self.page_info()
        if "error" in info:
            return info
        return {"status": "navigated", "url": info.get("url", url), "title": info.get("title", "")}

    def navigate(self, url: str) -> Dict[str, Any]:
        """Alias kept for callers of the old stub."""
        return self.goto_url(url)

    def page_info(self) -> Dict[str, Any]:
        try:
            value = self.evaluate(
                "({url: location.href, title: document.title, state: document.readyState})"
            )
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(value, dict):
            return {"error": "page_info returned no document state"}
        return {
            "url": value.get("url", ""),
            "title": value.get("title", ""),
            "state": value.get("state", "unknown"),
        }

    def snapshot(self) -> Dict[str, Any]:
        """Alias kept for callers of the old stub."""
        return self.page_info()

    def js_eval(self, script: str) -> Dict[str, Any]:
        """Return the script's value, or {"error": ...} when the page threw."""
        try:
            return {"result": self.evaluate(script)}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tabs ------------------------------------------------------------
    def list_tabs(self) -> Dict[str, Any]:
        try:
            self._ensure_page()
            tabs = self._targets()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        active = next((i for i, t in enumerate(tabs) if t.get("id") == self._target), 0)
        return {
            "tabs": [
                {"index": i, "id": t.get("id"), "title": t.get("title") or "",
                 "url": t.get("url") or ""}
                for i, t in enumerate(tabs)
            ],
            "active": active,
        }

    def open_tab(self, url: str) -> Dict[str, Any]:
        """Open a tab and attach to it. The tab it left behind stays open."""
        try:
            self._ensure_page()
            target = http_json(self.port, "PUT", "/json/new?url=" + quote(url, safe=""), self.timeout)
        except Exception as exc:
            return {"error": f"cannot open a tab: {type(exc).__name__}: {exc}"}
        if not isinstance(target, dict) or not target.get("webSocketDebuggerUrl"):
            return {"error": "the browser returned no new tab"}
        self._drop_page()
        self._target = target.get("id") or target["webSocketDebuggerUrl"].rsplit("/", 1)[-1]
        info = self.page_info()
        if "error" in info:
            return info
        return {"status": "opened", "tab": self._target, "index": self.list_tabs()["active"],
                "url": info["url"], "title": info["title"]}

    def use_tab(self, index: int) -> Dict[str, Any]:
        """Attach to the tab at *index*; every later call acts on that tab."""
        try:
            self._ensure_page()
            tabs = self._targets()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not 0 <= index < len(tabs):
            return {"error": f"no tab at index {index}: {len(tabs)} open"}
        self._drop_page()
        self._target = tabs[index].get("id")
        info = self.page_info()
        if "error" in info:
            return info
        return {"status": "switched", "tab": self._target, "index": index,
                "url": info["url"], "title": info["title"]}

    def close_tab(self, index: int) -> Dict[str, Any]:
        try:
            self._ensure_page()
            tabs = self._targets()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not 0 <= index < len(tabs):
            return {"error": f"no tab at index {index}: {len(tabs)} open"}
        target_id = tabs[index].get("id")
        try:
            http_json(self.port, "GET", f"/json/close/{quote(str(target_id), safe='')}", self.timeout)
        except Exception as exc:
            return {"error": f"cannot close tab {index}: {type(exc).__name__}: {exc}"}
        if self._target == target_id:
            # Its socket died with it; the next call attaches to whatever is left.
            self._drop_page()
            self._target = None
        return {"status": "closed", "tab": target_id}

    # -- input -----------------------------------------------------------
    def click(self, selector: str) -> Dict[str, Any]:
        """Click the element's centre with a real mouse event at its real coordinates."""
        script = _CLICK_JS % json.dumps(selector)
        try:
            box = self.evaluate(script)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(box, dict):
            return {"error": f"no element matches {selector!r}"}
        if box.get("w", 0) <= 0 or box.get("h", 0) <= 0:
            return {"error": f"{selector!r} is not visible ({box.get('w')}x{box.get('h')})"}
        x, y = round(float(box["x"]), 1), round(float(box["y"]), 1)
        try:
            self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, self.timeout)
            for kind in ("mousePressed", "mouseReleased"):
                self.call("Input.dispatchMouseEvent",
                          {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                          self.timeout)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"status": "clicked", "selector": selector, "x": x, "y": y}

    def fill(self, selector: str, text: str) -> Dict[str, Any]:
        """Type *text* into the field the selector names, through its own value setter.

        A field that kept something else (read-only, masked, maxlength) is an error: the
        tool must not report a fill the page never received.
        """
        script = _FILL_JS % (json.dumps(selector), json.dumps(text))
        try:
            result = self.evaluate(script)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            return {"error": f"no element matches {selector!r}"}
        if result.get("value") != text:
            return {"error": f"{selector!r} kept {result.get('value')!r} instead of the text typed"}
        return {"status": "filled", "selector": selector, "tag": result.get("tag"), "value": text}

    def screenshot(self, path: Optional[str] = None) -> Dict[str, Any]:
        try:
            result = self.call("Page.captureScreenshot", {"format": "png"}, 30.0)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        data = result.get("data")
        if not data:
            return {"error": "Page.captureScreenshot returned no image data"}
        out = (
            Path(path)
            if path
            else _PROJECT_ROOT / "screenshots" / f"browser-{int(time.time() * 1000)}.png"
        )
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            image = base64.b64decode(data)
            out.write_bytes(image)
        except OSError as exc:
            return {"error": f"cannot write the screenshot: {exc}"}
        return {"path": str(out), "bytes": len(image)}

    def close(self) -> Dict[str, Any]:
        """Browser.close, then terminate the process we launched, then drop the temp profile."""
        errors: List[str] = []
        if self._page is not None:
            try:
                self._page.call("Browser.close", timeout=5.0)
            except Exception as exc:
                errors.append(str(exc))
            self._drop_page()
        if self._proc is not None:
            # Terminate before anything else waits on the process: a thread parked
            # on a pipe holds its buffer lock and would deadlock a later close.
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
            self._proc = None
            self.port = None  # we owned this browser; a later call launches a fresh one
            self._target = None
        if self._profile is not None:
            profile, self._profile = self._profile, None
            shutil.rmtree(profile, ignore_errors=True)
            if profile.exists():
                # Still locked means something is still running: Chrome on Windows
                # sometimes hands the browser off to a successor process that
                # terminate() cannot reach. Sweep, then retry the removal.
                _kill_profile_holders(profile)
                for _ in range(10):
                    shutil.rmtree(profile, ignore_errors=True)
                    if not profile.exists():
                        break
                    time.sleep(0.2)
            if profile.exists():
                errors.append(f"temporary profile could not be removed: {profile}")
        return {"status": "closed", "errors": errors} if errors else {"status": "closed"}


_SESSION: Optional[BrowserTool] = None


def get_session() -> BrowserTool:
    global _SESSION
    if _SESSION is None:
        _SESSION = BrowserTool()
    return _SESSION


def _schema(properties: Optional[Dict[str, Any]] = None, required: Optional[List[str]] = None) -> Dict[str, Any]:
    """Same shape as environment._schema (copied: importing environment would be circular)."""
    return {"type": "object", "properties": properties or {}, "required": required or []}


def _guard(fn) -> Dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # a failed tool call must never raise into the loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_tools(env=None) -> Dict[str, dict]:
    """Ready tool dicts shaped like the entries in environment.py.

    Keys are tool names; values are {"name", "func", "description", "schema"}.
    The session is env.browser when the environment owns one, else the module
    singleton, and it stays alive across calls until browser_close tears it down.
    """

    def current() -> BrowserTool:
        return getattr(env, "browser", None) or get_session()

    def browser_navigate(url: str) -> Dict[str, Any]:
        return _guard(lambda: current().goto_url(url))

    def browser_eval(script: str) -> Dict[str, Any]:
        return _guard(lambda: current().js_eval(script))

    def browser_tabs() -> Dict[str, Any]:
        return _guard(lambda: current().list_tabs())

    def browser_open_tab(url: str) -> Dict[str, Any]:
        return _guard(lambda: current().open_tab(url))

    def browser_use_tab(index: int) -> Dict[str, Any]:
        return _guard(lambda: current().use_tab(int(index)))

    def browser_close_tab(index: int) -> Dict[str, Any]:
        return _guard(lambda: current().close_tab(int(index)))

    def browser_click(selector: str) -> Dict[str, Any]:
        return _guard(lambda: current().click(selector))

    def browser_fill(selector: str, text: str) -> Dict[str, Any]:
        return _guard(lambda: current().fill(selector, text))

    def browser_screenshot() -> Dict[str, Any]:
        return _guard(lambda: current().screenshot())

    def browser_close() -> Dict[str, Any]:
        global _SESSION
        browser = current()
        if browser is _SESSION:
            _SESSION = None
        return _guard(browser.close)

    return {
        "browser_navigate": {
            "name": "browser_navigate",
            "func": browser_navigate,
            "description": (
                "Open a URL in a headless Chrome/Edge and wait for it to load. The browser "
                "stays up for later browser_eval/browser_screenshot calls."
            ),
            "schema": _schema(
                {"url": {"type": "string", "description": "Absolute URL to open, e.g. https://example.com."}},
                ["url"],
            ),
        },
        "browser_eval": {
            "name": "browser_eval",
            "func": browser_eval,
            "description": (
                "Evaluate JavaScript in the current page and return its value. A thrown page "
                "exception comes back as {\"error\": ...}."
            ),
            "schema": _schema(
                {"script": {"type": "string", "description": "JavaScript expression to evaluate in the page."}},
                ["script"],
            ),
        },
        "browser_screenshot": {
            "name": "browser_screenshot",
            "func": browser_screenshot,
            "description": "Capture the current page as a PNG and return the path of the saved file.",
            "schema": _schema(),
        },
        "browser_click": {
            "name": "browser_click",
            "func": browser_click,
            "description": (
                "Click the element a CSS selector names, with a real mouse event at its real "
                "coordinates after scrolling it into view. A selector matching nothing, or an "
                "element with no visible box, comes back as {\"error\": ...}."
            ),
            "schema": _schema(
                {"selector": {"type": "string", "description": "CSS selector of the element to click, e.g. '#submit' or 'a.more'."}},
                ["selector"],
            ),
        },
        "browser_fill": {
            "name": "browser_fill",
            "func": browser_fill,
            "description": (
                "Type text into the input or textarea a CSS selector names, firing the input and "
                "change events a page listens for. If the field keeps something else (read-only, "
                "masked, maxlength) the result is {\"error\": ...}, never a reported success."
            ),
            "schema": _schema(
                {"selector": {"type": "string", "description": "CSS selector of the input/textarea."},
                 "text": {"type": "string", "description": "Text to type into the field."}},
                ["selector", "text"],
            ),
        },
        "browser_tabs": {
            "name": "browser_tabs",
            "func": browser_tabs,
            "description": "List the open tabs (index, id, title, url) and which index is active.",
            "schema": _schema(),
        },
        "browser_open_tab": {
            "name": "browser_open_tab",
            "func": browser_open_tab,
            "description": "Open a URL in a new tab and switch to it. The current tab stays open.",
            "schema": _schema(
                {"url": {"type": "string", "description": "Absolute URL for the new tab."}},
                ["url"],
            ),
        },
        "browser_use_tab": {
            "name": "browser_use_tab",
            "func": browser_use_tab,
            "description": "Switch to the tab at this index (see browser_tabs); later calls act on it.",
            "schema": _schema(
                {"index": {"type": "integer", "description": "Tab index from browser_tabs."}},
                ["index"],
            ),
        },
        "browser_close_tab": {
            "name": "browser_close_tab",
            "func": browser_close_tab,
            "description": "Close the tab at this index. Closing the active tab leaves the session on the next one.",
            "schema": _schema(
                {"index": {"type": "integer", "description": "Tab index from browser_tabs."}},
                ["index"],
            ),
        },
        "browser_close": {
            "name": "browser_close",
            "func": browser_close,
            "description": "Close the browser, its process and its temporary profile.",
            "schema": _schema(),
        },
    }
