"""Adım 5 testleri: snapshot_history/ klasörüne arşivleme + son 5 retention.

save_snapshot artık ana atomik write'in ardından timestamp'li bir kopyayı
snapshot_history/ altına atıp eski kopyaları temizliyor. Pattern dışı
dosyalar (README, manuel yedek) ASLA silinmemeli.
"""

import json
import os
import re
import sys
import time

import pytest


_STRICT_NAME_RE = re.compile(r"^snapshot_\d{8}_\d{6}\.json$")


def _import_due_report(monkeypatch, tmp_path):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token")
    monkeypatch.setenv("SMTP_PASSWORD", "test-pw")
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
    history = tmp_path / "snapshot_history"
    monkeypatch.setattr(clickup_due_report, "SNAPSHOT_FILE", str(snap))
    monkeypatch.setattr(clickup_due_report, "SNAPSHOT_HISTORY_DIR", str(history))
    return clickup_due_report, str(snap), str(history)


def _trivial_snapshot():
    return {
        "t1": {
            "name": "T", "list_name": "L", "space_name": "S",
            "due_date": None, "url": "", "assignees": [],
        },
    }


def _history_files(history_dir):
    """Strict pattern'e uyan snapshot dosyalarını listele (yeni→eski).

    Pattern: snapshot_YYYYMMDD_HHMMSS.json — clickup_due_report._SNAPSHOT_HISTORY_NAME_RE
    ile uyumlu olmak zorunda.
    """
    if not os.path.isdir(history_dir):
        return []
    files = [
        os.path.join(history_dir, f)
        for f in os.listdir(history_dir)
        if _STRICT_NAME_RE.match(f)
    ]
    files.sort(key=os.path.getmtime, reverse=True)
    return files


# ---------------------------------------------------------------------------
# İlk run + boş klasör senaryoları
# ---------------------------------------------------------------------------

def test_first_run_creates_history_dir_with_one_file(monkeypatch, tmp_path):
    cdr, snap_path, history_dir = _import_due_report(monkeypatch, tmp_path)
    assert not os.path.exists(history_dir)

    cdr.save_snapshot(_trivial_snapshot())

    assert os.path.exists(snap_path), "ana snapshot var olmalı"
    assert os.path.isdir(history_dir), "history klasörü yaratılmalı"
    files = _history_files(history_dir)
    assert len(files) == 1
    # Adlandırma pattern'e uymalı
    name = os.path.basename(files[0])
    assert name.startswith("snapshot_") and name.endswith(".json")
    # YYYYMMDD_HHMMSS — 8 + 1 + 6 = 15 karakter timestamp
    stamp = name[len("snapshot_"):-len(".json")]
    assert len(stamp) == 15
    assert stamp[8] == "_"
    assert stamp[:8].isdigit()
    assert stamp[9:].isdigit()


def test_history_copy_matches_main_content(monkeypatch, tmp_path):
    cdr, snap_path, history_dir = _import_due_report(monkeypatch, tmp_path)
    snap = _trivial_snapshot()
    cdr.save_snapshot(snap)

    with open(snap_path, encoding="utf-8") as f:
        main_data = json.load(f)
    archived = _history_files(history_dir)[0]
    with open(archived, encoding="utf-8") as f:
        copy_data = json.load(f)

    assert main_data == copy_data


def test_empty_history_dir_does_not_error(monkeypatch, tmp_path):
    """Klasör manuel oluşturuldu, boş — save_snapshot hata atmadan ilerlemeli."""
    cdr, _, history_dir = _import_due_report(monkeypatch, tmp_path)
    os.makedirs(history_dir, exist_ok=True)
    assert os.listdir(history_dir) == []

    cdr.save_snapshot(_trivial_snapshot())

    files = _history_files(history_dir)
    assert len(files) == 1


# ---------------------------------------------------------------------------
# Retention — keep son 5
# ---------------------------------------------------------------------------

def _seed_history(history_dir, n, base_mtime=None):
    """Mock geçmiş snapshot dosyalarını farklı mtime'larla oluştur."""
    os.makedirs(history_dir, exist_ok=True)
    if base_mtime is None:
        base_mtime = time.time() - 86400 * 7  # 7 gün önce
    for i in range(n):
        ts = base_mtime + i * 3600  # 1 saat aralıkla
        dt = time.gmtime(ts)
        name = f"snapshot_{time.strftime('%Y%m%d_%H%M%S', dt)}.json"
        # Aynı saniyeye düşmesin diye index'i basit kullanıyoruz
        path = os.path.join(history_dir, name)
        with open(path, "w") as f:
            f.write(f'{{"placeholder": {i}}}')
        os.utime(path, (ts, ts))


