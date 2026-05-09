"""ClickUp webhook receiver (Faz 2).

Endpoint'ler:
  POST /clickup-webhook  - ClickUp event ingestion
  GET  /stats?token=...  - Debug istatistik

HMAC SHA256 doğrulaması:
  ClickUp dokümantasyonu (https://clickup.com/api/developer-portal/webhooks):
  signature = hex_hmac_sha256(secret, raw_request_body)
  header    = X-Signature

  Header isimlerinde varyasyon olasılığına karşı X-Signature-256 ve
  X-Hub-Signature-256 (GitHub-style) da denenir; doğrulama compare_digest
  ile yapılır (timing-attack koruması).

Esnek payload parser:
  - history_items VEYA historyItems
  - field == 'due_date' (case-insensitive, snake/camelCase normalize)
  - event_id eksikse fallback olarak f'{task_id}-{date}-{field}' kullanılır
  - before/after string olarak saklanır (epoch_ms ya da null)

raw_payload her zaman saklanır → Faz 4'te ilk gerçek event geldiğinde
parse'ı revize etmek için referans elimizde olur.
"""

import hashlib
import hmac
import json
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

from webhook.db import get_connection, init_db
from webhook.logger import get_logger


WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_STATS_TOKEN = os.environ.get("WEBHOOK_STATS_TOKEN", "")

# Snapshot zenginleştirme — taskDeleted event geldiğinde son snapshot'tan
# task_name/list/space/due_date/assignees alınıyor. Daily report (clickup_due_report.py)
# bu dosyayı atomic write+rename ile güncelliyor; receiver mtime'a bakarak
# 10 MB+ dosyayı her event'te yeniden parse etmiyor.
SNAPSHOT_PATH = os.environ.get(
    "WEBHOOK_SNAPSHOT_PATH",
    os.path.expanduser("~/clickup-bot/due_date_snapshot.json"),
)
COMMENT_FETCH_TIMEOUT_S = float(
    os.environ.get("WEBHOOK_COMMENT_FETCH_TIMEOUT", "3")
)

_snapshot_lock = threading.Lock()
_snapshot_cache = {"mtime_ns": None, "data": None}

# ClickUp dokümantasyonuna göre asıl header. Diğerleri savunma amaçlı.
_SIGNATURE_HEADERS = ("X-Signature", "X-Signature-256", "X-Hub-Signature-256")

app = Flask(__name__)
log = get_logger()
init_db()


def _read_signature():
    for hname in _SIGNATURE_HEADERS:
        v = request.headers.get(hname)
        if v:
            # GitHub formatı 'sha256=<hex>'; ClickUp düz hex bekleniyor ama
            # her ihtimale karşı prefix'i temizle.
            if v.startswith("sha256="):
                v = v[7:]
            return v.strip()
    return ""


def verify_signature(raw_body, signature_hex):
    if not WEBHOOK_SECRET or not signature_hex:
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_hex)


def _normalize_field(value):
    if value is None:
        return ""
    return str(value).strip().lower().replace("_", "").replace("-", "")


def _is_due_date_field(field):
    return _normalize_field(field) == "duedate"


def _stringify(value):
    if value is None:
        return None
    return str(value)


def _coerce_int_ms(value):
    if value is None:
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def extract_changes(payload, task_id):
    """Payload'dan due_date değişikliklerini çıkar.

    Hem snake_case hem camelCase variantlarına bakar.
    """
    items = payload.get("history_items")
    if items is None:
        items = payload.get("historyItems") or []

    out = []
    for h in items:
        if not isinstance(h, dict):
            continue
        field = h.get("field") or h.get("fieldName") or ""
        if not _is_due_date_field(field):
            continue

        user = h.get("user") or h.get("userInfo") or {}
        user_id = user.get("id") if isinstance(user, dict) else None
        user_name = ""
        if isinstance(user, dict):
            user_name = (
                user.get("username")
                or user.get("name")
                or user.get("email")
                or ""
            )

        date_raw = h.get("date") or h.get("changedAt") or h.get("changed_at")
        changed_at_ms = _coerce_int_ms(date_raw)

        # event_id - resmi alan adı bilinmiyor, çoklu fallback
        event_id = (
            h.get("id")
            or h.get("event_id")
            or h.get("eventId")
            or f"{task_id}-{date_raw}-{_normalize_field(field)}"
        )

        out.append({
            "event_id": str(event_id),
            "field": str(field),
            "before": _stringify(h.get("before")),
            "after": _stringify(h.get("after")),
            "user_id": str(user_id) if user_id else None,
            "user_name": user_name,
            "changed_at_ms": changed_at_ms,
        })
    return out


