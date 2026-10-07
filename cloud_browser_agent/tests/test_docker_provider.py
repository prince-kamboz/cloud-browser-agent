import json
import unittest

from cloud_browser_agent.control.core import SessionService
from cloud_browser_agent.control.store import SqliteStore
from cloud_browser_agent.provider.base import ProviderError, QuotaExceeded
from cloud_browser_agent.provider.docker_chromium import DockerChromiumProvider

try:
    import docker.errors as de
except ImportError:                                  # the unit-test image installs the SDK; skip cleanly without it
    de = None


class FakeContainer:
    def __init__(self, name, docker):
        self.name, self.docker, self.status = name, docker, "running"

    def stop(self, timeout=10):
        self.status = "exited"
        self.docker.stopped.append((self.name, timeout))

    def remove(self, force=False):
        self.docker.live.pop(self.name, None)


class FakeVolume:
    def __init__(self, name, docker):
        self.name, self.docker = name, docker

    def remove(self, force=False):
        self.docker.vols.pop(self.name, None)


class _Containers:
    def __init__(self, d):
        self.d = d

    def run(self, image, **kw):
        if self.d.fail_run:
            raise self.d.fail_run
        self.d.run_calls.append((image, kw))
        c = self.d.live[kw["name"]] = FakeContainer(kw["name"], self.d)
        return c

    def get(self, name):
        if name not in self.d.live:
            raise de.NotFound("gone")
        return self.d.live[name]

    def list(self, filters=None):
        return [c for c in self.d.live.values() if c.status == "running"]


class _Volumes:
    def __init__(self, d):
        self.d = d

    def create(self, name, labels=None):
        v = self.d.vols[name] = FakeVolume(name, self.d)
        return v

    def get(self, name):
        if name not in self.d.vols:
            raise de.NotFound("gone")
        return self.d.vols[name]


class FakeDocker:
    """Just enough of the Docker SDK: containers.run/get/list and volumes.create/get."""
    def __init__(self):
        self.live, self.vols, self.stopped, self.run_calls, self.fail_run = {}, {}, [], [], None
        self.containers, self.volumes = _Containers(self), _Volumes(self)


class Http:
    def __init__(self):
        self.calls = []
        self.pages = [{"id": "P1", "type": "page", "title": "One", "url": "https://one.example/"},
                      {"id": "P2", "type": "page", "title": "Two", "url": "https://two.example/"},
                      {"id": "W1", "type": "service_worker", "title": "sw", "url": "x"}]

    def __call__(self, method, url, timeout=5):
        self.calls.append((method, url))
        if url.endswith("/json/version"):
            return 200, json.dumps({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/BID"}).encode()
        if url.endswith("/json/list"):
            return 200, json.dumps(self.pages).encode()
        return 200, b"{}"


@unittest.skipIf(de is None, "docker SDK not installed")
class DockerProviderTests(unittest.TestCase):
    def setUp(self):
        self.d, self.h = FakeDocker(), Http()
        self.p = DockerChromiumProvider("img", "net", client=self.d, http=self.h, max_browsers=2)

    def test_session_gets_the_profile_volume_network_and_limits(self):
        vol = self.p.create_profile("alice")
        s = self.p.start_session("alice", vol, viewport=(800, 600))
        image, kw = self.d.run_calls[0]
        self.assertEqual((image, kw["network"]), ("img", "net"))
        self.assertEqual(kw["volumes"], {vol: {"bind": "/data", "mode": "rw"}})
        self.assertEqual(kw["environment"]["SCREEN_WIDTH"], "800")
        self.assertEqual(kw["labels"]["cba.user"], "alice")
        self.assertEqual(self.p.session_status(s), "READY")
        self.p.wait_ready(s)

    def test_connection_is_the_container_on_the_docker_network(self):
        s = self.p.start_session("a", self.p.create_profile("a"))
        url, headers = self.p.automation_connection(s)
        self.assertEqual(url, f"ws://{s.browser_id}:8080/cdp/devtools/browser/BID")
        self.assertEqual(headers, {})

    def test_stop_is_graceful_and_keeps_the_profile(self):
        vol = self.p.create_profile("a")
        s = self.p.start_session("a", vol)
        self.p.stop_session(s)
        self.assertEqual(self.d.stopped, [(s.browser_id, 30)])      # stop with a grace period, not a kill
        self.assertEqual(self.p.session_status(s), "NOT_FOUND")
        self.assertIn(vol, self.d.vols)                               # the volume is the saved login
        self.p.stop_session(s)                                        # already gone: fine
        self.p.delete_profile(vol)
        self.assertNotIn(vol, self.d.vols)

    def test_tabs_only_pages_with_their_own_view_url_and_bridge_target(self):
        s = self.p.start_session("a", self.p.create_profile("a"))
        tabs = self.p.tabs(s)
        self.assertEqual([t["id"] for t in tabs], ["P1", "P2"])
        self.assertTrue(tabs[1]["view_url"].endswith(f"session={s.session_id}&tab=P2"))
        self.assertTrue(self.p.page_ws(s, "P2").endswith("/cdp/devtools/page/P2"))
        self.assertTrue(self.p.page_ws(s).endswith("/cdp/devtools/page/P1"))        # default: first page
        with self.assertRaises(ProviderError):
            self.p.page_ws(s, "nope")
        self.p.open_tab(s, "https://x.example/a b")
        self.p.close_tab(s, "P2")
        methods = [(m, u.split("/cdp")[1]) for m, u in self.h.calls if "/json/new" in u or "/json/close" in u]
        self.assertEqual(methods[0][0], "PUT")
        self.assertEqual(methods[1], ("GET", "/json/close/P2"))

    def test_browser_limit_maps_to_quota(self):
        self.p.start_session("a", self.p.create_profile("a"))
        self.p.start_session("b", self.p.create_profile("b"))
        with self.assertRaises(QuotaExceeded):
            self.p.start_session("c", self.p.create_profile("c"))

    def test_missing_image_is_a_clear_error(self):
        self.d.fail_run = de.ImageNotFound("no image")
        with self.assertRaises(ProviderError) as cm:
            self.p.start_session("a", None)
        self.assertIn("docker compose --profile docker build", str(cm.exception))

    def test_control_plane_does_not_leak_a_new_profile_when_the_start_fails(self):
        svc = SessionService(self.p, SqliteStore(":memory:"), strategy="overwrite")
        self.d.fail_run = de.APIError("boom")
        with self.assertRaises(ProviderError):
            svc.open("alice")
        self.assertEqual(self.d.vols, {})                              # the volume made for the attempt was removed
        self.assertNotIn("profile_id", svc.store.get("alice"))

    def test_control_plane_reuses_the_volume_across_sessions(self):
        svc = SessionService(self.p, SqliteStore(":memory:"), strategy="overwrite")
        first = svc.open("alice")
        self.assertFalse(first["restored"])
        svc.release("alice")
        again = svc.open("alice")
        self.assertTrue(again["restored"])
        self.assertEqual(len(self.d.vols), 1)


if __name__ == "__main__":
    unittest.main()
