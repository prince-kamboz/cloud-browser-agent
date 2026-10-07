"""Browser Use Cloud's browser API (https://docs.browser-use.com/cloud) as the remote-browser backend: PROVIDER=browseruse.

Only the *Browser* product is used (a managed Chromium over CDP), not Browser Use's hosted agents or its open-source
agent library: our own Deep Agents code stays the agent.

How the interface maps (API v4, auth header X-Browser-Use-API-Key):
  session            -> POST /browsers                       (status "active" = ready)
  profile            -> a Browser Use *profile*              (cookies, localStorage; the docs: "one profile per user")
  stop               -> PATCH /browsers/{id} {"action":"stop"}   (the docs say NOT to rely on a dropped CDP connection; this
                        also stops billing and refunds unused time)
  agent connection   -> session.cdpUrl                       (no extra headers)
  live view          -> session.liveUrl                      (a hosted page, shown in an iframe)

Built from the OpenAPI spec and docs; exercise it with cloud_browser_agent/bench/run_bench.sh once you have a key.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

from .base import BrowserProvider, ProviderError, QuotaExceeded, SessionInfo

API = "https://api.browser-use.com/api/v4"


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class BrowserUseProvider(BrowserProvider):
    viewer_kind = "iframe"
    profile_at_start = True      # a profile is attached when the browser starts; its state is kept when the session stops

    def __init__(self, api_key: str, api: str = API, transport: Callable = _urllib_transport,
                 proxy_country: Optional[str] = None, timeout: float = 30):
        if not api_key:
            raise ProviderError("BROWSER_USE_API_KEY is required")
        self._key, self.api, self._t, self._timeout = api_key, api, transport, timeout
        self.proxy_country = proxy_country or None      # None = no managed proxy (the API default is a US residential proxy)

    # ------------------------------------------------------------------------------------------ http
    def _call(self, method: str, path: str, body: Optional[dict] = None, ok_missing: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Browser-Use-API-Key": self._key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._t(method, f"{self.api}{path}", headers, data, self._timeout)
        except OSError as e:
            raise ProviderError(f"Browser Use unreachable: {e}") from e
        if ok_missing and status == 404:
            return None
        if status >= 400:
            msg = raw.decode("utf-8", "replace")[:300]
            try:
                j = json.loads(raw)
                msg = j.get("message") or j.get("detail") or msg
            except (ValueError, AttributeError):
                pass
            if status in (402, 429) or "concurren" in str(msg).lower() or "credit" in str(msg).lower():
                raise QuotaExceeded(f"{status}: {msg}")
            raise ProviderError(f"Browser Use {status}: {msg}")
        return json.loads(raw) if raw else {}

    # -------------------------------------------------------------------------------------- sessions
    def start_session(self, user_id, profile_id=None, timeout_s=3600, viewport=(1280, 800)) -> SessionInfo:
        body = {"timeout": max(1, min(math.ceil(timeout_s / 60), 240)), "proxyCountryCode": self.proxy_country,
                "browserScreenWidth": viewport[0], "browserScreenHeight": viewport[1], "metadata": {"user": user_id[:100]}}
        if profile_id:
            body["profileId"] = profile_id
        r = self._call("POST", "/browsers", body)
        return SessionInfo(session_id=r["id"], browser_id="browseruse", created_at=time.time(), viewport=viewport,
                           profile_id=profile_id, ws_url=r.get("cdpUrl") or "")

    def _get(self, session) -> dict:
        return self._call("GET", f"/browsers/{session.session_id}", ok_missing=True) or {"status": "NOT_FOUND"}

    def session_status(self, session) -> str:
        st = self._get(session).get("status", "UNKNOWN")
        return "READY" if st == "active" else st.upper()

    def wait_ready(self, session, timeout_s=60) -> None:
        end, delay = time.time() + timeout_s, 0.2
        while time.time() < end:
            st = self.session_status(session)
            if st == "READY":
                return
            if st in ("STOPPED", "NOT_FOUND"):
                raise ProviderError(f"session ended while starting ({st})")
            time.sleep(delay)
            delay = min(delay * 1.5, 1.5)
        raise ProviderError("session did not become ready in time")

    def stop_session(self, session) -> None:
        self._call("PATCH", f"/browsers/{session.session_id}", {"action": "stop"}, ok_missing=True)
        end = time.time() + 30                           # profile state is stored as the session winds down: wait for it
        while time.time() < end and self._get(session).get("status") == "active":
            time.sleep(0.5)

    # ------------------------------------------------------------------------------------ connecting
    def automation_connection(self, session) -> Tuple[str, Dict[str, str]]:
        r = self._get(session)
        url = r.get("cdpUrl")
        if not url or r.get("status") != "active":
            raise ProviderError(f"session is not running ({r.get('status')})")
        return url, {}

    def live_view_url(self, session, expires_s: int = 300) -> str:
        url = self._get(session).get("liveUrl")
        if not url:
            raise ProviderError("Browser Use returned no live view URL")
        return url

    def set_automation_enabled(self, session, enabled: bool) -> None:
        return None      # not supported; the agent's pause gate is what stops it while the user types secrets

    # --------------------------------------------------------------------------------------- profiles
    def create_profile(self, name: str) -> str:
        return self._call("POST", "/profiles", {"name": name[:100], "userId": name[:100]})["id"]

    def save_profile(self, session, profile_id: str) -> None:
        return None      # kept when the session stops

    def delete_profile(self, profile_id: str) -> None:
        self._call("DELETE", f"/profiles/{profile_id}", ok_missing=True)
