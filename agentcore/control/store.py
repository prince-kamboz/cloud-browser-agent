"""SQLite state for the control plane (stdlib only).

  users   one row per user: the saved-profile pointer and save status
  active  one row per live AgentCore session, so a control-plane restart can find (and stop) sessions it would
          otherwise forget: they keep running and billing until their time limit
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict, Optional


class SqliteStore:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)   # autocommit; we lock ourselves
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, record TEXT NOT NULL, updated_at REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS active (user_id TEXT PRIMARY KEY, record TEXT NOT NULL, updated_at REAL NOT NULL)")

    # ---- users
    def get(self, user: str) -> dict:
        return self._get("users", user) or {}

    def put(self, user: str, rec: dict) -> None:
        self._put("users", user, rec)

    def delete(self, user: str) -> None:
        self._delete("users", user)

    def all(self) -> Dict[str, dict]:
        return self._all("users")

    # ---- live sessions
    def put_active(self, user: str, rec: dict) -> None:
        self._put("active", user, rec)

    def delete_active(self, user: str) -> None:
        self._delete("active", user)

    def all_active(self) -> Dict[str, dict]:
        return self._all("active")

    # ---- plumbing (table names are internal constants, never user input)
    def _get(self, table: str, user: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(f"SELECT record FROM {table} WHERE user_id = ?", (user,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, table: str, user: str, rec: dict) -> None:
        with self._lock:
            self._db.execute(f"INSERT INTO {table} (user_id, record, updated_at) VALUES (?, ?, ?) "
                             f"ON CONFLICT(user_id) DO UPDATE SET record = excluded.record, updated_at = excluded.updated_at",
                             (user, json.dumps(rec), time.time()))

    def _delete(self, table: str, user: str) -> None:
        with self._lock:
            self._db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user,))

    def _all(self, table: str) -> Dict[str, dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT user_id, record FROM {table}").fetchall()
        return {u: json.loads(r) for u, r in rows}
