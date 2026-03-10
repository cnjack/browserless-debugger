"""
FastAPI application - Browser VNC API.

Provides:
  POST   /api/sessions           - create a new browser session
  GET    /api/sessions           - list active sessions
  DELETE /api/sessions/{id}      - close a session
  WS     /ws/{id}               - real-time frame stream + input events
  GET    /                       - web VNC viewer
"""
import asyncio
import logging
import os

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from browser import BrowserSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://localhost:3000")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Browser VNC API",
    description=(
        "Real-time browser remote-control API backed by Browserless. "
        "Streams JPEG frames via WebSocket (VNC-style) and accepts "
        "mouse/keyboard events."
    ),
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# In-memory session store (single process; use Redis for multi-worker setups)
sessions: dict[str, BrowserSession] = {}

# ── Schemas ───────────────────────────────────────────────────────────────────


class SessionRequest(BaseModel):
    url: str = Field("https://example.com", description="URL to open on launch")
    browserless_url: str = Field(
        None,
        description="Override the Browserless endpoint for this session",
    )
    width: int = Field(1280, ge=320, le=3840, description="Viewport width in pixels")
    height: int = Field(720, ge=240, le=2160, description="Viewport height in pixels")


class SessionInfo(BaseModel):
    session_id: str
    ws_url: str
    viewer_url: str
    url: str = ""
    client_count: int = 0


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    return FileResponse("static/index.html")


@app.post("/api/sessions", response_model=SessionInfo, tags=["Sessions"])
async def create_session(req: SessionRequest):
    """
    Spin up a new Chromium tab in Browserless and return a session ID.
    Connect to the WebSocket URL to receive live JPEG frames and send
    mouse/keyboard events.
    """
    bl_url = req.browserless_url or BROWSERLESS_URL
    session = BrowserSession(bl_url)
    session._vp_width = req.width
    session._vp_height = req.height

    try:
        session_id = await session.start(req.url)
    except Exception as exc:
        logger.error("Failed to create session: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"Cannot connect to Browserless at {bl_url}: {exc}",
        ) from exc

    sessions[session_id] = session
    logger.info("Session created: %s → %s", session_id, req.url)

    return SessionInfo(
        session_id=session_id,
        ws_url=f"/ws/{session_id}",
        viewer_url=f"/?session={session_id}",
        url=req.url,
    )


@app.get("/api/sessions", response_model=list[SessionInfo], tags=["Sessions"])
async def list_sessions():
    """List all active browser sessions."""
    return [
        SessionInfo(
            session_id=sid,
            ws_url=f"/ws/{sid}",
            viewer_url=f"/?session={sid}",
            url=s._current_url,
            client_count=s.client_count,
        )
        for sid, s in sessions.items()
    ]


@app.delete("/api/sessions/{session_id}", tags=["Sessions"])
async def close_session(session_id: str):
    """Close a browser session and free all resources."""
    session = sessions.pop(session_id, None)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.close()
    return {"status": "closed", "session_id": session_id}


@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    """
    VNC-style WebSocket endpoint.

    **Server → Client messages** (JSON):
    - `{"type": "frame", "data": "<base64-jpeg>", "format": "jpeg", "url": "...", "loading": bool}`
    - `{"type": "navigate", "url": "..."}`
    - `{"type": "loading"}` / `{"type": "loaded"}`

    **Client → Server messages** (JSON):
    - `{"type": "navigate",  "url": "https://..."}`
    - `{"type": "back"}` / `{"type": "forward"}` / `{"type": "reload"}`
    - `{"type": "mousemove",  "x": 100, "y": 200, "modifiers": 0}`
    - `{"type": "mousedown",  "x": 100, "y": 200, "button": "left", "clickCount": 1}`
    - `{"type": "mouseup",   "x": 100, "y": 200, "button": "left", "clickCount": 1}`
    - `{"type": "wheel",     "x": 100, "y": 200, "deltaX": 0, "deltaY": 120}`
    - `{"type": "keydown", "key": "a", "code": "KeyA", "text": "a", "modifiers": 0, "keyCode": 65}`
    - `{"type": "keyup",     "key": "a", "code": "KeyA", "modifiers": 0, "keyCode": 65}`
    - `{"type": "char",      "text": "a"}`
    - `{"type": "resize",    "width": 1440, "height": 900}`
    - `{"type": "screenshot"}` (no response - server-side only utility)

    **Modifier bitmask**: Alt=1, Ctrl=2, Meta=4, Shift=8
    """
    if session_id not in sessions:
        await websocket.close(code=1008, reason="Session not found")
        return

    session = sessions[session_id]
    await websocket.accept()
    session.add_client(websocket)
    logger.info("WS client joined session %s (total: %d)", session_id, session.client_count)

    try:
        while True:
            data = await websocket.receive_json()
            await session.handle_event(data)
    except WebSocketDisconnect:
        logger.info("WS client left session %s", session_id)
    except asyncio.CancelledError:
        logger.info("WS session %s task cancelled", session_id)
        raise
    except Exception as exc:
        logger.warning("WS error in session %s: %s", session_id, exc)
    finally:
        session.remove_client(websocket)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )
