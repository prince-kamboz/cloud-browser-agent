import datetime
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from agentcore.provider import agentcore as ac
from agentcore.provider.base import ProfileConflict, ProviderError, QuotaExceeded, SessionInfo

CREDS = Credentials("AKIAEXAMPLEEXAMPLE", "secret-secret-secret", "session-token")
FIXED = datetime.datetime(2026, 10, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)


def client_error(code, msg):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "Op")


class Recorder:
    """Stands in for a boto3 client: records the call and returns/raises what the test says."""
    def __init__(self, **responses): self.calls, self.responses = [], responses
    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            r = self.responses.get(name, {})
            if isinstance(r, Exception):
                raise r
            return r
        return call


class SigningTests(unittest.TestCase):
    def sign_at(self, when, **kw):
        with mock.patch("botocore.auth.get_current_datetime", return_value=when):
            return ac.sign_ws_headers(CREDS, "us-east-1", "aws.browser.v1", "sess123", **kw)

    def test_ws_headers_are_sigv4_for_the_agentcore_service(self):
        url, h = self.sign_at(FIXED)
        self.assertEqual(url, "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/sess123/automation")
        self.assertTrue(h["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLEEXAMPLE/20261006/us-east-1/bedrock-agentcore/aws4_request"))
        self.assertEqual(h["X-Amz-Security-Token"], "session-token")
        self.assertEqual(h["X-Amz-Date"], "20261006T120000Z")
        self.assertEqual((h["Upgrade"], h["Sec-WebSocket-Version"]), ("websocket", "13"))

    def test_the_date_header_is_the_instant_that_was_actually_signed(self):
        """Regression: the header used to be computed before signing; botocore signs with its own clock, so the two
        disagreed whenever a second boundary fell between them (intermittent 403s from AWS)."""
        long_ago = datetime.datetime(2020, 1, 1, 0, 0, 7, tzinfo=datetime.timezone.utc)
        _, h = self.sign_at(long_ago)
        self.assertEqual(h["X-Amz-Date"], "20200101T000007Z")
        self.assertIn("/20200101/us-east-1/bedrock-agentcore/aws4_request", h["Authorization"])

    def test_ws_headers_are_fresh_each_time(self):
        _, a = ac.sign_ws_headers(CREDS, "us-east-1", "b", "s")
        _, b = ac.sign_ws_headers(CREDS, "us-east-1", "b", "s")
        self.assertNotEqual(a["Sec-WebSocket-Key"], b["Sec-WebSocket-Key"])

    def test_no_session_token_header_for_long_lived_keys(self):
        _, h = ac.sign_ws_headers(Credentials("AKIAX", "s"), "us-east-1", "b", "s")
        self.assertNotIn("X-Amz-Security-Token", h)

    def test_live_view_url_is_presigned_and_capped_at_300s(self):
        u = ac.presign_live_view(CREDS, "ap-south-1", "aws.browser.v1", "sess123", 300)
        parsed = urlparse(u); q = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "bedrock-agentcore.ap-south-1.amazonaws.com")
        self.assertTrue(parsed.path.endswith("/sessions/sess123/live-view"))
        self.assertEqual(q["X-Amz-Expires"], ["300"])
        self.assertIn("X-Amz-Signature", q)
        with self.assertRaises(ValueError):
            ac.presign_live_view(CREDS, "ap-south-1", "aws.browser.v1", "sess123", 301)


class RequestShapeTests(unittest.TestCase):
    def provider(self, dp=None, cp=None):
        class B:                                              # minimal boto session
            def get_credentials(self): return CREDS
        return ac.AgentCoreProvider("us-east-1", boto_session=B(), data_client=dp or Recorder(), control_client=cp or Recorder())

    def test_start_without_profile(self):
        dp = Recorder(start_browser_session={"sessionId": "s1", "streams": {"automationStream": {"streamEndpoint": "wss://x"}}})
        info = self.provider(dp).start_session("alice", None, 600, (1280, 800))
        name, kw = dp.calls[0]
        self.assertEqual(name, "start_browser_session")
        self.assertEqual(kw["browserIdentifier"], "aws.browser.v1")
        self.assertEqual(kw["sessionTimeoutSeconds"], 600)
        self.assertEqual(kw["viewPort"], {"width": 1280, "height": 800})
        self.assertNotIn("profileConfiguration", kw)
        self.assertGreaterEqual(len(kw["clientToken"]), 33)   # the API requires 33..256 characters
        self.assertEqual((info.session_id, info.ws_url), ("s1", "wss://x"))

    def test_start_with_profile(self):
        dp = Recorder(start_browser_session={"sessionId": "s1"})
        self.provider(dp).start_session("alice", "prof_a-AbCdEfGhIj")
        self.assertEqual(dp.calls[0][1]["profileConfiguration"], {"profileIdentifier": "prof_a-AbCdEfGhIj"})

    def test_session_name_is_sanitised(self):
        dp = Recorder(start_browser_session={"sessionId": "s1"})
        self.provider(dp).start_session("al ice@x.com")
        self.assertRegex(dp.calls[0][1]["name"], r"^[a-zA-Z0-9_]{1,100}$")

    def test_profile_names_follow_the_console_rules(self):
        self.assertEqual(ac.clean_name("alice-1"), "alice_1")
        self.assertEqual(ac.clean_name("1abc"), "u1abc")
        self.assertEqual(len(ac.clean_name("x" * 90)), 48)

    def test_save_and_stream_toggle_shapes(self):
        dp = Recorder()
        p = self.provider(dp); s = SessionInfo("s1", "aws.browser.v1", 0, (1, 1))
        p.save_profile(s, "pid-1234567890")
        p.set_automation_enabled(s, False)
        self.assertEqual(dp.calls[0][0], "save_browser_session_profile")
        self.assertEqual(dp.calls[0][1]["profileIdentifier"], "pid-1234567890")
        self.assertEqual(dp.calls[1][1]["streamUpdate"], {"automationStreamUpdate": {"streamStatus": "DISABLED"}})


class ErrorMappingTests(unittest.TestCase):
    def provider(self, dp=None, cp=None):
        return ac.AgentCoreProvider("us-east-1", boto_session=type("B", (), {"get_credentials": lambda s: CREDS})(),
                                    data_client=dp or Recorder(), control_client=cp or Recorder())

    def test_the_error_this_account_returns_becomes_quota_exceeded(self):
        cp = Recorder(create_browser_profile=client_error("ValidationException", "maxBrowserProfiles limit exceeded for account 123"))
        with self.assertRaises(QuotaExceeded):
            self.provider(cp=cp).create_profile("alice")

    def test_service_quota_exception_code_is_quota_exceeded(self):
        dp = Recorder(start_browser_session=client_error("ServiceQuotaExceededException", "too many"))
        with self.assertRaises(QuotaExceeded):
            self.provider(dp).start_session("alice")

    def test_conflict_is_profile_conflict(self):
        dp = Recorder(save_browser_session_profile=client_error("ConflictException", "immutable"))
        with self.assertRaises(ProfileConflict):
            self.provider(dp).save_profile(SessionInfo("s", "b", 0, (1, 1)), "p-1234567890")

    def test_anything_else_is_a_provider_error_with_the_code(self):
        dp = Recorder(stop_browser_session=client_error("AccessDeniedException", "nope"))
        with self.assertRaises(ProviderError) as cm:
            self.provider(dp).stop_session(SessionInfo("s", "b", 0, (1, 1)))
        self.assertIn("AccessDeniedException", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
