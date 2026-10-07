"""One Playwright MCP process per user, connected to that user's remote browser.

The process is owned by a single long-lived task (the MCP client opens and closes its pipes in the task that created it).
Playwright connects to the browser lazily, on the first tool call, so start() makes a cheap call itself:
a bad address or rejected signature fails *here*, with a clear error, not in the middle of an agent run.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from cloud_browser_agent.agent.connection import (ConnectionSource, mcp_config, remove_private_config, write_private_config)
from cloud_browser_agent.agent.gate import Gate

# Tools the agent must not have: closing the page or resizing breaks the user's live view, the rest escape the sandbox.
BLOCKED_TOOLS = {
    "browser_close", "browser_resize", "browser_run_code_unsafe", "browser_file_upload", "browser_drop",
    "browser_emulate_media",
    "browser_take_screenshot",        # screenshots only reach the model through look(), so images never pile up in context
}


class ConnectError(Exception):
    pass


def describe(e: BaseException) -> str:
    """anyio wraps real failures in ExceptionGroups ("unhandled errors in a TaskGroup"): show the leaves instead."""
    leaves, stack = [], [e]
    while stack:
        x = stack.pop()
        sub = getattr(x, "exceptions", None)
        if sub:
            stack.extend(sub)
        else:
            leaves.append(f"{type(x).__name__}: {x}")
    return "; ".join(leaves)[:400]


class McpBrowser:
    def __init__(self, user: str, source: ConnectionSource, gate: Gate, command: str = "playwright-mcp",
                 cwd: str = "/tmp", start_timeout: float = 90, extra_args: Optional[List[str]] = None):
        self.user, self.source, self.gate = user, source, gate
        self.command, self.cwd, self.start_timeout = command, cwd, start_timeout
        self.extra_args = extra_args if extra_args is not None else ["--caps", "vision"]
        self.tools: list = []
        self.session = None
        self.ws_url: Optional[str] = None
        self.error: Optional[str] = None
        self._owner: Optional[asyncio.Task] = None
        self._ready = self._stop = None

    @property
    def alive(self) -> bool:
        return self._owner is not None and not self._owner.done() and self.session is not None

    async def start(self, connection: Optional[Tuple[str, Dict[str, str]]] = None) -> None:
        if self.alive:
            return
        ws_url, headers = connection or await asyncio.to_thread(self.source.get, self.user)   # fresh: signatures expire in minutes
        self.ws_url, self.error = ws_url, None
        cfg_path = write_private_config(mcp_config(ws_url, headers))
        self._ready, self._stop = asyncio.Event(), asyncio.Event()
        self._owner = asyncio.create_task(self._own(cfg_path))
        try:
            await asyncio.wait_for(self._ready.wait(), self.start_timeout)
        except asyncio.TimeoutError:
            self.error = self.error or "timed out starting the browser connection"
        finally:
            remove_private_config(cfg_path)                    # MCP read it at startup; the signed headers must not linger on disk
        if self.error:
            await self.stop()
            raise ConnectError(self.error)

    async def _own(self, cfg_path: str) -> None:
        try:
            client = MultiServerMCPClient({"browser": {"transport": "stdio", "command": self.command, "cwd": self.cwd,
                                                       "args": ["--config", cfg_path, *self.extra_args]}})
            async with client.session("browser") as session:
                tools = await load_mcp_tools(session, tool_interceptors=[self.gate])
                check = await session.call_tool("browser_tabs", {"action": "list"})   # forces the CDP connection now
                if getattr(check, "isError", False):
                    raise ConnectError(" ".join(getattr(c, "text", "") for c in check.content)[:400] or "browser rejected the connection")
                self.session = session
                self.tools = [t for t in tools if t.name not in BLOCKED_TOOLS]
                self._ready.set()
                await self._stop.wait()
        except BaseException as e:                              # surface the reason to start() instead of dying silently
            self.error = self.error or describe(e)
            self._ready.set()
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self.session, self.tools = None, []

    async def stop(self) -> None:
        if self._owner:
            if self._stop:
                self._stop.set()
            try:
                await asyncio.wait_for(self._owner, 10)
            except BaseException:
                self._owner.cancel()
            self._owner = None
        self.session, self.tools = None, []

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def call(self, name: str, args: dict):
        """Direct tool call that bypasses the agent's gate (used by look() for screenshots)."""
        if not self.alive:
            raise ConnectError("browser connection is not running")
        return await self.session.call_tool(name, args)
