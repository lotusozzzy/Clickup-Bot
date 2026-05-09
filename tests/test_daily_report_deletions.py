"""Adım 4 Parça C: daily report 'Silinen Task'lar' sheet + mail disclaimer.

Kapsam:
  - webhook.db.get_deletions_in_window (yeni fonksiyon)
  - clickup_due_report._parse_deletion_row (raw + enriched + graceful parse)
  - write_excel 4. sheet ("Silinen Task'lar") — boş/dolu/partial
  - send_mail body — silindi liste satırı ve disclaimer'ın varlığı
"""

import json
import os
import smtplib
import sqlite3
import sys

import pytest


def _import_modules(monkeypatch, tmp_path):
    """clickup_bot env requirement'larını sahte değerlerle karşılayıp
    clickup_due_report + webhook.db modüllerini taze import eder.
    Tmp DB ve snapshot path'leri set eder.
    """
    db_file = tmp_path / "deletions_test.db"
    snap_file = tmp_path / "snapshot.json"
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(db_file))
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token")
    monkeypatch.setenv("SMTP_PASSWORD", "test-pw")
    monkeypatch.setenv("CLICKUP_WORKSPACE_ID", "9999999")
    monkeypatch.setenv("KEEP_EXCEL_ON_DISK", "true")

    for mod_name in list(sys.modules):
        if (
            mod_name == "clickup_bot"
            or mod_name == "clickup_due_report"
            or mod_name.startswith("webhook")
        ):
            del sys.modules[mod_name]

    from webhook import db
    db.init_db(str(db_file))

    import clickup_due_report as cdr
    monkeypatch.setattr(cdr, "SNAPSHOT_FILE", str(snap_file))

    return cdr, db, str(db_file)


