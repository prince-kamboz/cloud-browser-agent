"""Pause point in front of every browser tool call: this is what makes 'Take over' work.

While `paused` the agent's tool calls wait. If the user had control, the waiting call is NOT executed
(the page changed under it) and the model is told to look again.
"""
from __future__ import annotations

import asyncio

from mcp.types import CallToolResult, TextContent

TOOK_CONTROL = ("The user took control of the browser and has now handed it back, so this action was NOT executed. "
                "Tabs and the page may have changed. Call browser_tabs to list the tabs and select the right one, "
                "then browser_snapshot, then continue.")


class Gate:
    def __init__(self, poll_s: float = 0.2):
        self.paused = False
        self.poll_s = poll_s

    def pause(self): self.paused = True
    def resume(self): self.paused = False

    async def __call__(self, request, handler):
        waited = False
        while self.paused:
            waited = True
            await asyncio.sleep(self.poll_s)
        if waited:
            return CallToolResult(content=[TextContent(type="text", text=TOOK_CONTROL)])
        return await handler(request)
