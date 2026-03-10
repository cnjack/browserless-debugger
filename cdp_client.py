"""
Chrome DevTools Protocol (CDP) WebSocket client.

Connects to a single browser target and provides async send/event APIs.
"""
import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class CDPClient:
    """Async CDP client over a raw WebSocket connection to a browser target."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._id_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: dict[str, list[Callable]] = {}
        self._recv_task: asyncio.Task | None = None
        self._event_tasks: set[asyncio.Task] = set()

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self):
        """Open the WebSocket and start the receive loop."""
        self._ws = await websockets.connect(
            self.ws_url,
            max_size=100 * 1024 * 1024,  # 100 MB - large screencast frames
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("CDP connected to %s", self.ws_url)

    async def disconnect(self):
        """Close the connection and cancel all pending requests."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._ws:
            await self._ws.close()
            self._ws = None

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict[str, Any]:
        """Send a CDP command and await its response."""
        if self._ws is None:
            raise RuntimeError("CDP client is not connected")

        msg_id = self._next_id()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut

        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        await self._ws.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP command '{method}' timed out after {timeout}s") from None
        except asyncio.CancelledError:
            self._pending.pop(msg_id, None)
            # If the current task itself is being cancelled, propagate properly.
            # Otherwise this is a CDP-level future cancellation (connection lost).
            task = asyncio.current_task()
            if task and task.cancelling() > 0:
                raise
            raise RuntimeError(f"CDP command '{method}' cancelled (connection lost)") from None

    # ── Event listeners ───────────────────────────────────────────────────────

    def on(self, event: str, callback: Callable):
        """Register an async or sync callback for a CDP event."""
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable):
        if event in self._listeners:
            self._listeners[event] = [
                cb for cb in self._listeners[event] if cb is not callback
            ]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if "id" in msg:
                    # Response to a pending send()
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(msg["error"].get("message", "CDP error"))
                            )
                        else:
                            fut.set_result(msg.get("result", {}))

                elif "method" in msg:
                    # Unsolicited event from the browser
                    method = msg["method"]
                    params = msg.get("params", {})
                    for cb in list(self._listeners.get(method, [])):
                        task = asyncio.create_task(self._safe_call(cb, params))
                        self._event_tasks.add(task)
                        task.add_done_callback(self._event_tasks.discard)

        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.error("CDP recv loop error: %s", exc)

    @staticmethod
    async def _safe_call(cb: Callable, params: dict):
        try:
            result = cb(params)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("CDP event callback error: %s", exc)
