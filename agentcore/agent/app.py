"""HTTP API of the v2 agent service.

  POST /chat        {user, message, thread_id}  -> server-sent events (action / assistant / error / done)
  POST /pause|resume|stop|disconnect  {user}
  GET  /health

Where the browser address comes from (SOURCE):
  control   the control plane (CONTROL_URL + INTERNAL_TOKEN): production shape
  static    STATIC_WS_URL (+ STATIC_HEADERS as JSON): a fixed address, for a local Chromium
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agentcore.agent.connection import ControlPlaneSource, StaticSource
from agentcore.agent.service import AgentService


def make_source():
    if os.getenv("SOURCE", "control") == "static":
        return StaticSource(os.environ["STATIC_WS_URL"], json.loads(os.getenv("STATIC_HEADERS", "{}")))
    return ControlPlaneSource(os.getenv("CONTROL_URL", "http://control:8100"), os.getenv("INTERNAL_TOKEN", ""))


def make_service() -> AgentService:
    from langgraph.checkpoint.memory import MemorySaver
    return AgentService(make_source(), os.getenv("MAIN_MODEL", "gpt-6.1-sol"), os.getenv("VISION_MODEL", "gpt-6.1-sol"),
                        checkpointer=MemorySaver())


svc = make_service()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[o for o in os.getenv("CORS_ORIGINS", "http://localhost:8100,http://127.0.0.1:8100").split(",") if o],
                   allow_methods=["GET", "POST"], allow_headers=["*"])


class ChatReq(BaseModel):
    user: str
    message: str
    thread_id: str = "default"


class UserReq(BaseModel):
    user: str


@app.get("/health")
def health():
    return {"key_set": bool(os.getenv("OPENAI_API_KEY")),
            "users": {u: {"connected": rt.mcp.alive, "paused": rt.gate.paused, "busy": rt.lock.locked()}
                      for u, rt in svc.rts.items()}}


@app.post("/chat")
async def chat(req: ChatReq):
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"error": "OPENAI_API_KEY is not set"}, status_code=400)

    async def stream():
        async for ev in svc.chat(req.user, req.message, req.thread_id):
            yield f"data: {json.dumps(ev, default=str)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/pause")
def pause(r: UserReq):
    svc.pause(r.user); return {"paused": True}


@app.post("/resume")
def resume(r: UserReq):
    svc.resume(r.user); return {"paused": False}


@app.post("/stop")
async def stop(r: UserReq):
    await svc.stop(r.user); return {"stopped": True}


@app.post("/disconnect")
async def disconnect(r: UserReq):
    await svc.disconnect(r.user); return {"disconnected": True}
