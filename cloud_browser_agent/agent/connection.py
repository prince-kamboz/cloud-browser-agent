"""How the agent learns where the remote browser is and how to authenticate to it.

AgentCore gives a WebSocket address plus SigV4 headers that are only valid for a few minutes, so:
  * a connection is fetched right before the MCP process starts, never cached
  * on any connect failure the MCP process is restarted with a fresh connection (see mcp_session.py)
  * the headers are credentials: they go into a 0600 temp file for Playwright MCP, never onto a command line
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from typing import Dict, Protocol, Tuple

# Set by the WebSocket client itself; passing them through would only risk a mismatch.
HANDSHAKE_HEADERS = {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version",
                     "sec-websocket-extensions", "sec-websocket-protocol", "content-length"}


class ConnectionSource(Protocol):
    def get(self, user: str) -> Tuple[str, Dict[str, str]]:
        """(ws_url, headers) valid right now."""


class StaticSource:
    """Fixed address, for tests and for a local Chromium."""
    def __init__(self, ws_url: str, headers: Dict[str, str] | None = None):
        self.ws_url, self.headers = ws_url, dict(headers or {})

    def get(self, user: str):
        return self.ws_url, dict(self.headers)


class ProviderSource:
    """Ask the provider to sign a fresh connection (the agent and the provider live in one process)."""
    def __init__(self, provider, sessions):
        self.provider, self.sessions = provider, sessions     # sessions: user -> SessionInfo

    def get(self, user: str):
        return self.provider.automation_connection(self.sessions[user])


class ControlPlaneSource:
    """Ask the control plane (GET /api/sessions/{user}/connection, internal token required)."""
    def __init__(self, base_url: str, token: str, timeout: float = 10):
        self.base, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def get(self, user: str):
        req = urllib.request.Request(f"{self.base}/api/sessions/{user}/connection", headers={"x-internal-token": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.load(r)
        return d["ws_url"], d["headers"]


def mcp_config(ws_url: str, headers: Dict[str, str], connect_timeout_ms: int = 30000) -> dict:
    """Playwright MCP config that connects to a remote CDP WebSocket with extra headers."""
    return {"browser": {
        "cdpEndpoint": ws_url,
        "cdpHeaders": {k: v for k, v in headers.items() if k.lower() not in HANDSHAKE_HEADERS},
        "cdpTimeout": connect_timeout_ms,
    }}


def write_private_config(cfg: dict) -> str:
    """Write the config where only this user can read it. The caller deletes it once MCP has started."""
    d = tempfile.mkdtemp(prefix="mcp-")                       # mkdtemp is 0700
    path = os.path.join(d, "config.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


def remove_private_config(path: str) -> None:
    try:
        os.remove(path)
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass
