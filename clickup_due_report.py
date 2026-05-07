"""ClickUp günlük tarih değişiklik raporu.

Cariler haricindeki tüm Space'lerdeki AÇIK task'lar üzerinde:
  - Bir önceki snapshot'a göre due_date değişen task'ları bulur.
  - "Değişti + son yorumu yazan SEN değilsin" → Sekme 1
  - "Değişti + hiç yorum yok ya da son yorumu sen yazmışsın" → Sekme 2
  - "Önceden tarih vardı, şu an yok" (tarih kaldırıldı) → Sekme 3

İlk çalıştırmada karşılaştıracak veri olmadığı için sadece snapshot
kaydedilir ve bilgi maili atılır.

Snapshot dosyası: due_date_snapshot.json (script ile aynı dizinde).
Hassas ayarlar (API token, mail şifresi) clickup_bot modülünden import
edilir; o da değerleri os.environ'dan okur.
"""

import datetime
import json
import os
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

EXCLUDE_SPACE = "Cariler"
SNAPSHOT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "due_date_snapshot.json"
)


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
                snapshot[tid] = {
                    "due_date": _normalize_due(t.get("due_date")),
                    "name": t.get("name", ""),
                    "url": t.get("url", ""),
                    "list_name": l["name"],
                    "space_name": space_name,
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
    payload = {
        "saved_at": datetime.datetime.now().isoformat(),
        "tasks": snapshot,
    }
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


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


def extract_due_date_history(task_data):
    """Task JSON'undan SADECE due_date değişikliklerini en yeniden eskiye sırala."""
    if not task_data:
        return []
    items = task_data.get("history_items") or []
    out = []
    for h in items:
        if h.get("field") != "due_date":
            continue
        user = h.get("user") or {}
        try:
            date_ms = int(h.get("date") or 0)
        except (ValueError, TypeError):
            date_ms = 0
        out.append({
            "user_id": user.get("id"),
            "username": user.get("username", ""),
            "date_ms": date_ms,
        })
    out.sort(key=lambda x: x["date_ms"], reverse=True)
    return out


def fetch_all_comments(session, task_id):
    """Task'ın tüm yorumlarını en yeniden eskiye sırala."""
    r = safe_get(
        session,
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        attempt_label=f"comments {task_id}",
    )
    if r is None or r.status_code != 200:
        return []
    try:
        comments = r.json().get("comments", []) or []
    except ValueError:
        return []

    def _key(c):
        try:
            return int(c.get("date") or 0)
        except (ValueError, TypeError):
            return 0
    comments.sort(key=_key, reverse=True)
    return comments


def _extract_comment_text(comment):
    if not comment:
        return ""
    if comment.get("comment_text"):
        return comment["comment_text"]
    parts = comment.get("comment") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


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


def _attribute_change(history, my_user_id, snapshot_taken_ms):
    """Bir tarih değişikliğini atfeder.

    Returns:
        skip (bool):     True ise satır rapordan çıkarılır (sadece kullanıcı yapmış)
        changer_name:    'Tarihi Değiştiren' kolonu için (None ise dolduran tarafça doldurulur)
        quality (str):   metrik için kalite etiketi
            'verified'   → snapshot penceresinde başkası bulundu
            'self_only'  → snapshot penceresinde sadece kullanıcı (skip=True)
            'incomplete' → snapshot var ama pencerede hiç kayıt yok (anomali)
            'fallback'   → snapshot zamanı yok, en yeni overall'a düştük
            'missing'    → history hiç yok
    """
    if not history:
        return (False, None, "missing")

    if snapshot_taken_ms is not None:
        period = [h for h in history if h["date_ms"] > snapshot_taken_ms]
        others = [h for h in period if h["user_id"] != my_user_id]

        if not period:
            # Diff var ama pencerede history yok → anomali
            return (False, None, "incomplete")

        if not others:
            # Pencerede sadece ben → atla
            return (True, None, "self_only")

        return (False, others[0]["username"] or "(bilinmiyor)", "verified")

    # Snapshot zamanı yok → en yeni overall fallback
    most_recent = history[0]
    if most_recent["user_id"] == my_user_id:
        return (True, None, "fallback")
    return (False, most_recent["username"] or "(bilinmiyor)", "fallback")


def diff_snapshots(prev, curr, session, my_user_id):
    """Üç liste + metrik dict döner.

    Filtre: Tarih değişikliğini SADECE kullanıcı yapmışsa (snapshot
    penceresinde history'deki tüm due_date kayıtlarının user.id'si
    my_user_id ile eşleşiyorsa) o satır rapordan çıkarılır. Yorumlar
    filtreleme kararına etki etmez; sadece bağlam için gösterilir.
    """
    changed_with_comment = []
    changed_no_comment = []
    removed = []

    metrics = {
        "total_diff": 0,         # diff yakalanan toplam task
        "filtered_mine": 0,      # ben yaptığım için atlanan
        "verified": 0,           # snapshot penceresinde başkası bulundu
        "fallback": 0,           # snapshot zamanı yok, overall kullanıldı
        "incomplete": 0,         # pencerede history yok (anomali)
        "missing": 0,            # history hiç yok
    }

    prev_tasks = (prev or {}).get("tasks", {}) or {}
    snapshot_taken_ms = _snapshot_saved_at_ms(prev)

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

        metrics["total_diff"] += 1

        # Task'ı tek seferde çek (history + date_updated bir arada)
        task_data = fetch_task(session, tid)
        time.sleep(BASE_SLEEP)
        history = extract_due_date_history(task_data)

        skip, changer_name, quality = _attribute_change(
            history, my_user_id, snapshot_taken_ms
        )

        if skip:
            metrics["filtered_mine"] += 1
            continue

        metrics[quality] += 1

        # Yorumları çek (tek defa, hem son yorum hem fallback için)
        comments = fetch_all_comments(session, tid)
        time.sleep(BASE_SLEEP)

        last_c = comments[0] if comments else None
        last_comment_by = ""
        last_comment_text = ""
        if last_c:
            user = last_c.get("user") or {}
            last_comment_by = user.get("username", "")
            last_comment_text = _extract_comment_text(last_c)

        # Fallback bağlam: history yok/eksikse date_updated yakını yorumu sahibini al
        yorum_tahmin = ""
        if quality in ("missing", "incomplete") and task_data:
            try:
                date_updated_ms = int(task_data.get("date_updated") or 0)
            except (ValueError, TypeError):
                date_updated_ms = 0
            if date_updated_ms:
                near = _comment_near_date(comments, date_updated_ms)
                if near:
                    near_user = near.get("user") or {}
                    yorum_tahmin = near_user.get("username") or ""

        display_changer = changer_name if changer_name else "(history yok)"

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
        if last_c:
            changed_with_comment.append(row)
        else:
            row["last_comment_by"] = ""
            row["last_comment_text"] = ""
            changed_no_comment.append(row)

    in_report = metrics["total_diff"] - metrics["filtered_mine"]
    uncovered = metrics["incomplete"] + metrics["missing"]
    coverage_pct = (
        (metrics["verified"] + metrics["fallback"]) / max(in_report, 1) * 100
    )
    uncovered_pct = uncovered / max(in_report, 1) * 100
    log(
        f"📊 Atıf metrikleri: total_diff={metrics['total_diff']}, "
        f"filtered_mine={metrics['filtered_mine']}, in_report={in_report}, "
        f"verified={metrics['verified']}, fallback={metrics['fallback']}, "
        f"incomplete={metrics['incomplete']}, missing={metrics['missing']} "
        f"({coverage_pct:.0f}% atıflandı, {uncovered_pct:.0f}% atıflanamadı)"
    )

    return changed_with_comment, changed_no_comment, removed, metrics


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


def write_excel(changed_with, changed_no, removed, atlanan_listeler=None):
    atlanan_listeler = atlanan_listeler or []
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
        # Atıf güvenilirliği uyarısı (>%10 atıflanamadıysa)
        atif_uyarisi = ""
        m = summary.get("metrics") or {}
        in_report = m.get("total_diff", 0) - m.get("filtered_mine", 0)
        uncovered = m.get("incomplete", 0) + m.get("missing", 0)
        if in_report > 0:
            uncovered_pct = uncovered / in_report * 100
            if uncovered_pct > 10:
                atif_uyarisi = (
                    f'<p style="background:#fff3cd; padding:12px; '
                    f'border-left:4px solid #ffa500;">'
                    f'<b>⚠️ Atıf güvenilirliği uyarısı:</b> Bu raporda {in_report} '
                    f'satırın {uncovered} tanesi ({uncovered_pct:.0f}%) için ClickUp '
                    f'history_items dönmedi. Bu satırlarda "Tarihi Değiştiren" '
                    f'<i>(history yok)</i> görünür ve atıf doğrulanamadı — '
                    f'kullanıcının kendi değişikliği olabilir.</p>'
                )

        baglamno_tu = (
            '<p style="color:#888;font-size:12px;font-style:italic;">'
            'Yorum kolonları sadece bağlam içindir, filtreleme tamamen task '
            'tarihçesi (history_items) ile yapılır.</p>'
        )
        body = f"""
        <html><body style="font-family: Arial, sans-serif; color: #{SF_GRAY};">
          <h2 style="color: #{SF_ORANGE};">Günlük Tarih Değişikliği Raporu</h2>
          {atif_uyarisi}
          <p>Bir önceki çalıştırmadan bu yana tespit edilen değişiklikler ekteki Excel dosyasındadır.
             Tarihi gerçekten kim değiştirdiği ClickUp'ın task tarihçesinden okunur;
             <b>{me_info.get('username', '')}</b> tarafından yapılan değişiklikler raporda yer almaz.</p>
          <ul>
            <li><b>Tarih değişti (yorum var):</b> {summary['degisti_yorumlu']}</li>
            <li><b>Tarih değişti (yorum yok):</b> {summary['degisti_yorumsuz']}</li>
            <li><b>Tarih kaldırıldı:</b> {summary['kaldirildi']}</li>
          </ul>
          {atlanan_uyari}
          {baglamno_tu}
          <p style="color: #888; font-size: 12px;">
            Çalıştıran: {me_info.get('username', '')} ({me_info.get('email', '')}).
            Atıf metrikleri: verified={m.get('verified', 0)}, fallback={m.get('fallback', 0)},
            incomplete={m.get('incomplete', 0)}, missing={m.get('missing', 0)},
            filtered_mine={m.get('filtered_mine', 0)}.
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
        f"(history_items üzerinden doğrulanır)."
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

    log("🔍 Değişiklikler hesaplanıyor (değişen task'lar için history + yorum çekiliyor)...")
    changed_with, changed_no, removed, metrics = diff_snapshots(
        prev, current, session, me["id"]
    )

    log(
        f"📊 Sonuç: değişti+yorum={len(changed_with)}, "
        f"değişti-yorum={len(changed_no)}, kaldırıldı={len(removed)}, "
        f"atlanan liste={len(atlanan_listeler)}."
    )

    if not (changed_with or changed_no or removed or atlanan_listeler):
        log("ℹ️ Bu çalıştırmada değişiklik tespit edilmedi, mail gönderilmiyor.")
        save_snapshot(current)
        return

    dosya = write_excel(changed_with, changed_no, removed, atlanan_listeler)
    summary = {
        "degisti_yorumlu": len(changed_with),
        "degisti_yorumsuz": len(changed_no),
        "kaldirildi": len(removed),
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
