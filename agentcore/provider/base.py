"""The one interface the control plane needs from a remote-browser backend.

AgentCoreProvider is the real one; FakeProvider simulates it so the lifecycle logic can be tested with no AWS access.
(v1's Docker Chromium is deliberately NOT a provider here: this branch is AgentCore only.)
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


class ProviderError(Exception):
    """Anything the backend refused or failed at."""


class QuotaExceeded(ProviderError):
    """Account limit hit, e.g. "maxBrowserProfiles limit exceeded" on a new AWS account."""


class ProfileConflict(ProviderError):
    """The backend refused to overwrite an existing profile (profiles may be write-once)."""


@dataclass
class SessionInfo:
    session_id: str
    browser_id: str
    created_at: float
    viewport: Tuple[int, int]
    profile_id: Optional[str] = None
    ws_url: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


class BrowserProvider(abc.ABC):
    # True when a profile must be attached at session start and is written when the session closes (Browserbase contexts).
    # False when a profile is created and saved from a running session (AgentCore).
    profile_at_start = False

    # --- sessions
    @abc.abstractmethod
    def start_session(self, user_id: str, profile_id: Optional[str] = None, timeout_s: int = 3600,
                      viewport: Tuple[int, int] = (1280, 800)) -> SessionInfo: ...

    @abc.abstractmethod
    def wait_ready(self, session: SessionInfo, timeout_s: float = 60) -> None: ...

    @abc.abstractmethod
    def session_status(self, session: SessionInfo) -> str:
        """'READY' while usable; anything else ('TERMINATED', ...) means it is gone."""

    @abc.abstractmethod
    def stop_session(self, session: SessionInfo) -> None: ...

    # --- connecting
    @abc.abstractmethod
    def automation_connection(self, session: SessionInfo) -> Tuple[str, Dict[str, str]]:
        """(wss URL, freshly signed headers) for the agent's CDP connection. Headers expire: sign right before connecting."""

    @abc.abstractmethod
    def live_view_url(self, session: SessionInfo, expires_s: int = 300) -> str:
        """Time-limited signed URL for the user's live view (max 300 s)."""

    @abc.abstractmethod
    def set_automation_enabled(self, session: SessionInfo, enabled: bool) -> None:
        """Switch the agent's automation stream off while the user types secrets, on again after."""

    # --- tabs (optional: a backend whose live view shows one page at a time lets the UI draw its own tab strip)
    def tabs(self, session: SessionInfo):
        """[{id, title, url, view_url}] or None when the backend's live view already has tabs."""
        return None

    def open_tab(self, session: SessionInfo, url: str = "about:blank") -> None:
        raise ProviderError("this backend does not support opening tabs from the control plane")

    def close_tab(self, session: SessionInfo, tab_id: str) -> None:
        raise ProviderError("this backend does not support closing tabs from the control plane")

    # --- profiles (persisted cookies + local storage)
    @abc.abstractmethod
    def create_profile(self, name: str) -> str: ...

    @abc.abstractmethod
    def save_profile(self, session: SessionInfo, profile_id: str) -> None: ...

    @abc.abstractmethod
    def delete_profile(self, profile_id: str) -> None: ...
