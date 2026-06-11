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


def test_comment_before_change_no_match_forward_only(monkeypatch, tmp_path):
    """Test 3: yorum T-1h (değişiklikten önce) → forward-only, eşleşmez."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    matched = cdr._match_comments_to_changes(
        [_comment(T - 1 * HOUR_MS)], [T], window_since_ms=T - 24 * HOUR_MS
    )
    assert matched == []


def test_exact_boundaries_inclusive(monkeypatch, tmp_path):
    """Sınır kontrolü: c_ts == ch ve c_ts == ch+8h tam sınırda eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T = 1_750_000_000_000
    at_start = cdr._match_comments_to_changes(
        [_comment(T)], [T], window_since_ms=None
    )
    at_end = cdr._match_comments_to_changes(
        [_comment(T + cdr.COMMENT_MATCH_WINDOW_MS)], [T], window_since_ms=None
    )
    just_past = cdr._match_comments_to_changes(
        [_comment(T + cdr.COMMENT_MATCH_WINDOW_MS + 1)], [T], window_since_ms=None
    )
    assert len(at_start) == 1
    assert len(at_end) == 1
    assert just_past == []


def test_two_changes_any_match_suffices(monkeypatch, tmp_path):
    """Test 4: T1, T2=T1+12h; yorum T1+10h → ikisine de uymaz (T1+8h geçti,
    T2'den önce/forward-only). Yorum T2+3h → T2 ile eşleşir."""
    cdr = _import_due_report(monkeypatch, tmp_path)
    T1 = 1_750_000_000_000
    T2 = T1 + 12 * HOUR_MS

    no_match = cdr._match_comments_to_changes(
        [_comment(T1 + 10 * HOUR_MS)], [T1, T2], window_since_ms=None
    )
    assert no_match == []

    match = cdr._match_comments_to_changes(
        [_comment(T2 + 3 * HOUR_MS)], [T1, T2], window_since_ms=None
    )
    assert len(match) == 1


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
