"""SQLite: одно соединение, WAL, ожидание при занятой базе, схема."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    username      TEXT,
    phone         TEXT,
    photo_file_id TEXT,
    created_at    REAL NOT NULL,
    tags          TEXT NOT NULL DEFAULT '[]'      -- JSON-список меток
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(tg_id),
    funnel      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active','cancelled','completed')),
    started_at  REAL NOT NULL,
    finished_at REAL,
    -- последний отправленный шаг-запрос, на который ещё не ответили (ждём контакт или отказ)
    awaiting_step TEXT,
    -- ответ пришёл, пока шаг был в отправке: целевой шаг ставится, когда отправка закончится
    jump_to       TEXT
);
-- страховка от гонок: у пользователя не бывает двух идущих прогонов
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active
    ON runs(user_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    user_id    INTEGER NOT NULL REFERENCES users(tg_id),
    funnel     TEXT NOT NULL,
    step_key   TEXT NOT NULL,
    due_at     REAL NOT NULL,
    status     TEXT NOT NULL CHECK (status IN
               ('pending','processing','sent','cancelled','expired','error')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    REAL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS queue_due ON queue(status, due_at);
-- планируется только следующий шаг: у прогона не бывает двух неотправленных записей
CREATE UNIQUE INDEX IF NOT EXISTS queue_one_open
    ON queue(run_id) WHERE status IN ('pending','processing');

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    user_id INTEGER,
    run_id  INTEGER,
    kind    TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(id DESC);
CREATE INDEX IF NOT EXISTS events_kind_user ON events(kind, user_id);
"""


class Database:
    def __init__(self, path: str):
        # isolation_level=None: транзакциями управляем сами через tx()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """BEGIN IMMEDIATE: берём блокировку записи сразу, а не посреди транзакции."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def log_event(self, now, kind, user_id=None, run_id=None, details=None):
        self.conn.execute(
            "INSERT INTO events(ts, user_id, run_id, kind, details) VALUES (?,?,?,?,?)",
            (now, user_id, run_id, kind, details),
        )

    def close(self):
        self.conn.close()
