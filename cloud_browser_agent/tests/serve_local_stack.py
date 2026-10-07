"""Local stand-ins so the REAL agent service can run end to end without AWS:
   test page (8099)  +  fake signed gateway in front of Chromium (9300)  +  stub control plane (8100) that signs on demand.
"""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler

from botocore.credentials import Credentials

from cloud_browser_agent.provider.agentcore import sign_ws_headers
from cloud_browser_agent.tests.fake_gateway import FakeGateway

CREDS = Credentials("AKIATESTTESTTESTTEST", "test-secret-test-secret", "test-session-token")
TOKEN = os.environ.get("INTERNAL_TOKEN", "e2e-token")
SESSION = {"id": "sess0001"}                       # the "current" remote browser session for user alice


def page_server():
    os.makedirs("/tmp/site", exist_ok=True)
    open("/tmp/site/index.html", "w").write(
        "<html><head><title>Account page</title></head><body><h1>Welcome back, Alice</h1>"
        "<p>Your plan: Pro</p><button id=b>Continue to dashboard</button>"
        "<div style=\"width:140px;height:140px;background:#2a9d4a;margin-top:24px\"></div></body></html>")
    HTTPServer(("127.0.0.1", 8099), lambda *a, **k: SimpleHTTPRequestHandler(*a, directory="/tmp/site", **k)).serve_forever()


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.headers.get("x-internal-token") != TOKEN:
            self.send_response(403); self.end_headers(); return
        if self.path != "/api/sessions/alice/connection":
            self.send_response(404); self.end_headers(); return
        _, headers = sign_ws_headers(CREDS, "us-east-1", "aws.browser.v1", SESSION["id"])      # fresh signature every call
        body = json.dumps({"ws_url": f"ws://127.0.0.1:9300/browser-streams/aws.browser.v1/sessions/{SESSION['id']}/automation",
                           "headers": headers}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(body)


async def main():
    threading.Thread(target=page_server, daemon=True).start()
    threading.Thread(target=HTTPServer(("127.0.0.1", 8100), Stub).serve_forever, daemon=True).start()
    gw = FakeGateway(CREDS)
    await gw.start(9300)
    print("local stack up", flush=True)
    while True:
        await asyncio.sleep(3600)

asyncio.run(main())
