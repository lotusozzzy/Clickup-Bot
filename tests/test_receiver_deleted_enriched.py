"""Adım 4 Parça B testleri: receiver __deleted__ enrichment + snapshot cache + comment fetch."""

import json
import os
import sqlite3
import time
from unittest.mock import MagicMock

import pytest


def _query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _write_snapshot(snapshot_path, tasks, saved_at="2026-05-09T12:00:00"):
    """Test snapshot dosyası yaz (daily report'un çıktısını taklit)."""
    payload = {"saved_at": saved_at, "tasks": tasks}
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _mock_response(status_code, json_body=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    return r


def _patch_requests(monkeypatch, receiver, response=None, side_effect=None):
    """receiver.requests.get'i mock'la — receiver başka requests çağrısı yapmıyor."""
    if side_effect is not None:
        mock_get = MagicMock(side_effect=side_effect)
    else:
        mock_get = MagicMock(return_value=response)
    monkeypatch.setattr(receiver.requests, "get", mock_get)
    return mock_get


# ---------------------------------------------------------------------------
# Snapshot hit → __deleted__ + zenginleştirme
# ---------------------------------------------------------------------------

def test_deleted_snapshot_hit_writes_enriched(webhook_env, signed_post, monkeypatch):
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-abc": {
            "name": "Faturayi kontrol et",
            "list_name": "Muhasebe-Mayis",
            "space_name": "Finans",
            "due_date": "1715000000000",
            "url": "https://example.clickup",
            "assignees": ["Ali", "Veli"],
        },
    })
    _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    resp = signed_post({"event": "taskDeleted", "task_id": "task-abc"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kind"] == "deleted"
    assert body["ingested"] == 1

    rows = _query(
        webhook_env["db_path"],
        "SELECT field, before_value, user_id, user_name "
        "FROM events WHERE task_id = ?",
        ("task-abc",),
    )
    assert len(rows) == 1
    field, before_json, user_id, user_name = rows[0]
    assert field == "__deleted__"
    enriched = json.loads(before_json)
    assert enriched["task_name"] == "Faturayi kontrol et"
    assert enriched["list_name"] == "Muhasebe-Mayis"
    assert enriched["space_name"] == "Finans"
    assert enriched["last_due_date"] == "1715000000000"
    assert enriched["assignees"] == ["Ali", "Veli"]
    assert enriched["last_comment_author"] is None  # 404 fetch
    assert enriched["last_comment_text"] is None
    # ClickUp payload'ında "kim sildi" olmadığı için null
    assert user_id is None
    assert user_name is None


# ---------------------------------------------------------------------------
# Snapshot miss → __deleted_raw__ fallback (Adım 1 davranışı korunur)
# ---------------------------------------------------------------------------

def test_deleted_snapshot_miss_falls_back_to_raw(webhook_env, signed_post, monkeypatch):
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "other-task": {
            "name": "X", "list_name": "L", "space_name": "S",
            "due_date": None, "assignees": [],
        },
    })
    # Snapshot miss → comment fetch çağrılmamalı (early return raw save'e)
    mock_get = _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    resp = signed_post({"event": "taskDeleted", "task_id": "missing-task"})
    assert resp.get_json()["kind"] == "deleted_raw"

    rows = _query(
        webhook_env["db_path"],
        "SELECT field FROM events WHERE task_id = ?",
        ("missing-task",),
    )
    assert rows == [("__deleted_raw__",)]
    assert mock_get.call_count == 0


def test_deleted_no_snapshot_file_falls_back_to_raw(webhook_env, signed_post, monkeypatch):
    """Snapshot.json yoksa receiver __deleted_raw__ ile graceful devam etsin."""
    receiver = webhook_env["receiver"]
    assert not os.path.exists(webhook_env["snapshot_path"])
    mock_get = _patch_requests(monkeypatch, receiver, response=_mock_response(200))

    resp = signed_post({"event": "taskDeleted", "task_id": "any-task"})
    assert resp.status_code == 200
    assert resp.get_json()["kind"] == "deleted_raw"

    rows = _query(
        webhook_env["db_path"],
        "SELECT field FROM events",
    )
    assert rows == [("__deleted_raw__",)]
    assert mock_get.call_count == 0


# ---------------------------------------------------------------------------
# Snapshot in-memory cache
# ---------------------------------------------------------------------------

