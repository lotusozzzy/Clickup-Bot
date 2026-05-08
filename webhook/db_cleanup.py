#!/usr/bin/env python3
"""SQLite event store - 30 günden eski kayıtları sil + VACUUM.

Haftalık cron (Pazar 03:00).

Kullanım:
    python3 webhook/db_cleanup.py            # gerçek temizlik
    python3 webhook/db_cleanup.py --dry-run  # sadece sayım
"""

import datetime
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook._envloader import ensure_env  # noqa: E402

ensure_env()

from webhook.db import DB_PATH, get_connection  # noqa: E402

RETENTION_DAYS = int(os.environ.get("WEBHOOK_RETENTION_DAYS", "30"))


def _ts():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _filesize(p):
    try:
        return Path(p).stat().st_size
    except FileNotFoundError:
        return 0


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    cutoff_ms = int(time.time() * 1000) - RETENTION_DAYS * 86400 * 1000
    cutoff_iso = datetime.datetime.fromtimestamp(cutoff_ms / 1000).isoformat(timespec="seconds")

    print(f"{_ts()} db_cleanup başlıyor ({'dry-run' if dry_run else 'live'})")
    print(f"  DB        : {DB_PATH}")
    print(f"  retention : {RETENTION_DAYS} gün")
    print(f"  cutoff_ms : {cutoff_ms} ({cutoff_iso})")

    if not Path(DB_PATH).exists():
        print(f"  ⚠️ DB dosyası yok, çıkılıyor.")
        return 0

    size_before = _filesize(DB_PATH)
    with get_connection() as conn:
        n_total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        n_old = conn.execute(
            "SELECT COUNT(*) FROM events WHERE received_at_ms < ?",
            (cutoff_ms,),
        ).fetchone()[0]

    print(f"  toplam    : {n_total}")
    print(f"  eski      : {n_old}")

    if dry_run:
        print(f"  --dry-run, hiçbir şey silinmiyor.")
        return 0

    if n_old == 0:
        print(f"  silinecek satır yok.")
        return 0

    with get_connection() as conn:
        conn.execute(
            "DELETE FROM events WHERE received_at_ms < ?",
            (cutoff_ms,),
        )
    print(f"  ✓ {n_old} satır silindi.")

    # VACUUM ayrı autocommit bağlantı (transaction içinde çalışmaz).
    # receiver yazma yaparken çakışabilir; timeout=30 + try/except.
    try:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.isolation_level = None  # autocommit
        c.execute("VACUUM")
        c.close()
        print(f"  ✓ VACUUM tamam")
    except sqlite3.OperationalError as e:
        # Lock alınamadı vs - kritik değil, yarın tekrar denenir.
        print(f"  ⚠️ VACUUM atlandı: {e}")

    size_after = _filesize(DB_PATH)
    delta = size_before - size_after
    print(f"  boyut     : {size_before} → {size_after} bayt  ({delta:+d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
