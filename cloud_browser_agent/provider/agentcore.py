"""Amazon Bedrock AgentCore Browser, through boto3.

Built from the AWS docs and the bedrock-agentcore SDK source (BrowserClient.generate_ws_headers /
generate_live_view_url), not yet run against a live account: see cloud_browser_agent/bench/step0_bench.py.
"""
from __future__ import annotations

import base64
import re
import secrets
import time
import uuid
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from botocore.auth import SigV4Auth, SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from .base import (BrowserProvider, ProfileConflict, ProviderError, QuotaExceeded, SessionInfo)

SERVICE = "bedrock-agentcore"
DEFAULT_BROWSER = "aws.browser.v1"      # AWS's built-in browser: nothing to create
MAX_LIVE_VIEW_EXPIRES = 300


def data_plane_host(region: str) -> str:
    return f"bedrock-agentcore.{region}.amazonaws.com"


def sign_ws_headers(credentials, region: str, browser_id: str, session_id: str) -> Tuple[str, Dict[str, str]]:
    """SigV4-sign the automation WebSocket handshake (same shape the AWS SDK produces).

    botocore stamps its OWN timestamp when it signs and overwrites X-Amz-Date with it, so the header we send must be
    read back from the signed request. Sending a date computed beforehand breaks the signature whenever a second
    boundary falls in between (an intermittent 403 that is very hard to reproduce).
    """
    host = data_plane_host(region)
    path = f"/browser-streams/{browser_id}/sessions/{session_id}/automation"
    req = AWSRequest(method="GET", url=f"https://{host}{path}", headers={"host": host})
    SigV4Auth(credentials, SERVICE, region).add_auth(req)
    headers = {
        "Host": host,
        "X-Amz-Date": req.headers["X-Amz-Date"],
        "Authorization": req.headers["Authorization"],
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
        "User-Agent": f"BrowserAgentV2/1.0 (Session: {session_id})",
    }
    if getattr(credentials, "token", None):
        headers["X-Amz-Security-Token"] = credentials.token
    return f"wss://{host}{path}", headers


def presign_live_view(credentials, region: str, browser_id: str, session_id: str, expires_s: int = 300) -> str:
    if expires_s > MAX_LIVE_VIEW_EXPIRES:
        raise ValueError(f"live view URLs cannot be valid for more than {MAX_LIVE_VIEW_EXPIRES} s")
    url = urlparse(f"https://{data_plane_host(region)}/browser-streams/{browser_id}/sessions/{session_id}/live-view")
    req = AWSRequest(method="GET", url=url.geturl(), headers={"host": url.hostname})
    SigV4QueryAuth(credentials=credentials, service_name=SERVICE, region_name=region, expires=expires_s).add_auth(req)
    return req.url


def clean_name(raw: str, limit: int = 48) -> str:
    """Profile names: letters, digits, underscore; must start with a letter; max 48."""
    n = re.sub(r"[^a-zA-Z0-9_]", "_", raw)
    if not n or not n[0].isalpha():
        n = "u" + n
    return n[:limit]


def _token() -> str:
    return str(uuid.uuid4()) + "-" + uuid.uuid4().hex[:8]   # clientToken must be 33..256 chars


def _map_error(e: ClientError) -> ProviderError:
    code = e.response.get("Error", {}).get("Code", "")
    msg = e.response.get("Error", {}).get("Message", str(e))
    if code in ("ServiceQuotaExceededException", "LimitExceededException") or "limit exceeded" in msg.lower():
        return QuotaExceeded(msg)
    if code == "ConflictException":
        return ProfileConflict(msg)
    return ProviderError(f"{code}: {msg}")


class AgentCoreProvider(BrowserProvider):
    viewer_kind = "dcv"          # the live view is an Amazon DCV stream, shown by viewer/dcv.html

    def __init__(self, region: str = "us-east-1", browser_id: str = DEFAULT_BROWSER, boto_session=None,
                 data_client=None, control_client=None):
        import boto3
        self.region, self.browser_id = region, browser_id
        self._boto = boto_session or boto3.Session()
        self.dp = data_client or self._boto.client("bedrock-agentcore", region_name=region)
        self.cp = control_client or self._boto.client("bedrock-agentcore-control", region_name=region)

    def _creds(self):
        c = self._boto.get_credentials()
        if c is None:
            raise ProviderError("no AWS credentials available")
        return c.get_frozen_credentials()

    def start_session(self, user_id, profile_id=None, timeout_s=3600, viewport=(1280, 800)) -> SessionInfo:
        req = {
            "browserIdentifier": self.browser_id,
            "name": clean_name(f"u_{user_id}", 100),
            "sessionTimeoutSeconds": int(timeout_s),
            "viewPort": {"width": viewport[0], "height": viewport[1]},
            "clientToken": _token(),
        }
        if profile_id:
            req["profileConfiguration"] = {"profileIdentifier": profile_id}
        try:
            r = self.dp.start_browser_session(**req)
        except ClientError as e:
            raise _map_error(e) from e
        stream = (r.get("streams") or {}).get("automationStream") or {}
        return SessionInfo(session_id=r["sessionId"], browser_id=self.browser_id, created_at=time.time(),
                           viewport=viewport, profile_id=profile_id,
                           ws_url=stream.get("streamEndpoint") or
                           f"wss://{data_plane_host(self.region)}/browser-streams/{self.browser_id}/sessions/{r['sessionId']}/automation")

    def session_status(self, session) -> str:
        try:
            return self.dp.get_browser_session(browserIdentifier=session.browser_id, sessionId=session.session_id)["status"]
        except ClientError as e:
            raise _map_error(e) from e

    def wait_ready(self, session, timeout_s=60) -> None:
        end, delay = time.time() + timeout_s, 0.2
        while time.time() < end:
            st = self.session_status(session)
            if st == "READY":
                return
            if st in ("TERMINATED", "FAILED"):
                raise ProviderError(f"session ended while starting ({st})")
            time.sleep(delay)
            delay = min(delay * 1.5, 1.5)
        raise ProviderError("session did not become READY in time")

    def stop_session(self, session) -> None:
        try:
            self.dp.stop_browser_session(browserIdentifier=session.browser_id, sessionId=session.session_id)
        except ClientError as e:
            raise _map_error(e) from e

    def automation_connection(self, session):
        return sign_ws_headers(self._creds(), self.region, session.browser_id, session.session_id)

    def live_view_url(self, session, expires_s=300) -> str:
        return presign_live_view(self._creds(), self.region, session.browser_id, session.session_id, expires_s)

    def set_automation_enabled(self, session, enabled) -> None:
        try:
            self.dp.update_browser_stream(
                browserIdentifier=session.browser_id, sessionId=session.session_id,
                streamUpdate={"automationStreamUpdate": {"streamStatus": "ENABLED" if enabled else "DISABLED"}})
        except ClientError as e:
            raise _map_error(e) from e

    def create_profile(self, name) -> str:
        try:
            r = self.cp.create_browser_profile(name=clean_name(name), clientToken=_token())
        except ClientError as e:
            raise _map_error(e) from e
        return r["profileId"]

    def save_profile(self, session, profile_id) -> None:
        try:
            self.dp.save_browser_session_profile(profileIdentifier=profile_id, browserIdentifier=session.browser_id,
                                                 sessionId=session.session_id, clientToken=_token())
        except ClientError as e:
            raise _map_error(e) from e

    def delete_profile(self, profile_id) -> None:
        try:
            self.cp.delete_browser_profile(profileId=profile_id)
        except ClientError as e:
            raise _map_error(e) from e
