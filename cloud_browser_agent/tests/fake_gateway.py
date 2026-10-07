"""A stand-in for AgentCore's automation endpoint, in front of a local Chromium.

Like AWS it refuses the WebSocket handshake unless the request carries a valid SigV4 signature for the expected host
and path, then relays CDP frames to Chromium. It RECOMPUTES the signature from the test credentials, so any header the
client corrupts, trims or drops on the way (Playwright MCP, config parsing, the ws library) is caught.
"""
import asyncio
import datetime
import hashlib
import hmac
import json
import re
import urllib.request

import websockets
from botocore.credentials import Credentials
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.http11 import Response

SIGNED_HOST = "bedrock-agentcore.us-east-1.amazonaws.com"      # what the signature covers (the client connects to localhost)
PATH_RE = re.compile(r"^/browser-streams/aws\.browser\.v1/sessions/([0-9a-zA-Z]+)/automation$")


class FakeGateway:
    def __init__(self, creds: Credentials, region="us-east-1", chromium_http="http://127.0.0.1:9222", clock=None):
        self.creds, self.region, self.chromium_http = creds, region, chromium_http
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.accepted, self.rejected = [], []                   # for assertions
        self.server = None

    def _valid(self, path, headers) -> str:
        """Return '' if the signature is good, else why not. An independent SigV4 implementation (not botocore), checked
        against the date the CLIENT SENT, with AWS's five-minute clock-skew limit."""
        if not PATH_RE.match(path):
            return "bad path"
        auth, date, token = headers.get("Authorization"), headers.get("X-Amz-Date"), headers.get("X-Amz-Security-Token")
        if not auth or not date:
            return "missing Authorization or X-Amz-Date"
        if token != self.creds.token:
            return "wrong or missing X-Amz-Security-Token"
        m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, SignedHeaders=([^,]+), Signature=([0-9a-f]{64})$", auth)
        if not m:
            return "malformed Authorization"
        akid, day, region, service, signed, sig = m.groups()
        if akid != self.creds.access_key or service != "bedrock-agentcore" or region != self.region or day != date[:8]:
            return "credential scope mismatch"
        sent = datetime.datetime.strptime(date, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        if abs((self.clock() - sent).total_seconds()) > 300:
            return "request expired"
        values = {"host": SIGNED_HOST, "x-amz-date": date, "x-amz-security-token": token}
        names = signed.split(";")
        if not set(names) <= set(values):
            return f"unexpected signed headers {names}"
        canonical = "\n".join(["GET", path, "", "".join(f"{n}:{values[n]}\n" for n in names), signed, hashlib.sha256(b"").hexdigest()])
        scope = f"{day}/{region}/{service}/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        key = ("AWS4" + self.creds.secret_key).encode()
        for part in (day, region, service, "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        want = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        return "" if hmac.compare_digest(want, sig) else "signature mismatch"

    def _process_request(self, connection, request):
        why = self._valid(request.path, request.headers)
        if why:
            self.rejected.append(why)
            return Response(403, "Forbidden", websockets.Headers(), why.encode())
        self.accepted.append(request.path)
        return None

    async def _handler(self, client):
        info = json.load(urllib.request.urlopen(f"{self.chromium_http}/json/version", timeout=5))
        async with connect(info["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as chromium:
            async def pump(src, dst):
                async for msg in src:
                    await dst.send(msg)
            await asyncio.wait([asyncio.create_task(pump(client, chromium)), asyncio.create_task(pump(chromium, client))],
                               return_when=asyncio.FIRST_COMPLETED)

    async def start(self, port=9300):
        self.server = await serve(self._handler, "127.0.0.1", port, process_request=self._process_request, max_size=None)
        return f"ws://127.0.0.1:{port}"

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
