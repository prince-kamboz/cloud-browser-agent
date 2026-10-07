"""Dev-only helpers that run next to the local Chromium: a small test website, the signed fake gateway, the live viewer."""
import asyncio
import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

from agentcore.dev import screencast_server
from agentcore.provider.local import DEV_CREDS
from agentcore.tests.fake_gateway import FakeGateway

SITE = {
    "index.html": """<html><head><title>Account page</title></head><body style="font-family:system-ui;padding:40px">
<h1>Welcome back, Alice</h1><p>Your plan: Pro</p>
<p><a href="/login.html">Sign in to another account</a></p>
<button>Continue to dashboard</button>
<div style="width:140px;height:140px;background:#2a9d4a;margin-top:24px"></div></body></html>""",
    "login.html": """<html><head><title>Sign in</title></head><body style="font-family:system-ui;padding:40px">
<h1>Sign in</h1>
<p><input id=u placeholder="username" style="font-size:18px;padding:8px"></p>
<p><input id=p type=password placeholder="password" style="font-size:18px;padding:8px"></p>
<p id=out style="font-size:20px;color:#2a9d4a"></p>
<script>const out=document.getElementById('out');
for (const id of ['u','p']) document.getElementById(id).addEventListener('input', () =>
  out.textContent = 'username has ' + document.getElementById('u').value.length + ' chars, password has ' + document.getElementById('p').value.length + ' chars');
</script></body></html>""",
}


def site():
    os.makedirs("/tmp/site", exist_ok=True)
    for name, html in SITE.items():
        open(f"/tmp/site/{name}", "w").write(html)
    HTTPServer(("127.0.0.1", 8099), lambda *a, **k: SimpleHTTPRequestHandler(*a, directory="/tmp/site", **k)).serve_forever()


async def main():
    threading.Thread(target=site, daemon=True).start()
    await FakeGateway(DEV_CREDS).start(9300)
    print("dev services up", flush=True)
    await screencast_server.main(8300)

if __name__ == "__main__":
    asyncio.run(main())
