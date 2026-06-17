"""Bug fix testleri: yorum kategorizasyonu 8 saat forward-window kuralı.

Eski (hatalı) davranış: task'ta HERHANGİ bir yorum varsa "+Yorum" sheet'ine
düşüyordu (örn. 86c8n08nz "Beypazarı İnternet" — eski yorum yüzünden yanlış
kategori). Yeni kural: pencere içindeki bir tarih değişikliğinin
changed_at_ms'inden itibaren 8 saat İÇİNDE (forward-only) yazılmış en az
bir yorum varsa "+Yorum"; gösterilen yorum eşleşenlerin en yenisi.

no_events fallback'i: webhook event'i olmayan task'larda değişiklik zamanı
bilinmediği için yorum rapor penceresi içindeyse eşleşmiş sayılır.
"""

import datetime
import sys

import pytest


HOUR_MS = 3600 * 1000


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

    import clickup_due_report as cdr

    snap = tmp_path / "due_date_snapshot.json"
    monkeypatch.setattr(cdr, "SNAPSHOT_FILE", str(snap))
    monkeypatch.setattr(
        cdr, "SNAPSHOT_HISTORY_DIR", str(tmp_path / "snapshot_history")
    )
    return cdr


def _comment(ts_ms, username="Yorumcu", text="yorum metni"):
    return {
        "date": str(ts_ms),
        "user": {"username": username},
        "comment_text": text,
    }


# ---------------------------------------------------------------------------
# Pure helper testleri — _match_comments_to_changes
# ---------------------------------------------------------------------------

def test_comment_2h_after_change_matches(monkeypatch, tmp_path):
    """Test 1: değişiklik T, yorum T+2h → eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T + 2 * HOUR_MS)], [T], window_since_ms=T - 24 * HOUR_MS
    )
    assert len(matched) == 1


def test_comment_9h_after_change_no_match(monkeypatch, tmp_path):
    """Test 2: değişiklik T, yorum T+9h → 8 saat penceresi dışı, eşleşmez."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T + 9 * HOUR_MS)], [T], window_since_ms=T - 24 * HOUR_MS
    )
    assert matched == []


def test_comment_9s_before_change_matches(monkeypatch, tmp_path):
    """Test 1 (BUG REPRO): yorum değişiklikten 9 sn ÖNCE → simetrik pencerede
    eşleşir. Forward-only mantıkta FAIL ederdi (ekip önce yorum yazıyor)."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T - 9_000)], [T], window_since_ms=T - 24 * HOUR_MS
    )
    assert len(matched) == 1


def test_comment_5s_after_change_matches(monkeypatch, tmp_path):
    """Test 2: yorum 5 sn SONRA → eşleşir (korundu)."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T + 5_000)], [T], window_since_ms=None
    )
    assert len(matched) == 1


def test_comment_7h_before_change_matches(monkeypatch, tmp_path):
    """Test 3: yorum 7 saat ÖNCE (sol sınır içi) → eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T - 7 * HOUR_MS)], [T], window_since_ms=None
    )
    assert len(matched) == 1


def test_comment_9h_before_change_no_match(monkeypatch, tmp_path):
    """Test 4: yorum 9 saat ÖNCE (sol sınır dışı) → eşleşmez."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T - 9 * HOUR_MS)], [T], window_since_ms=None
    )
    assert matched == []


def test_comment_7h_after_change_matches(monkeypatch, tmp_path):
    """Test 5: yorum 7 saat SONRA (sağ sınır içi) → eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T + 7 * HOUR_MS)], [T], window_since_ms=None
    )
    assert len(matched) == 1


def test_comment_9h_after_change_no_match_symmetric(monkeypatch, tmp_path):
    """Test 6: yorum 9 saat SONRA (sağ sınır dışı) → eşleşmez."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T + 9 * HOUR_MS)], [T], window_since_ms=None
    )
    assert matched == []


