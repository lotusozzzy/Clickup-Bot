"""ClickUp günlük tarih değişiklik raporu.

Cariler haricindeki tüm Space'lerdeki AÇIK task'lar üzerinde:
  - Bir önceki snapshot'a göre due_date değişen task'ları bulur.
  - "Değişti + değişiklikten sonraki 8 saat içinde yorum var" → Sekme 1
    (COMMENT_MATCH_WINDOW_MS, forward-only; task'taki alakasız eski
    yorumlar kategoriyi etkilemez)
  - "Değişti + eşleşen yorum yok" → Sekme 2
  - "Önceden tarih vardı, şu an yok" (tarih kaldırıldı) → Sekme 3
  - Webhook event store'dan silinen task'lar → Sekme 4

ATIF KAYNAĞI (Faz 5'ten itibaren): webhook event store
(webhook_events.db). ClickUp'a abone olduğumuz webhook 'taskUpdated'
event'leri receiver tarafından SQLite'a yazılıyor. Daily report,
snapshot diff'inde değişen her task için bu DB'ye sorgu atıp
"snapshot saved_at'ten beri olan due_date event'lerini" çekiyor.

  - Tüm event'ler kullanıcının kendisinden ise           → atla (filtered_self_only)
  - En az bir non-self event varsa                       → verified, en yeni non-self göster
  - Bu pencerede hiç event yoksa                         → no_events; bağlam için
                                                            date_updated yakını yorumu
                                                            'Olası Değiştiren (yorum)'
                                                            kolonuna düşer (filtre değil)

Yorumlar SADECE bağlam: 'Son Yorumu Yazan' / 'Son Yorum'.
Filtreleme kararına etki etmez.

İlk çalıştırmada karşılaştıracak veri olmadığı için sadece snapshot
kaydedilir ve bilgi maili atılır.

Snapshot dosyası: due_date_snapshot.json (script ile aynı dizinde).
Hassas ayarlar (API token, mail şifresi) clickup_bot modülünden import
edilir; o da değerleri os.environ'dan okur.
"""

import datetime
import json
import os
import re
import shutil
import signal
import smtplib
import sys
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo

from clickup_bot import (
    ALICI_MAIL_DAILY as ALICI_MAIL,
    BASE_SLEEP,
    GONDEREN_MAIL,
    GONDEREN_SIFRE,
    KEEP_EXCEL_ON_DISK,
    LIST_WATCHDOG_SECONDS,
    MAX_PAGES_PER_LIST,
    SF_GRAY,
    SF_ORANGE,
    SMTP_PORT,
    SMTP_SUNUCU,
    WORKSPACE_ID,
    _WatchdogTimeout,
    _watchdog_handler,
    get_session,
    log,
    safe_get,
)

from webhook.db import get_deletions_in_window, get_due_date_events_batch

EXCLUDE_SPACE = "Cariler"
# "+Yorum" kategorisi: tarih değişikliğinin changed_at_ms'inden itibaren
# bu pencere İÇİNDE (forward-only) yazılmış en az bir yorum varsa.
# Task'taki alakasız eski yorumlar kategoriyi etkilemez (bug fix, 11 Haziran).
COMMENT_MATCH_WINDOW_MS = 8 * 3600 * 1000  # 8 saat
SNAPSHOT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "due_date_snapshot.json"
)
SNAPSHOT_HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "snapshot_history"
)
SNAPSHOT_HISTORY_KEEP = 5
# Sadece bu pattern'e uyan dosyalar retention dahilinde — README.md, manuel
# yedekler vb. dokunulmaz.
_SNAPSHOT_HISTORY_NAME_RE = re.compile(r"^snapshot_\d{8}_\d{6}\.json$")


def _normalize_due(due):
    """ClickUp due_date alanını standart bir biçime indirger.

    None, boş string ve '0' hepsi 'tarih yok' olarak ele alınır.
    """
    if due is None:
        return None
    s = str(due).strip()
    if not s or s == "0":
        return None
    return s


def fmt_date(ms_str):
    """Milisaniye epoch string'ini 'gg.aa.yyyy' formatına çevirir."""
    s = _normalize_due(ms_str)
    if s is None:
        return ""
    try:
        ts = int(s) / 1000
        return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return ""


def fmt_datetime(ms):
    """Milisaniye epoch'tan 'gg.aa.yyyy SS:DD' formatına. Boş → ''."""
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError, OSError):
        return ""


def get_me(session):
    r = safe_get(session, "https://api.clickup.com/api/v2/user", attempt_label="user")
    if r is None or r.status_code != 200:
        return None
    user = r.json().get("user", {})
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "email": user.get("email"),
    }


def get_target_spaces(session):
    r = safe_get(
        session,
        f"https://api.clickup.com/api/v2/team/{WORKSPACE_ID}/space",
        attempt_label="spaces",
    )
    if r is None or r.status_code != 200:
        return []
    spaces = r.json().get("spaces", [])
    return [s for s in spaces if s.get("name", "").lower() != EXCLUDE_SPACE.lower()]


