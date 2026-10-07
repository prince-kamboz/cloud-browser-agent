"""HTTP API around SessionService.

  POST /api/sessions                      {user}  -> open (restores saved logins), returns a live-view URL
  GET  /api/sessions/{user}/live-view             -> a fresh live-view URL (they last 5 minutes)
  POST /api/sessions/{user}/heartbeat             -> "still here"
  POST /api/sessions/{user}/release               -> save logins, stop the browser
  POST /api/sessions/{user}/secret-entry {on}     -> hide the page from the agent while a password is typed
  DELETE /api/users/{user}/logins                 -> delete saved logins ("clear cookies")
  GET  /api/sessions/{user}/connection            -> signed CDP endpoint for the agent service ONLY (needs INTERNAL_TOKEN)

POC: no user authentication yet. On AWS the user comes from the Cognito token, never from the request body.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentcore.control.core import SessionService
from agentcore.control.store import SqliteStore
from agentcore.provider.base import ProviderError, QuotaExceeded

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("control")


def make_service() -> SessionService:
    kind = os.getenv("PROVIDER", "agentcore")
    if kind == "local":
        from agentcore.provider.local import LocalDevProvider
        provider = LocalDevProvider(time.time, viewer_kind=os.getenv("DEV_VIEWER", "iframe"))
    elif kind == "fake":
        from agentcore.provider.fake import FakeProvider
        provider = FakeProvider(time.time, profile_mode=os.getenv("FAKE_PROFILE_MODE", "overwrite"),
                                max_profiles=int(os.getenv("FAKE_MAX_PROFILES", "100")))
    elif kind == "browserbase":
        from agentcore.provider.browserbase import BrowserbaseProvider
        provider = BrowserbaseProvider(os.getenv("BROWSERBASE_API_KEY", ""), os.getenv("BROWSERBASE_PROJECT_ID", ""),
                                       region=os.getenv("BROWSERBASE_REGION") or None)
    else:
        from agentcore.provider.agentcore import AgentCoreProvider
        provider = AgentCoreProvider(region=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
                                     browser_id=os.getenv("AGENTCORE_BROWSER_ID", "aws.browser.v1"))
    return SessionService(
        provider, SqliteStore(os.getenv("STATE_DB", "state.db")),
        idle_after_s=float(os.getenv("IDLE_AFTER_S", "120")), save_every_s=float(os.getenv("SAVE_EVERY_S", "60")),
        session_timeout_s=int(os.getenv("SESSION_TIMEOUT_S", "3600")), strategy=os.getenv("PROFILE_STRATEGY") or ("overwrite" if kind == "browserbase" else "rotate"))


svc = make_service()


def _reaper():
    while True:
        time.sleep(float(os.getenv("REAP_EVERY_S", "5")))
        try:
            did = svc.reap()
            if did:
                log.info("reaper: %s", did)
        except Exception:                                    # never let the loop die
            log.exception("reaper failed")


@asynccontextmanager
async def lifespan(_app):
    log.info("recovered sessions: %s", svc.recover())
    threading.Thread(target=_reaper, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
USER_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def user_of(raw: str) -> str:
    u = (raw or "").strip().lower()
    if not USER_RE.match(u):
        raise HTTPException(400, "user must be 1-32 characters: a-z, 0-9, - or _")
    return u


class OpenReq(BaseModel):
    user: str


class SecretReq(BaseModel):
    on: bool


@app.get("/health")
def health():
    return {"provider": type(svc.p).__name__, "strategy": svc.strategy, "active": len(svc.active)}


def viewer_url(live_url: str, viewport) -> str:
    """What the UI puts in its iframe. DCV needs our viewer page (it drives the DCV client); anything else is a page already."""
    if getattr(svc.p, "viewer_kind", "dcv") == "dcv":
        return "/viewer/dcv.html#" + urlencode({"url": live_url, "w": viewport[0], "h": viewport[1]})
    return live_url


@app.get("/api/config")
def config():
    return {"agent_url": os.getenv("AGENT_URL", ""), "viewer_kind": getattr(svc.p, "viewer_kind", "dcv"),
            "provider": type(svc.p).__name__}


@app.post("/api/sessions")
def open_session(req: OpenReq):
    try:
        r = svc.open(user_of(req.user))
        r["viewer_url"] = viewer_url(r["live_view_url"], r["viewport"])
        return r
    except QuotaExceeded as e:
        raise HTTPException(503, f"AWS account quota reached: {e}")
    except ProviderError as e:
        raise HTTPException(502, str(e))


@app.get("/api/sessions")
def status():
    return svc.status()


@app.get("/api/sessions/{user}/live-view")
def live_view(user: str):
    try:
        u = user_of(user)
        live = svc.live_view(u)
        return {"live_view_url": live, "viewer_url": viewer_url(live, svc.active[u].info.viewport)}
    except KeyError:
        raise HTTPException(404, "no active session")


@app.post("/api/sessions/{user}/heartbeat")
def heartbeat(user: str):
    r = svc.heartbeat(user_of(user))
    if r.get("ended"):
        raise HTTPException(404, r["reason"])
    return r


@app.post("/api/sessions/{user}/release")
def release(user: str):
    log.info("release requested over HTTP for %s", user)
    return svc.release(user_of(user))


@app.post("/api/sessions/{user}/secret-entry")
def secret_entry(user: str, req: SecretReq):
    try:
        svc.secret_entry(user_of(user), req.on)
    except KeyError:
        raise HTTPException(404, "no active session")
    return {"automation_enabled": not req.on}


@app.delete("/api/users/{user}/logins")
def forget(user: str):
    svc.forget(user_of(user))
    return {"ok": True}


@app.get("/api/sessions/{user}/connection")
def connection(user: str, x_internal_token: str = Header(default="")):
    expected = os.getenv("INTERNAL_TOKEN", "")
    if not expected or x_internal_token != expected:         # these headers are credentials: closed unless configured
        raise HTTPException(403, "internal only")
    try:
        ws_url, headers = svc.connection(user_of(user))
    except KeyError:
        raise HTTPException(404, "no active session")
    return {"ws_url": ws_url, "headers": headers}


# ---- the v2 UI and the DCV viewer page (mounted last so the API routes above win)
_UI = Path(os.getenv("UI_DIR", Path(__file__).resolve().parent.parent / "ui"))
_DCV = Path(os.getenv("DCV_SDK_DIR", "/app/dcv-sdk"))      # fetched from AWS's npm package at image build; NOT in git (license)
if _DCV.is_dir():
    app.mount("/dcv-sdk", StaticFiles(directory=str(_DCV)), name="dcv-sdk")
if _UI.is_dir():
    app.mount("/", StaticFiles(directory=str(_UI), html=True), name="ui")
