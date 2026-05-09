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


# SQLite parametre limiti (varsayılan 999, defansif olarak 500 kullan)
_BATCH_SIZE = 500


def get_deletions_in_window(since_ms, db_path=None):
    """Pencere içinde gelen silme event'lerini DESC sıralı döndür.

    field IN ('__deleted__', '__deleted_raw__') — daily report her ikisini de
    aynı sheet'te gösteriyor. raw satırlarda before_value None'dur (snapshot
    miss durumu); consumer JSON parse'ı kendi yapar.

    Parametreler:
        since_ms: int — bu epoch_ms değerinden SONRAKİ silmeler döner.
                        None gelirse boş liste (önceki snapshot yok demek).

    Dönüş:
        list[dict]: {task_id, field, before_value, changed_at_ms}
    """
    if since_ms is None:
        return []
    sql = """
        SELECT task_id, field, before_value, changed_at_ms
        FROM events
        WHERE field IN ('__deleted__', '__deleted_raw__')
          AND changed_at_ms > ?
        ORDER BY changed_at_ms DESC
    """
    rows = []
    with get_connection(db_path) as conn:
        for r in conn.execute(sql, (since_ms,)):
            rows.append({
                "task_id": r[0],
                "field": r[1],
                "before_value": r[2],
                "changed_at_ms": r[3],
            })
    return rows


def get_due_date_events_batch(task_ids, since_ms, db_path=None):
    """Bir grup task için 'due_date' değişikliği event'lerini topluca çek.

    Parametreler:
        task_ids: iterable (list/set/tuple) - sorgulanacak task ID'leri
        since_ms: int     - bu epoch_ms değerinden SONRAKİ event'ler döner
                            (snapshot saved_at ms cinsinden ekran)

    Dönüş:
        {task_id: [event_dict, ...]}  — her task için en yeni event en başta.
        İstenen task_id'lerden hiç event olmayanlar boş liste ile gelir.

    Tek bir IN clause yerine 500'lü chunk'lar halinde sorgular
    (SQLITE_MAX_VARIABLE_NUMBER limitine yaklaşmamak için).
    """
    task_ids = [tid for tid in task_ids if tid]
    by_task = {tid: [] for tid in task_ids}
    if not task_ids:
        return by_task

    sql_template = """
        SELECT task_id, user_id, user_name, before_value, after_value, changed_at_ms
        FROM events
        WHERE field = 'due_date'
          AND changed_at_ms > ?
          AND task_id IN ({placeholders})
        ORDER BY changed_at_ms DESC
    """
    with get_connection(db_path) as conn:
        for i in range(0, len(task_ids), _BATCH_SIZE):
            chunk = task_ids[i : i + _BATCH_SIZE]
            placeholders = ",".join(["?"] * len(chunk))
            sql = sql_template.format(placeholders=placeholders)
            params = [since_ms] + chunk
            for row in conn.execute(sql, params):
                tid = row[0]
                by_task[tid].append({
                    "user_id": row[1],
                    "user_name": row[2] or "",
                    "before_value": row[3],
                    "after_value": row[4],
                    "changed_at_ms": row[5],
                })
    return by_task
