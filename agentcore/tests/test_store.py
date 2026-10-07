import os
import tempfile
import threading
import unittest

from agentcore.control.core import SessionService
from agentcore.control.store import SqliteStore
from agentcore.provider.fake import FakeProvider


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class StoreTests(unittest.TestCase):
    def test_roundtrip_update_delete(self):
        s = SqliteStore()
        self.assertEqual(s.get("alice"), {})
        s.put("alice", {"profile_id": "p1"}); s.put("alice", {"profile_id": "p2", "x": 1})
        self.assertEqual(s.get("alice"), {"profile_id": "p2", "x": 1})
        s.put("bob", {"profile_id": "b"})
        self.assertEqual(set(s.all()), {"alice", "bob"})
        s.delete("alice")
        self.assertEqual(set(s.all()), {"bob"})

    def test_data_survives_reopening_the_database_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.db")
            SqliteStore(path).put("alice", {"profile_id": "p1"})
            self.assertEqual(SqliteStore(path).get("alice"), {"profile_id": "p1"})

    def test_concurrent_writers_do_not_lose_rows(self):
        s = SqliteStore()
        ts = [threading.Thread(target=lambda i=i: [s.put(f"u{i}", {"n": n}) for n in range(50)]) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(s.all()), 8)
        self.assertTrue(all(v == {"n": 49} for v in s.all().values()))


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = FakeProvider(self.clock)
        self.store = SqliteStore()

    def service(self):
        return SessionService(self.p, self.store, clock=self.clock, strategy="overwrite")

    def test_live_session_is_adopted_after_a_control_plane_restart(self):
        a = self.service(); a.open("alice")
        self.p.browser_state(a.active["alice"].info)["cookies"] = {"sid": "A1"}
        b = self.service()                                    # the process restarted: new object, same database
        self.assertEqual(b.active, {})
        self.assertEqual(b.recover(), {"adopted": ["alice"], "ended": []})
        self.assertTrue(b.heartbeat("alice")["ended"] is False)
        self.assertTrue(b.release("alice")["saved"])          # it can still save and stop the session it adopted
        self.assertEqual(len(self.p.live_sessions()), 0)
        self.assertEqual(self.store.all_active(), {})
        self.assertTrue(b.open("alice")["restored"])

    def test_session_that_died_while_we_were_down_is_recorded_not_adopted(self):
        a = self.service(); a.open("alice")
        self.p.sessions[a.active["alice"].info.session_id]["stopped"] = True
        b = self.service()
        self.assertEqual(b.recover(), {"adopted": [], "ended": ["alice"]})
        self.assertIn("ended_unexpectedly_at", self.store.get("alice"))
        self.assertEqual(self.store.all_active(), {})
        self.assertFalse(b.open("alice")["reused"])

    def test_released_session_leaves_no_active_row(self):
        a = self.service(); a.open("alice")
        self.assertEqual(set(self.store.all_active()), {"alice"})
        a.release("alice")
        self.assertEqual(self.store.all_active(), {})

    def test_saved_profile_pointer_survives_a_restart(self):
        a = self.service(); a.open("alice")
        self.p.browser_state(a.active["alice"].info)["cookies"] = {"sid": "A1"}
        a.release("alice")
        b = self.service(); b.recover()
        b.open("alice")
        self.assertEqual(self.p.browser_state(b.active["alice"].info)["cookies"], {"sid": "A1"})


if __name__ == "__main__":
    unittest.main()
