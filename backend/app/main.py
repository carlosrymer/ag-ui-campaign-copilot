"""AG-UI HTTP server for the campaign copilot.

One protocol endpoint, `POST /agui`, which accepts a RunAgentInput and returns an
SSE stream of AG-UI events encoded by the official `ag_ui.encoder.EventEncoder`.

Sessions are held in memory and keyed by thread_id so a run can be interrupted for
human approval and resumed on a later request against the same thread.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .agent import CampaignAgent, Session
from .tools import BRAND, CHANNELS, SEGMENTS

DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "gemini-3.6-flash")

app = FastAPI(title="AG-UI Campaign Copilot", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

SESSIONS: dict[str, Session] = {}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": DEFAULT_MODEL,
        "gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
        "moonshot_key": bool(os.environ.get("MOONSHOT_API_KEY")),
        "segments": len(SEGMENTS),
        "channels": len(CHANNELS),
        "brand": BRAND["company"],
    }


@app.get("/dataset")
async def dataset() -> dict[str, Any]:
    return {"segments": SEGMENTS, "channels": CHANNELS, "brand": BRAND}


@app.post("/agui")
async def agui(request: Request) -> Any:
    """AG-UI run endpoint. Body is a RunAgentInput; response is an SSE event stream."""
    raw = await request.json()
    try:
        run_input = RunAgentInput.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=422, content={"error": f"invalid RunAgentInput: {exc}"})

    fwd = run_input.forwarded_props if isinstance(run_input.forwarded_props, dict) else {}
    model = fwd.get("model") or DEFAULT_MODEL

    session = SESSIONS.get(run_input.thread_id)
    if session is None:
        brief = ""
        for m in run_input.messages:
            if m.role == "user" and isinstance(getattr(m, "content", None), str):
                brief = m.content
        if not brief:
            return JSONResponse(status_code=400, content={"error": "no user message in RunAgentInput"})
        session = Session(thread_id=run_input.thread_id, brief=brief, model=model)
        SESSIONS[run_input.thread_id] = session

    agent = CampaignAgent(session)
    encoder = EventEncoder(accept=request.headers.get("accept"))

    async def event_stream():
        async for event in agent.run(run_input.run_id, resume=run_input.resume):
            yield encoder.encode(event)

    return StreamingResponse(
        event_stream(),
        media_type=encoder.get_content_type(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/threads/{thread_id}/state")
async def thread_state(thread_id: str) -> Any:
    session = SESSIONS.get(thread_id)
    if not session or session.state is None:
        return JSONResponse(status_code=404, content={"error": "unknown thread"})
    return {"state": session.state.data, "meter": session.meter.summary(),
            "pending_interrupt": session.pending}
