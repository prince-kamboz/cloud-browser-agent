import threading
import unittest

from cloud_browser_agent.control.core import SessionService
from cloud_browser_agent.control.store import SqliteStore
from cloud_browser_agent.provider.base import ProviderError, QuotaExceeded
from cloud_browser_agent.provider.fake import FakeProvider


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def make(mode="overwrite", strategy="overwrite", max_profiles=100, **kw):
    clock = Clock()
    p = FakeProvider(clock, profile_mode=mode, max_profiles=max_profiles)
    svc = SessionService(p, SqliteStore(), clock=clock, strategy=strategy, **kw)
    svc.sleeps = []
    svc.sleep = svc.sleeps.append                        # retries wait on the fake clock, not for real
    return clock, p, svc


def login(p, svc, user, value):
    """The user logs in: cookies and local storage change inside the live browser."""
    st = p.browser_state(svc.active[user].info)
    st["cookies"] = {"sid": value}
    st["localStorage"] = {"token": value}


class LifecycleTests(unittest.TestCase):
    def test_login_survives_release_and_reopen(self):
        clock, p, svc = make()
        r = svc.open("alice")
        self.assertFalse(r["restored"])                       # first ever open: nothing saved yet
        login(p, svc, "alice", "A1")
        self.assertTrue(svc.release("alice")["saved"])
        self.assertEqual(len(p.live_sessions()), 0)           # resources really released
        r = svc.open("alice")
        self.assertTrue(r["restored"])
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A1"})
        self.assertEqual(p.browser_state(svc.active["alice"].info)["localStorage"], {"token": "A1"})

    def test_users_are_isolated(self):
        clock, p, svc = make()
        svc.open("alice"); svc.open("bob")
        login(p, svc, "alice", "A"); login(p, svc, "bob", "B")
        svc.release("alice"); svc.release("bob")
        svc.open("alice"); svc.open("bob")
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A"})
        self.assertEqual(p.browser_state(svc.active["bob"].info)["cookies"], {"sid": "B"})

    def test_overwrite_strategy_keeps_latest_login(self):
        clock, p, svc = make(mode="overwrite", strategy="overwrite")
        svc.open("alice"); login(p, svc, "alice", "A1"); svc.release("alice")
        svc.open("alice"); login(p, svc, "alice", "A2"); svc.release("alice")
        svc.open("alice")
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A2"})
        self.assertEqual(len(p.profiles), 1)

    def test_rotate_strategy_works_even_if_profiles_are_write_once(self):
        clock, p, svc = make(mode="immutable", strategy="rotate")
        svc.open("alice"); login(p, svc, "alice", "A1"); svc.release("alice")
        clock.advance(10)
        svc.open("alice"); login(p, svc, "alice", "A2"); self.assertTrue(svc.release("alice")["saved"])
        svc.open("alice")
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A2"})
        self.assertEqual(len(p.profiles), 1)                  # the old profile was deleted, quota not leaked

    def test_overwrite_strategy_on_write_once_profiles_fails_loudly_not_silently(self):
        clock, p, svc = make(mode="immutable", strategy="overwrite")
        svc.open("alice"); login(p, svc, "alice", "A1"); svc.release("alice")
        svc.open("alice"); login(p, svc, "alice", "A2")
        self.assertFalse(svc.release("alice")["saved"])       # the second save is refused...
        rec = svc.store.get("alice")
        self.assertTrue(rec["conflict"])                      # ...and we know it, so the UI can warn
        svc.open("alice")
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A1"})  # old login kept

    def test_zero_profile_quota_is_survivable(self):
        clock, p, svc = make(max_profiles=0)                  # this account today
        svc.open("alice"); login(p, svc, "alice", "A1")
        out = svc.release("alice")
        self.assertFalse(out["saved"])
        self.assertEqual(len(p.live_sessions()), 0)           # the browser is still released, no leak
        rec = svc.store.get("alice")
        self.assertTrue(rec["degraded"])
        self.assertIn("maxBrowserProfiles", rec["save_error"])
        self.assertFalse(svc.open("alice")["restored"])       # next open works, just without saved logins

    def test_idle_user_is_released_and_active_user_is_not(self):
        clock, p, svc = make(idle_after_s=120, save_every_s=10_000)
        svc.open("alice"); svc.open("bob")
        login(p, svc, "alice", "A")
        clock.advance(100); svc.heartbeat("bob")
        clock.advance(30)                                     # alice idle 130 s, bob idle 30 s
        self.assertEqual(svc.reap(), ["released:alice"])
        self.assertNotIn("alice", svc.active); self.assertIn("bob", svc.active)
        self.assertEqual(len(p.live_sessions()), 1)
        self.assertTrue(svc.open("alice")["restored"])        # and alice's login came back

    def test_autosave_protects_against_the_session_dying(self):
        clock, p, svc = make(idle_after_s=10_000, save_every_s=60, session_timeout_s=300)
        svc.open("alice"); login(p, svc, "alice", "A1")
        clock.advance(61); self.assertEqual(svc.reap(), ["autosaved:alice"])
        login(p, svc, "alice", "A2")                          # changed after the last save...
        clock.advance(300)                                    # ...then AWS ends the session at its time limit
        self.assertEqual(svc.reap(), ["ended:alice"])
        self.assertNotIn("alice", svc.active)
        self.assertIn("ended_unexpectedly_at", svc.store.get("alice"))
        svc.open("alice")                                     # reopens with the last autosave, A1 (A2 was lost)
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A1"})

    def test_a_wall_clock_jump_does_not_release_or_autosave(self):
        """Regression: the dev stack's VM was suspended, the wall clock jumped 15 minutes, and every user looked idle for
        15 minutes. Idle and autosave are durations, so they run on a monotonic clock that did not move."""
        wall, mono = Clock(), Clock()
        p = FakeProvider(wall)
        svc = SessionService(p, SqliteStore(), clock=wall, mono=mono, idle_after_s=120, save_every_s=60, strategy="overwrite")
        svc.open("alice")
        wall.advance(900)                                     # the wall clock jumps 15 minutes...
        mono.advance(5)                                       # ...while only 5 real seconds passed
        self.assertEqual(svc.reap(), [])
        self.assertIn("alice", svc.active)
        mono.advance(130)                                     # genuinely idle for 135 s
        self.assertEqual(svc.reap(), ["released:alice"])

    def test_two_tabs_racing_to_open_make_one_browser(self):
        clock, p, svc = make()
        results = []
        ts = [threading.Thread(target=lambda: results.append(svc.open("alice"))) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(p.live_sessions()), 1)
        self.assertEqual(len({r["session_id"] for r in results}), 1)

    def test_secret_entry_hides_the_page_from_the_agent_and_blocks_idle_release(self):
        clock, p, svc = make(idle_after_s=120)
        svc.open("alice")
        svc.secret_entry("alice", True)
        self.assertFalse(p.sessions[svc.active["alice"].info.session_id]["automation"])
        clock.advance(1000)
        self.assertEqual(svc.reap(), [])                      # typing a password: never yanked away
        svc.secret_entry("alice", False)
        self.assertTrue(p.sessions[svc.active["alice"].info.session_id]["automation"])

    def test_forget_deletes_saved_logins(self):
        clock, p, svc = make()
        svc.open("alice"); login(p, svc, "alice", "A"); svc.release("alice")
        self.assertEqual(len(p.profiles), 1)
        svc.forget("alice")
        self.assertEqual(len(p.profiles), 0)
        self.assertFalse(svc.open("alice")["restored"])

    def test_deleted_profile_does_not_stop_the_user_opening(self):
        clock, p, svc = make()
        svc.open("alice"); login(p, svc, "alice", "A"); svc.release("alice")
        p.profiles.clear()                                    # someone deleted it in the console
        r = svc.open("alice")
        self.assertFalse(r["restored"])
        self.assertIn("profile_error", svc.store.get("alice"))


