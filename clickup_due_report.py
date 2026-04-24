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
    ALICI_MAIL,
    BASE_SLEEP,
    GONDEREN_MAIL,
    GONDEREN_SIFRE,
    MAX_PAGES_PER_LIST,
    SF_GRAY,
    SF_ORANGE,
    SMTP_PORT,
    SMTP_SUNUCU,
    WORKSPACE_ID,
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

    Dönen dict: {task_id: {due_date, name, url, list_name, space_name}}.
    """
    snapshot = {}
    spaces = get_target_spaces(session)
    log(f"📦 {len(spaces)} space taranacak (Cariler hariç).")
    for s in spaces:
        space_name = s.get("name", "")
        lists = get_all_lists_in_space(session, s["id"], space_name)
        log(f"  🗂️  '{space_name}' içinde {len(lists)} liste.")
        for l in lists:
            log(f"    🔍 {l['name']} taranıyor...")
            tasks = fetch_open_tasks(session, l["id"], l["name"])
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
    return snapshot


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


def _comment_text(comment):
    """ClickUp comment objesinden düz metin çıkarır."""
    if comment.get("comment_text"):
        return comment["comment_text"]
    parts = []
    for chunk in comment.get("comment", []) or []:
        if isinstance(chunk, dict) and "text" in chunk:
            parts.append(chunk["text"])
    return "".join(parts)


def get_last_comment(session, task_id):
    r = safe_get(
        session,
        f"https://api.clickup.com/api/v2/task/{task_id}/comment",
        attempt_label=f"comments {task_id}",
    )
    if r is None or r.status_code != 200:
        return None
    comments = r.json().get("comments", []) or []
    if not comments:
        return None
    # Tarihe göre azalan sırala (en yeni en başta)
    def _date_key(c):
        try:
            return int(c.get("date", "0"))
        except (ValueError, TypeError):
            return 0
    comments.sort(key=_date_key, reverse=True)
    last = comments[0]
    user = last.get("user") or {}
    return {
        "user_id": user.get("id"),
        "username": user.get("username", ""),
        "text": _comment_text(last),
        "date": last.get("date"),
    }


def diff_snapshots(prev, curr, session, my_user_id):
    """Üç liste döner: değişti+yorum, değişti-yorum, kaldırıldı."""
    changed_with_comment = []
    changed_no_comment = []
    removed = []

    prev_tasks = (prev or {}).get("tasks", {}) or {}

    all_ids = set(prev_tasks.keys()) | set(curr.keys())
    for tid in all_ids:
        p = prev_tasks.get(tid)
        c = curr.get(tid)
        prev_due = _normalize_due(p.get("due_date")) if p else None
        curr_due = _normalize_due(c.get("due_date")) if c else None

        # İkisinde de tarih yoksa ilgilenmiyoruz
        if not prev_due and not curr_due:
            continue

        # Task şu an açık değil (kapanmış/arşivlenmiş/silinmiş) → atla
        if not c:
            continue

        # Tarih kaldırıldı: önceden vardı, şimdi yok
        if prev_due and not curr_due:
            removed.append({
                "task_id": tid,
                "name": c.get("name") or (p.get("name") if p else ""),
                "list_name": c.get("list_name", ""),
                "space_name": c.get("space_name", ""),
                "url": c.get("url", ""),
                "old_date": fmt_date(prev_due),
            })
            continue

        # Yeni eklenen tarih: kullanıcı bunları istemedi
        if not prev_due and curr_due:
            continue

        # İkisi de var, karşılaştır
        if str(prev_due) != str(curr_due):
            last_comment = get_last_comment(session, tid)
            time.sleep(BASE_SLEEP)

            row = {
                "task_id": tid,
                "name": c.get("name", ""),
                "list_name": c.get("list_name", ""),
                "space_name": c.get("space_name", ""),
                "url": c.get("url", ""),
                "old_date": fmt_date(prev_due),
                "new_date": fmt_date(curr_due),
                "last_comment_by": (last_comment or {}).get("username", ""),
                "last_comment_text": (last_comment or {}).get("text", ""),
            }

            last_user_id = (last_comment or {}).get("user_id")
            if last_comment and last_user_id and last_user_id != my_user_id:
                changed_with_comment.append(row)
            else:
                row["last_comment_by"] = ""
                row["last_comment_text"] = ""
                changed_no_comment.append(row)

    return changed_with_comment, changed_no_comment, removed


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


def write_excel(changed_with, changed_no, removed):
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
                    "Son Yorumu Yazan", "Son Yorum", "URL"]
    headers_no = ["Task", "Space", "Liste", "Eski Tarih", "Yeni Tarih", "URL"]
    headers_removed = ["Task", "Space", "Liste", "Kaldırılan Tarih", "URL"]

    _add_sheet(
        wb, "Tarih Değişti + Yorum", headers_with,
        [[r["name"], r["space_name"], r["list_name"], r["old_date"], r["new_date"],
          r["last_comment_by"], r["last_comment_text"], r["url"]] for r in changed_with],
        "Tbl_DegistiYorumlu",
        header_fill, header_font, wrap_alignment, thin_border,
    )
    _add_sheet(
        wb, "Tarih Değişti - Yorum Yok", headers_no,
        [[r["name"], r["space_name"], r["list_name"], r["old_date"], r["new_date"], r["url"]]
         for r in changed_no],
        "Tbl_DegistiYorumsuz",
        header_fill, header_font, wrap_alignment, thin_border,
    )
    _add_sheet(
        wb, "Tarih Kaldırıldı", headers_removed,
        [[r["name"], r["space_name"], r["list_name"], r["old_date"], r["url"]]
         for r in removed],
        "Tbl_Kaldirildi",
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

    if is_first_run:
        body = f"""
        <html><body style="font-family: Arial, sans-serif; color: #{SF_GRAY};">
          <h2 style="color: #{SF_ORANGE};">İlk Snapshot Oluşturuldu</h2>
          <p>Bugün ilk kez çalıştırıldığı için karşılaştıracak geçmiş veri yok.
             <b>{summary['toplam_task']}</b> açık task izlemeye alındı.</p>
          <p>Yarın akşamki rapordan itibaren tarih değişiklikleri görünecek.</p>
          <p style="color: #888; font-size: 12px;">
            Çalıştıran: {me_info.get('username', '')} ({me_info.get('email', '')})
          </p>
        </body></html>
        """
    else:
        body = f"""
        <html><body style="font-family: Arial, sans-serif; color: #{SF_GRAY};">
          <h2 style="color: #{SF_ORANGE};">Günlük Tarih Değişikliği Raporu</h2>
          <p>Bir önceki çalıştırmadan bu yana tespit edilen değişiklikler ekteki Excel dosyasındadır.</p>
          <ul>
            <li><b>Tarih değişti + son yorumu başkası yazmış:</b> {summary['degisti_yorumlu']}</li>
            <li><b>Tarih değişti - yorum yok / sadece sen yorumlamışsın:</b> {summary['degisti_yorumsuz']}</li>
            <li><b>Tarih kaldırıldı:</b> {summary['kaldirildi']}</li>
          </ul>
          <p style="color: #888; font-size: 12px;">
            Çalıştıran: {me_info.get('username', '')} ({me_info.get('email', '')}) — bu kullanıcının
            yaptığı değişikliklere ait yorumlar 2. sekmeye düşer.
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
    log(f"👤 '{me['username']}' ({me['email']}) olarak çalışılıyor — bu kullanıcının "
        f"yorumladığı değişiklikler 'yorumsuz' tarafına gidecek.")

    log("📸 Güncel snapshot alınıyor...")
    current = build_current_snapshot(session)
    log(f"✓ Toplam {len(current)} açık task bulundu.")

    prev = load_previous_snapshot()
    if prev is None:
        log("ℹ️ Önceki snapshot yok — ilk çalıştırma. Karşılaştırma yapılmıyor.")
        save_snapshot(current)
        try:
            send_mail(None, me, {"toplam_task": len(current)}, True)
        except Exception as e:
            log(f"❌ Mail Hatası: {e}")
        return

    log("🔍 Değişiklikler hesaplanıyor (değişen task'lar için yorumlar çekiliyor)...")
    changed_with, changed_no, removed = diff_snapshots(prev, current, session, me["id"])

    log(
        f"📊 Sonuç: değişti+yorum={len(changed_with)}, "
        f"değişti-yorum={len(changed_no)}, kaldırıldı={len(removed)}."
    )

    if not (changed_with or changed_no or removed):
        log("ℹ️ Bu çalıştırmada değişiklik tespit edilmedi, mail gönderilmiyor.")
        save_snapshot(current)
        return

    dosya = write_excel(changed_with, changed_no, removed)
    summary = {
        "degisti_yorumlu": len(changed_with),
        "degisti_yorumsuz": len(changed_no),
        "kaldirildi": len(removed),
    }
    try:
        send_mail(dosya, me, summary, False)
    except Exception as e:
        log(f"❌ Mail Hatası: {e}")
    finally:
        if os.path.exists(dosya):
            os.remove(dosya)

    save_snapshot(current)
    log("✅ İşlem tamamlandı.")


if __name__ == "__main__":
    main()
