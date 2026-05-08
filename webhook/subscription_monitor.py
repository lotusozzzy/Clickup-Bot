#!/usr/bin/env python3
"""ClickUp webhook abonelik sağlık kontrolü.

Her 6 saatte bir cron ile çalışır. Kontrol ettikleri:
  - ENDPOINT (default: https://webhook.solarfame.com/clickup-webhook) için
    workspace'te kayıtlı webhook var mı
  - status alanı 'active' mi (5xx sonrası ClickUp 'suspended' yapabilir)
  - health.fail_count > 5 mi

Anomali bulursa mail atar. SPAM koruması: aynı cooldown içinde (24sa)
ikinci mail göndermez. Cooldown timestamp'i ~/clickup-bot/sub_monitor_alert.txt
dosyasında.

Otomatik recovery yapmaz - sadece bildirir. Manuel kurtarma:
    python3 webhook/subscribe_webhook.py --replace
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook._envloader import ensure_env  # noqa: E402

ensure_env()

from clickup_bot import WORKSPACE_ID, get_session  # noqa: E402
from webhook._mailer import send_alert  # noqa: E402

ENDPOINT = os.environ.get(
    "WEBHOOK_ENDPOINT", "https://webhook.solarfame.com/clickup-webhook"
)
COOLDOWN_FILE = Path("~/clickup-bot/sub_monitor_alert.txt").expanduser()
COOLDOWN_HOURS = 24
FAIL_COUNT_THRESHOLD = 5


def _ts():
    return datetime.datetime.now().isoformat(timespec="seconds")


def read_last_alert():
    try:
        return float(COOLDOWN_FILE.read_text().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0.0


def write_last_alert(epoch):
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_FILE.write_text(str(epoch))


def collect_anomalies(session):
    """Returns (anomalies_list, debug_dump_dict)."""
    url = f"https://api.clickup.com/api/v2/team/{WORKSPACE_ID}/webhook"
    try:
        r = session.get(url, timeout=20)
    except Exception as e:  # noqa: BLE001
        return [f"API çağrısı başarısız: {e}"], {}

    if r.status_code != 200:
        return (
            [f"GET /team/{WORKSPACE_ID}/webhook → HTTP {r.status_code}: {r.text[:200]}"],
            {},
        )

    try:
        data = r.json()
    except ValueError:
        return ["Webhook listesi JSON parse hatası"], {}

    webhooks = data.get("webhooks") or []
    debug = {"webhook_count": len(webhooks), "endpoint": ENDPOINT}

    ours = [w for w in webhooks if w.get("endpoint") == ENDPOINT]
    debug["matching_count"] = len(ours)

    anomalies = []

    if not ours:
        anomalies.append(
            f"Bu endpoint için kayıtlı webhook YOK: {ENDPOINT}. "
            f"Workspace'te toplam {len(webhooks)} webhook var ama hiçbiri bu adres değil."
        )
        debug["all_webhooks"] = webhooks
        return anomalies, debug

    for w in ours:
        wid = w.get("id", "?")
        # ClickUp'ta status birden fazla yerde olabilir
        status = (
            (w.get("health") or {}).get("status")
            or w.get("status")
            or ""
        )
        fail_count = (w.get("health") or {}).get("fail_count", 0)

        if status and status.lower() != "active":
            anomalies.append(
                f"webhook id={wid} status='{status}' (active beklenir)"
            )
        try:
            fc = int(fail_count)
        except (ValueError, TypeError):
            fc = 0
        if fc > FAIL_COUNT_THRESHOLD:
            anomalies.append(
                f"webhook id={wid} health.fail_count={fc} (>{FAIL_COUNT_THRESHOLD})"
            )

    debug["our_webhooks"] = ours
    return anomalies, debug


def main():
    session = get_session()
    anomalies, debug = collect_anomalies(session)

    if not anomalies:
        print(f"{_ts()} OK ({debug.get('matching_count', 0)} webhook eşleşti)")
        return 0

    print(f"{_ts()} ANOMALI ({len(anomalies)} adet):")
    for a in anomalies:
        print(f"  - {a}")

    now = time.time()
    last_alert = read_last_alert()
    delta_h = (now - last_alert) / 3600
    if last_alert and delta_h < COOLDOWN_HOURS:
        print(
            f"  cooldown ({COOLDOWN_HOURS}sa) içinde - son uyarı {delta_h:.1f}sa önce, "
            f"mail atılmıyor."
        )
        return 1

    items_html = "".join(f"<li>{a}</li>" for a in anomalies)
    debug_dump = json.dumps(debug, indent=2, ensure_ascii=False)
    body = f"""\
<html><body style="font-family: Arial, sans-serif; color:#333;">
  <h2 style="color:#cc6600;">⚠️ ClickUp webhook anomalisi</h2>
  <ul>
    {items_html}
  </ul>
  <p><b>Endpoint:</b> {ENDPOINT}</p>
  <p><b>Manuel kurtarma:</b><br>
    <code>cd ~/clickup-bot &amp;&amp; python3 webhook/subscribe_webhook.py --replace</code><br>
    (mevcut webhook'ları silip yenisini açar, .env'deki secret'ı günceller,
    service'i restart eder.)
  </p>
  <h3>Debug dump</h3>
  <pre style="background:#f4f4f4; padding:10px; font-size:11px; overflow-x:auto;">{debug_dump}</pre>
  <hr>
  <p style="color:#888; font-size:11px;">
    Bu uyarı önümüzdeki {COOLDOWN_HOURS} saat boyunca tekrar gönderilmeyecek
    (cooldown). Hemen sıfırlamak için: <code>rm {COOLDOWN_FILE}</code>
  </p>
</body></html>
"""
    try:
        to = send_alert("⚠️ ClickUp webhook anomalisi", body)
        print(f"  ALERT mail sent → {to}")
        write_last_alert(now)
    except Exception as e:  # noqa: BLE001
        print(f"  ALERT mail FAILED: {e}")
        # cooldown'ı set ETME - bir saat sonra tekrar denesin
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
