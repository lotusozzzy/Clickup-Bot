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
import time

from flask import Flask, jsonify, request

from webhook.db import get_connection, init_db
from webhook.logger import get_logger


WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_STATS_TOKEN = os.environ.get("WEBHOOK_STATS_TOKEN", "")

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
