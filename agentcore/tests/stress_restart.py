"""Restart the MCP connection many times in a row and report every failure's real cause."""
import asyncio
import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

from botocore.credentials import Credentials

from agentcore.agent.connection import StaticSource
from agentcore.agent.gate import Gate
from agentcore.agent.mcp_session import ConnectError, McpBrowser
from agentcore.provider.agentcore import sign_ws_headers
from agentcore.tests.fake_gateway import FakeGateway

CREDS = Credentials("AKIATESTTESTTESTTEST", "test-secret-test-secret", "test-session-token")


async def main(n=12):
    os.makedirs("/tmp/site", exist_ok=True)
    open("/tmp/site/index.html", "w").write("<title>t</title><h1>Welcome back</h1>")
    srv = HTTPServer(("127.0.0.1", 8099), lambda *a, **k: SimpleHTTPRequestHandler(*a, directory="/tmp/site", **k))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    gw = FakeGateway(CREDS); base = await gw.start(9300)
    def conn(i):
        sid = f"sess{i:04d}"
        _, h = sign_ws_headers(CREDS, "us-east-1", "aws.browser.v1", sid)
        return f"{base}/browser-streams/aws.browser.v1/sessions/{sid}/automation", h
    m = McpBrowser("alice", StaticSource(*conn(0)), Gate(), start_timeout=60)
    fails = []
    await m.start()
    await next(t for t in m.tools if t.name == "browser_navigate").ainvoke({"url": "http://127.0.0.1:8099/"})
    for i in range(n):
        m.source = StaticSource(*conn(i))
        try:
            await m.stop(); await m.start()
            snap = await next(t for t in m.tools if t.name == "browser_snapshot").ainvoke({})
            assert "Welcome back" in str(snap)
            print(f"restart {i + 1}/{n} ok", flush=True)
        except Exception as e:
            fails.append((i + 1, type(e).__name__, str(e)[:300])); print(f"restart {i + 1}/{n} FAILED: {fails[-1]}", flush=True)
    await m.stop(); await gw.stop()
    print(f"\n{n - len(fails)}/{n} restarts ok")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