def _event_type(payload):
    """Payload'daki event type string'ini lowercase normalize et.

    ClickUp top-level 'event' alanı genelde 'taskUpdated', 'taskDeleted'
    gibi camelCase. Defansif olarak 'eventType' variantına da bak.
    """
    value = payload.get("event") or payload.get("eventType") or ""
    return str(value).strip().lower()


def _is_task_deleted_event(payload):
    return _event_type(payload) == "taskdeleted"


def _extract_top_level_user(payload):
    """Payload top-level 'user'/'userInfo' alanından best-effort kullanıcı.

    Adım 1: raw save için yeterli. history_items içine girmiyoruz —
    gerçek taskDeleted payload'ı (Adım 3) görülmeden parse yazılmıyor.
    Adım 4'te yapılandırılmış parser eklenecek.
    """
    user = payload.get("user") or payload.get("userInfo") or {}
    if not isinstance(user, dict):
        return None, None
    user_id = user.get("id")
    user_name = (
        user.get("username")
        or user.get("name")
        or user.get("email")
        or None
    )
    return (
        str(user_id) if user_id else None,
        user_name or None,
    )


def _build_deleted_event_id(payload, task_id, received_at_ms):
    """taskDeleted için event_id türet (idempotent retry'da çakışmaması için).

    Birincil: payload'daki id/event_id/eventId. Yoksa
    'deleted-{task_id}-{received_at_ms}' — aynı silme event'i ms cinsinden
    aynı zamanda iki kez ulaşırsa çakışır, bu kabul edilebilir bir trade-off.
    """
    eid = (
        payload.get("event_id")
        or payload.get("eventId")
        or payload.get("id")
    )
    if eid:
        return f"deleted-{eid}"
    return f"deleted-{task_id or 'unknown'}-{received_at_ms}"


def _ingest_deleted_raw(payload, task_id, raw_payload_str, received_at_ms):
    """taskDeleted event'ini field='__deleted_raw__' ile DB'ye yaz.

    Parse YOK — sadece raw payload saklanır. Adım 4'te gerçek payload'a
    göre parser eklenip __deleted_raw__ kayıtları __deleted__ olarak
    yeniden işlenebilir.
    """
    user_id, user_name = _extract_top_level_user(payload)
    event_id = _build_deleted_event_id(payload, task_id, received_at_ms)

    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, task_id, field, before_value, after_value,
                    user_id, user_name, changed_at_ms, raw_payload, received_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    task_id or "",
                    "__deleted_raw__",
                    None,
                    None,
                    user_id,
                    user_name,
                    received_at_ms,
                    raw_payload_str,
                    received_at_ms,
                ),
            )
            if cur.rowcount > 0:
                log.info(
                    "Ingested taskDeleted (raw): task=%s user=%s event_id=%s",
                    task_id or "?",
                    user_name or user_id or "?",
                    event_id,
                )
                return jsonify({"ok": True, "ingested": 1, "kind": "deleted_raw"}), 200
            return jsonify({"ok": True, "ingested": 0, "kind": "deleted_raw_dup"}), 200
    except Exception as e:  # noqa: BLE001
        log.error("DB write failed (deleted_raw): %s", e, exc_info=True)
        return jsonify({"error": "db error"}), 500


def _load_snapshot_cached(path=None):
    """Snapshot.json'ı in-memory cache'le; mtime değiştiyse yeniden parse.

    Daily report dosyayı atomic rename ile yazıyor (clickup_due_report.save_snapshot)
    — yarım/corrupt JSON ihtimali yok. Cache key olarak st_mtime_ns kullanıyoruz;
    aynı mtime'da yeniden parse etmiyoruz, dolayısıyla 10 MB JSON event başına
    bir kez okunmuş oluyor (snapshot günde 1 kez yenileniyor).
    """
    p = path or SNAPSHOT_PATH
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("Snapshot stat failed (%s): %s", p, e)
        return None

    mtime_ns = st.st_mtime_ns
    with _snapshot_lock:
        if (
            _snapshot_cache["mtime_ns"] == mtime_ns
            and _snapshot_cache["data"] is not None
        ):
            return _snapshot_cache["data"]
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (IOError, ValueError) as e:
            log.warning("Snapshot read failed (%s): %s", p, e)
            return None
        _snapshot_cache["mtime_ns"] = mtime_ns
        _snapshot_cache["data"] = data
        log.info(
            "Snapshot reloaded: tasks=%d saved_at=%s",
            len(data.get("tasks") or {}),
            data.get("saved_at", "?"),
        )
        return data


def _snapshot_lookup(task_id, snapshot_data):
    """Snapshot'tan task için zenginleştirme alanlarını çek.

    Dönüş: dict (task_name, list_name, space_name, last_due_date, assignees)
    veya None — task_id snapshot'ta yoksa.
    """
    if not snapshot_data or not task_id:
        return None
    tasks = snapshot_data.get("tasks") or {}
    task = tasks.get(task_id)
    if not task:
        return None
    return {
        "task_name": task.get("name") or "",
        "list_name": task.get("list_name") or "",
        "space_name": task.get("space_name") or "",
        "last_due_date": task.get("due_date"),
        "assignees": list(task.get("assignees") or []),
    }


def _try_fetch_last_comment(task_id, timeout=None):
    """Best-effort: silinen task'ın son yorumunu dene.

    Silinmiş task /comment endpoint'i tipik olarak 404 (ITEM_013) — beklenen
    davranış, info olarak loglanır. Network/parse hatasında da None döner.
    Receiver hızlı kalsın diye timeout default 3 sn (env override edilebilir).
    """
    if not task_id:
        return None, None
    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        return None, None

    effective_timeout = (
        timeout if timeout is not None else COMMENT_FETCH_TIMEOUT_S
    )
    try:
        r = requests.get(
            f"https://api.clickup.com/api/v2/task/{task_id}/comment",
            headers={"Authorization": token},
            timeout=effective_timeout,
        )
    except requests.RequestException as e:
        log.info("Comment fetch error task=%s: %s", task_id, e)
        return None, None

    if r.status_code == 404:
        log.info("Comment fetch 404 (task gone): %s", task_id)
        return None, None
    if r.status_code != 200:
        log.info("Comment fetch HTTP %d task=%s", r.status_code, task_id)
        return None, None

    try:
        comments = (r.json() or {}).get("comments") or []
    except ValueError:
        return None, None
    if not comments:
        return None, None

    try:
        comments.sort(key=lambda c: int(c.get("date") or 0), reverse=True)
    except (TypeError, ValueError):
        pass

    latest = comments[0]
    user = latest.get("user") or {}
    author = user.get("username") or user.get("email") or None
    text = latest.get("comment_text")
    if not text:
        parts = latest.get("comment") or []
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict)
        )
    return author, (text or None)


def _ingest_deleted(payload, task_id, raw_payload_str, received_at_ms):
    """taskDeleted'ı snapshot zenginleştirmesi ile DB'ye yaz.

    Snapshot'ta task yoksa veya snapshot dosyası yoksa: __deleted_raw__
    fallback'i (Adım 1 davranışı korunur). Daily report her iki field'ı da
    okuduğu için bu graceful — eski payload'lar kayıp gitmez.
    """
    snapshot_data = _load_snapshot_cached()
    enriched = _snapshot_lookup(task_id, snapshot_data)

    if enriched is None:
        log.info(
            "taskDeleted snapshot miss task=%s — fallback to __deleted_raw__",
            task_id or "?",
        )
        return _ingest_deleted_raw(
            payload, task_id, raw_payload_str, received_at_ms
        )

    last_comment_author, last_comment_text = _try_fetch_last_comment(task_id)
    enriched["last_comment_author"] = last_comment_author
    enriched["last_comment_text"] = last_comment_text

    user_id, user_name = _extract_top_level_user(payload)
    event_id = _build_deleted_event_id(payload, task_id, received_at_ms)
    before_json = json.dumps(enriched, ensure_ascii=False)

    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, task_id, field, before_value, after_value,
                    user_id, user_name, changed_at_ms, raw_payload, received_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    task_id or "",
                    "__deleted__",
                    before_json,
                    None,
                    user_id,
                    user_name,
                    received_at_ms,
                    raw_payload_str,
                    received_at_ms,
                ),
            )
            if cur.rowcount > 0:
                log.info(
                    "Ingested taskDeleted: task=%s name=%r list=%r assignees=%d comment=%s",
                    task_id,
                    enriched["task_name"][:40],
                    enriched["list_name"],
                    len(enriched["assignees"]),
                    "yes" if last_comment_author else "no",
                )
                return jsonify({"ok": True, "ingested": 1, "kind": "deleted"}), 200
            return jsonify({"ok": True, "ingested": 0, "kind": "deleted_dup"}), 200
    except Exception as e:  # noqa: BLE001
        log.error("DB write failed (deleted): %s", e, exc_info=True)
        return jsonify({"error": "db error"}), 500


@app.route("/clickup-webhook", methods=["POST"])
def clickup_webhook():
    raw = request.get_data() or b""
    signature = _read_signature()

    if not verify_signature(raw, signature):
        log.warning(
            "HMAC fail: sig_present=%s body_len=%d remote=%s",
            bool(signature),
            len(raw),
            request.remote_addr,
        )
        return jsonify({"error": "unauthorized"}), 401

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.error("JSON parse failed: %s body_head=%r", e, raw[:200])
        return jsonify({"error": "invalid json"}), 400

    task_id = (
        payload.get("task_id")
        or payload.get("taskId")
        or ""
    )
    raw_payload_str = raw.decode("utf-8", errors="replace")
    received_at_ms = int(time.time() * 1000)

    # taskDeleted: snapshot zenginleştirmesi + best-effort comment fetch.
    # Snapshot miss durumunda __deleted_raw__ fallback'ine düşer (Adım 1 davranışı).
    if _is_task_deleted_event(payload):
        return _ingest_deleted(
            payload, task_id, raw_payload_str, received_at_ms
        )

    changes = extract_changes(payload, task_id)

    if not changes:
        log.info(
            "Event ack (no due_date change): task=%s event=%s",
            task_id,
            payload.get("event", "<unknown>"),
        )
        return jsonify({"ok": True, "ingested": 0}), 200

    inserted = 0
    try:
        with get_connection() as conn:
            for ch in changes:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO events (
                        event_id, task_id, field, before_value, after_value,
                        user_id, user_name, changed_at_ms, raw_payload, received_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ch["event_id"],
                        task_id,
                        ch["field"],
                        ch["before"],
                        ch["after"],
                        ch["user_id"],
                        ch["user_name"],
                        ch["changed_at_ms"],
                        raw_payload_str,
                        received_at_ms,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                    log.info(
                        "Ingested due_date: task=%s user=%s before=%s after=%s",
                        task_id,
                        ch["user_name"] or ch["user_id"] or "?",
                        ch["before"],
                        ch["after"],
                    )
    except Exception as e:  # noqa: BLE001
        log.error("DB write failed: %s", e, exc_info=True)
        return jsonify({"error": "db error"}), 500

    return jsonify({"ok": True, "ingested": inserted}), 200


@app.route("/stats", methods=["GET"])
def stats():
    token = request.args.get("token", "")
    if not WEBHOOK_STATS_TOKEN or not hmac.compare_digest(token, WEBHOOK_STATS_TOKEN):
        return jsonify({"error": "unauthorized"}), 401

    cutoff_24h_ms = int(time.time() * 1000) - 24 * 3600 * 1000
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        latest = conn.execute(
            "SELECT MAX(received_at_ms) FROM events"
        ).fetchone()[0]
        last_24h = conn.execute(
            "SELECT COUNT(*) FROM events WHERE received_at_ms > ?",
            (cutoff_24h_ms,),
        ).fetchone()[0]

    return jsonify({
        "total_events": total or 0,
        "latest_event_at": latest,
        "last_24h_count": last_24h or 0,
    }), 200