def test_exact_boundaries_inclusive(monkeypatch, tmp_path):
    """Test 7/8: sol ve sağ sınır kesin kontrolü.
    c_ts == ch-WINDOW ve ch+WINDOW dahil; ±1ms dışı hariç."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    W = cdr.COMMENT_MATCH_WINDOW_MS

    left_edge = cdr._match_comments_to_changes(
        [_comment(T - W)], [T], window_since_ms=None
    )
    left_past = cdr._match_comments_to_changes(
        [_comment(T - W - 1)], [T], window_since_ms=None
    )
    at_change = cdr._match_comments_to_changes(
        [_comment(T)], [T], window_since_ms=None
    )
    right_edge = cdr._match_comments_to_changes(
        [_comment(T + W)], [T], window_since_ms=None
    )
    right_past = cdr._match_comments_to_changes(
        [_comment(T + W + 1)], [T], window_since_ms=None
    )
    assert len(left_edge) == 1     # Test 7: tam -8h dahil
    assert left_past == []         # Test 8: -8h - 1ms hariç
    assert len(at_change) == 1
    assert len(right_edge) == 1
    assert right_past == []


def test_two_changes_any_match_suffices(monkeypatch, tmp_path):
    """Test 9: T1, T2=T1+20h (pencereler arası gerçek boşluk var).
    Boşluktaki yorum (T1+10h) hiçbiriyle eşleşmez; T1 öncesi/T2 yakını eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T1 = 1_750_000_000_000
    T2 = T1 + 20 * HOUR_MS  # 20h > 2*8h → [T1±8h] ve [T2±8h] çakışmaz

    # T1+10h: T1'den 10h (>8h), T2'den 10h (>8h) → boşlukta, eşleşmez
    no_match = cdr._match_comments_to_changes(
        [_comment(T1 + 10 * HOUR_MS)], [T1, T2], window_since_ms=None
    )
    assert no_match == []

    # T1-3h: T1'in sol penceresinde → eşleşir (herhangi biriyle yeter)
    m1 = cdr._match_comments_to_changes(
        [_comment(T1 - 3 * HOUR_MS)], [T1, T2], window_since_ms=None
    )
    assert len(m1) == 1

    # T2+3h: T2'nin sağ penceresinde → eşleşir
    m2 = cdr._match_comments_to_changes(
        [_comment(T2 + 3 * HOUR_MS)], [T1, T2], window_since_ms=None
    )
    assert len(m2) == 1


def test_no_events_fallback_comment_in_report_window(monkeypatch, tmp_path):
    """Test 5: no_events (change_timestamps boş) + yorum rapor penceresi
    içinde → eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    since = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(since + 5 * HOUR_MS)], [], window_since_ms=since
    )
    assert len(matched) == 1


def test_no_events_fallback_old_comment_no_match(monkeypatch, tmp_path):
    """Test 6: no_events + yalnızca pencere ÖNCESİ eski yorum → eşleşmez."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    since = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(since - 3 * HOUR_MS)], [], window_since_ms=since
    )
    assert matched == []


def test_no_events_and_no_since_no_match(monkeypatch, tmp_path):
    """no_events + window_since_ms=None (saved_at parse hatası) → eşleşme yok."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    matched = cdr._match_comments_to_changes(
        [_comment(1_750_000_000_000)], [], window_since_ms=None
    )
    assert matched == []


def test_no_comments_no_match(monkeypatch, tmp_path):
    """Test 7: hiç yorum yok → eşleşme yok."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    matched = cdr._match_comments_to_changes(
        [], [1_750_000_000_000], window_since_ms=None
    )
    assert matched == []


def test_multiple_matches_newest_first(monkeypatch, tmp_path):
    """Test 8 (helper kısmı): birden çok eşleşen yorum → en yeni başta."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [
            _comment(T + 1 * HOUR_MS, username="Eski", text="ilk yorum"),
            _comment(T + 5 * HOUR_MS, username="Yeni", text="son yorum"),
            _comment(T + 3 * HOUR_MS, username="Orta", text="orta yorum"),
        ],
        [T],
        window_since_ms=None,
    )
    assert [m["user"]["username"] for m in matched] == ["Yeni", "Orta", "Eski"]


def test_malformed_comment_entries_skipped(monkeypatch, tmp_path):
    """Bozuk yorum objeleri (dict değil / date yok / date bozuk) atlanır."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [
            "not-a-dict",
            {"user": {"username": "X"}},               # date yok
            {"date": "abc", "user": {}},               # date parse edilemez
            _comment(T + 1 * HOUR_MS, username="OK"),
        ],
        [T],
        window_since_ms=None,
    )
    assert len(matched) == 1
    assert matched[0]["user"]["username"] == "OK"