def test_keep_5_when_5_exist_then_oldest_removed(monkeypatch, tmp_path):
    cdr, _, history_dir = _import_due_report(monkeypatch, tmp_path)
    _seed_history(history_dir, 5)
    before = sorted(_history_files(history_dir), key=os.path.getmtime)
    oldest_path = before[0]

    cdr.save_snapshot(_trivial_snapshot())

    files = _history_files(history_dir)
    assert len(files) == 5, "yeni dosya eklendi, en eski silindi → 5 kaldı"
    assert oldest_path not in files


def test_keep_5_when_10_exist_legacy_cleanup(monkeypatch, tmp_path):
    """Legacy birikim: önceki sürüm retention yapmıyordu, 10 dosya birikti."""
    cdr, _, history_dir = _import_due_report(monkeypatch, tmp_path)
    _seed_history(history_dir, 10)

    cdr.save_snapshot(_trivial_snapshot())

    files = _history_files(history_dir)
    assert len(files) == 5


def test_keep_when_3_exist_only_adds_no_remove(monkeypatch, tmp_path):
    cdr, _, history_dir = _import_due_report(monkeypatch, tmp_path)
    _seed_history(history_dir, 3)

    cdr.save_snapshot(_trivial_snapshot())

    files = _history_files(history_dir)
    assert len(files) == 4


# ---------------------------------------------------------------------------
# Pattern dışı dosyalar dokunulmaz
# ---------------------------------------------------------------------------

def test_unrelated_files_in_history_dir_are_preserved(monkeypatch, tmp_path):
    cdr, _, history_dir = _import_due_report(monkeypatch, tmp_path)
    os.makedirs(history_dir, exist_ok=True)

    # Pattern eşleşmeyen dosyalar — silinmemeli
    untouchables = [
        "README.md",
        "snapshot_invalid.json",            # eksik timestamp
        "snapshot_20240101.json",           # eksik HHMMSS bölümü
        "snapshot_20240101_999999_extra.json",  # ek suffix
        "snapshot_yyyymmdd_hhmmss.json",    # rakam değil
        "backup_20240101_120000.json",      # snapshot_ prefix değil
    ]
    for name in untouchables:
        path = os.path.join(history_dir, name)
        with open(path, "w") as f:
            f.write("x")

    # Ayrıca pattern'e uyan 6 eski dosya seed et (limitin üzerinde)
    _seed_history(history_dir, 6, base_mtime=time.time() - 86400 * 3)

    cdr.save_snapshot(_trivial_snapshot())

    # Pattern eşleşmeyen dosyaların hepsi DURMALI
    for name in untouchables:
        path = os.path.join(history_dir, name)
        assert os.path.exists(path), f"{name} silinmiş olmamalıydı"

    # Pattern'e uyan dosyalar son 5 ile sınırlanmalı
    matching = _history_files(history_dir)
    assert len(matching) == 5


# ---------------------------------------------------------------------------
# Ana save davranışı bozulmadı
# ---------------------------------------------------------------------------

def test_main_snapshot_still_atomic_and_correct(monkeypatch, tmp_path):
    """Ana SNAPSHOT_FILE atomic write davranışı korunmuş olmalı + içerik doğru."""
    cdr, snap_path, history_dir = _import_due_report(monkeypatch, tmp_path)

    rename_calls = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        rename_calls.append((str(src), str(dst)))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", tracking_replace)

    cdr.save_snapshot({
        "tA": {"name": "A", "list_name": "L", "space_name": "S",
               "due_date": None, "url": "", "assignees": ["X"]},
    })

    # Ana dosya os.replace ile yazıldı, tmp leftover yok
    assert len(rename_calls) == 1
    src, dst = rename_calls[0]
    assert src.endswith(".tmp")
    assert dst == snap_path
    assert not os.path.exists(snap_path + ".tmp")

    # History dosyası da var
    history_files = _history_files(history_dir)
    assert len(history_files) == 1


def test_history_archive_failure_does_not_break_main_save(monkeypatch, tmp_path):
    """shutil.copy2 başarısız olsa bile ana SNAPSHOT_FILE yazılmış olmalı."""
    cdr, snap_path, _ = _import_due_report(monkeypatch, tmp_path)

    import shutil as _shutil

    def failing_copy(src, dst, **kwargs):
        raise OSError("disk full simulation")

    monkeypatch.setattr(_shutil, "copy2", failing_copy)
    monkeypatch.setattr(cdr.shutil, "copy2", failing_copy)

    # Hata atmadan dönmeli (içeride yakalanıyor)
    cdr.save_snapshot(_trivial_snapshot())

    assert os.path.exists(snap_path), "ana snapshot yine de yazılmış olmalı"
