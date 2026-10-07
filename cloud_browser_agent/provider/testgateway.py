"""Test-only provider (PROVIDER=testgw): fake bookkeeping (sessions, profiles) but a REAL local Chromium behind a signed fake gateway,
and a real screencast viewer. Lets the whole v2 stack (UI, control plane, agent) run with real pixels and no AWS.

viewer_kind="iframe": the live view is a plain web page (our screencast viewer)
viewer_kind="dcv"   : the UI loads the DCV viewer page instead (use with the stub DCV script to exercise that code path)
"""
from __future__ import annotations

import os

from botocore.credentials import Credentials

from .agentcore import sign_ws_headers
from .fake import FakeProvider

DEV_CREDS = Credentials("AKIATESTTESTTESTTEST", "test-secret-test-secret", "test-session-token")


class TestGatewayProvider(FakeProvider):
    def __init__(self, clock, viewer_kind: str = "iframe", **kw):
        super().__init__(clock, **kw)
        self.viewer_kind = viewer_kind
        self.gateway = os.getenv("DEV_GATEWAY", "ws://127.0.0.1:9300")
        self.screen = os.getenv("DEV_SCREEN", "http://127.0.0.1:8300")

    def start_session(self, user_id, profile_id=None, timeout_s=3600, viewport=(1280, 800)):
        info = super().start_session(user_id, profile_id, timeout_s, viewport)
        info.ws_url = f"{self.gateway}/browser-streams/aws.browser.v1/sessions/{info.session_id}/automation"
        return info

    def automation_connection(self, session):
        _, headers = sign_ws_headers(DEV_CREDS, "us-east-1", session.browser_id, session.session_id)   # fresh signature each call
        return session.ws_url, headers

    def live_view_url(self, session, expires_s=300):
        if expires_s > 300:
            raise ValueError("max 300")
        # for viewer_kind="dcv" this URL is just a placeholder the stub DCV script accepts
        return f"{self.screen}/live.html?session={session.session_id}&expires={expires_s}"