def _insert_event(db_path, **kw):
    """events tablosuna tek satır INSERT — defaults ile."""
    defaults = {
        "event_id": kw.get("event_id"),
        "task_id": kw.get("task_id", "t1"),
        "field": kw["field"],
        "before_value": kw.get("before_value"),
        "after_value": kw.get("after_value"),
        "user_id": kw.get("user_id"),
        "user_name": kw.get("user_name"),
        "changed_at_ms": kw["changed_at_ms"],
        "raw_payload": kw.get("raw_payload", "{}"),
        "received_at_ms": kw.get("received_at_ms", kw["changed_at_ms"]),
    }
    if not defaults["event_id"]:
        defaults["event_id"] = f"{defaults['task_id']}-{defaults['changed_at_ms']}-{defaults['field']}"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO events (
                event_id, task_id, field, before_value, after_value,
                user_id, user_name, changed_at_ms, raw_payload, received_at_ms
            ) VALUES (
                :event_id, :task_id, :field, :before_value, :after_value,
                :user_id, :user_name, :changed_at_ms, :raw_payload, :received_at_ms
            )
            """,
            defaults,
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# webhook.db.get_deletions_in_window
# ---------------------------------------------------------------------------

def test_get_deletions_returns_rows_after_since(monkeypatch, tmp_path):
    cdr, db, db_path = _import_modules(monkeypatch, tmp_path)
    _insert_event(db_path, task_id="old", field="__deleted__",
                  before_value=json.dumps({"task_name": "Eski"}),
                  changed_at_ms=1_000_000)
    _insert_event(db_path, task_id="new", field="__deleted__",
                  before_value=json.dumps({"task_name": "Yeni"}),
                  changed_at_ms=2_000_000)

    rows = db.get_deletions_in_window(since_ms=1_500_000, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["task_id"] == "new"
    assert rows[0]["field"] == "__deleted__"
    assert json.loads(rows[0]["before_value"])["task_name"] == "Yeni"


def test_get_deletions_includes_both_field_types(monkeypatch, tmp_path):
    cdr, db, db_path = _import_modules(monkeypatch, tmp_path)
    _insert_event(db_path, task_id="enriched", field="__deleted__",
                  before_value=json.dumps({"task_name": "X"}),
                  changed_at_ms=2_000_000)
    _insert_event(db_path, task_id="raw", field="__deleted_raw__",
                  before_value=None,
                  changed_at_ms=3_000_000)
    # due_date event, dahil olmamalı
    _insert_event(db_path, task_id="dd", field="due_date",
                  before_value="100", after_value="200",
                  changed_at_ms=2_500_000)

    rows = db.get_deletions_in_window(since_ms=1_000_000, db_path=db_path)
    fields = sorted(r["field"] for r in rows)
    assert fields == ["__deleted__", "__deleted_raw__"]
    # DESC sıralı
    assert rows[0]["task_id"] == "raw"
    assert rows[1]["task_id"] == "enriched"


def test_get_deletions_excludes_old_rows(monkeypatch, tmp_path):
    cdr, db, db_path = _import_modules(monkeypatch, tmp_path)
    _insert_event(db_path, task_id="t1", field="__deleted__",
                  before_value=json.dumps({"task_name": "X"}),
                  changed_at_ms=1_000_000)
    rows = db.get_deletions_in_window(since_ms=2_000_000, db_path=db_path)
    assert rows == []


def test_get_deletions_none_since_returns_empty(monkeypatch, tmp_path):
    cdr, db, db_path = _import_modules(monkeypatch, tmp_path)
    _insert_event(db_path, task_id="t1", field="__deleted__",
                  before_value=json.dumps({"task_name": "X"}),
                  changed_at_ms=1_000_000)
    assert db.get_deletions_in_window(since_ms=None, db_path=db_path) == []


# ---------------------------------------------------------------------------
# _parse_deletion_row
# ---------------------------------------------------------------------------

def test_parse_deletion_row_enriched_full(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    row = {
        "task_id": "abc",
        "field": "__deleted__",
        "before_value": json.dumps({
            "task_name": "Faturayı kontrol et",
            "list_name": "Muhasebe",
            "space_name": "Finans",
            "last_due_date": "1715000000000",  # 06.05.2024 14:53 TR
            "assignees": ["Ali", "Veli"],
            "last_comment_author": "Mehmet",
            "last_comment_text": "Hızlı bir hatırlatma",
        }),
        "changed_at_ms": 1_715_500_000_000,
    }
    parsed = cdr._parse_deletion_row(row)
    assert parsed["task_name"] == "Faturayı kontrol et"
    assert parsed["space_name"] == "Finans"
    assert parsed["list_name"] == "Muhasebe"
    # last_due_date dd.mm.yyyy formatına çevrilir
    assert "." in parsed["last_due_date"]
    assert parsed["assignees_str"] == "Ali, Veli"
    assert parsed["last_comment_author"] == "Mehmet"
    assert parsed["last_comment_text"] == "Hızlı bir hatırlatma"
    # Silinme zamanı dd.mm.yyyy HH:MM
    assert ":" in parsed["deleted_at"]
    assert parsed["task_id"] == "abc"


def test_parse_deletion_row_raw_partial(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    row = {
        "task_id": "raw-task",
        "field": "__deleted_raw__",
        "before_value": None,
        "changed_at_ms": 1_715_500_000_000,
    }
    parsed = cdr._parse_deletion_row(row)
    assert parsed["task_id"] == "raw-task"
    assert parsed["task_name"] == "(snapshot dışı)"
    assert parsed["space_name"] == ""
    assert parsed["list_name"] == ""
    assert parsed["last_due_date"] == ""
    assert parsed["assignees_str"] == ""
    assert parsed["last_comment_author"] == ""
    assert parsed["last_comment_text"] == ""
    assert ":" in parsed["deleted_at"]


def test_parse_deletion_row_invalid_json_graceful(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    row = {
        "task_id": "broken",
        "field": "__deleted__",
        "before_value": "{not json at all",
        "changed_at_ms": 1_715_500_000_000,
    }
    parsed = cdr._parse_deletion_row(row)
    assert parsed["task_name"] == "(snapshot dışı)"
    assert parsed["task_id"] == "broken"


def test_parse_deletion_row_truncates_long_comment(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    long_text = "x" * 1000
    row = {
        "task_id": "lc",
        "field": "__deleted__",
        "before_value": json.dumps({
            "task_name": "T", "list_name": "L", "space_name": "S",
            "last_due_date": None, "assignees": [],
            "last_comment_author": "A",
            "last_comment_text": long_text,
        }),
        "changed_at_ms": 1_715_500_000_000,
    }
    parsed = cdr._parse_deletion_row(row)
    assert len(parsed["last_comment_text"]) == 500
    assert parsed["last_comment_text"].endswith("...")


def test_parse_deletion_row_empty_assignees_list(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    row = {
        "task_id": "e",
        "field": "__deleted__",
        "before_value": json.dumps({
            "task_name": "T", "list_name": "L", "space_name": "S",
            "last_due_date": None, "assignees": [],
            "last_comment_author": None, "last_comment_text": None,
        }),
        "changed_at_ms": 1_715_500_000_000,
    }
    parsed = cdr._parse_deletion_row(row)
    assert parsed["assignees_str"] == ""
    assert parsed["last_comment_author"] == ""
    assert parsed["last_comment_text"] == ""


# ---------------------------------------------------------------------------
# write_excel — 4. sheet "Silinen Task'lar"
# ---------------------------------------------------------------------------

def _open_excel(path):
    import openpyxl
    return openpyxl.load_workbook(path)


def test_write_excel_creates_silinen_sheet_with_data(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    deletions = [
        {
            "field": "__deleted__",
            "task_id": "abc",
            "task_name": "Faturayı kontrol et",
            "space_name": "Finans",
            "list_name": "Muhasebe",
            "last_due_date": "06.05.2024",
            "assignees_str": "Ali, Veli",
            "last_comment_author": "Mehmet",
            "last_comment_text": "Hızlı bir hatırlatma",
            "deleted_at": "09.05.2026 18:00",
        },
    ]
    dosya = cdr.write_excel(
        changed_with=[], changed_no=[], removed=[],
        atlanan_listeler=None, deletions=deletions,
    )
    wb = _open_excel(dosya)
    assert "Silinen Task'lar" in wb.sheetnames
    ws = wb["Silinen Task'lar"]
    headers = [c.value for c in ws[1]]
    assert headers == [
        "Task İsmi", "Space", "Liste",
        "Son Bilinen Tarih", "Atayanlar",
        "Son Yorumlayan", "Son Yorum",
        "Silinme Zamanı", "Task ID",
    ]
    # Veri satırı
    assert ws.cell(row=2, column=1).value == "Faturayı kontrol et"
    assert ws.cell(row=2, column=2).value == "Finans"
    assert ws.cell(row=2, column=5).value == "Ali, Veli"
    assert ws.cell(row=2, column=8).value == "09.05.2026 18:00"
    assert ws.cell(row=2, column=9).value == "abc"


def test_write_excel_silinen_sheet_empty_shows_placeholder(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    dosya = cdr.write_excel(
        changed_with=[], changed_no=[], removed=[],
        atlanan_listeler=None, deletions=[],
    )
    wb = _open_excel(dosya)
    ws = wb["Silinen Task'lar"]
    # _add_sheet boşken "(Bu kategoride değişiklik yok)" satırı koyar
    assert ws.cell(row=2, column=1).value == "(Bu kategoride değişiklik yok)"


def test_write_excel_silinen_sheet_partial_for_raw(monkeypatch, tmp_path):
    """__deleted_raw__ satırı: Task ID + Silinme Zamanı dolu, gerisi boş."""
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    deletions = [
        {
            "field": "__deleted_raw__",
            "task_id": "raw-only",
            "task_name": "(snapshot dışı)",
            "space_name": "",
            "list_name": "",
            "last_due_date": "",
            "assignees_str": "",
            "last_comment_author": "",
            "last_comment_text": "",
            "deleted_at": "09.05.2026 17:50",
        },
    ]
    dosya = cdr.write_excel(
        changed_with=[], changed_no=[], removed=[],
        atlanan_listeler=None, deletions=deletions,
    )
    ws = _open_excel(dosya)["Silinen Task'lar"]
    assert ws.cell(row=2, column=1).value == "(snapshot dışı)"
    # openpyxl boş string'i None olarak yazıyor — falsy kontrol yap
    assert not ws.cell(row=2, column=2).value
    assert not ws.cell(row=2, column=3).value
    assert ws.cell(row=2, column=8).value == "09.05.2026 17:50"
    assert ws.cell(row=2, column=9).value == "raw-only"


def test_write_excel_existing_three_sheets_unchanged(monkeypatch, tmp_path):
    """Regression: mevcut 3 sheet aynen var, sıralama korunmuş."""
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    dosya = cdr.write_excel(
        changed_with=[], changed_no=[], removed=[],
        atlanan_listeler=None, deletions=[],
    )
    wb = _open_excel(dosya)
    assert wb.sheetnames == [
        "Tarih Değişti + Yorum",
        "Tarih Değişti - Yorum Yok",
        "Tarih Kaldırıldı",
        "Silinen Task'lar",
    ]


# ---------------------------------------------------------------------------
# send_mail — body içeriği
# ---------------------------------------------------------------------------

class _FakeSMTP:
    """smtplib.SMTP_SSL mock'u — gönderilen mesajı yakalar."""

    captured = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, *args, **kwargs):
        pass

    def send_message(self, msg):
        _FakeSMTP.captured.append(msg)


