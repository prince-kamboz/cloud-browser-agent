import json
import unittest

from cloud_browser_agent.control.core import SessionService
from cloud_browser_agent.control.store import SqliteStore
from cloud_browser_agent.provider.base import ProviderError, QuotaExceeded
from cloud_browser_agent.provider.browserbase import BrowserbaseProvider


class FakeApi:
    """Just enough of api.browserbase.com to check what we send and how we read the answers."""
    def __init__(self):
        self.calls, self.sessions, self.contexts, self.fail = [], {}, set(), None

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/v1", 1)[1]
        data = json.loads(body) if body else None
        self.calls.append((method, path, data, headers))
        if self.fail:
            return self.fail
        if (method, path) == ("POST", "/contexts"):
            cid = f"ctx{len(self.contexts) + 1}"; self.contexts.add(cid)
            return 200, json.dumps({"id": cid, "uploadUrl": "x"}).encode()
        if (method, path) == ("POST", "/sessions"):
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = {"id": sid, "status": "RUNNING", "connectUrl": f"wss://connect.example/?sessionId={sid}&signingKey=k"}
            return 200, json.dumps(self.sessions[sid]).encode()
        if method == "GET" and path.startswith("/sessions/") and path.endswith("/debug"):
            return 200, json.dumps({"debuggerFullscreenUrl": "https://live.example/view"}).encode()
        if method == "GET" and path.startswith("/sessions/"):
            s = self.sessions.get(path.split("/")[2])
            return (200, json.dumps(s).encode()) if s else (404, b"{}")
        if method == "POST" and path.startswith("/sessions/"):
            s = self.sessions.get(path.split("/")[2])
            if not s:
                return 404, b"{}"
            s["status"] = "COMPLETED"
            return 200, b"{}"
        if method == "DELETE" and path.startswith("/contexts/"):
            self.contexts.discard(path.split("/")[2])
            return 204, b""
        return 404, b"{}"


class BrowserbaseTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.p = BrowserbaseProvider("bb_key", "proj", transport=self.api)

    def test_session_carries_context_keepalive_and_no_recording(self):
        ctx = self.p.create_profile("u")
        s = self.p.start_session("alice", ctx, timeout_s=10, viewport=(800, 600))
        body = [c for c in self.api.calls if c[:2] == ("POST", "/sessions")][0][2]
        self.assertEqual(body["browserSettings"]["context"], {"id": ctx, "persist": True})
        self.assertIs(body["keepAlive"], True)
        self.assertIs(body["browserSettings"]["recordSession"], False)
        self.assertEqual(body["timeout"], 60)                       # clamped to Browserbase's minimum
        self.assertEqual(body["browserSettings"]["viewport"], {"width": 800, "height": 600})
        self.assertEqual(self.api.calls[0][3]["X-BB-API-Key"], "bb_key")
        self.assertEqual(s.profile_id, ctx)

    def test_connection_has_no_headers_and_is_fetched_fresh(self):
        s = self.p.start_session("a")
        url, headers = self.p.automation_connection(s)
        self.assertTrue(url.startswith("wss://connect.example/"))
        self.assertEqual(headers, {})
        self.api.sessions[s.session_id]["status"] = "COMPLETED"
        with self.assertRaises(ProviderError):
            self.p.automation_connection(s)

    def test_status_mapping_and_stop_waits_for_close(self):
        s = self.p.start_session("a")
        self.assertEqual(self.p.session_status(s), "READY")
        self.p.stop_session(s)
        self.assertEqual(self.p.session_status(s), "COMPLETED")
        self.api.sessions.clear()
        self.assertEqual(self.p.session_status(s), "NOT_FOUND")
        self.p.stop_session(s)                                       # already gone: not an error

    def test_live_view_url(self):
        self.assertEqual(self.p.live_view_url(self.p.start_session("a")), "https://live.example/view")

    def test_errors_map_to_quota(self):
        self.api.fail = (429, json.dumps({"message": "Too many concurrent sessions"}).encode())
        with self.assertRaises(QuotaExceeded):
            self.p.start_session("a")
        self.api.fail = (500, b"boom")
        with self.assertRaises(ProviderError):
            self.p.start_session("a")

    def test_needs_keys(self):
        with self.assertRaises(ProviderError):
            BrowserbaseProvider("", "")

    def test_control_plane_creates_the_context_before_the_first_session_and_reuses_it(self):
        svc = SessionService(self.p, SqliteStore(":memory:"), strategy="overwrite")
        first = svc.open("alice")
        self.assertFalse(first["restored"])
        pid = svc.store.get("alice")["profile_id"]
        self.assertEqual(pid, "ctx1")
        svc.release("alice")
        again = svc.open("alice")
        self.assertTrue(again["restored"])
        self.assertEqual(svc.store.get("alice")["profile_id"], pid)   # same context, no new one per save
        self.assertEqual(len(self.api.contexts), 1)
        sess_bodies = [c[2] for c in self.api.calls if c[:2] == ("POST", "/sessions")]
        self.assertEqual({b["browserSettings"]["context"]["id"] for b in sess_bodies}, {"ctx1"})


if __name__ == "__main__":
    unittest.main()