# ---------------------------------------------------------------------------
# diff_snapshots entegrasyonu — sheet seçimi + gösterilen yorum
# ---------------------------------------------------------------------------

def _task_entry(due, name="Task"):
    return {
        "due_date": due, "name": name, "url": "https://x",
        "list_name": "L", "space_name": "S", "assignees": [],
    }


def _run_diff(cdr, monkeypatch, *, saved_at_ms, change_ts, comments,
              my_user_id="999", changer_id="1"):
    """Tek task'lı diff_snapshots koşusu.

    prev: due=A, curr: due=B (değişti). Webhook event'leri change_ts
    listesinden üretilir (changer_id kullanıcısıyla — self DEĞİL).
    """
    saved_at_iso = datetime.datetime.fromtimestamp(
        saved_at_ms / 1000
    ).isoformat()
    prev = {"saved_at": saved_at_iso,
            "tasks": {"t1": _task_entry("1700000000000")}}
    curr = {"t1": _task_entry("1700090000000")}

    events = [
        {"user_id": changer_id, "user_name": "Beste",
         "before_value": "1700000000000", "after_value": "1700090000000",
         "changed_at_ms": ts}
        for ts in sorted(change_ts, reverse=True)
    ]
    monkeypatch.setattr(
        cdr, "get_due_date_events_batch",
        lambda ids, since, db_path=None: {tid: list(events) for tid in ids},
    )
    monkeypatch.setattr(
        cdr, "fetch_all_comments",
        lambda session, tid, oldest_needed_ms=None: list(comments),
    )
    monkeypatch.setattr(cdr, "fetch_task", lambda session, tid: None)
    monkeypatch.setattr(cdr.time, "sleep", lambda s: None)

    return cdr.diff_snapshots(prev, curr, session=None, my_user_id=my_user_id)


def test_diff_old_comment_goes_to_no_comment_sheet(monkeypatch, tmp_path):
    """Bug repro (86c8n08nz): tarih değişti, task'ta sadece ESKİ yorum var
    → artık 'Yorum Yok' sheet'ine düşmeli."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    T = saved_at + 2 * HOUR_MS  # pencere içi değişiklik
    old_comment = _comment(saved_at - 30 * 24 * HOUR_MS, text="1 ay önceki yorum")

    with_c, no_c, removed, metrics = _run_diff(
        cdr, monkeypatch, saved_at_ms=saved_at, change_ts=[T],
        comments=[old_comment],
    )
    assert with_c == []
    assert len(no_c) == 1
    assert no_c[0]["last_comment_by"] == ""
    assert no_c[0]["last_comment_text"] == ""


def test_diff_comment_within_8h_goes_to_with_comment_sheet(monkeypatch, tmp_path):
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    T = saved_at + 2 * HOUR_MS
    fresh = _comment(T + 2 * HOUR_MS, username="Beste", text="tarihi güncelledim")

    with_c, no_c, removed, metrics = _run_diff(
        cdr, monkeypatch, saved_at_ms=saved_at, change_ts=[T], comments=[fresh],
    )
    assert len(with_c) == 1
    assert no_c == []
    assert with_c[0]["last_comment_by"] == "Beste"
    assert with_c[0]["last_comment_text"] == "tarihi güncelledim"


def test_diff_shows_newest_matching_not_newest_overall(monkeypatch, tmp_path):
    """Test 8 (entegrasyon): gösterilen yorum 'task'ın son yorumu' değil,
    'eşleşenlerin en yenisi'. Pencere dışı daha yeni bir yorum varsa bile."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    T = saved_at + 2 * HOUR_MS
    comments = [
        _comment(T + 20 * HOUR_MS, username="Sonraki", text="alakasız geç yorum"),
        _comment(T + 6 * HOUR_MS, username="EşleşenYeni", text="ikinci yorum"),
        _comment(T + 1 * HOUR_MS, username="EşleşenEski", text="ilk yorum"),
    ]
    with_c, no_c, _, _ = _run_diff(
        cdr, monkeypatch, saved_at_ms=saved_at, change_ts=[T], comments=comments,
    )
    assert len(with_c) == 1
    assert with_c[0]["last_comment_by"] == "EşleşenYeni"
    assert with_c[0]["last_comment_text"] == "ikinci yorum"


