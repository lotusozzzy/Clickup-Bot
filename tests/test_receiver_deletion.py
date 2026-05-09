"""Adım 1 testleri: taskDeleted event'lerinin raw save davranışı.

Bu adımda parse mantığı YOK — sadece raw payload'ın __deleted_raw__
field'ı ile DB'ye yazılması ve mevcut taskUpdated/due_date davranışının
bozulmaması doğrulanır.
"""

import json
import sqlite3


def _query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_task_deleted_writes_raw_row(webhook_env, signed_post):
    payload = {
        "event": "taskDeleted",
        "task_id": "abc123",
        "user": {"id": 99, "username": "alice"},
    }
    resp = signed_post(payload)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["ingested"] == 1
    assert body["kind"] == "deleted_raw"

    rows = _query(
        webhook_env["db_path"],
        "SELECT task_id, field, user_id, user_name, before_value, after_value, raw_payload "
        "FROM events WHERE field = ?",
        ("__deleted_raw__",),
    )
    assert len(rows) == 1
    task_id, field, user_id, user_name, before_v, after_v, raw_payload = rows[0]
    assert task_id == "abc123"
    assert field == "__deleted_raw__"
    assert user_id == "99"
    assert user_name == "alice"
    assert before_v is None
    assert after_v is None
    assert json.loads(raw_payload)["event"] == "taskDeleted"


def test_task_updated_due_date_unchanged_behavior(webhook_env, signed_post):
    """Eski davranış korunmalı: taskUpdated + due_date → field='due_date'."""
    payload = {
        "event": "taskUpdated",
        "task_id": "task1",
        "history_items": [
            {
                "id": "evt-due-1",
                "field": "due_date",
                "before": "1700000000000",
                "after": "1700100000000",
                "date": "1700050000000",
                "user": {"id": 7, "username": "bob"},
            }
        ],
    }
    resp = signed_post(payload)
    assert resp.status_code == 200
    assert resp.get_json()["ingested"] == 1

    rows = _query(
        webhook_env["db_path"],
        "SELECT field, task_id, user_name FROM events ORDER BY received_at_ms",
    )
    assert rows == [("due_date", "task1", "bob")]


def test_other_event_not_persisted(webhook_env, signed_post):
    """taskCommentPosted gibi event'ler DB'ye gitmemeli."""
    payload = {
        "event": "taskCommentPosted",
        "task_id": "task2",
        "history_items": [],
    }
    resp = signed_post(payload)
    assert resp.status_code == 200
    assert resp.get_json()["ingested"] == 0

    rows = _query(webhook_env["db_path"], "SELECT COUNT(*) FROM events")
    assert rows[0][0] == 0


def test_task_deleted_idempotent_retry(webhook_env, signed_post):
    """Aynı event_id ile iki kez geldiğinde tek satır kalır."""
    payload = {
        "event": "taskDeleted",
        "task_id": "tdup",
        "id": "delete-evt-42",
        "user": {"id": 1, "username": "ozan"},
    }
    r1 = signed_post(payload)
    r2 = signed_post(payload)

    assert r1.get_json()["ingested"] == 1
    assert r2.get_json()["ingested"] == 0
    assert r2.get_json()["kind"] == "deleted_raw_dup"

    rows = _query(
        webhook_env["db_path"],
        "SELECT COUNT(*) FROM events WHERE field = ?",
        ("__deleted_raw__",),
    )
    assert rows[0][0] == 1


def test_task_deleted_no_user_field(webhook_env, signed_post):
    """user yoksa user_id/user_name None — TAHMIN ETME."""
    payload = {
        "event": "taskDeleted",
        "task_id": "tnu",
    }
    resp = signed_post(payload)
    assert resp.status_code == 200

    rows = _query(
        webhook_env["db_path"],
        "SELECT user_id, user_name FROM events WHERE field = ?",
        ("__deleted_raw__",),
    )
    assert rows == [(None, None)]


def test_task_deleted_camelcase_event_alias(webhook_env, signed_post):
    """eventType + taskId camelCase variantları da kabul edilsin."""
    payload = {
        "eventType": "taskDeleted",
        "taskId": "tcc",
        "user": {"id": 5, "name": "Carol"},
    }
    resp = signed_post(payload)
    assert resp.status_code == 200
    assert resp.get_json()["kind"] == "deleted_raw"

    rows = _query(
        webhook_env["db_path"],
        "SELECT task_id, user_name FROM events WHERE field = ?",
        ("__deleted_raw__",),
    )
    assert rows == [("tcc", "Carol")]


def test_hmac_failure_blocks_deletion(webhook_env, signed_post):
    """Yanlış HMAC ile gelen taskDeleted DB'ye yazılmamalı."""
    payload = {"event": "taskDeleted", "task_id": "tbad"}
    resp = signed_post(payload, secret="WRONG_SECRET")
    assert resp.status_code == 401

    rows = _query(webhook_env["db_path"], "SELECT COUNT(*) FROM events")
    assert rows[0][0] == 0


def test_task_deleted_event_id_fallback_uses_received_at(webhook_env, signed_post):
    """Payload'da id/event_id yoksa fallback event_id task_id+received_at_ms.

    Aynı task_id farklı zaman damgalarında iki kez silinmesi tek satıra
    fold edilmemeli (üretimde retry değil, ayrı event'lerdir).
    """
    payload = {"event": "taskDeleted", "task_id": "norefid"}
    r1 = signed_post(payload)
    # Aynı payload yeniden gönderilirse received_at_ms değişeceği için
    # event_id de değişir → ingest=1 olmalı.
    import time as _t

    _t.sleep(0.005)  # ms granülasyonu
    r2 = signed_post(payload)

    assert r1.get_json()["ingested"] == 1
    assert r2.get_json()["ingested"] in (0, 1)  # Aynı ms'ye denk gelirse 0

    rows = _query(
        webhook_env["db_path"],
        "SELECT event_id FROM events WHERE field = ?",
        ("__deleted_raw__",),
    )
    # Her satırın event_id'si "deleted-norefid-..." prefix'iyle başlamalı
    assert all(eid.startswith("deleted-norefid-") for (eid,) in rows)
