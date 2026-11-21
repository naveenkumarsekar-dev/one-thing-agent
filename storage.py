"""Storage: Postgres when DATABASE_URL is set (Heroku), SQLite otherwise (local).

Tables:
  entries    - daily goals and accomplishments
  memories   - long-term facts the agent has learned about the user
  followups  - wake-ups the agent scheduled for itself
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS entries (
        id {pk}, chat_id {big} NOT NULL, day TEXT NOT NULL,
        kind TEXT NOT NULL,            -- 'goal' or 'done'
        text TEXT NOT NULL,
        status TEXT NOT NULL,          -- goal: open/completed/dropped/carried_over; done: done
        note TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memories (
        id {pk}, chat_id {big} NOT NULL, fact TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS followups (
        id {pk}, chat_id {big} NOT NULL,
        due_at TEXT NOT NULL,          -- UTC ISO timestamp
        reason TEXT NOT NULL,
        status TEXT NOT NULL,          -- pending/fired/cancelled
        created_at TEXT NOT NULL)""",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, database_url: str | None = None, sqlite_path: str = "one_thing_agent.db"):
        url = database_url or ""
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self.url, self.sqlite_path = url, sqlite_path
        types = {"pk": "SERIAL PRIMARY KEY", "big": "BIGINT"} if self.pg else {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "big": "INTEGER"}
        with self._conn() as c:
            for ddl in SCHEMA:
                c.execute(ddl.format(**types))

    @contextmanager
    def _conn(self):
        if self.pg:
            import psycopg  # only needed on Heroku

            # New connection per call: low traffic, and it survives Heroku Postgres restarts.
            with psycopg.connect(self.url, autocommit=True) as conn:
                yield conn
        else:
            conn = sqlite3.connect(self.sqlite_path)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _exec(self, sql: str, params: tuple = ()):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self._conn() as c:
            cur = c.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
            return rows, cur.rowcount

    @staticmethod
    def _dicts(rows, cols):
        return [dict(zip(cols, r)) for r in rows]

    # ---- entries -----------------------------------------------------------
    def add_entry(self, chat_id: int, day: str, kind: str, text: str) -> int:
        status = "open" if kind == "goal" else "done"
        rows, _ = self._exec(
            "INSERT INTO entries (chat_id, day, kind, text, status, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (chat_id, day, kind, text, status, _utcnow()),
        )
        return rows[0][0]

    def update_goal(self, chat_id: int, entry_id: int, status: str, note: str | None) -> bool:
        _, n = self._exec(
            "UPDATE entries SET status = ?, note = ? WHERE id = ? AND chat_id = ? AND kind = 'goal'",
            (status, note, entry_id, chat_id),
        )
        return n > 0

    def list_entries(self, chat_id: int, start_day: str, end_day: str) -> list[dict]:
        cols = ["id", "day", "kind", "text", "status", "note"]
        rows, _ = self._exec(
            f"SELECT {', '.join(cols)} FROM entries WHERE chat_id = ? AND day >= ? AND day <= ? ORDER BY day, id",
            (chat_id, start_day, end_day),
        )
        return self._dicts(rows, cols)

    # ---- memories ----------------------------------------------------------
    def add_memory(self, chat_id: int, fact: str) -> int:
        rows, _ = self._exec(
            "INSERT INTO memories (chat_id, fact, created_at) VALUES (?, ?, ?) RETURNING id", (chat_id, fact, _utcnow())
        )
        return rows[0][0]

    def delete_memory(self, chat_id: int, memory_id: int) -> bool:
        _, n = self._exec("DELETE FROM memories WHERE id = ? AND chat_id = ?", (memory_id, chat_id))
        return n > 0

    def list_memories(self, chat_id: int) -> list[dict]:
        rows, _ = self._exec("SELECT id, fact FROM memories WHERE chat_id = ? ORDER BY id", (chat_id,))
        return self._dicts(rows, ["id", "fact"])

    # ---- follow-ups --------------------------------------------------------
    def add_followup(self, chat_id: int, due_at_utc: str, reason: str) -> int:
        rows, _ = self._exec(
            "INSERT INTO followups (chat_id, due_at, reason, status, created_at) VALUES (?, ?, ?, 'pending', ?) RETURNING id",
            (chat_id, due_at_utc, reason, _utcnow()),
        )
        return rows[0][0]

    def cancel_followup(self, chat_id: int, followup_id: int) -> bool:
        _, n = self._exec(
            "UPDATE followups SET status = 'cancelled' WHERE id = ? AND chat_id = ? AND status = 'pending'",
            (followup_id, chat_id),
        )
        return n > 0

    def pending_followups(self, chat_id: int) -> list[dict]:
        rows, _ = self._exec(
            "SELECT id, due_at, reason FROM followups WHERE chat_id = ? AND status = 'pending' ORDER BY due_at", (chat_id,)
        )
        return self._dicts(rows, ["id", "due_at", "reason"])

    def claim_due_followups(self, now_utc: str) -> list[dict]:
        """Return pending follow-ups that are due and mark them fired, so each fires once."""
        rows, _ = self._exec(
            "UPDATE followups SET status = 'fired' WHERE status = 'pending' AND due_at <= ? RETURNING id, chat_id, due_at, reason",
            (now_utc,),
        )
        return self._dicts(rows, ["id", "chat_id", "due_at", "reason"])