def test_diff_no_events_fallback_uses_report_window(monkeypatch, tmp_path):
    """Test 5/6 (entegrasyon): webhook event'i yok → rapor penceresi fallback."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000

    # Pencere içi yorum → +Yorum
    in_window = _comment(saved_at + 4 * HOUR_MS, username="P", text="yeni")
    with_c, no_c, _, m = _run_diff(
        cdr, monkeypatch, saved_at_ms=saved_at, change_ts=[], comments=[in_window],
    )
    assert len(with_c) == 1
    assert m["no_events"] == 1

    # Sadece pencere öncesi yorum → Yorum Yok
    old = _comment(saved_at - 4 * HOUR_MS, username="P", text="eski")
    with_c2, no_c2, _, _ = _run_diff(
        cdr, monkeypatch, saved_at_ms=saved_at, change_ts=[], comments=[old],
    )
    assert with_c2 == []
    assert len(no_c2) == 1


# ---------------------------------------------------------------------------
# fetch_all_comments pagination (25-yorum sayfa limiti aşımı)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, comments):
        self.status_code = 200
        self._comments = comments

    def json(self):
        return {"comments": self._comments}


def _make_page(start_ts, count, prefix):
    """count adet yorum; en yeni start_ts, geriye 1'er saat inerek."""
    return [
        {
            "id": f"{prefix}-{i}",
            "date": str(start_ts - i * HOUR_MS),
            "user": {"username": f"u{prefix}{i}"},
            "comment_text": f"c{prefix}{i}",
        }
        for i in range(count)
    ]


def test_fetch_comments_single_page_when_no_threshold(monkeypatch, tmp_path):
    """oldest_needed_ms=None → tek istek (eski davranış korunur)."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    calls = []

    def fake_safe_get(session, url, attempt_label=""):
        calls.append(url)
        return _FakeResp(_make_page(1_750_000_000_000, 25, "a"))

    monkeypatch.setattr(cdr, "safe_get", fake_safe_get)
    out = cdr.fetch_all_comments(None, "t1")
    assert len(calls) == 1
    assert len(out) == 25


def test_fetch_comments_paginates_until_threshold(monkeypatch, tmp_path):
    """Eşik ilk sayfanın en eskisinden daha geride → 2. sayfa istenir;
    2. sayfa eşiğin altına inince durur."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    page1 = _make_page(T, 25, "a")             # T .. T-24h
    page2 = _make_page(T - 25 * HOUR_MS, 25, "b")  # T-25h .. T-49h
    calls = []

    def fake_safe_get(session, url, attempt_label=""):
        calls.append(url)
        return _FakeResp(page2 if "start=" in url else page1)

    monkeypatch.setattr(cdr, "safe_get", fake_safe_get)
    monkeypatch.setattr(cdr.time, "sleep", lambda s: None)

    # Eşik: T-30h → page1 yetmez (en eskisi T-24h), page2 gerekir
    out = cdr.fetch_all_comments(None, "t1", oldest_needed_ms=T - 30 * HOUR_MS)
    assert len(calls) == 2
    assert "start=" in calls[1] and "start_id=a-24" in calls[1]
    assert len(out) == 50
    # En yeni başta sıralı
    assert out[0]["id"] == "a-0"


def test_fetch_comments_stops_when_first_page_covers(monkeypatch, tmp_path):
    """Eşik ilk sayfa içinde → ek istek atılmaz."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    calls = []

    def fake_safe_get(session, url, attempt_label=""):
        calls.append(url)
        return _FakeResp(_make_page(T, 25, "a"))  # en eski T-24h

    monkeypatch.setattr(cdr, "safe_get", fake_safe_get)
    out = cdr.fetch_all_comments(None, "t1", oldest_needed_ms=T - 10 * HOUR_MS)
    assert len(calls) == 1
    assert len(out) == 25


def test_fetch_comments_pagination_dedup_and_loop_guard(monkeypatch, tmp_path):
    """Endpoint hep aynı sayfayı dönerse: dedup → new_items boş → döngü durur."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    same_page = _make_page(T, 25, "a")
    calls = []

    def fake_safe_get(session, url, attempt_label=""):
        calls.append(url)
        return _FakeResp(same_page)

    monkeypatch.setattr(cdr, "safe_get", fake_safe_get)
    monkeypatch.setattr(cdr.time, "sleep", lambda s: None)

    out = cdr.fetch_all_comments(
        None, "t1", oldest_needed_ms=T - 100 * HOUR_MS
    )
    assert len(out) == 25      # dup'lar eklenmedi
    assert len(calls) == 2     # 2. istek aynı sayfayı döndü → durdu


