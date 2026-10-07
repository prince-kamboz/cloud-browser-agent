import os
import unittest

os.environ["PROVIDER"] = "fake"
os.environ["STATE_DB"] = ":memory:"
os.environ["REAP_EVERY_S"] = "3600"

from fastapi.testclient import TestClient            # noqa: E402

from cloud_browser_agent.control import app as appmod          # noqa: E402


class AppTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("INTERNAL_TOKEN", None)
        self.c = TestClient(appmod.app)
        appmod.svc.active.clear()

    def test_open_heartbeat_release(self):
        r = self.c.post("/api/sessions", json={"user": "Alice"}).json()
        self.assertEqual(r["user"], "alice")
        self.assertTrue(r["live_view_url"].startswith("https://"))
        self.assertEqual(self.c.post("/api/sessions/alice/heartbeat").status_code, 200)
        self.assertTrue(self.c.post("/api/sessions/alice/release").json()["released"])
        self.assertEqual(self.c.post("/api/sessions/alice/heartbeat").status_code, 404)   # the UI shows "session ended"

    def test_bad_user_names_are_rejected(self):
        self.assertEqual(self.c.post("/api/sessions", json={"user": "../etc"}).status_code, 400)
        self.assertEqual(self.c.post("/api/sessions", json={"user": ""}).status_code, 400)

    def test_connection_endpoint_is_closed_without_the_internal_token(self):
        self.c.post("/api/sessions", json={"user": "bob"})
        self.assertEqual(self.c.get("/api/sessions/bob/connection").status_code, 403)
        os.environ["INTERNAL_TOKEN"] = "t0ken"
        self.assertEqual(self.c.get("/api/sessions/bob/connection", headers={"x-internal-token": "wrong"}).status_code, 403)
        ok = self.c.get("/api/sessions/bob/connection", headers={"x-internal-token": "t0ken"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("ws_url", ok.json())

    def test_secret_entry_toggle_and_missing_session(self):
        self.c.post("/api/sessions", json={"user": "carol"})
        self.assertFalse(self.c.post("/api/sessions/carol/secret-entry", json={"on": True}).json()["automation_enabled"])
        self.assertTrue(self.c.post("/api/sessions/carol/secret-entry", json={"on": False}).json()["automation_enabled"])
        self.assertEqual(self.c.post("/api/sessions/nobody/secret-entry", json={"on": True}).status_code, 404)

    def test_config_and_viewer_url_for_a_plain_page_viewer(self):
        cfg = self.c.get("/api/config").json()
        self.assertEqual(cfg["viewer_kind"], "iframe")
        r = self.c.post("/api/sessions", json={"user": "erin"}).json()
        self.assertEqual(r["viewer_url"], r["live_view_url"])          # an iframe viewer is the live URL itself

    def test_dcv_viewer_url_carries_the_signed_link_and_the_viewport(self):
        appmod.svc.p.viewer_kind = "dcv"
        try:
            r = self.c.post("/api/sessions", json={"user": "frank"}).json()
        finally:
            appmod.svc.p.viewer_kind = "iframe"
        self.assertTrue(r["viewer_url"].startswith("/viewer/dcv.html#url="))
        self.assertIn("w=1280", r["viewer_url"]); self.assertIn("h=800", r["viewer_url"])
        self.assertIn("fake", r["viewer_url"])                          # the (url-encoded) signed live-view link

    def test_the_ui_and_viewer_pages_are_served(self):
        home = self.c.get("/")
        self.assertEqual(home.status_code, 200); self.assertIn("Browser Agent v2", home.text)
        self.assertEqual(self.c.get("/viewer/dcv.html").status_code, 200)
        self.assertEqual(self.c.get("/app.js").status_code, 200)
        self.assertEqual(self.c.get("/api/sessions").status_code, 200)    # API routes still win over the static mount

    def test_forget_logins(self):
        self.c.post("/api/sessions", json={"user": "dave"})
        self.c.post("/api/sessions/dave/release")
        self.assertEqual(self.c.delete("/api/users/dave/logins").status_code, 200)


if __name__ == "__main__":
    unittest.main()