def _capture_sent_body(monkeypatch, cdr, summary, dosya=None):
    _FakeSMTP.captured = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(cdr, "smtplib", smtplib)
    cdr.send_mail(
        dosya,
        me_info={"username": "Ozan", "email": "ozan@example.com"},
        summary=summary,
        is_first_run=False,
    )
    assert len(_FakeSMTP.captured) == 1
    msg = _FakeSMTP.captured[0]
    # MIMEMultipart ilk parça MIMEText (body); base64 encoded payload'ı decode et
    body_part = msg.get_payload()[0]
    return body_part.get_payload(decode=True).decode("utf-8")


def test_send_mail_includes_silindi_count_and_disclaimer(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    summary = {
        "degisti_yorumlu": 0,
        "degisti_yorumsuz": 0,
        "kaldirildi": 0,
        "silindi": 3,
        "deletions_enriched": 2,
        "atlanan": 0,
        "metrics": {"total_diff": 0, "filtered_self_only": 0,
                    "verified": 0, "no_events": 0},
    }
    body = _capture_sent_body(monkeypatch, cdr, summary)
    assert "<li><b>Silindi:</b> 3</li>" in body
    assert "non-Enterprise" in body
    assert "Trash" in body or "geri yüklemek" in body or "geri yükleyebilirsiniz" in body


def test_send_mail_no_disclaimer_when_zero_deletions(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    summary = {
        "degisti_yorumlu": 5,
        "degisti_yorumsuz": 2,
        "kaldirildi": 1,
        "silindi": 0,
        "deletions_enriched": 0,
        "atlanan": 0,
        "metrics": {"total_diff": 8, "filtered_self_only": 0,
                    "verified": 8, "no_events": 0},
    }
    body = _capture_sent_body(monkeypatch, cdr, summary)
    assert "<li><b>Silindi:</b> 0</li>" in body
    assert "non-Enterprise" not in body  # disclaimer gizli


def test_send_mail_metrics_footer_includes_deletions(monkeypatch, tmp_path):
    cdr, _, _ = _import_modules(monkeypatch, tmp_path)
    summary = {
        "degisti_yorumlu": 0, "degisti_yorumsuz": 0, "kaldirildi": 0,
        "silindi": 4, "deletions_enriched": 3,
        "atlanan": 0,
        "metrics": {"total_diff": 0, "filtered_self_only": 0,
                    "verified": 0, "no_events": 0},
    }
    body = _capture_sent_body(monkeypatch, cdr, summary)
    assert "deletions=4" in body
    assert "deletions_enriched=3" in body
