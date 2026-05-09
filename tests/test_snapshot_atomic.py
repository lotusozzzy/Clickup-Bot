"""Adım 4 Parça A testleri: snapshot save atomic davranışı + assignees ekleme.

clickup_due_report.save_snapshot artık tmp+rename yapıyor (race-safe için)
ve build_current_snapshot artık task assignees'i snapshot'a yazıyor.
"""

import json
import os
import sys

import pytest


def _import_due_report(monkeypatch, tmp_path):
    """clickup_due_report'u taze import edip SNAPSHOT_FILE'ı tmp'e patch et."""
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("SMTP_PASSWORD", "test-smtp-pw")
    monkeypatch.setenv("CLICKUP_WORKSPACE_ID", "9999999")

    for mod_name in list(sys.modules):
        if (
            mod_name == "clickup_bot"
            or mod_name == "clickup_due_report"
            or mod_name.startswith("webhook")
        ):
            del sys.modules[mod_name]

    import clickup_due_report

    snap = tmp_path / "due_date_snapshot.json"
    monkeypatch.setattr(clickup_due_report, "SNAPSHOT_FILE", str(snap))
    return clickup_due_report, str(snap)


def test_save_snapshot_creates_file_and_no_tmp_leftover(monkeypatch, tmp_path):
    cdr, snap_path = _import_due_report(monkeypatch, tmp_path)
    cdr.save_snapshot({
        "t1": {"name": "T", "list_name": "L", "space_name": "S",
               "due_date": None, "url": "u", "assignees": ["a"]},
    })
    assert os.path.exists(snap_path)
    assert not os.path.exists(snap_path + ".tmp"), \
        "tmp dosya os.replace sonrası kalmamalı"


def test_save_snapshot_round_trip_preserves_assignees(monkeypatch, tmp_path):
    cdr, snap_path = _import_due_report(monkeypatch, tmp_path)
    cdr.save_snapshot({
        "tA": {"name": "Alpha", "list_name": "L1", "space_name": "S1",
               "due_date": "1700000000000", "url": "u1",
               "assignees": ["Ali Veli", "Selin Ay"]},
        "tB": {"name": "Beta", "list_name": "L2", "space_name": "S2",
               "due_date": None, "url": "u2", "assignees": []},
    })
    with open(snap_path, encoding="utf-8") as f:
        data = json.load(f)
    assert "saved_at" in data
    tasks = data["tasks"]
    assert tasks["tA"]["assignees"] == ["Ali Veli", "Selin Ay"]
    assert tasks["tB"]["assignees"] == []
    assert tasks["tA"]["due_date"] == "1700000000000"


def test_save_snapshot_overwrites_previous_run(monkeypatch, tmp_path):
    cdr, snap_path = _import_due_report(monkeypatch, tmp_path)
    cdr.save_snapshot({
        "old": {"name": "old", "list_name": "L", "space_name": "S",
                "due_date": None, "url": "", "assignees": []},
    })
    cdr.save_snapshot({
        "new": {"name": "new", "list_name": "L", "space_name": "S",
                "due_date": None, "url": "", "assignees": []},
    })
    with open(snap_path, encoding="utf-8") as f:
        data = json.load(f)
    assert "new" in data["tasks"]
    assert "old" not in data["tasks"]


def test_save_snapshot_uses_atomic_rename(monkeypatch, tmp_path):
    """os.replace çağrısı yapılıyor mu, src .tmp ile bitiyor mu?"""
    cdr, snap_path = _import_due_report(monkeypatch, tmp_path)
    rename_calls = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        rename_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)
    cdr.save_snapshot({
        "x": {"name": "x", "list_name": "L", "space_name": "S",
              "due_date": None, "url": "", "assignees": []},
    })
    assert len(rename_calls) == 1
    src, dst = rename_calls[0]
    assert src.endswith(".tmp")
    assert dst == snap_path


def test_save_snapshot_calls_fsync(monkeypatch, tmp_path):
    """fsync gerçekten çağrılıyor mu (durability garantisi)."""
    cdr, _ = _import_due_report(monkeypatch, tmp_path)
    fsync_calls = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    cdr.save_snapshot({
        "x": {"name": "x", "list_name": "L", "space_name": "S",
              "due_date": None, "url": "", "assignees": []},
    })
    assert len(fsync_calls) == 1


def test_build_current_snapshot_extracts_assignees(monkeypatch, tmp_path):
    """Mock'lu integration: assignees alanı task objesinden snapshot'a kopyalanıyor mu."""
    cdr, _ = _import_due_report(monkeypatch, tmp_path)

    monkeypatch.setattr(
        cdr, "get_target_spaces",
        lambda session: [{"id": "sp1", "name": "Space1"}],
    )
    monkeypatch.setattr(
        cdr, "get_all_lists_in_space",
        lambda session, space_id, space_name: [
            {"id": "list1", "name": "List1", "space": space_name},
        ],
    )

    fake_tasks = [
        {
            "id": "t1",
            "name": "Test Task",
            "url": "https://example",
            "due_date": "1715000000000",
            "assignees": [
                {"username": "Alice", "id": 1},
                {"username": "Bob", "id": 2},
            ],
        },
        {
            "id": "t2",
            "name": "No assignees",
            "url": "https://example",
            "due_date": None,
            "assignees": [],
        },
        {
            "id": "t3",
            "name": "Email-only assignee",
            "due_date": None,
            "assignees": [{"email": "no-name@example.com"}],
        },
        {
            "id": "t4",
            "name": "Bogus assignee shape",
            "due_date": None,
            "assignees": ["string-not-dict", {"username": ""}, {"email": None}],
        },
    ]
    monkeypatch.setattr(
        cdr, "fetch_open_tasks",
        lambda session, list_id, list_name: fake_tasks,
    )

    snapshot, atlanan = cdr.build_current_snapshot(session=None)

    assert atlanan == []
    assert snapshot["t1"]["assignees"] == ["Alice", "Bob"]
    assert snapshot["t2"]["assignees"] == []
    assert snapshot["t3"]["assignees"] == ["no-name@example.com"]
    # Bogus shape'ler filtrelenmeli
    assert snapshot["t4"]["assignees"] == []
    # Mevcut alanlar korunuyor (regression check)
    assert snapshot["t1"]["name"] == "Test Task"
    assert snapshot["t1"]["list_name"] == "List1"
    assert snapshot["t1"]["space_name"] == "Space1"
    assert snapshot["t1"]["due_date"] == "1715000000000"
