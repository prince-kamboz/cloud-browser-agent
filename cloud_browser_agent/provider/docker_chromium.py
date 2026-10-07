"""A real Chromium in a local Docker container per session (PROVIDER=docker). No account, no cloud, no cost.

How the interface maps:
  session            -> one container from the cba-chromium image, on the same Docker network as the agent
  profile            -> a Docker volume mounted at /data (Chromium's user-data-dir): cookies, localStorage, IndexedDB, tabs
  stop               -> `docker stop` (the image's start.sh asks Chromium to quit through CDP so it flushes to the volume),
                        then the container is removed; the volume stays
  agent connection   -> ws://<container>:8080/cdp/devtools/browser/<id> over the Docker network (no credentials)
  live view          -> our screencast viewer page, fed by a WebSocket bridge in the control plane (control/live_bridge.py)
  tabs               -> Chromium's own /json endpoints, so the UI draws the tab strip as it does for Browserbase

The control plane needs the Docker socket for this, which is root on the host: fine on a laptop, not for a shared server.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from .base import BrowserProvider, ProviderError, QuotaExceeded, SessionInfo

LABEL = "cba.managed"
CDP_PORT = 8080


def _http(method: str, url: str, timeout: float = 5):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class DockerChromiumProvider(BrowserProvider):
    viewer_kind = "screencast"
    profile_at_start = True      # the volume is mounted when the container starts; Chromium writes to it as it runs

    def __init__(self, image: str = "cba-chromium", network: str = "cba-net", client=None,
                 http: Callable = _http, max_browsers: int = 5, memory: str = "1g", stop_timeout_s: int = 30):
        if client is None:
            import docker
            client = docker.from_env()
        self.dc, self.image, self.network, self._http = client, image, network, http
        self.max_browsers, self.memory, self.stop_timeout_s = max_browsers, memory, stop_timeout_s

    # ------------------------------------------------------------------------------------------ helpers
    def _errors(self):
        import docker.errors as de
        return de

    def _base(self, session) -> str:
        return f"http://{session.browser_id}:{CDP_PORT}/cdp"

    def _json(self, session, path: str, method: str = "GET"):
        status, body = self._http(method, f"{self._base(session)}{path}")
        if status >= 400:
            raise ProviderError(f"browser returned {status} for {path}")
        return json.loads(body) if body else {}

    # ------------------------------------------------------------------------------------------ sessions
    def start_session(self, user_id, profile_id=None, timeout_s=3600, viewport=(1280, 800)) -> SessionInfo:
        de = self._errors()
        try:
            running = self.dc.containers.list(filters={"label": LABEL})
        except de.DockerException as e:
            raise ProviderError(f"cannot reach Docker: {e}") from e
        if len(running) >= self.max_browsers:
            raise QuotaExceeded(f"{len(running)} browsers are already running (DOCKER_MAX_BROWSERS={self.max_browsers})")
        sid = uuid.uuid4().hex[:12]
        name = f"cba-browser-{sid}"
        volumes = {profile_id: {"bind": "/data", "mode": "rw"}} if profile_id else None
        try:
            self.dc.containers.run(
                self.image, name=name, detach=True, network=self.network, volumes=volumes, shm_size="256m",
                mem_limit=self.memory, pids_limit=512,
                environment={"SCREEN_WIDTH": str(viewport[0]), "SCREEN_HEIGHT": str(viewport[1])},
                labels={LABEL: "1", "cba.user": user_id[:100], "cba.session": sid})
        except de.ImageNotFound as e:
            raise ProviderError(f"image '{self.image}' not found: build it with `docker compose --profile docker build chromium`") from e
        except de.DockerException as e:
            raise ProviderError(f"could not start a browser container: {e}") from e
        return SessionInfo(session_id=sid, browser_id=name, created_at=time.time(), viewport=viewport, profile_id=profile_id)

    def _container(self, session):
        de = self._errors()
        try:
            return self.dc.containers.get(session.browser_id)
        except de.NotFound:
            return None
        except de.DockerException as e:
            raise ProviderError(f"cannot reach Docker: {e}") from e

    def session_status(self, session) -> str:
        c = self._container(session)
        if c is None:
            return "NOT_FOUND"
        return "READY" if c.status == "running" else c.status.upper()

    def wait_ready(self, session, timeout_s=60) -> None:
        end, delay = time.time() + timeout_s, 0.2
        while time.time() < end:
            if self.session_status(session) != "READY":
                raise ProviderError("the browser container stopped while starting")
            try:
                if self._http("GET", f"{self._base(session)}/json/version")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 1.0)
        raise ProviderError("the browser did not become ready in time")

    def stop_session(self, session) -> None:
        de = self._errors()
        c = self._container(session)
        if c is None:
            return
        try:
            c.stop(timeout=self.stop_timeout_s)       # start.sh quits Chromium cleanly first, so the volume is flushed
            c.remove(force=True)
        except de.NotFound:
            pass
        except de.DockerException as e:
            raise ProviderError(f"could not stop the browser: {e}") from e

    # ------------------------------------------------------------------------------------------ connecting
    def automation_connection(self, session) -> Tuple[str, Dict[str, str]]:
        try:
            ws = self._json(session, "/json/version")["webSocketDebuggerUrl"]       # ws://localhost/devtools/browser/<id>
        except (OSError, KeyError) as e:
            raise ProviderError(f"browser not reachable: {e}") from e
        path = "/" + ws.split("/", 3)[3]
        return f"ws://{session.browser_id}:{CDP_PORT}/cdp{path}", {}

    def live_view_url(self, session, expires_s: int = 300) -> str:
        return f"/viewer/screencast.html#session={session.session_id}"

    def set_automation_enabled(self, session, enabled: bool) -> None:
        return None      # not supported; the agent's pause gate is what stops it while the user types secrets

    # ------------------------------------------------------------------------------------------ tabs
    def _pages(self, session) -> List[dict]:
        try:
            return [t for t in self._json(session, "/json/list") if t.get("type") == "page"]
        except OSError as e:
            raise ProviderError(f"browser not reachable: {e}") from e

    def tabs(self, session):
        return [{"id": t["id"], "title": t.get("title") or t.get("url") or "New tab", "url": t.get("url", ""),
                 "view_url": f"/viewer/screencast.html#session={session.session_id}&tab={t['id']}"} for t in self._pages(session)]

    def open_tab(self, session, url="about:blank") -> None:
        self._json(session, f"/json/new?{urllib.request.quote(url, safe=':/?=&%#')}", method="PUT")

    def close_tab(self, session, tab_id: str) -> None:
        self._json(session, f"/json/close/{urllib.request.quote(tab_id)}")

    def page_ws(self, session, tab_id: Optional[str] = None) -> str:
        """The WebSocket the live-view bridge relays: one page's DevTools endpoint, on the Docker network."""
        pages = self._pages(session)
        page = next((t for t in pages if t["id"] == tab_id), None) if tab_id else (pages[0] if pages else None)
        if page is None:
            raise ProviderError("no such tab")
        return f"ws://{session.browser_id}:{CDP_PORT}/cdp/devtools/page/{page['id']}"

    # ------------------------------------------------------------------------------------------ profiles
    def create_profile(self, name: str) -> str:
        de = self._errors()
        try:
            return self.dc.volumes.create(name=f"cba-profile-{uuid.uuid4().hex[:12]}", labels={LABEL: "1", "cba.name": name[:100]}).name
        except de.DockerException as e:
            raise ProviderError(f"could not create a profile volume: {e}") from e

    def save_profile(self, session, profile_id: str) -> None:
        return None      # Chromium writes to the volume as it runs; the clean shutdown flushes the rest

    def delete_profile(self, profile_id: str) -> None:
        de = self._errors()
        try:
            self.dc.volumes.get(profile_id).remove(force=True)
        except de.NotFound:
            pass
        except de.DockerException as e:
            raise ProviderError(f"could not delete the profile: {e}") from e
