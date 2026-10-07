"""Live view for backends that give us a raw DevTools endpoint (PROVIDER=docker).

The browser page in the user's tab opens a WebSocket here; we open one to one page of the remote Chromium, start a CDP
screencast, and relay: JPEG frames go to the viewer, mouse / key / text events go back as Input.* commands.
(Browserbase and AgentCore stream the video themselves, so they do not use this.)
"""
from __future__ import annotations

import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect


async def run(client: WebSocket, page_ws_url: str) -> None:
    async with connect(page_ws_url, max_size=None, open_timeout=10) as cdp:
        n = 0

        async def send(method, params=None):
            nonlocal n
            n += 1
            await cdp.send(json.dumps({"id": n, "method": method, "params": params or {}}))

        await send("Page.enable")
        await send("Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1})

        async def from_chromium():
            async for raw in cdp:
                m = json.loads(raw)
                if m.get("method") == "Page.screencastFrame":
                    p = m["params"]
                    await send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
                    await client.send_text(json.dumps({"t": "frame", "data": p["data"],
                                                       "w": p["metadata"]["deviceWidth"], "h": p["metadata"]["deviceHeight"]}))

        async def from_client():
            while True:
                m = json.loads(await client.receive_text())
                if m["t"] == "mouse":
                    await send("Input.dispatchMouseEvent", {k: v for k, v in m.items() if k != "t"})
                elif m["t"] == "key":
                    await send("Input.dispatchKeyEvent", {k: v for k, v in m.items() if k != "t"})
                elif m["t"] == "text":
                    await send("Input.insertText", {"text": m["text"]})

        tasks = [asyncio.create_task(from_chromium()), asyncio.create_task(from_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
