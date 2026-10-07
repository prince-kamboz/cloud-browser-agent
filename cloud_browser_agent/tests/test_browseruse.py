import json
import unittest

from cloud_browser_agent.control.core import SessionService
from cloud_browser_agent.control.store import SqliteStore
from cloud_browser_agent.provider.base import ProviderError, QuotaExceeded
from cloud_browser_agent.provider.browseruse import BrowserUseProvider


class FakeApi:
    """Just enough of api.browser-use.com/api/v4 to check what we send and how we read the answers."""
    def __init__(self):
        self.calls, self.sessions, self.profiles, self.fail = [], {}, set(), None

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/api/v4", 1)[1]
        data = json.loads(body) if body else None
        self.calls.append((method, path, data, headers))
        if self.fail:
            return self.fail
        if (method, path) == ("POST", "/profiles"):
            pid = f"prof{len(self.profiles) + 1}"; self.profiles.add(pid)
            return 200, json.dumps({"id": pid}).encode()
        if (method, path) == ("POST", "/browsers"):
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = {"id": sid, "status": "active", "cdpUrl": f"https://{sid}.cdp.example", "liveUrl": f"https://live.example/{sid}"}
            return 200, json.dumps(self.sessions[sid]).encode()
        if method == "GET" and path.startswith("/browsers/"):
            s = self.sessions.get(path.split("/")[2])
            return (200, json.dumps(s).encode()) if s else (404, b"{}")
        if method == "PATCH" and path.startswith("/browsers/"):
            s = self.sessions.get(path.split("/")[2])
            if not s:
                return 404, b"{}"
            s["status"] = "stopped"
            return 200, b"{}"
        if method == "DELETE" and path.startswith("/profiles/"):
            self.profiles.discard(path.split("/")[2])
            return 204, b""
        return 404, b"{}"


class BrowserUseTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.p = BrowserUseProvider("bu_key", transport=self.api)

    def test_session_request_carries_profile_size_timeout_and_no_proxy_by_default(self):
        prof = self.p.create_profile("alice")
        s = self.p.start_session("alice", prof, timeout_s=90, viewport=(800, 600))
        body = [c for c in self.api.calls if c[:2] == ("POST", "/browsers")][0][2]
        self.assertEqual((body["profileId"], body["timeout"]), (prof, 2))          # minutes, rounded up
        self.assertEqual((body["browserScreenWidth"], body["browserScreenHeight"]), (800, 600))
        self.assertIsNone(body["proxyCountryCode"])
        self.assertEqual(self.api.calls[0][3]["X-Browser-Use-API-Key"], "bu_key")
        self.assertEqual(s.profile_id, prof)

    def test_proxy_country_is_opt_in(self):
        p = BrowserUseProvider("k", transport=self.api, proxy_country="de")
        p.start_session("a")
        self.assertEqual([c for c in self.api.calls if c[:2] == ("POST", "/browsers")][0][2]["proxyCountryCode"], "de")

    def test_connection_live_view_and_status(self):
        s = self.p.start_session("a")
        self.assertEqual(self.p.automation_connection(s), (f"https://{s.session_id}.cdp.example", {}))
        self.assertEqual(self.p.live_view_url(s), f"https://live.example/{s.session_id}")
        self.assertEqual(self.p.session_status(s), "READY")

    def test_stop_uses_the_stop_action_not_a_dropped_connection(self):
        s = self.p.start_session("a")
        self.p.stop_session(s)
        stops = [c for c in self.api.calls if c[0] == "PATCH"]
        self.assertEqual(stops[0][2], {"action": "stop"})
        self.assertEqual(self.p.session_status(s), "STOPPED")
        with self.assertRaises(ProviderError):
            self.p.automation_connection(s)
        self.api.sessions.clear()
        self.assertEqual(self.p.session_status(s), "NOT_FOUND")
        self.p.stop_session(s)                                                      # already gone: fine

    def test_errors_map_to_quota(self):
        self.api.fail = (402, json.dumps({"message": "Insufficient credits"}).encode())
        with self.assertRaises(QuotaExceeded):
            self.p.start_session("a")
        self.api.fail = (500, b"boom")
        with self.assertRaises(ProviderError):
            self.p.start_session("a")

    def test_needs_a_key(self):
        with self.assertRaises(ProviderError):
            BrowserUseProvider("")

    def test_control_plane_creates_the_profile_first_and_reuses_it(self):
        svc = SessionService(self.p, SqliteStore(":memory:"), strategy="overwrite")
        self.assertFalse(svc.open("alice")["restored"])
        pid = svc.store.get("alice")["profile_id"]
        svc.release("alice")
        self.assertTrue(svc.open("alice")["restored"])
        self.assertEqual(svc.store.get("alice")["profile_id"], pid)
        self.assertEqual(len(self.api.profiles), 1)


if __name__ == "__main__":
    unittest.main()