def test_snapshot_cache_reuses_when_mtime_unchanged(webhook_env, signed_post, monkeypatch):
    """Aynı snapshot mtime ile iki taskDeleted → snapshot dosyası 1 kez okunur."""
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-1": {"name": "A", "list_name": "L1", "space_name": "S1",
                   "due_date": None, "assignees": []},
        "task-2": {"name": "B", "list_name": "L1", "space_name": "S1",
                   "due_date": None, "assignees": []},
    })
    _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    open_calls = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        if isinstance(path, str) and path == webhook_env["snapshot_path"]:
            open_calls.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)

    signed_post({"event": "taskDeleted", "task_id": "task-1"})
    signed_post({"event": "taskDeleted", "task_id": "task-2"})

    # 2 event geldi ama snapshot tek kez parse edildi
    assert len(open_calls) == 1


def test_snapshot_cache_invalidates_on_mtime_change(webhook_env, signed_post, monkeypatch):
    """Daily report yeniden yazınca cache invalidate olmalı."""
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-A": {"name": "Old name", "list_name": "L", "space_name": "S",
                   "due_date": None, "assignees": []},
    })
    _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    signed_post({"event": "taskDeleted", "task_id": "task-A"})

    # mtime değişsin diye küçük bir bekleme + farklı içerik
    time.sleep(0.02)
    _write_snapshot(
        webhook_env["snapshot_path"],
        {
            "task-A": {"name": "Old name", "list_name": "L", "space_name": "S",
                       "due_date": None, "assignees": []},
            "task-B": {"name": "Brand new", "list_name": "L2", "space_name": "S2",
                       "due_date": "1716000000000", "assignees": ["Ozan"]},
        },
        saved_at="2026-05-09T13:00:00",
    )

    signed_post({"event": "taskDeleted", "task_id": "task-B"})

    rows = _query(
        webhook_env["db_path"],
        "SELECT task_id, field, before_value FROM events ORDER BY changed_at_ms",
    )
    assert len(rows) == 2
    second = rows[1]
    assert second[0] == "task-B"
    assert second[1] == "__deleted__"
    enriched = json.loads(second[2])
    assert enriched["task_name"] == "Brand new"
    assert enriched["assignees"] == ["Ozan"]
    assert enriched["last_due_date"] == "1716000000000"


# ---------------------------------------------------------------------------
# Comment fetch davranışları
# ---------------------------------------------------------------------------

def test_comment_fetch_200_extracts_latest_author_and_text(webhook_env, signed_post, monkeypatch):
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-X": {"name": "T", "list_name": "L", "space_name": "S",
                   "due_date": None, "assignees": []},
    })
    _patch_requests(
        monkeypatch, receiver,
        response=_mock_response(200, {"comments": [
            {"date": "1700000000000", "user": {"username": "Eski"},
             "comment_text": "Eski yorum"},
            {"date": "1715000000000", "user": {"username": "Yeni"},
             "comment_text": "Son yorum"},
        ]}),
    )

    signed_post({"event": "taskDeleted", "task_id": "task-X"})

    rows = _query(
        webhook_env["db_path"],
        "SELECT before_value FROM events WHERE task_id = ?",
        ("task-X",),
    )
    enriched = json.loads(rows[0][0])
    assert enriched["last_comment_author"] == "Yeni"
    assert enriched["last_comment_text"] == "Son yorum"


def test_comment_fetch_200_handles_comment_parts_array(webhook_env, signed_post, monkeypatch):
    """ClickUp 'comment' alanı array of {text:...} formunda gelebilir."""
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-P": {"name": "T", "list_name": "L", "space_name": "S",
                   "due_date": None, "assignees": []},
    })
    _patch_requests(
        monkeypatch, receiver,
        response=_mock_response(200, {"comments": [{
            "date": "1715000000000",
            "user": {"username": "Mehmet"},
            "comment": [
                {"text": "Hızlı bir "},
                {"text": "merhaba"},
            ],
        }]}),
    )

    signed_post({"event": "taskDeleted", "task_id": "task-P"})

    rows = _query(
        webhook_env["db_path"],
        "SELECT before_value FROM events WHERE task_id = ?",
        ("task-P",),
    )
    enriched = json.loads(rows[0][0])
    assert enriched["last_comment_author"] == "Mehmet"
    assert enriched["last_comment_text"] == "Hızlı bir merhaba"


def test_comment_fetch_timeout_yields_null_does_not_break_ingest(webhook_env, signed_post, monkeypatch):
    """Comment fetch timeout receiver'ı durdurmamalı — null bırak, devam et."""
    import requests as _requests

    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-T": {"name": "T", "list_name": "L", "space_name": "S",
                   "due_date": None, "assignees": []},
    })
    _patch_requests(monkeypatch, receiver, side_effect=_requests.Timeout("timeout"))

    resp = signed_post({"event": "taskDeleted", "task_id": "task-T"})
    assert resp.status_code == 200
    assert resp.get_json()["kind"] == "deleted"

    rows = _query(
        webhook_env["db_path"],
        "SELECT before_value FROM events WHERE task_id = ?",
        ("task-T",),
    )
    enriched = json.loads(rows[0][0])
    assert enriched["last_comment_author"] is None
    assert enriched["last_comment_text"] is None


