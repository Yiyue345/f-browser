"""Local browser history and bookmarks. Never persist CAS tickets or URL query strings."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from fuckclassroom.auth.webvpn import safe_history_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _title(value: str) -> str:
    # Titles are local only, but limit control characters and database growth.
    return " ".join(str(value or "").split())[:180] or "校园内网页面"


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS visits(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                  visited_at TEXT NOT NULL, visit_count INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS visits_recent ON visits(visited_at DESC);
                CREATE TABLE IF NOT EXISTS bookmarks(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
            """)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=8)
        db.row_factory = sqlite3.Row
        return db

    def record(self, url: str, title: str) -> None:
        safe = safe_history_url(url)
        with self._lock, self._connect() as db:
            db.execute("""
                INSERT INTO visits(url,title,visited_at,visit_count)
                VALUES(?,?,?,1)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title, visited_at=excluded.visited_at,
                    visit_count=visits.visit_count+1
            """, (safe, _title(title), _now()))

    def list_history(self, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as db:
            rows = db.execute("""
                SELECT id,url,title,visited_at,visit_count FROM visits
                ORDER BY visited_at DESC,id DESC LIMIT ?
            """, (max(1, min(200, int(limit))),)).fetchall()
            return [dict(row) for row in rows]

    def clear_history(self) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM visits")

    def delete_visit(self, visit_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM visits WHERE id=?", (int(visit_id),))

    def bookmark(self, url: str, title: str) -> None:
        safe = safe_history_url(url)
        with self._lock, self._connect() as db:
            db.execute("""
                INSERT INTO bookmarks(url,title,created_at) VALUES(?,?,?)
                ON CONFLICT(url) DO UPDATE SET title=excluded.title
            """, (safe, _title(title), _now()))

    def list_bookmarks(self) -> list[dict]:
        with self._lock, self._connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT id,url,title,created_at FROM bookmarks
                ORDER BY created_at DESC,id DESC LIMIT 300
            """).fetchall()]

    def delete_bookmark(self, bookmark_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM bookmarks WHERE id=?", (int(bookmark_id),))
