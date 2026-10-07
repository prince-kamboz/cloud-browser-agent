"""Capture the README screenshots from the real, running UI (PROVIDER=docker, so no cloud account and no secrets are involved).

Drives a headless Chromium (the cba-chromium image) over CDP on the compose network: opens the UI, starts a two-tab task and
saves PNGs to docs/images/. Run it with dev/take_screenshots.sh; the agent uses OPENAI_API_KEY, so a task costs a few cents.
"""
import asyncio
import base64
import json
import os
import sys
import urllib.request

import websockets

UI = os.getenv("UI_URL", "http://cloud-browser-agent-control-1:8100")
OUT = os.getenv("OUT_DIR", "/w/docs/images")
SHOT = os.getenv("SHOT_BASE", "http://cba-shot:8080/cdp")
W, H = 1440, 900
TASK = ("Open example.com in one tab and news.ycombinator.com in a new tab. "
        "Tell me the title of example.com and the top Hacker News story, in a small table.")


class Cdp:
    def __init__(self, ws):
        self.ws, self.n, self._pending = ws, 0, {}
        asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self._pending:
                self._pending.pop(m["id"]).set_result(m)

    async def call(self, method, params=None, sid=None):
        self.n += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[self.n] = fut
        msg = {"id": self.n, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        r = await asyncio.wait_for(fut, 60)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message')}")
        return r.get("result", {})


async def main():
    ver = json.load(urllib.request.urlopen(f"{SHOT}/json/version", timeout=10))
    path = "/" + ver["webSocketDebuggerUrl"].split("/", 3)[3]
    cdp = Cdp(await websockets.connect(f"ws://{SHOT.split('//')[1].split('/')[0]}/cdp{path}", max_size=None))
    tid = (await cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
    sid = (await cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
    await cdp.call("Page.enable", sid=sid)
    await cdp.call("Emulation.setDeviceMetricsOverride", {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False}, sid=sid)

    async def js(expr):
        r = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, sid=sid)
        return r.get("result", {}).get("value")

    async def shot(name):
        r = await cdp.call("Page.captureScreenshot", {"format": "png"}, sid=sid)
        open(f"{OUT}/{name}.png", "wb").write(base64.b64decode(r["data"]))
        print("saved", name, flush=True)

    async def wait_for(expr, timeout=120, step=1.0):
        for _ in range(int(timeout / step)):
            if await js(expr):
                return True
            await asyncio.sleep(step)
        sys.exit(f"timed out waiting for: {expr}")          # a silent timeout once produced five screenshots of a broken page

    await cdp.call("Page.navigate", {"url": UI}, sid=sid)
    await wait_for("document.readyState === 'complete' && !!document.getElementById('openBtn')")
    await asyncio.sleep(1)
    await shot("01-start")

    await js("document.getElementById('user').value = 'demo'")
    await js("document.getElementById('openBtn').click()")
    await wait_for("document.getElementById('statusText').textContent === 'Browser ready'", 60)
    await asyncio.sleep(4)
    await js(f"(() => {{ const i = document.getElementById('input'); i.value = {json.dumps(TASK)}; document.getElementById('composer').requestSubmit(); }})()")
    await wait_for("document.getElementById('statusText').textContent.startsWith('Agent is working')", 30)
    await js("document.getElementById('cardHit').click()")          # open the large panel
    await wait_for("document.querySelectorAll('.tab:not(.add)').length >= 2", 90, 1.5)
    await asyncio.sleep(3)
    await shot("02-agent-working")

    await js("document.getElementById('takeoverBtn').click()")      # take over: the agent pauses
    await wait_for("document.getElementById('statusText').textContent.includes('in control')", 15)
    await asyncio.sleep(1.5)
    await shot("03-take-over")

    await js("document.getElementById('takeoverBtn').click()")      # hand back and let it finish
    await wait_for("document.getElementById('statusText').textContent === 'Browser ready'", 150, 2)
    await asyncio.sleep(2)
    await shot("04-panel-done")
    await js("document.getElementById('collapseBtn').click()")
    await asyncio.sleep(2)
    await shot("05-card-answer")

    await js("document.getElementById('closeBtn').click()")         # release the browser
    await wait_for("document.getElementById('statusText').textContent === 'No browser open'", 60)
    print("done")

asyncio.run(main())
