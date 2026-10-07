"""Integration test: the v2 agent connection against a LOCAL Chromium, through the fake signed gateway.

Run inside the v2-agent image, in the network namespace of a Chromium container (see run_local_integration.sh).
"""
import asyncio
import datetime
import os
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from unittest import mock

from botocore.credentials import Credentials

from cloud_browser_agent.agent.connection import StaticSource
from cloud_browser_agent.agent.gate import Gate, TOOK_CONTROL
from cloud_browser_agent.agent.mcp_session import ConnectError, McpBrowser
from cloud_browser_agent.provider.agentcore import sign_ws_headers
from cloud_browser_agent.tests.fake_gateway import FakeGateway

CREDS = Credentials("AKIATESTTESTTESTTEST", "test-secret-test-secret", "test-session-token")
PAGE = "<html><head><title>Login test page</title></head><body><h1 id=h>Welcome back</h1><button id=b>Continue</button></body></html>"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def serve_page():
    os.makedirs("/tmp/site", exist_ok=True)
    open("/tmp/site/index.html", "w").write(PAGE)
    h = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory="/tmp/site", **k)
    srv = HTTPServer(("127.0.0.1", 8099), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def text(result):
    return " ".join(getattr(c, "text", "") for c in getattr(result, "content", []))


def tool(mcp, name):
    return next(t for t in mcp.tools if t.name == name)


async def main():
    serve_page()
    gw = FakeGateway(CREDS)
    base = await gw.start(9300)
    session_id = "sess0001"
    ws_unsigned = f"{base}/browser-streams/aws.browser.v1/sessions/{session_id}/automation"

    def signed(sid=session_id, creds=CREDS):
        _, headers = sign_ws_headers(creds, "us-east-1", "aws.browser.v1", sid)
        return ws_unsigned.replace(session_id, sid), headers

    # 1. no credentials: the gateway must refuse, and we must say so clearly
    m = McpBrowser("alice", StaticSource(ws_unsigned, {}), Gate(), start_timeout=40)
    try:
        await m.start()
        check("1 connection without credentials is refused", False, "connected anyway")
    except ConnectError as e:
        check("1 connection without credentials is refused", bool(gw.rejected), f"gateway said: {gw.rejected[-1] if gw.rejected else '-'}")
    await m.stop()

    # 2. wrong secret: signature must not verify
    bad = Credentials("AKIATESTTESTTESTTEST", "WRONG-secret-WRONG-secret", "test-session-token")
    ws, h = signed(creds=bad)
    m = McpBrowser("alice", StaticSource(ws, h), Gate(), start_timeout=40)
    try:
        await m.start(); check("2 a bad signature is refused", False)
    except ConnectError:
        check("2 a bad signature is refused", "signature mismatch" in gw.rejected, f"reasons so far: {sorted(set(gw.rejected))}")
    await m.stop()

    # 2b. a perfectly valid signature that is more than five minutes old: this is why headers must be fetched fresh
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=6)
    with mock.patch("botocore.auth.get_current_datetime", return_value=old):
        ws, h = signed()
    m = McpBrowser("alice", StaticSource(ws, h), Gate(), start_timeout=40)
    try:
        await m.start(); check("2b a stale (6 min old) signature is refused", False)
    except ConnectError:
        check("2b a stale (6 min old) signature is refused", "request expired" in gw.rejected, f"reasons: {sorted(set(gw.rejected))}")
    await m.stop()

    # 3. correct signed headers go through Playwright MCP intact and the browser is usable
    gate = Gate()
    ws, h = signed()
    m = McpBrowser("alice", StaticSource(ws, h), gate, start_timeout=60)
    await m.start()
    check("3a signed connection accepted by the gateway", len(gw.accepted) >= 1, f"accepted: {gw.accepted[-1:]}")
    names = {t.name for t in m.tools}
    check("3b agent tools loaded, dangerous ones removed", "browser_navigate" in names and "browser_snapshot" in names
          and not names & {"browser_close", "browser_run_code_unsafe", "browser_take_screenshot"}, f"{len(names)} tools")
    nav = await tool(m, "browser_navigate").ainvoke({"url": "http://127.0.0.1:8099/"})
    snap = await tool(m, "browser_snapshot").ainvoke({})
    check("3c navigate + snapshot read the real page through the signed connection",
          "Welcome back" in str(snap) and "Login test page" in str(snap), "heading + title found in the accessibility snapshot")

    # 4. screenshot path used by look(): an image comes back
    shot = await m.call("browser_take_screenshot", {"type": "png"})
    img = next((c for c in shot.content if getattr(c, "type", "") == "image"), None)
    check("4 screenshot for look() returns a PNG", img is not None and len(img.data) > 1000, f"{len(img.data) if img else 0} base64 chars")

    # 5. takeover: while paused a tool call waits, then is NOT executed and the model is told to look again
    gate.pause()
    t0 = time.time()
    task = asyncio.create_task(tool(m, "browser_snapshot").ainvoke({}))
    await asyncio.sleep(1.2)
    waiting = not task.done()
    gate.resume()
    out = await asyncio.wait_for(task, 15)
    check("5 paused tool call waits, then reports 'took control' instead of acting", waiting and "NOT executed" in str(out),
          f"waited {time.time() - t0:.1f}s")

    # 6. the browser session changes (new address + fresh signature): restart with a new connection
    before = len(gw.accepted)
    ws2, h2 = signed("sess0002")
    m.source = StaticSource(ws2, h2)
    await m.restart()
    snap2 = await tool(m, "browser_snapshot").ainvoke({})
    check("6 restart picks up a fresh signed connection for a new session", len(gw.accepted) > before and "sess0002" in gw.accepted[-1]
          and "Welcome back" in str(snap2), f"accepted now: {gw.accepted[-1]}")

    # 7. stop leaves nothing behind
    await m.stop()
    check("7 stop closes the connection", not m.alive)
    leftovers = [p for p in os.listdir("/tmp") if p.startswith("mcp-")]
    check("7b no signed-header config files left on disk", leftovers == [], f"found: {leftovers}")

    await gw.stop()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
