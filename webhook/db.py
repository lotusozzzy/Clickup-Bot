"""ClickUp webhook event'leri için SQLite katmanı.

DB yolu varsayılan olarak ~/clickup-bot/webhook_events.db.
WEBHOOK_DB_PATH env var'ı override eder (test için kullanışlı).

Şema notları:
- event_id PRIMARY KEY → INSERT OR IGNORE ile aynı event tekrar yazılmaz
  (idempotent: ClickUp aynı event'i retry edebilir)
- changed_at_ms INTEGER → ms epoch (changed_at_ms üzerinden index var)
- raw_payload TEXT → tüm webhook body'si saklanır, parse'ı sonradan değiştirsek
  bile geçmiş event'leri yeniden işleyebiliriz
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "WEBHOOK_DB_PATH",
    os.path.expanduser("~/clickup-bot/webhook_events.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    field           TEXT NOT NULL,
    before_value    TEXT,
    after_value     TEXT,
    user_id         TEXT,
    user_name       TEXT,
    changed_at_ms   INTEGER NOT NULL,
    raw_payload     TEXT NOT NULL,
    received_at_ms  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_time
    ON events(task_id, changed_at_ms);

CREATE INDEX IF NOT EXISTS idx_received
    ON events(received_at_ms);
"""


def init_db(db_path=None):
    path = db_path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_connection(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