def get_all_lists_in_space(session, space_id, space_name):
    lists = []
    r = safe_get(
        session,
        f"https://api.clickup.com/api/v2/space/{space_id}/list",
        attempt_label=f"space {space_name} lists",
    )
    if r is not None and r.status_code == 200:
        for l in r.json().get("lists", []) or []:
            lists.append({"id": l["id"], "name": l["name"], "space": space_name})

    r_folders = safe_get(
        session,
        f"https://api.clickup.com/api/v2/space/{space_id}/folder",
        attempt_label=f"space {space_name} folders",
    )
    folders = r_folders.json().get("folders", []) if (r_folders is not None and r_folders.status_code == 200) else []
    for f in folders:
        r_fl = safe_get(
            session,
            f"https://api.clickup.com/api/v2/folder/{f['id']}/list",
            attempt_label=f"folder {f.get('name', f['id'])}",
        )
        if r_fl is not None and r_fl.status_code == 200:
            for l in r_fl.json().get("lists", []) or []:
                lists.append({"id": l["id"], "name": l["name"], "space": space_name})
    return lists


def fetch_open_tasks(session, list_id, list_name):
    """Listedeki tüm AÇIK task'ları getirir (subtasks dahil).

    Kapanmış/arşivli olanları getirmez (include_closed=false).
    """
    seen = set()
    tasks = []
    page = 0
    while page < MAX_PAGES_PER_LIST:
        url = (
            f"https://api.clickup.com/api/v2/list/{list_id}/task"
            f"?include_closed=false&subtasks=true&page={page}"
        )
        label = f"{list_name} sayfa {page}"
        r = safe_get(session, url, attempt_label=label)
        if r is None or r.status_code != 200:
            break
        try:
            payload = r.json()
        except ValueError:
            break
        page_tasks = payload.get("tasks", []) or []
        if not page_tasks:
            break
        new_ids = {t.get("id") for t in page_tasks if t.get("id")}
        if new_ids and new_ids.issubset(seen):
            break
        seen.update(new_ids)
        tasks.extend(page_tasks)
        if payload.get("last_page") is True:
            break
        if len(page_tasks) < 100:
            break
        page += 1
        time.sleep(BASE_SLEEP)
    return tasks


def build_current_snapshot(session):
    """Açık task'ların mevcut due_date durumunu çıkarır.

    Dönen tuple: (snapshot_dict, atlanan_listeler).
    snapshot_dict: {task_id: {due_date, name, url, list_name, space_name}}
    atlanan_listeler: watchdog devreye girip yarıda kesilen liste isimleri.
    """
    snapshot = {}
    atlanan_listeler = []
    spaces = get_target_spaces(session)
    log(f"📦 {len(spaces)} space taranacak (Cariler hariç).")
    baslangic = time.time()
    for s in spaces:
        space_name = s.get("name", "")
        lists = get_all_lists_in_space(session, s["id"], space_name)
        log(f"  🗂️  '{space_name}' içinde {len(lists)} liste.")
        for l in lists:
            log(f"    🔍 {l['name']} taranıyor...")
            tasks = []
            try:
                signal.signal(signal.SIGALRM, _watchdog_handler)
                signal.alarm(LIST_WATCHDOG_SECONDS)
                tasks = fetch_open_tasks(session, l["id"], l["name"])
            except _WatchdogTimeout:
                log(f"      ⏱️ '{l['name']}' {LIST_WATCHDOG_SECONDS}s içinde bitmedi, atlandı.")
                atlanan_listeler.append({
                    "ad": f"{l['name']} ({space_name})",
                    "sebep": f"Zaman aşımı (>{LIST_WATCHDOG_SECONDS}s)",
                })
            finally:
                signal.alarm(0)

            for t in tasks:
                tid = t.get("id")
                if not tid:
                    continue
                # Assignees task objesinde zaten geliyor — ek API çağrısı yok.
                # Silinen task'ı raporlarken "kim atanmıştı" bağlamı için saklanır.
                assignees = []
                for a in (t.get("assignees") or []):
                    if not isinstance(a, dict):
                        continue
                    name = a.get("username") or a.get("email") or ""
                    if name:
                        assignees.append(name)
                snapshot[tid] = {
                    "due_date": _normalize_due(t.get("due_date")),
                    "name": t.get("name", ""),
                    "url": t.get("url", ""),
                    "list_name": l["name"],
                    "space_name": space_name,
                    "assignees": assignees,
                }
    gecen = int(time.time() - baslangic)
    log(f"✓ Tarama bitti: {len(snapshot)} açık task, {gecen}s, "
        f"{len(atlanan_listeler)} liste atlandı.")
    return snapshot, atlanan_listeler


def load_previous_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        log(f"⚠️ Snapshot okunamadı ({e}), sıfırdan başlanıyor.")
        return None


def save_snapshot(snapshot):
    """Snapshot'ı atomik write+rename ile yaz; ardından history'ye arşivle.

    Ana dosya (SNAPSHOT_FILE): webhook receiver bunu taskDeleted event'lerinde
    okuyor — yazma sırasında corrupt JSON ile karşılaşmasın diye .tmp'e yaz,
    fsync, sonra os.replace() (atomic rename, POSIX + Windows).

    History (snapshot_history/snapshot_YYYYMMDD_HHMMSS.json): ana save
    stabilize olduktan sonra timestamp'li kopya atılır ve son
    SNAPSHOT_HISTORY_KEEP tanesi tutulur. Archive/retention hata verirse
    log'lanır ama daily run kesilmez — ana save zaten yapılmış oldu.
    """
    payload = {
        "saved_at": datetime.datetime.now().isoformat(),
        "tasks": snapshot,
    }
    tmp_path = SNAPSHOT_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, SNAPSHOT_FILE)

    try:
        _archive_snapshot_history(
            SNAPSHOT_FILE, SNAPSHOT_HISTORY_DIR, SNAPSHOT_HISTORY_KEEP
        )
    except OSError as e:
        log(f"⚠️ Snapshot history archive failed: {e}")


