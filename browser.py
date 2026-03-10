"""
Browser session management.

Each BrowserSession owns one Chrome tab (via CDP) and streams
screencast frames to all connected WebSocket clients.
"""
import json
import logging
import uuid
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import WebSocket

from cdp_client import CDPClient

logger = logging.getLogger(__name__)

# Default browser viewport
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720


class BrowserSession:
    """
    Lifecycle:
        session = BrowserSession("http://localhost:3000")
        session_id = await session.start("https://example.com")
        # attach WebSocket clients - they receive JPEG frames in real-time
        session.add_client(ws)
        # send control events
        await session.handle_event({"type": "click", "x": 100, "y": 200})
        # clean up
        await session.close()
    """

    def __init__(self, browserless_url: str = "http://localhost:3000"):
        self.browserless_url = browserless_url.rstrip("/")
        self.session_id: str = str(uuid.uuid4())
        self.cdp: CDPClient | None = None
        self._clients: set[WebSocket] = set()
        self._current_url: str = ""
        self._is_loading: bool = False
        self._vp_width: int = DEFAULT_WIDTH
        self._vp_height: int = DEFAULT_HEIGHT

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, initial_url: str = "https://example.com") -> str:
        """
        Create a new browser page via Browserless /json/new,
        connect to its CDP endpoint, and begin screencasting.
        Returns the session_id.
        """
        # Ask Browserless to open a new page target
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.put(f"{self.browserless_url}/json/new")
            resp.raise_for_status()
            target = resp.json()

        ws_url = self._resolve_ws_url(target["webSocketDebuggerUrl"])

        self.cdp = CDPClient(ws_url)
        await self.cdp.connect()

        # Enable required CDP domains
        await self.cdp.send("Page.enable")
        await self.cdp.send("Runtime.enable")

        # Fix viewport
        await self._set_viewport(self._vp_width, self._vp_height)

        # Event wiring
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.cdp.on("Page.frameNavigated", self._on_navigated)
        self.cdp.on("Page.frameStartedLoading", self._on_loading_start)
        self.cdp.on("Page.loadEventFired", self._on_load)

        # Begin streaming
        await self._start_screencast()

        # Navigate to initial page
        if initial_url:
            await self.navigate(initial_url)

        return self.session_id

    async def close(self):
        """Stop screencasting and disconnect CDP."""
        try:
            if self.cdp:
                await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        if self.cdp:
            await self.cdp.disconnect()
        self._clients.clear()
        logger.info("Session %s closed", self.session_id)

    # ── Client management ─────────────────────────────────────────────────────

    def add_client(self, ws: WebSocket):
        self._clients.add(ws)

    def remove_client(self, ws: WebSocket):
        self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ── Event handling ────────────────────────────────────────────────────────

    async def handle_event(self, event: dict):
        """Dispatch an input/control event from a client to the browser."""
        t = event.get("type")

        if t == "navigate":
            await self.navigate(event.get("url", ""))

        elif t == "back":
            await self.cdp.send("Runtime.evaluate", {"expression": "history.back()"})

        elif t == "forward":
            await self.cdp.send("Runtime.evaluate", {"expression": "history.forward()"})

        elif t == "reload":
            await self.cdp.send("Runtime.evaluate", {"expression": "location.reload()"})

        elif t == "mousemove":
            await self.cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": event["x"],
                "y": event["y"],
                "modifiers": event.get("modifiers", 0),
            })

        elif t == "mousedown":
            await self.cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": event["x"],
                "y": event["y"],
                "button": event.get("button", "left"),
                "clickCount": event.get("clickCount", 1),
                "modifiers": event.get("modifiers", 0),
            })

        elif t == "mouseup":
            await self.cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": event["x"],
                "y": event["y"],
                "button": event.get("button", "left"),
                "clickCount": event.get("clickCount", 1),
                "modifiers": event.get("modifiers", 0),
            })

        elif t == "wheel":
            await self.cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": event["x"],
                "y": event["y"],
                "deltaX": event.get("deltaX", 0),
                "deltaY": event.get("deltaY", 0),
                "modifiers": event.get("modifiers", 0),
            })

        elif t == "keydown":
            # Include text for printable characters so that input fields receive the value
            text = event.get("text", "")
            await self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "key": event.get("key", ""),
                "code": event.get("code", ""),
                "text": text,
                "modifiers": event.get("modifiers", 0),
                "windowsVirtualKeyCode": event.get("keyCode", 0),
                "nativeVirtualKeyCode": event.get("keyCode", 0),
                "isSystemKey": False,
            })

        elif t == "keyup":
            await self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "key": event.get("key", ""),
                "code": event.get("code", ""),
                "modifiers": event.get("modifiers", 0),
                "windowsVirtualKeyCode": event.get("keyCode", 0),
                "nativeVirtualKeyCode": event.get("keyCode", 0),
            })

        elif t == "char":
            # triggers textInput / input events for typing into fields
            await self.cdp.send("Input.dispatchKeyEvent", {
                "type": "char",
                "text": event.get("text", ""),
                "modifiers": event.get("modifiers", 0),
            })

        elif t == "resize":
            w = max(320, min(event.get("width", DEFAULT_WIDTH), 3840))
            h = max(240, min(event.get("height", DEFAULT_HEIGHT), 2160))
            self._vp_width, self._vp_height = w, h
            await self._set_viewport(w, h)
            # Restart screencast with new dimensions
            await self.cdp.send("Page.stopScreencast")
            await self._start_screencast()

        elif t == "screenshot":
            result = await self.cdp.send("Page.captureScreenshot",
                                         {"format": "png", "quality": 100})
            return result.get("data")

    # ── Navigation helper ─────────────────────────────────────────────────────

    async def navigate(self, url: str):
        if url and not url.startswith(("http://", "https://", "about:", "data:", "file:")):
            url = "https://" + url
        await self.cdp.send("Page.navigate", {"url": url})

    # ── CDP event callbacks ───────────────────────────────────────────────────

    async def _on_frame(self, params: dict):
        session_id = params.get("sessionId")
        data = params.get("data", "")

        # Acknowledge immediately so Chrome keeps sending
        try:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

        msg = json.dumps({
            "type": "frame",
            "data": data,
            "format": "jpeg",
            "url": self._current_url,
            "loading": self._is_loading,
        })
        await self._broadcast(msg)

    async def _on_navigated(self, params: dict):
        frame = params.get("frame", {})
        url = frame.get("url", "")
        if url and not url.startswith(("chrome-", "devtools:")):
            self._current_url = url
            await self._broadcast(json.dumps({"type": "navigate", "url": url}))

    async def _on_loading_start(self, params: dict):
        self._is_loading = True
        await self._broadcast(json.dumps({"type": "loading"}))

    async def _on_load(self, params: dict):
        self._is_loading = False
        await self._broadcast(json.dumps({"type": "loaded"}))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _broadcast(self, msg: str):
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def _set_viewport(self, width: int, height: int):
        await self.cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False,
        })

    async def _start_screencast(self):
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 75,
            "maxWidth": self._vp_width,
            "maxHeight": self._vp_height,
            "everyNthFrame": 1,
        })

    def _resolve_ws_url(self, ws_url: str) -> str:
        """
        Rewrite the WebSocket URL returned by /json/new so that the host
        matches the configured BROWSERLESS_URL (handles Docker/remote setups).
        """
        bl = urlparse(self.browserless_url)
        ws = urlparse(ws_url)
        scheme = "wss" if bl.scheme == "https" else "ws"
        return urlunparse((scheme, bl.netloc, ws.path, "", "", ""))