def test_fetch_comments_respects_max_extra_pages(monkeypatch, tmp_path):
    """Eşiğe hiç ulaşılamasa bile sayfa limiti aşılmaz."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    calls = []

    def fake_safe_get(session, url, attempt_label=""):
        idx = len(calls)
        calls.append(url)
        return _FakeResp(_make_page(T - idx * 25 * HOUR_MS, 25, f"p{idx}"))

    monkeypatch.setattr(cdr, "safe_get", fake_safe_get)
    monkeypatch.setattr(cdr.time, "sleep", lambda s: None)

    cdr.fetch_all_comments(None, "t1", oldest_needed_ms=0)  # ulaşılmaz eşik
    assert len(calls) == 1 + cdr._COMMENT_MAX_EXTRA_PAGES


def test_diff_self_only_still_skipped(monkeypatch, tmp_path):
    """Regression: tüm event'ler self ise satır rapora girmez (mevcut filtre)."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    T = saved_at + 2 * HOUR_MS
    with_c, no_c, removed, m = _run_diff(
        cdr, monkeypatch, saved_at_ms=saved_at, change_ts=[T],
        comments=[_comment(T + 1 * HOUR_MS)],
        my_user_id="42", changer_id="42",  # self
    )
    assert with_c == [] and no_c == [] and removed == []
    assert m["filtered_self_only"] == 1


# ---------------------------------------------------------------------------
# "Olası Değiştiren (yorum)" kolonu pencere hizalaması (15 Haz bug fix)
# ---------------------------------------------------------------------------
#
# Önceki testler fetch_task'i None döndürüyordu → yorum_tahmin hiç
# hesaplanmıyordu, bu yüzden çelişki yakalanamamıştı. Aşağıdaki helper gerçek
# diff_snapshots akışını date_updated dolu + gerçekçi yorumlarla çalıştırır.

def _run_diff_no_events(cdr, monkeypatch, *, saved_at_ms, comments,
                        date_updated_ms, is_removed=False, my_user_id="999"):
    """no_events senaryosu: webhook event'i YOK (change_ts boş), fetch_task
    gerçek date_updated döndürür, fetch_all_comments verilen yorumları döner.

    is_removed=True → curr due None (Tarih Kaldırıldı sheet'i).
    """
    saved_at_iso = datetime.datetime.fromtimestamp(
        saved_at_ms / 1000
    ).isoformat()
    prev = {"saved_at": saved_at_iso,
            "tasks": {"t1": _task_entry("1700000000000")}}
    if is_removed:
        curr = {"t1": _task_entry(None)}
    else:
        curr = {"t1": _task_entry("1700090000000")}

    # no_events: event store boş döner
    monkeypatch.setattr(
        cdr, "get_due_date_events_batch",
        lambda ids, since, db_path=None: {tid: [] for tid in ids},
    )
    monkeypatch.setattr(
        cdr, "fetch_all_comments",
        lambda session, tid, oldest_needed_ms=None: list(comments),
    )
    monkeypatch.setattr(
        cdr, "fetch_task",
        lambda session, tid: {"date_updated": str(date_updated_ms)},
    )
    monkeypatch.setattr(cdr.time, "sleep", lambda s: None)

    return cdr.diff_snapshots(prev, curr, session=None, my_user_id=my_user_id)


