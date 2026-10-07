"""Browserbase (https://docs.browserbase.com) as the remote-browser backend.

How the interface maps:
  session            -> POST /v1/sessions            (status RUNNING = ready)
  profile            -> a Browserbase *context*      (cookies + local storage, saved when the session ends)
  stop               -> POST /v1/sessions/{id} status=REQUEST_RELEASE   (this is also what saves the context)
  agent connection   -> session.connectUrl           (a wss URL that already carries its credentials: no signed headers)
  live view          -> GET /v1/sessions/{id}/debug  (debuggerFullscreenUrl: a normal web page we show in an iframe)

keepAlive is on: without it Browserbase ends the session when the last CDP client disconnects (the agent
reconnects often and the user may have the live view open with no agent attached).

Differences from AgentCore that the control plane has to live with:
  * a context is written when the session closes, so save_profile() mid-session is a no-op. Use PROFILE_STRATEGY=overwrite
    (one context per user, reused); "rotate" would create a new context every autosave.
  * there is no switch to hide the agent stream from the live view, so set_automation_enabled() is a no-op; the agent's
    pause gate is what stops it acting while the user types secrets. Session recording is turned off so typed secrets
    do not end up in a replay.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from .base import BrowserProvider, ProviderError, QuotaExceeded, SessionInfo

API = "https://api.browserbase.com/v1"
MIN_TIMEOUT, MAX_TIMEOUT = 60, 21600


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class BrowserbaseProvider(BrowserProvider):
    viewer_kind = "iframe"       # the live view is a plain web page
    profile_at_start = True      # a context is attached when the session starts, saved when it ends

    def __init__(self, api_key: str, project_id: str, api: str = API, transport: Callable = _urllib_transport,
                 region: Optional[str] = None, timeout: float = 30):
        if not api_key or not project_id:
            raise ProviderError("BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID are required")
        self._key, self.project_id, self.api, self._t, self.region, self._timeout = api_key, project_id, api, transport, region, timeout

    # ------------------------------------------------------------------------------------------ http
    def _call(self, method: str, path: str, body: Optional[dict] = None, ok_missing: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-BB-API-Key": self._key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._t(method, f"{self.api}{path}", headers, data, self._timeout)
        except OSError as e:
            raise ProviderError(f"Browserbase unreachable: {e}") from e
        if ok_missing and status == 404:
            return None
        if status >= 400:
            msg = raw.decode("utf-8", "replace")[:300]
            try:
                msg = json.loads(raw).get("message", msg)
            except (ValueError, AttributeError):
                pass
            if status in (402, 429) or "concurren" in msg.lower() or "limit" in msg.lower():
                raise QuotaExceeded(f"{status}: {msg}")
            raise ProviderError(f"Browserbase {status}: {msg}")
        return json.loads(raw) if raw else {}

    # -------------------------------------------------------------------------------------- sessions
    def start_session(self, user_id, profile_id=None, timeout_s=3600, viewport=(1280, 800)) -> SessionInfo:
        settings: dict = {"viewport": {"width": viewport[0], "height": viewport[1]}, "recordSession": False,
                          "logSession": False}
        if profile_id:
            settings["context"] = {"id": profile_id, "persist": True}
        body = {"projectId": self.project_id, "timeout": max(MIN_TIMEOUT, min(int(timeout_s), MAX_TIMEOUT)),
                "keepAlive": True, "browserSettings": settings, "userMetadata": {"user": user_id[:200]}}
        if self.region:
            body["region"] = self.region
        r = self._call("POST", "/sessions", body)
        return SessionInfo(session_id=r["id"], browser_id="browserbase", created_at=time.time(), viewport=viewport,
                           profile_id=profile_id, ws_url=r.get("connectUrl", ""))

    def _get(self, session) -> dict:
        r = self._call("GET", f"/sessions/{session.session_id}", ok_missing=True)
        return r or {"status": "NOT_FOUND"}

    def session_status(self, session) -> str:
        st = self._get(session).get("status", "UNKNOWN")
        return "READY" if st == "RUNNING" else st          # COMPLETED / TIMED_OUT / ERROR / NOT_FOUND all mean gone

    def wait_ready(self, session, timeout_s=60) -> None:
        end, delay = time.time() + timeout_s, 0.2
        while time.time() < end:
            st = self.session_status(session)
            if st == "READY":
                return
            if st in ("COMPLETED", "TIMED_OUT", "ERROR", "NOT_FOUND"):
                raise ProviderError(f"session ended while starting ({st})")
            time.sleep(delay)
            delay = min(delay * 1.5, 1.5)
        raise ProviderError("session did not become ready in time")

    def stop_session(self, session) -> None:
        self._call("POST", f"/sessions/{session.session_id}",
                   {"projectId": self.project_id, "status": "REQUEST_RELEASE"}, ok_missing=True)
        # the context is uploaded while the session winds down; wait for it so a quick reopen sees the saved state
        end = time.time() + 30
        while time.time() < end and self._get(session).get("status") == "RUNNING":
            time.sleep(0.5)

    # ------------------------------------------------------------------------------------ connecting
    def automation_connection(self, session) -> Tuple[str, Dict[str, str]]:
        r = self._get(session)       # fetched fresh, so it also works for a session re-adopted after a restart
        url = r.get("connectUrl")
        if not url or r.get("status") != "RUNNING":
            raise ProviderError(f"session is not running ({r.get('status')})")
        return url, {}

    def live_view_url(self, session, expires_s: int = 300) -> str:
        r = self._call("GET", f"/sessions/{session.session_id}/debug")
        url = r.get("debuggerFullscreenUrl") or r.get("debuggerUrl")
        if not url:
            raise ProviderError("Browserbase returned no live view URL")
        return url

    def set_automation_enabled(self, session, enabled: bool) -> None:
        return None      # not supported; see module docstring

    # ------------------------------------------------------------------------------------------- tabs
    # Browserbase's live view shows ONE page; /debug lists every page with its own live-view URL, so our UI draws the tabs.
    def tabs(self, session):
        r = self._call("GET", f"/sessions/{session.session_id}/debug")
        return [{"id": p["id"], "title": p.get("title") or p.get("url") or "New tab", "url": p.get("url", ""),
                 "view_url": p.get("debuggerFullscreenUrl") or p.get("debuggerUrl", "")} for p in r.get("pages", [])]

    def _cdp(self, session, method: str, params: dict):
        from websockets.sync.client import connect          # one short-lived browser-level CDP call
        url, _ = self.automation_connection(session)
        try:
            with connect(url, max_size=None, open_timeout=15) as ws:
                ws.send(json.dumps({"id": 1, "method": method, "params": params}))
                while True:
                    m = json.loads(ws.recv(timeout=15))
                    if m.get("id") == 1:
                        if "error" in m:
                            raise ProviderError(f"{method}: {m['error'].get('message')}")
                        return m.get("result", {})
        except (OSError, TimeoutError) as e:
            raise ProviderError(f"{method} failed: {e}") from e

    def open_tab(self, session, url="about:blank") -> None:
        self._cdp(session, "Target.createTarget", {"url": url})

    def close_tab(self, session, tab_id: str) -> None:
        self._cdp(session, "Target.closeTarget", {"targetId": tab_id})

    # --------------------------------------------------------------------------------------- profiles
    def create_profile(self, name: str) -> str:
        return self._call("POST", "/contexts", {"projectId": self.project_id})["id"]

    def save_profile(self, session, profile_id: str) -> None:
        return None      # written automatically when the session is released

    def delete_profile(self, profile_id: str) -> None:
        self._call("DELETE", f"/contexts/{profile_id}", ok_missing=True)
