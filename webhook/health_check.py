#!/usr/bin/env python3
"""Webhook receiver canlılık kontrolü.

Saatlik cron ile çalıştırılır. /health endpoint'ine GET atar:
  - 200 + body 'ok' bekler
  - Başarı → counter dosyasını 0'a sıfırla
  - Fail  → counter += 1
  - Counter == 3 olunca:
      * Mail at: subject "🚨 Webhook receiver DOWN", journal log'la birlikte
      * Counter'ı MUTE_VALUE (100) yap → manuel reset edilene kadar bir
        daha mail gelmez (spam koruması)

Manuel reset:
    rm ~/clickup-bot/health_fail_count.txt

Log:
    cron --> ~/clickup-bot/health_check.log
"""

import datetime
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook._envloader import ensure_env  # noqa: E402

ensure_env()

import requests  # noqa: E402

from webhook._mailer import send_alert  # noqa: E402

URL = os.environ.get("WEBHOOK_HEALTH_URL", "https://webhook.solarfame.com/health")
COUNTER_FILE = Path("~/clickup-bot/health_fail_count.txt").expanduser()
ALERT_THRESHOLD = 3
MUTE_VALUE = 100  # alert sonrası counter buna set, manuel reset edilmedikçe susar
SERVICE_NAME = "clickup-webhook"


def _ts():
    return datetime.datetime.now().isoformat(timespec="seconds")


def read_counter():
    try:
        return int(COUNTER_FILE.read_text().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def write_counter(n):
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    COUNTER_FILE.write_text(str(n))


def check_health():
    try:
        r = requests.get(URL, timeout=10)
        return r.status_code == 200 and r.text.strip().lower() == "ok", (
            f"HTTP {r.status_code}, body={r.text[:60]!r}"
        )
    except requests.RequestException as e:
        return False, f"request error: {e}"


def grab_journal():
    """Best-effort: journalctl tail (sudo'suz, ubuntu adm/systemd-journal grubuna bağlı)."""
    for cmd in (
        ["journalctl", "-u", SERVICE_NAME, "-n", "50", "--no-pager"],
        ["sudo", "-n", "journalctl", "-u", SERVICE_NAME, "-n", "50", "--no-pager"],
    ):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "(journalctl okunamadı - manuel kontrol: 'sudo journalctl -u clickup-webhook -n 50')"


def main():
    ok, detail = check_health()
    counter = read_counter()
    ts = _ts()

    if ok:
        if counter > 0:
            print(f"{ts} RECOVERED counter {counter} → 0  (was failing, now {detail})")
        else:
            print(f"{ts} OK  ({detail})")
        write_counter(0)
        return 0

    new_counter = counter + 1
    write_counter(new_counter)
    print(f"{ts} FAIL counter={new_counter}  ({detail})")

    if new_counter < ALERT_THRESHOLD:
        return 1

    if new_counter > ALERT_THRESHOLD:
        # Zaten alert gönderildi (MUTE_VALUE'ya set edilmiş) - sessiz kal
        if new_counter < MUTE_VALUE:
            # Tuhaf - normalde MUTE_VALUE'da olmalı. Yine de mail atma.
            print(f"{ts} muted (counter {new_counter}); manuel reset gerek.")
        return 1

    # new_counter == ALERT_THRESHOLD: mail gönder, sonra mute
    journal = grab_journal()
    body = f"""\
<html><body style="font-family: Arial, sans-serif; color:#333;">
  <h2 style="color:#cc0000;">🚨 Webhook receiver DOWN</h2>
  <p>Üst üste {ALERT_THRESHOLD} sağlık kontrolü başarısız.</p>
  <ul>
    <li><b>URL:</b> {URL}</li>
    <li><b>Son hata:</b> {detail}</li>
    <li><b>Zaman:</b> {ts}</li>
  </ul>
  <h3>Son journalctl (clickup-webhook)</h3>
  <pre style="background:#f4f4f4; padding:10px; font-size:11px;">{journal}</pre>
  <hr>
  <p>
    <b>Manuel kontrol/recovery:</b><br>
    <code>sudo systemctl status clickup-webhook</code><br>
    <code>sudo systemctl restart clickup-webhook</code><br>
    <br>
    Bu uyarı <b>sustu</b>; bir daha mail almak için:<br>
    <code>rm {COUNTER_FILE}</code>
  </p>
</body></html>
"""
    try:
        to = send_alert("🚨 Webhook receiver DOWN", body)
        print(f"{ts} ALERT sent → {to}")
    except Exception as e:  # noqa: BLE001
        print(f"{ts} ALERT send failed: {e}")
        # Mail atılamadı; counter ALERT_THRESHOLD'da kalsın ki bir saat sonra tekrar denesin
        write_counter(ALERT_THRESHOLD - 1)
        return 1

    write_counter(MUTE_VALUE)
    print(f"{ts} counter muted at {MUTE_VALUE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