def test_olasi_degistiren_blanked_for_out_of_window_comment(monkeypatch, tmp_path):
    """15 Haz bug repro: yorum pencere ÖNCESİNDEN ama date_updated'a ±24h yakın.
    Sheet 'Yorum Yok' VE 'Olası Değiştiren' (yorum_tahmin) boş olmalı — çelişki yok."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    # Yorum pencereden 6 saat ÖNCE (12 Haz Selin yorumu senaryosu)
    comment_ts = saved_at - 6 * HOUR_MS
    # date_updated yoruma yakın (±24h içinde) → eski kodda kolon dolardı
    date_updated = comment_ts + 3 * HOUR_MS

    with_c, no_c, removed, m = _run_diff_no_events(
        cdr, monkeypatch, saved_at_ms=saved_at,
        comments=[_comment(comment_ts, username="Selin Aslandoğdu")],
        date_updated_ms=date_updated,
    )
    assert with_c == []
    assert len(no_c) == 1
    assert m["no_events"] == 1
    # Çelişki düzeltmesi: pencere dışı yorum → kolon boş
    assert no_c[0]["yorum_tahmin"] == ""


def test_olasi_degistiren_shown_for_in_window_comment(monkeypatch, tmp_path):
    """Yorum pencere İÇİNDE + date_updated'a yakın → sheet '+Yorum' VE
    'Olası Değiştiren' dolu (mevcut faydalı davranış korunur)."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    comment_ts = saved_at + 5 * HOUR_MS   # pencere içi
    date_updated = comment_ts + 1 * HOUR_MS

    with_c, no_c, removed, m = _run_diff_no_events(
        cdr, monkeypatch, saved_at_ms=saved_at,
        comments=[_comment(comment_ts, username="Selin Aslandoğdu")],
        date_updated_ms=date_updated,
    )
    # Pencere içi yorum → +Yorum sheet'i
    assert len(with_c) == 1
    assert no_c == []
    assert with_c[0]["yorum_tahmin"] == "Selin Aslandoğdu"


def test_olasi_degistiren_blanked_on_removed_sheet(monkeypatch, tmp_path):
    """Tarih Kaldırıldı sheet'i de aynı yorum_tahmin mekanizmasını kullanır;
    pencere dışı yorum orada da boşaltılmalı."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    comment_ts = saved_at - 6 * HOUR_MS
    date_updated = comment_ts + 2 * HOUR_MS

    with_c, no_c, removed, m = _run_diff_no_events(
        cdr, monkeypatch, saved_at_ms=saved_at,
        comments=[_comment(comment_ts, username="Selin Aslandoğdu")],
        date_updated_ms=date_updated,
        is_removed=True,
    )
    assert with_c == [] and no_c == []
    assert len(removed) == 1
    assert removed[0]["yorum_tahmin"] == ""


def test_olasi_degistiren_shown_in_window_on_removed_sheet(monkeypatch, tmp_path):
    """Tarih Kaldırıldı + pencere içi yorum → kolon dolu kalır (regresyon değil)."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    comment_ts = saved_at + 4 * HOUR_MS
    date_updated = comment_ts + 1 * HOUR_MS

    with_c, no_c, removed, m = _run_diff_no_events(
        cdr, monkeypatch, saved_at_ms=saved_at,
        comments=[_comment(comment_ts, username="Selin Aslandoğdu")],
        date_updated_ms=date_updated,
        is_removed=True,
    )
    assert len(removed) == 1
    assert removed[0]["yorum_tahmin"] == "Selin Aslandoğdu"


def test_olasi_degistiren_picks_only_in_window_among_mixed(monkeypatch, tmp_path):
    """date_updated'a en yakın yorum pencere dışı ama pencere içinde de
    başka yorum var: _comment_near_date en yakını (pencere dışı) seçerse
    gate onu boşaltır. Sheet seçimi pencere içi yorumla '+Yorum' kalır.

    Bu, kolon (en-yakın) ile sheet (pencere) mantıklarının ayrı olduğunu
    ve gate'in yalnızca kolonu pencereye sabitlediğini doğrular."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    saved_at = 1_750_000_000_000
    out_win = saved_at - 1 * HOUR_MS      # pencere dışı, date_updated'a çok yakın
    in_win = saved_at + 10 * HOUR_MS      # pencere içi ama date_updated'tan uzak
    date_updated = saved_at               # en yakın = out_win

    with_c, no_c, removed, m = _run_diff_no_events(
        cdr, monkeypatch, saved_at_ms=saved_at,
        comments=[
            _comment(out_win, username="PencereDışı"),
            _comment(in_win, username="PencereİçiYorum"),
        ],
        date_updated_ms=date_updated,
    )
    # Sheet seçimi: pencere içi yorum var → +Yorum
    assert len(with_c) == 1
    # Son Yorumu Yazan = eşleşen (pencere içi) yorum
    assert with_c[0]["last_comment_by"] == "PencereİçiYorum"
    # Olası Değiştiren: en-yakın pencere dışı → boşaltıldı
    assert with_c[0]["yorum_tahmin"] == ""
