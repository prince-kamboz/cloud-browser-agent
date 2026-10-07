"""Session lifecycle for AgentCore Browser (the browser tool only): one browser per user, logins saved to a profile, released when idle.

  open      take the user's lock, start a session with their saved profile (if any), hand back a live-view URL
  heartbeat the user is still here
  save      write the session's cookies + local storage to the user's profile (AgentCore only saves when asked)
  release   save, then StopBrowserSession
  reap      periodic: autosave, release idle users, notice sessions AWS ended on its own (time limit)

Profile strategies (we do not yet know whether a second save into one profile overwrites or is refused):
  overwrite  one profile per user, saved into again and again
  rotate     every save creates a fresh profile, then the pointer moves and the old one is deleted
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from agentcore.control.store import SqliteStore
from agentcore.provider.base import (BrowserProvider, ProfileConflict, ProviderError, QuotaExceeded, SessionInfo)


@dataclass
class Active:
    user: str
    info: SessionInfo
    last_seen: float
    last_saved: float
    secret_entry: bool = False


class SessionService:
    def __init__(self, provider: BrowserProvider, store: SqliteStore, clock: Callable[[], float] = time.time,
                 idle_after_s: float = 120, save_every_s: float = 60, session_timeout_s: int = 3600,
                 strategy: str = "rotate", viewport=(1280, 800), mono: Optional[Callable[[], float]] = None):
        assert strategy in ("overwrite", "rotate")
        self.p, self.store, self.clock = provider, store, clock
        # Idle and autosave are DURATIONS, so they use a monotonic clock. A wall clock can jump (laptop or VM suspend,
        # time sync, VM migration): after a 15-minute jump every user would look idle for 15 minutes and be released.
        # Wall time (self.clock) is only used for timestamps that are stored.
        self.mono = mono or (time.monotonic if clock is time.time else clock)
        self.idle_after_s, self.save_every_s, self.session_timeout_s = idle_after_s, save_every_s, session_timeout_s
        self.strategy, self.viewport = strategy, viewport
        self.active: Dict[str, Active] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._meta = threading.Lock()

    def _persist(self, a: "Active") -> None:
        self.store.put_active(a.user, {"session_id": a.info.session_id, "browser_id": a.info.browser_id,
                                       "viewport": list(a.info.viewport), "profile_id": a.info.profile_id,
                                       "created_at": a.info.created_at, "last_seen": a.last_seen,
                                       "last_saved": a.last_saved})

    def recover(self) -> dict:
        """After a restart: re-adopt sessions that are still alive, record the ones that are not.
        Without this a restart would orphan live (billing) AgentCore sessions until their time limit."""
        adopted, gone = [], []
        for user, r in self.store.all_active().items():
            info = SessionInfo(session_id=r["session_id"], browser_id=r["browser_id"], created_at=r["created_at"],
                               viewport=tuple(r["viewport"]), profile_id=r.get("profile_id"))
            try:
                alive = self.p.session_status(info) == "READY"
            except ProviderError:
                alive = False
            if alive:
                now = self.mono()            # a restart gives everyone a fresh grace period (the old numbers were another clock)
                self.active[user] = Active(user, info, last_seen=now, last_saved=now)
                adopted.append(user)
            else:
                rec = self.store.get(user)
                rec.update(ended_unexpectedly_at=self.clock())
                self.store.put(user, rec)
                self.store.delete_active(user)
                gone.append(user)
        return {"adopted": adopted, "ended": gone}

    def _lock(self, user: str) -> threading.RLock:
        with self._meta:
            return self._locks.setdefault(user, threading.RLock())

    # ------------------------------------------------------------------------------------------ open
    def open(self, user: str) -> dict:
        with self._lock(user):                                   # one browser per user, even if two tabs race
            a = self.active.get(user)
            if a and self.p.session_status(a.info) == "READY":
                a.last_seen = self.mono()
                return self._summary(a, restored=bool(a.info.profile_id), reused=True)
            rec = self.store.get(user)
            if a:                                                # AWS ended it on its own (time limit, crash)
                rec.update(ended_unexpectedly_at=self.clock(), unsaved_for_s=int(self.mono() - a.last_saved))
                self.store.put(user, rec)
                self.active.pop(user, None)
                self.store.delete_active(user)
            pid = rec.get("profile_id")
            restored = bool(pid)
            if not pid and self.p.profile_at_start:             # Browserbase: the context must exist before the session does
                pid = self.p.create_profile(f"{user}_p")
                rec["profile_id"] = pid
                self.store.put(user, rec)
            try:
                info = self.p.start_session(user, pid, self.session_timeout_s, self.viewport)
            except ProviderError:
                if not pid:
                    raise
                # the saved profile is unusable: start clean rather than refuse to open, and say so
                rec.update(profile_id=None, profile_error="saved profile could not be loaded")
                pid, restored = None, False
                if self.p.profile_at_start:
                    pid = self.p.create_profile(f"{user}_p")
                    rec["profile_id"] = pid
                self.store.put(user, rec)
                info = self.p.start_session(user, pid, self.session_timeout_s, self.viewport)
            self.p.wait_ready(info)
            now = self.mono()
            a = Active(user, info, last_seen=now, last_saved=now)
            self.active[user] = a
            self._persist(a)
            return self._summary(a, restored=restored, reused=False)

    def _summary(self, a: Active, restored: bool, reused: bool) -> dict:
        return {"user": a.user, "session_id": a.info.session_id, "restored": restored, "reused": reused,
                "viewport": list(a.info.viewport), "live_view_url": self.p.live_view_url(a.info)}

    # ------------------------------------------------------------------------------ connect / heartbeat
    def connection(self, user: str):
        """(wss URL, signed headers) for the agent service. Server-side only: these are credentials."""
        a = self._need(user)
        return self.p.automation_connection(a.info)

    def live_view(self, user: str) -> str:
        return self.p.live_view_url(self._need(user).info)

    def heartbeat(self, user: str) -> dict:
        with self._lock(user):
            a = self.active.get(user)
            if not a:
                return {"ended": True, "reason": "no session"}
            if self.p.session_status(a.info) != "READY":
                return {"ended": True, "reason": "session ended"}
            a.last_seen = self.mono()
            self._persist(a)
            return {"ended": False}

    def _need(self, user: str) -> Active:
        a = self.active.get(user)
        if not a:
            raise KeyError(f"no active session for {user}")
        return a

    # ------------------------------------------------------------------------------------------- save
    def save(self, user: str, reason: str = "manual") -> bool:
        with self._lock(user):
            a = self.active.get(user)
            if not a:
                return False
            rec = self.store.get(user)
            rec.pop("cleanup_error", None)
            old = rec.get("profile_id")
            try:
                if self.strategy == "overwrite":
                    pid = old or self.p.create_profile(f"{user}_p")
                    if not old:
                        rec["profile_id"] = pid                 # remember it even if the save below fails
                        self.store.put(user, rec)
                    self.p.save_profile(a.info, pid)
                else:
                    new = self.p.create_profile(f"{user}_{int(self.clock())}")
                    self.p.save_profile(a.info, new)
                    rec["profile_id"] = new
                    if old and old != new:
                        try:
                            self.p.delete_profile(old)
                        except ProviderError as e:               # leaked profile counts against the quota: surface it
                            rec["cleanup_error"] = str(e)
            except ProviderError as e:
                rec.update(save_error=str(e), degraded=isinstance(e, QuotaExceeded),
                           conflict=isinstance(e, ProfileConflict), last_save_failed_at=self.clock())
                self.store.put(user, rec)
                return False
            for k in ("save_error", "degraded", "conflict"):
                rec.pop(k, None)
            rec.update(saved_at=self.clock(), last_save_reason=reason)
            self.store.put(user, rec)
            a.last_saved = self.mono()
            self._persist(a)
            return True

    # ---------------------------------------------------------------------------------------- release
    def release(self, user: str, save: bool = True) -> dict:
        with self._lock(user):
            a = self.active.get(user)
            if not a:
                return {"released": False, "saved": False}
            saved = False
            if save and self.p.session_status(a.info) == "READY":
                saved = self.save(user, "release")
            try:
                if self.p.session_status(a.info) == "READY":
                    self.p.stop_session(a.info)
            finally:
                self.active.pop(user, None)
                self.store.delete_active(user)
            return {"released": True, "saved": saved}

    # ------------------------------------------------------------------------------------- secrets
    def secret_entry(self, user: str, on: bool) -> None:
        """While the user types a password the agent's automation stream is switched off, so it cannot see it."""
        with self._lock(user):
            a = self._need(user)
            self.p.set_automation_enabled(a.info, not on)
            a.secret_entry = on
            a.last_seen = self.mono()

    # ------------------------------------------------------------------------------------------ reap
    def reap(self) -> List[str]:
        """Run every few seconds. Returns what it did, for logging and tests."""
        did = []
        for user in list(self.active):
            with self._lock(user):
                a = self.active.get(user)
                if not a:
                    continue
                now = self.mono()
                if self.p.session_status(a.info) != "READY":
                    rec = self.store.get(user)
                    rec.update(ended_unexpectedly_at=self.clock(), unsaved_for_s=int(now - a.last_saved))
                    self.store.put(user, rec)
                    self.active.pop(user, None)
                    self.store.delete_active(user)
                    did.append(f"ended:{user}")
                elif a.secret_entry:
                    continue                                     # user is typing a password: never interrupt
                elif now - a.last_seen > self.idle_after_s:
                    self.release(user)
                    did.append(f"released:{user}")
                elif now - a.last_saved >= self.save_every_s:
                    self.save(user, "autosave")
                    did.append(f"autosaved:{user}")
        return did

    # --------------------------------------------------------------------------------------- privacy
    def forget(self, user: str) -> None:
        """Delete the user's saved logins (the 'clear cookies' button)."""
        with self._lock(user):
            if user in self.active:
                self.release(user, save=False)
            rec = self.store.get(user)
            if rec.get("profile_id"):
                self.p.delete_profile(rec["profile_id"])
            self.store.delete(user)

    def status(self) -> dict:
        now = self.mono()
        return {"active": [{"user": a.user, "session_id": a.info.session_id, "idle_s": int(now - a.last_seen),
                            "since_save_s": int(now - a.last_saved), "secret_entry": a.secret_entry}
                           for a in self.active.values()],
                "users": self.store.all()}