def test_comment_fetch_skipped_when_token_missing(webhook_env, signed_post, monkeypatch):
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-NT": {"name": "T", "list_name": "L", "space_name": "S",
                    "due_date": None, "assignees": []},
    })
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    mock_get = _patch_requests(monkeypatch, receiver, response=_mock_response(200))

    resp = signed_post({"event": "taskDeleted", "task_id": "task-NT"})
    assert resp.get_json()["kind"] == "deleted"
    assert mock_get.call_count == 0  # token yok → fetch atlandı

    rows = _query(
        webhook_env["db_path"],
        "SELECT before_value FROM events WHERE task_id = ?",
        ("task-NT",),
    )
    enriched = json.loads(rows[0][0])
    assert enriched["last_comment_author"] is None


def test_comment_fetch_404_yields_null(webhook_env, signed_post, monkeypatch):
    """Silinmiş task için 404 — beklenen, null bırakılmalı."""
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-G": {"name": "T", "list_name": "L", "space_name": "S",
                   "due_date": None, "assignees": []},
    })
    _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    signed_post({"event": "taskDeleted", "task_id": "task-G"})

    rows = _query(
        webhook_env["db_path"],
        "SELECT before_value FROM events WHERE task_id = ?",
        ("task-G",),
    )
    enriched = json.loads(rows[0][0])
    assert enriched["last_comment_author"] is None
    assert enriched["last_comment_text"] is None


# ---------------------------------------------------------------------------
# Idempotency + regression
# ---------------------------------------------------------------------------

def test_idempotent_retry_writes_once(webhook_env, signed_post, monkeypatch):
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-DUP": {"name": "T", "list_name": "L", "space_name": "S",
                     "due_date": None, "assignees": []},
    })
    _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    payload = {
        "event": "taskDeleted", "task_id": "task-DUP", "id": "delete-evt-X",
    }
    r1 = signed_post(payload)
    r2 = signed_post(payload)

    assert r1.get_json()["ingested"] == 1
    assert r1.get_json()["kind"] == "deleted"
    assert r2.get_json()["ingested"] == 0
    assert r2.get_json()["kind"] == "deleted_dup"

    rows = _query(
        webhook_env["db_path"],
        "SELECT COUNT(*) FROM events WHERE field = ?",
        ("__deleted__",),
    )
    assert rows[0][0] == 1


def test_existing_due_date_flow_unchanged(webhook_env, signed_post):
    """Regression: taskUpdated/due_date akışı aynen çalışmalı."""
    payload = {
        "event": "taskUpdated",
        "task_id": "regression-task",
        "history_items": [{
            "id": "h1",
            "field": "due_date",
            "before": "1700000000000",
            "after": "1701000000000",
            "date": "1700500000000",
            "user": {"id": 1, "username": "alice"},
        }],
    }
    resp = signed_post(payload)
    assert resp.status_code == 200
    assert resp.get_json()["ingested"] == 1

    rows = _query(
        webhook_env["db_path"],
        "SELECT field FROM events WHERE task_id = ?",
        ("regression-task",),
    )
    assert rows == [("due_date",)]


def test_unicode_task_name_round_trips(webhook_env, signed_post, monkeypatch):
    """JSON serialize ensure_ascii=False, Türkçe karakterler bozulmasın."""
    receiver = webhook_env["receiver"]
    _write_snapshot(webhook_env["snapshot_path"], {
        "task-tr": {"name": "Müşteri görüşmesi yapılacak",
                    "list_name": "Çağrı Merkezi", "space_name": "Müşteri",
                    "due_date": None, "assignees": ["Şule Öztürk"]},
    })
    _patch_requests(monkeypatch, receiver, response=_mock_response(404))

    signed_post({"event": "taskDeleted", "task_id": "task-tr"})

    rows = _query(
        webhook_env["db_path"],
        "SELECT before_value FROM events WHERE task_id = ?",
        ("task-tr",),
    )
    enriched = json.loads(rows[0][0])
    assert enriched["task_name"] == "Müşteri görüşmesi yapılacak"
    assert enriched["space_name"] == "Müşteri"
    assert enriched["assignees"] == ["Şule Öztürk"]
