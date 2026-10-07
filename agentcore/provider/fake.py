"""In-memory stand-in for AgentCore Browser, for tests and local development without AWS.

It can behave either way on the open questions, so the control plane is proven against both:
  profile_mode="overwrite": saving into an existing profile replaces its data
  profile_mode="immutable": a second save into the same profile is refused (ProfileConflict)
  max_profiles=0         : like this account today ("maxBrowserProfiles limit exceeded")
"""
from __future__ import annotations

import copy
import itertools
import threading
from typing import Callable, Dict, Optional, Tuple

from .base import BrowserProvider, ProfileConflict, ProviderError, QuotaExceeded, SessionInfo


class FakeProvider(BrowserProvider):
    viewer_kind = "iframe"

    def __init__(self, clock: Callable[[], float], profile_mode: str = "overwrite", max_profiles: int = 100,
                 start_latency_s: float = 0.0):
        assert profile_mode in ("overwrite", "immutable")
        self.clock, self.profile_mode, self.max_profiles = clock, profile_mode, max_profiles
        self.start_latency_s = start_latency_s
        self.profiles: Dict[str, Optional[dict]] = {}        # id -> saved state (None = created, never saved)
        self.sessions: Dict[str, dict] = {}                  # id -> {"info", "state", "ends_at", "stopped", "automation"}
        self.calls: list = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # --- helpers for tests -------------------------------------------------------------------------------
    def browser_state(self, session: SessionInfo) -> dict:
        """What the page holds right now (cookies, local storage). Tests mutate this like a user logging in."""
        return self.sessions[session.session_id]["state"]

    def live_sessions(self):
        return [s for s in self.sessions.values() if not s["stopped"] and self.clock() < s["ends_at"]]

    # --- sessions ----------------------------------------------------------------------------------------
    def start_session(self, user_id, profile_id=None, timeout_s=3600, viewport=(1280, 800)) -> SessionInfo:
        with self._lock:
            sid = f"sess{next(self._ids):04d}"
            if profile_id is not None and profile_id not in self.profiles:
                raise ProviderError(f"unknown profile {profile_id}")
            state = copy.deepcopy(self.profiles.get(profile_id) or {}) if profile_id else {}
            info = SessionInfo(session_id=sid, browser_id="aws.browser.v1", created_at=self.clock(),
                               viewport=viewport, profile_id=profile_id,
                               ws_url=f"wss://fake/browser-streams/aws.browser.v1/sessions/{sid}/automation")
            self.sessions[sid] = {"info": info, "state": state, "ends_at": self.clock() + timeout_s,
                                  "stopped": False, "automation": True}
            self.calls.append(("start", user_id, profile_id))
            return info

    def wait_ready(self, session, timeout_s=60) -> None:
        return None

    def session_status(self, session) -> str:
        s = self.sessions.get(session.session_id)
        if not s or s["stopped"] or self.clock() >= s["ends_at"]:
            return "TERMINATED"
        return "READY"

    def stop_session(self, session) -> None:
        self.calls.append(("stop", session.session_id))
        self.sessions[session.session_id]["stopped"] = True

    def automation_connection(self, session) -> Tuple[str, Dict[str, str]]:
        return session.ws_url, {"Authorization": "fake-sigv4"}

    def live_view_url(self, session, expires_s=300) -> str:
        if expires_s > 300:
            raise ValueError("max 300")
        return f"https://fake/live-view/{session.session_id}?expires={expires_s}"

    def set_automation_enabled(self, session, enabled) -> None:
        self.calls.append(("automation", session.session_id, enabled))
        self.sessions[session.session_id]["automation"] = enabled

    # --- profiles ----------------------------------------------------------------------------------------
    def create_profile(self, name) -> str:
        with self._lock:
            if len(self.profiles) >= self.max_profiles:
                raise QuotaExceeded("maxBrowserProfiles limit exceeded for account (fake)")
            pid = f"{name}-{next(self._ids):010d}"
            self.profiles[pid] = None
            self.calls.append(("create_profile", pid))
            return pid

    def save_profile(self, session, profile_id) -> None:
        s = self.sessions[session.session_id]
        if s["stopped"] or self.clock() >= s["ends_at"]:
            raise ProviderError("session must be active to save a profile")
        if profile_id not in self.profiles:
            raise ProviderError(f"unknown profile {profile_id}")
        if self.profile_mode == "immutable" and self.profiles[profile_id] is not None:
            raise ProfileConflict("profile is immutable (fake)")
        self.profiles[profile_id] = copy.deepcopy(s["state"])
        self.calls.append(("save_profile", profile_id))

    def delete_profile(self, profile_id) -> None:
        self.profiles.pop(profile_id, None)
        self.calls.append(("delete_profile", profile_id))