def _archive_snapshot_history(source_path, history_dir, keep):
    """Snapshot'ın timestamp'li kopyasını history klasörüne at; eskileri sil.

    - Klasör yoksa yaratılır.
    - Dest dosya adı: snapshot_YYYYMMDD_HHMMSS.json (lokal zaman).
      Aynı saniye içinde iki run olursa shutil.copy2 üzerine yazar; kabul
      edilebilir trade-off.
    - Retention: pattern'e uyan dosyaları mtime'a göre yeni→eski sırala,
      ilk `keep` tane hariç gerisini sil. Pattern dışı (README.md,
      snapshot_invalid.json vb.) ASLA silinmez.
    """
    os.makedirs(history_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(history_dir, f"snapshot_{timestamp}.json")
    shutil.copy2(source_path, dest_path)

    candidates = []
    for entry in os.listdir(history_dir):
        if not _SNAPSHOT_HISTORY_NAME_RE.match(entry):
            continue
        full = os.path.join(history_dir, entry)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        candidates.append((mtime, full))

    candidates.sort(key=lambda x: x[0], reverse=True)

    removed = 0
    for _mtime, path in candidates[keep:]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass

    kept = min(len(candidates), keep)
    log(f"📦 Snapshot history: kept {kept}, removed {removed} ({history_dir})")


def fetch_task(session, task_id):
    """Tek API çağrısıyla task'ı çek - history_items + date_updated dahil."""
    r = safe_get(
        session,
        f"https://api.clickup.com/api/v2/task/{task_id}",
        attempt_label=f"task {task_id}",
    )
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# NOT: history_items tabanlı eski extract_due_date_history() fonksiyonu Faz 5'te
# kaldırıldı. ClickUp v2 GET /task/{id} yanıtı history_items'ı public olarak
# döndürmüyor (missing=100% gözlemlendi). Yerine webhook event store'u
# (webhook_events.db) kullanılıyor — webhook.db.get_due_date_events_batch.


# ClickUp v2 /comment endpoint'i tek istekte en yeni ~25 yorumu döner.
# 8h pencere eşleşmesi için daha eskilere inmek gerekebilir; defansif
# sayfa limiti sonsuz döngü/rate-limit koruması.
_COMMENT_MAX_EXTRA_PAGES = 8


def fetch_all_comments(session, task_id, oldest_needed_ms=None):
    """Task yorumlarını en yeniden eskiye sıralı getir.

    ClickUp v2 endpoint'i pagination'sız yalnızca en yeni ~25 yorumu döner.
    oldest_needed_ms verilirse: alınan en eski yorum bu eşikten daha yeniyken
    'start'+'start_id' parametreleriyle geriye doğru sayfalanır (en fazla
    _COMMENT_MAX_EXTRA_PAGES ek sayfa). None → tek istek (eski davranış).
    """
    base_url = f"https://api.clickup.com/api/v2/task/{task_id}/comment"
    comments = []
    seen_ids = set()
    next_start = None
    next_start_id = None

    max_pages = 1 + (
        _COMMENT_MAX_EXTRA_PAGES if oldest_needed_ms is not None else 0
    )
    for page_no in range(max_pages):
        url = base_url
        if next_start is not None and next_start_id:
            url = f"{base_url}?start={next_start}&start_id={next_start_id}"
        r = safe_get(
            session, url, attempt_label=f"comments {task_id} p{page_no}"
        )
        if r is None or r.status_code != 200:
            break
        try:
            page = r.json().get("comments", []) or []
        except ValueError:
            break

        new_items = []
        for c in page:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid is not None and cid in seen_ids:
                continue
            if cid is not None:
                seen_ids.add(cid)
            new_items.append(c)
        if not new_items:
            break
        comments.extend(new_items)

        if oldest_needed_ms is None:
            break

        oldest_c = min(new_items, key=_comment_ts)
        oldest_ts = _comment_ts(oldest_c)
        # Eşiğin altına indik veya timestamp parse edilemiyor → dur.
        if oldest_ts == 0 or oldest_ts <= oldest_needed_ms:
            break
        if not oldest_c.get("id"):
            break
        next_start = oldest_ts
        next_start_id = oldest_c.get("id")
        time.sleep(BASE_SLEEP)

    comments.sort(key=_comment_ts, reverse=True)
    return comments


def _extract_comment_text(comment):
    if not comment:
        return ""
    if comment.get("comment_text"):
        return comment["comment_text"]
    parts = comment.get("comment") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _comment_ts(comment):
    """ClickUp comment objesinin 'date' alanını int ms'e çevir. Hata → 0."""
    try:
        return int(comment.get("date") or 0)
    except (ValueError, TypeError):
        return 0


def _match_comments_to_changes(comments, change_timestamps, window_since_ms):
    """Tarih değişikliklerine 8 saatlik forward-window ile yorum eşleştir.

    Kural ("+Yorum" kategorisi, 11 Haziran bug fix):
      - Bir yorum, HERHANGİ bir tarih değişikliğinin changed_at_ms'inden
        itibaren COMMENT_MATCH_WINDOW_MS içinde yazılmışsa eşleşir:
        ch <= c_ts <= ch + WINDOW. Forward-only — değişiklikten ÖNCE
        yazılmış yorum eşleşMEZ.
      - change_timestamps boş (no_events; webhook event'i yok, değişiklik
        zamanı bilinmiyor): fallback — yorum rapor penceresi içindeyse
        (c_ts > window_since_ms) eşleşmiş sayılır. window_since_ms None ise
        (snapshot saved_at parse edilememiş) hiç eşleşme olmaz.

    Dönüş: eşleşen yorumlar, en yeni en başta.
    """
    matched = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        c_ts = _comment_ts(c)
        if not c_ts:
            continue
        if change_timestamps:
            if any(
                ch <= c_ts <= ch + COMMENT_MATCH_WINDOW_MS
                for ch in change_timestamps
            ):
                matched.append(c)
        elif window_since_ms is not None and c_ts > window_since_ms:
            matched.append(c)
    matched.sort(key=_comment_ts, reverse=True)
    return matched


def _comment_near_date(comments, target_ms, window_hours=24):
    """target_ms'ye en yakın yorumu bul; pencere dışında ise None."""
    if not comments or not target_ms:
        return None
    window_ms = window_hours * 3600 * 1000
    best = None
    best_diff = None
    for c in comments:
        try:
            c_ms = int(c.get("date") or 0)
        except (ValueError, TypeError):
            continue
        diff = abs(c_ms - target_ms)
        if diff > window_ms:
            continue
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = c
    return best


def _snapshot_saved_at_ms(prev):
    """Önceki snapshot'ın 'saved_at' alanını ms epoch'a çevir.

    save_snapshot() her run sonunda payload['saved_at']'ı
    datetime.now().isoformat() olarak yazar. Bu zaman, history içinde
    'önceki rapordan beri olan değişiklikler' penceresinin alt sınırıdır:
    bu zamandan SONRAKİ history kayıtları yeni değişiklik sayılır.

    None döner: prev yok / saved_at yok / parse hatası.
    """
    if not prev:
        return None
    saved_at = prev.get("saved_at")
    if not saved_at:
        return None
    try:
        return int(datetime.datetime.fromisoformat(saved_at).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _is_my_user(event_user_id, my_user_id):
    """Event user_id ile API user_id eşleşir mi (str/int normalize)."""
    if event_user_id is None or my_user_id is None:
        return False
    return str(event_user_id) == str(my_user_id)


def _attribute_change(events, my_user_id):
    """Bir tarih değişikliğini, webhook event'lerine bakarak atfeder.

    events: [{user_id, user_name, before_value, after_value, changed_at_ms}, ...]
            en yeni en başta sıralı; SADECE snapshot penceresi içindeki kayıtlar.
            (Pencere filtresi get_due_date_events_batch'in 'since_ms' parametresinde
            zaten uygulandı — burada tekrar filtrelemiyoruz.)

    Returns:
        skip (bool):    True ise satır rapordan çıkarılır
        changer_name:   'Tarihi Değiştiren' kolonu için
        changer_id:     atıf doğrulamak isteyen consumer için
        quality (str):
            'verified'  → en az bir non-self event var, atıf yapıldı
            'self_only' → tüm event'ler kullanıcının kendisinden (skip=True)
            'no_events' → bu task için pencerede hiç event yok
                          (subscribe öncesi değişiklik veya event kaybı)
    """
    if not events:
        return {
            "skip": False,
            "changer_name": None,
            "changer_id": None,
            "quality": "no_events",
        }

    non_mine = [e for e in events if not _is_my_user(e["user_id"], my_user_id)]

    if not non_mine:
        return {
            "skip": True,
            "changer_name": None,
            "changer_id": None,
            "quality": "self_only",
        }

    most_recent_other = non_mine[0]  # events zaten DESC sıralı
    return {
        "skip": False,
        "changer_name": most_recent_other["user_name"] or "(bilinmiyor)",
        "changer_id": most_recent_other["user_id"],
        "quality": "verified",
    }


def diff_snapshots(prev, curr, session, my_user_id):
    """Üç liste + metrik dict döner.

    Filtre: Tarih değişikliğini SADECE kullanıcı yapmışsa (webhook
    event store'unda snapshot penceresindeki tüm due_date event'lerinin
    user_id'si my_user_id ile eşleşiyorsa) o satır rapordan çıkarılır.
    Yorumlar filtreleme kararına etki etmez; sadece bağlam için gösterilir.

    Atıf kaynağı: webhook_events.db (Faz 2-4 ile kurulan event store).
    history_items API kanalı public değildi, kaldırıldı.
    """
    changed_with_comment = []
    changed_no_comment = []
    removed = []

    metrics = {
        "total_diff": 0,            # snapshot diff'inde tarih değişen toplam task
        "filtered_self_only": 0,    # kullanıcının kendi yaptığı için atlanan
        "verified": 0,              # webhook event'lerinden başkası bulundu
        "no_events": 0,             # pencerede webhook event'i yok
    }

    prev_tasks = (prev or {}).get("tasks", {}) or {}
    snapshot_taken_ms = _snapshot_saved_at_ms(prev)
    if snapshot_taken_ms is None:
        log("⚠️ snapshot.saved_at parse edilemedi - tüm satırlar 'no_events' olarak işlenecek.")

    # 1. Tüm diff'leri topla (henüz event sorgulamadan)
    diffs = []  # liste: (tid, p, c, prev_due, curr_due, is_removed)
    all_ids = set(prev_tasks.keys()) | set(curr.keys())
    for tid in all_ids:
        p = prev_tasks.get(tid)
        c = curr.get(tid)
        prev_due = _normalize_due(p.get("due_date")) if p else None
        curr_due = _normalize_due(c.get("due_date")) if c else None

        if not prev_due and not curr_due:
            continue
        if not c:
            continue
        if not prev_due and curr_due:
            continue  # yeni eklenen tarih, raporlanmaz

        is_removed = bool(prev_due and not curr_due)
        is_changed = bool(prev_due and curr_due and str(prev_due) != str(curr_due))
        if not is_removed and not is_changed:
            continue

        diffs.append((tid, p, c, prev_due, curr_due, is_removed))
        metrics["total_diff"] += 1

    # 2. Tek SQLite çağrısıyla tüm değişen task'lar için event'leri çek
    if snapshot_taken_ms is not None and diffs:
        events_by_task = get_due_date_events_batch(
            [d[0] for d in diffs], snapshot_taken_ms
        )
    else:
        events_by_task = {}

    # 3. Her diff için atıf yap, gerekirse yorum/comment fallback fetch
    for tid, p, c, prev_due, curr_due, is_removed in diffs:
        events = events_by_task.get(tid, [])
        attr = _attribute_change(events, my_user_id)

        if attr["skip"]:
            metrics["filtered_self_only"] += 1
            continue

        metrics[attr["quality"]] += 1

        # Kategorize: "task'ta herhangi bir yorum var" DEĞİL — değişiklikten
        # sonraki 8 saat içinde yazılmış yorum var mı (forward-only).
        # Gösterilen yorum da eşleşenlerin en yenisi; alakasız eski yorumlar
        # ne kategoriyi ne de kolonları etkiler.
        change_ts_list = [
            e["changed_at_ms"] for e in events if e.get("changed_at_ms")
        ]

        # Yorumlar: pencere eşleşmesi en eski değişikliğe kadar inmeli;
        # _comment_near_date (yorum_tahmin) ±24h baktığı için o kadar padding.
        # Tek sayfa ~25 yorum yettiğinde ek istek atılmaz.
        _candidates = change_ts_list or (
            [snapshot_taken_ms] if snapshot_taken_ms else []
        )
        oldest_needed = (
            min(_candidates) - 24 * 3600 * 1000 if _candidates else None
        )
        comments = fetch_all_comments(
            session, tid, oldest_needed_ms=oldest_needed
        )
        time.sleep(BASE_SLEEP)
        matched_comments = _match_comments_to_changes(
            comments, change_ts_list, snapshot_taken_ms
        )
        matched_c = matched_comments[0] if matched_comments else None
        last_comment_by = ""
        last_comment_text = ""
        if matched_c:
            user = matched_c.get("user") or {}
            last_comment_by = user.get("username", "")
            last_comment_text = _extract_comment_text(matched_c)

        # Fallback: webhook event yoksa, date_updated'a yakın yorumu tahmini değiştiren olarak göster
        yorum_tahmin = ""
        if attr["quality"] == "no_events":
            task_data = fetch_task(session, tid)
            time.sleep(BASE_SLEEP)
            if task_data:
                try:
                    date_updated_ms = int(task_data.get("date_updated") or 0)
                except (ValueError, TypeError):
                    date_updated_ms = 0
                if date_updated_ms:
                    near = _comment_near_date(comments, date_updated_ms)
                    if near:
                        near_user = near.get("user") or {}
                        yorum_tahmin = near_user.get("username") or ""

        display_changer = (
            attr["changer_name"]
            if attr["changer_name"]
            else "(webhook event yok)"
        )

        row = {
            "task_id": tid,
            "name": c.get("name") or (p.get("name") if p else ""),
            "list_name": c.get("list_name", ""),
            "space_name": c.get("space_name", ""),
            "url": c.get("url", ""),
            "old_date": fmt_date(prev_due),
            "changer": display_changer,
            "yorum_tahmin": yorum_tahmin,
            "last_comment_by": last_comment_by,
            "last_comment_text": last_comment_text,
        }

        if is_removed:
            removed.append(row)
            continue

        row["new_date"] = fmt_date(curr_due)
        if matched_c:
            changed_with_comment.append(row)
        else:
            row["last_comment_by"] = ""
            row["last_comment_text"] = ""
            changed_no_comment.append(row)

    in_report = metrics["total_diff"] - metrics["filtered_self_only"]
    no_events_pct = (
        metrics["no_events"] / max(in_report, 1) * 100 if in_report > 0 else 0
    )
    verified_pct = (
        metrics["verified"] / max(in_report, 1) * 100 if in_report > 0 else 0
    )
    log(
        f"📊 Atıf metrikleri: total_diff={metrics['total_diff']}, "
        f"filtered_self_only={metrics['filtered_self_only']}, "
        f"in_report={in_report}, verified={metrics['verified']} ({verified_pct:.0f}%), "
        f"no_events={metrics['no_events']} ({no_events_pct:.0f}%)"
    )

    return changed_with_comment, changed_no_comment, removed, metrics


# ----- Silinen task satırı normalleştirme ----------------------------------

_COMMENT_TEXT_TRUNCATE = 500


def _parse_deletion_row(row):
    """get_deletions_in_window satırını sheet hücrelerine uygun dict'e çevir.

    field='__deleted__'      → before_value JSON parse (zenginleştirilmiş)
    field='__deleted_raw__'  → snapshot miss; sadece task_id ve silinme
                                zamanı dolu, diğerleri boş.
    JSON parse hatası: graceful — partial row, "(snapshot dışı)".
    Yorum metni 500 karaktere kırpılır.
    """
    field = row.get("field") or ""
    task_id = row.get("task_id") or ""
    deleted_at_ms = row.get("changed_at_ms") or 0

    parsed = {
        "field": field,
        "task_id": task_id,
        "task_name": "",
        "space_name": "",
        "list_name": "",
        "last_due_date": "",
        "assignees_str": "",
        "last_comment_author": "",
        "last_comment_text": "",
        "deleted_at": fmt_datetime(deleted_at_ms),
    }

    if field != "__deleted__":
        parsed["task_name"] = "(snapshot dışı)"
        return parsed

    raw = row.get("before_value")
    try:
        d = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        d = {}
    if not isinstance(d, dict):
        d = {}

    parsed["task_name"] = (d.get("task_name") or "").strip() or "(snapshot dışı)"
    parsed["space_name"] = d.get("space_name") or ""
    parsed["list_name"] = d.get("list_name") or ""
    parsed["last_due_date"] = fmt_date(d.get("last_due_date"))

    assignees = d.get("assignees") or []
    if isinstance(assignees, list):
        parsed["assignees_str"] = ", ".join(
            str(a) for a in assignees if a
        )

    parsed["last_comment_author"] = d.get("last_comment_author") or ""

    text = d.get("last_comment_text") or ""
    if len(text) > _COMMENT_TEXT_TRUNCATE:
        text = text[: _COMMENT_TEXT_TRUNCATE - 3] + "..."
    parsed["last_comment_text"] = text

    return parsed


# ----- Excel & Mail ---------------------------------------------------------

def _add_sheet(wb, title, headers, rows, table_name, header_fill, header_font,
               wrap_alignment, thin_border):
    ws = wb.create_sheet(title=title[:31])
    ws.append(headers)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = thin_border
    ws.row_dimensions[1].height = 30

    for row in rows:
        ws.append(row)

    # Boş tabloda da en az "veri yok" satırı kalsın - okurken kafa karıştırmasın.
    if not rows:
        ws.append(["(Bu kategoride değişiklik yok)"] + [""] * (len(headers) - 1))

    for r in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=len(headers)):
        for cell in r:
            cell.alignment = wrap_alignment
            cell.border = thin_border

    for col_idx in range(1, len(headers) + 1):
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        max_data_len = 0
        for r in ws.iter_rows(min_row=2, max_row=ws.max_row,
                              min_col=col_idx, max_col=col_idx):
            for cell in r:
                if cell.value is None:
                    continue
                text = str(cell.value)
                if len(text) > max_data_len:
                    max_data_len = len(text)
        header_text = headers[col_idx - 1]
        min_for_header = max(
            (len(w) for w in header_text.split()), default=len(header_text)
        )
        width = max(min_for_header + 2, max_data_len + 3)
        ws.column_dimensions[col_letter].width = min(width, 50)

    if ws.max_row >= 2:
        last_col = ws.cell(row=1, column=len(headers)).column_letter
        tab_ref = f"A1:{last_col}{ws.max_row}"
        tab = Table(displayName=table_name, ref=tab_ref)
        tab.tableStyleInfo = TableStyleInfo(
            name="TableStyleLight1", showFirstColumn=False, showLastColumn=False,
            showRowStripes=True, showColumnStripes=False,
        )
        ws.add_table(tab)


def write_excel(changed_with, changed_no, removed, atlanan_listeler=None, deletions=None):
    atlanan_listeler = atlanan_listeler or []
    deletions = deletions or []
    dosya = f"Tarih_Degisiklik_Raporu_{datetime.datetime.now().strftime('%d_%m_%Y')}.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    header_fill = PatternFill(start_color=SF_ORANGE, end_color=SF_ORANGE, fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    wrap_alignment = Alignment(wrap_text=True, vertical="center")
    thin_border = Border(
        left=Side(style='thin', color="D3D3D3"),
        right=Side(style='thin', color="D3D3D3"),
        top=Side(style='thin', color="D3D3D3"),
        bottom=Side(style='thin', color="D3D3D3"),
    )

    headers_with = ["Task", "Space", "Liste", "Eski Tarih", "Yeni Tarih",
                    "Tarihi Değiştiren", "Olası Değiştiren (yorum)",
                    "Son Yorumu Yazan", "Son Yorum", "URL"]
    headers_no = ["Task", "Space", "Liste", "Eski Tarih", "Yeni Tarih",
                  "Tarihi Değiştiren", "Olası Değiştiren (yorum)", "URL"]
    headers_removed = ["Task", "Space", "Liste", "Kaldırılan Tarih",
                       "Tarihi Kaldıran", "Olası Değiştiren (yorum)", "URL"]

    _add_sheet(
        wb, "Tarih Değişti + Yorum", headers_with,
        [[r["name"], r["space_name"], r["list_name"], r["old_date"], r["new_date"],
          r.get("changer", ""), r.get("yorum_tahmin", ""),
          r["last_comment_by"], r["last_comment_text"], r["url"]]
         for r in changed_with],
        "Tbl_DegistiYorumlu",
        header_fill, header_font, wrap_alignment, thin_border,
    )
    _add_sheet(
        wb, "Tarih Değişti - Yorum Yok", headers_no,
        [[r["name"], r["space_name"], r["list_name"], r["old_date"], r["new_date"],
          r.get("changer", ""), r.get("yorum_tahmin", ""), r["url"]]
         for r in changed_no],
        "Tbl_DegistiYorumsuz",
        header_fill, header_font, wrap_alignment, thin_border,
    )
    _add_sheet(
        wb, "Tarih Kaldırıldı", headers_removed,
        [[r["name"], r["space_name"], r["list_name"], r["old_date"],
          r.get("changer", ""), r.get("yorum_tahmin", ""), r["url"]]
         for r in removed],
        "Tbl_Kaldirildi",
        header_fill, header_font, wrap_alignment, thin_border,
    )

    headers_deleted = [
        "Task İsmi", "Space", "Liste",
        "Son Bilinen Tarih", "Atayanlar",
        "Son Yorumlayan", "Son Yorum",
        "Silinme Zamanı", "Task ID",
    ]
    _add_sheet(
        wb, "Silinen Task'lar", headers_deleted,
        [[d["task_name"], d["space_name"], d["list_name"],
          d["last_due_date"], d["assignees_str"],
          d["last_comment_author"], d["last_comment_text"],
          d["deleted_at"], d["task_id"]]
         for d in deletions],
        "Tbl_Silinen",
        header_fill, header_font, wrap_alignment, thin_border,
    )

    if atlanan_listeler:
        _add_sheet(
            wb, "Atlanan Listeler", ["Liste Adı", "Atlanma Sebebi"],
            [[a["ad"], a["sebep"]] for a in atlanan_listeler],
            "Tbl_Atlanan",
            header_fill, header_font, wrap_alignment, thin_border,
        )

    wb.save(dosya)
    return dosya


def send_mail(dosya, me_info, summary, is_first_run):
    msg = MIMEMultipart()
    konu_tarih = datetime.datetime.now().strftime("%d.%m.%Y")
    msg['From'] = GONDEREN_MAIL
    msg['To'] = ALICI_MAIL
    msg['Subject'] = f"Günlük Tarih Değişikliği Raporu - {konu_tarih}"

    atlanan_uyari = ""
    atlanan_cnt = summary.get("atlanan", 0)
    if atlanan_cnt:
        atlanan_uyari = (
            f"<p style='color:#cc6600;'><b>Uyarı:</b> {atlanan_cnt} liste zaman aşımı "
            f"nedeniyle taranamadı. Excel'in <b>Atlanan Listeler</b> sekmesine bakın.</p>"
        )

    if is_first_run:
        body = f"""
        <html><body style="font-family: Arial, sans-serif; color: #{SF_GRAY};">
          <h2 style="color: #{SF_ORANGE};">İlk Snapshot Oluşturuldu</h2>
          <p>Bugün ilk kez çalıştırıldığı için karşılaştıracak geçmiş veri yok.
             <b>{summary['toplam_task']}</b> açık task izlemeye alındı.</p>
          <p>Yarın akşamki rapordan itibaren tarih değişiklikleri görünecek.</p>
          {atlanan_uyari}
          <p style="color: #888; font-size: 12px;">
            Çalıştıran: {me_info.get('username', '')} ({me_info.get('email', '')})
          </p>
        </body></html>
        """
    else:
        # Atıf güvenilirliği uyarısı: webhook event store'unda kayıt bulamadığımız oran %10+ ise
        atif_uyarisi = ""
        m = summary.get("metrics") or {}
        in_report = m.get("total_diff", 0) - m.get("filtered_self_only", 0)
        no_events = m.get("no_events", 0)
        if in_report > 0:
            no_events_pct = no_events / in_report * 100
            if no_events_pct > 10:
                atif_uyarisi = (
                    f'<p style="background:#fff3cd; padding:12px; '
                    f'border-left:4px solid #ffa500;">'
                    f'<b>⚠️ Atıf güvenilirliği uyarısı:</b> Bu raporda {in_report} '
                    f'satırın {no_events} tanesi ({no_events_pct:.0f}%) için '
                    f'webhook event\'i yok. Bu satırlarda "Tarihi Değiştiren" '
                    f'<i>(webhook event yok)</i> görünür — webhook subscribe öncesi '
                    f'yapılmış değişiklikler veya event kaybı olabilir. Zaman geçtikçe '
                    f'bu oran düşer.</p>'
                )

        # Silinen task'lar — ClickUp non-Enterprise'da "kim sildi" alınamaz.
        silindi = summary.get("silindi", 0)
        silindi_disclaimer = ""
        if silindi > 0:
            silindi_disclaimer = (
                '<p style="background:#e7f3fe; padding:12px; '
                'border-left:4px solid #2196f3;">'
                'ℹ️ <b>Silinen task\'lar:</b> ClickUp non-Enterprise plan\'da silen '
                'kullanıcı bilgisi alınamaz. Bu liste TÜM silmeleri içerir, kendi '
                'yaptıklarınız dahil. Tanımadığınız bir silme görürseniz ClickUp '
                'arayüzünden geri yükleyebilirsiniz (Trash).</p>'
            )

        baglamno_tu = (
            '<p style="color:#888;font-size:12px;font-style:italic;">'
            'Atıf bilgileri webhook receiver tarafından gerçek zamanlı yakalanan '
            'event\'lerden alınır. Yorum kolonları sadece bağlam içindir; '
            'filtreleme tamamen webhook event store\'u ile yapılır.</p>'
        )
        body = f"""
        <html><body style="font-family: Arial, sans-serif; color: #{SF_GRAY};">
          <h2 style="color: #{SF_ORANGE};">Günlük Tarih Değişikliği Raporu</h2>
          {atif_uyarisi}
          {silindi_disclaimer}
          <p>Bir önceki çalıştırmadan bu yana tespit edilen değişiklikler ekteki Excel dosyasındadır.
             Tarihi kim değiştirdiği webhook event store'undan okunur;
             <b>{me_info.get('username', '')}</b> tarafından yapılan değişiklikler raporda yer almaz.</p>
          <ul>
            <li><b>Tarih değişti (yorum var):</b> {summary['degisti_yorumlu']}</li>
            <li><b>Tarih değişti (yorum yok):</b> {summary['degisti_yorumsuz']}</li>
            <li><b>Tarih kaldırıldı:</b> {summary['kaldirildi']}</li>
            <li><b>Silindi:</b> {silindi}</li>
          </ul>
          {atlanan_uyari}
          {baglamno_tu}
          <p style="color: #888; font-size: 12px;">
            Çalıştıran: {me_info.get('username', '')} ({me_info.get('email', '')}).
            Atıf metrikleri: verified={m.get('verified', 0)},
            no_events={m.get('no_events', 0)},
            filtered_self_only={m.get('filtered_self_only', 0)},
            total_diff={m.get('total_diff', 0)}.
            Silmeler: deletions={silindi},
            deletions_enriched={summary.get('deletions_enriched', 0)}.
          </p>
        </body></html>
        """
    msg.attach(MIMEText(body, 'html'))

    if dosya:
        with open(dosya, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={dosya}")
        msg.attach(part)

    with smtplib.SMTP_SSL(SMTP_SUNUCU, SMTP_PORT, timeout=30) as s:
        s.login(GONDEREN_MAIL, GONDEREN_SIFRE)
        s.send_message(msg)
    log("✨ Mail gönderildi.")


def main():
    log(f"🚀 Tarih değişiklik raporu başlatılıyor — {datetime.datetime.now()}")
    session = get_session()

    me = get_me(session)
    if not me or not me.get("id"):
        log("❌ ClickUp kullanıcı bilgisi alınamadı (token doğru mu?), çıkılıyor.")
        sys.exit(1)
    log(
        f"👤 '{me['username']}' ({me['email']}) olarak çalışılıyor — "
        f"bu kullanıcının yaptığı tarih değişiklikleri rapora eklenmez "
        f"(webhook event store üzerinden doğrulanır)."
    )

    log("📸 Güncel snapshot alınıyor...")
    current, atlanan_listeler = build_current_snapshot(session)

    prev = load_previous_snapshot()
    if prev is None:
        log("ℹ️ Önceki snapshot yok — ilk çalıştırma. Karşılaştırma yapılmıyor.")
        save_snapshot(current)
        try:
            send_mail(None, me, {
                "toplam_task": len(current),
                "atlanan": len(atlanan_listeler),
            }, True)
        except Exception as e:
            log(f"❌ Mail Hatası: {e}")
        return

    log("🔍 Değişiklikler hesaplanıyor (webhook event store + yorum bağlamı)...")
    changed_with, changed_no, removed, metrics = diff_snapshots(
        prev, current, session, me["id"]
    )

    # Silinen task'lar — webhook event store'da __deleted__ / __deleted_raw__
    snapshot_taken_ms = _snapshot_saved_at_ms(prev)
    deletion_rows = get_deletions_in_window(snapshot_taken_ms)
    deletions = [_parse_deletion_row(r) for r in deletion_rows]
    deletions_enriched = sum(1 for d in deletions if d["field"] == "__deleted__")
    enriched_pct = (
        deletions_enriched / max(len(deletions), 1) * 100
        if deletions else 0
    )
    log(
        f"📊 Silinen task'lar: deletions={len(deletions)}, "
        f"enriched={deletions_enriched} ({enriched_pct:.0f}%)"
    )

    log(
        f"📊 Sonuç: değişti+yorum={len(changed_with)}, "
        f"değişti-yorum={len(changed_no)}, kaldırıldı={len(removed)}, "
        f"silindi={len(deletions)}, atlanan liste={len(atlanan_listeler)}."
    )

    if not (changed_with or changed_no or removed or deletions or atlanan_listeler):
        log("ℹ️ Bu çalıştırmada değişiklik tespit edilmedi, mail gönderilmiyor.")
        save_snapshot(current)
        return

    dosya = write_excel(
        changed_with, changed_no, removed, atlanan_listeler, deletions
    )
    summary = {
        "degisti_yorumlu": len(changed_with),
        "degisti_yorumsuz": len(changed_no),
        "kaldirildi": len(removed),
        "silindi": len(deletions),
        "deletions_enriched": deletions_enriched,
        "atlanan": len(atlanan_listeler),
        "metrics": metrics,
    }
    try:
        send_mail(dosya, me, summary, False)
    except Exception as e:
        log(f"❌ Mail Hatası: {e}")
    finally:
        if not KEEP_EXCEL_ON_DISK and os.path.exists(dosya):
            os.remove(dosya)
        elif KEEP_EXCEL_ON_DISK and os.path.exists(dosya):
            log(f"📁 KEEP_EXCEL_ON_DISK=true → '{dosya}' diskte tutuluyor.")

    save_snapshot(current)
    log("✅ İşlem tamamlandı.")


if __name__ == "__main__":
    main()