class SavedProfileLoadTests(unittest.TestCase):
    """A saved profile that fails to load must not silently cost the user their logins."""

    def saved_user(self):
        clock, p, svc = make()
        svc.open("alice"); login(p, svc, "alice", "A"); svc.release("alice")
        return clock, p, svc, svc.store.get("alice")["profile_id"]

    def fail_starts_with(self, p, profile_id, times):
        real, left = p.start_session, [times]
        def start(user_id, pid=None, *a, **k):
            if pid == profile_id and left[0] > 0:
                left[0] -= 1
                raise ProviderError("profile is in use by another session")
            return real(user_id, pid, *a, **k)
        p.start_session = start

    def test_a_busy_profile_is_retried_and_the_logins_come_back(self):
        clock, p, svc, pid = self.saved_user()
        self.fail_starts_with(p, pid, times=2)                # e.g. the previous session is still winding down
        r = svc.open("alice")
        self.assertTrue(r["restored"])
        self.assertNotIn("profile_warning", r)
        self.assertEqual(svc.sleeps, [1.0, 3.0])              # waited between the attempts
        self.assertEqual(svc.store.get("alice")["profile_id"], pid)
        self.assertEqual(p.browser_state(svc.active["alice"].info)["cookies"], {"sid": "A"})

    def test_a_profile_that_never_loads_is_kept_not_dropped(self):
        clock, p, svc, pid = self.saved_user()
        self.fail_starts_with(p, pid, times=99)
        r = svc.open("alice")
        self.assertFalse(r["restored"])
        self.assertIn("kept", r["profile_warning"])           # the user is told
        rec = svc.store.get("alice")
        self.assertEqual(rec["kept_profiles"], [pid])         # the old pointer is remembered
        self.assertIn(pid, p.profiles)                        # and the profile itself was not deleted
        svc.forget("alice")                                   # "forget logins" still removes everything
        self.assertNotIn(pid, p.profiles)

    def test_a_quota_error_never_replaces_the_saved_profile(self):
        clock, p, svc, pid = self.saved_user()
        real = p.start_session
        def start(user_id, pid_=None, *a, **k):
            raise QuotaExceeded("maxBrowserSessions limit exceeded")
        p.start_session = start
        with self.assertRaises(QuotaExceeded):
            svc.open("alice")
        self.assertEqual(svc.store.get("alice")["profile_id"], pid)
        self.assertEqual(svc.sleeps, [])                      # no pointless retries on a quota error
        p.start_session = real
        self.assertTrue(svc.open("alice")["restored"])

    def test_the_warning_is_cleared_once_a_profile_loads_again(self):
        clock, p, svc, pid = self.saved_user()
        rec = svc.store.get("alice"); rec["profile_error"] = "old warning"; svc.store.put("alice", rec)
        self.assertTrue(svc.open("alice")["restored"])
        self.assertNotIn("profile_error", svc.store.get("alice"))


if __name__ == "__main__":
    unittest.main()
