"""Dev-only live view: streams the local Chromium to a canvas (CDP screencast) and forwards mouse and keyboard back.
The local stand-in for Amazon DCV. Serves /live.html and the WebSocket at /ws on one port.
"""
import asyncio
import json
import os
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

CHROMIUM = os.getenv("CHROMIUM_HTTP", "http://127.0.0.1:9222")
PAGE = Path(__file__).resolve().parent.parent / "ui" / "viewer" / "dev.html"


def first_page_ws() -> str:
    tabs = json.load(urllib.request.urlopen(f"{CHROMIUM}/json/list", timeout=5))
    return next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")


def serve_page(connection, request):
    path = request.path.split("?")[0]
    if path in ("/", "/live.html"):
        return Response(200, "OK", Headers({"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}), PAGE.read_bytes())
    if path != "/ws":
        return Response(404, "Not Found", Headers(), b"not found")
    return None                                              # /ws: continue with the WebSocket handshake


async def handler(client):
    async with connect(first_page_ws(), max_size=None, open_timeout=10) as cdp:
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
                    await client.send(json.dumps({"t": "frame", "data": p["data"],
                                                  "w": p["metadata"]["deviceWidth"], "h": p["metadata"]["deviceHeight"]}))

        async def from_client():
            async for raw in client:
                m = json.loads(raw)
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


async def main(port=8300):
    async with serve(handler, "0.0.0.0", port, process_request=serve_page, max_size=None):
        print("dev screencast up on", port, flush=True)
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main(int(os.getenv("DEV_SCREEN_PORT", "8300"))))
