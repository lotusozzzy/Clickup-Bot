#!/usr/bin/env python3
"""ClickUp'a webhook subscribe et + secret'ı .env'e yaz.

Kullanım:
    python3 webhook/subscribe_webhook.py             # dup varsa abort eder
    python3 webhook/subscribe_webhook.py --replace   # mevcut dup'ı silip yenisini açar

İşleyiş:
  1) Mevcut webhook'ları list - aynı endpoint zaten varsa abort/replace
  2) POST /api/v2/team/{team_id}/webhook
  3) Response'tan secret + webhook_id çıkar (esnek shape parse)
  4) .env'e WEBHOOK_SECRET=<secret> ve WEBHOOK_STATS_TOKEN=<random> yaz (atomik)
  5) Maskeli response'u webhook/subscribe_response.json'a yaz
  6) sudo -n systemctl restart clickup-webhook (olmazsa kullanıcıya talimat)
  7) Self-test: yeni secret ile fake-imzalı POST → 200 beklenir

Secret create response'unda BİR KEZ döner; daha sonra alınamaz.
Bu yüzden .env yazımı atomik (write+rename), sonra restart, sonra doğrulama.
"""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook._envloader import ensure_env  # noqa: E402

ensure_env()

from clickup_bot import WORKSPACE_ID, get_session  # noqa: E402

import requests  # noqa: E402

# -----------------------------------------------------------------------------
ENDPOINT = os.environ.get(
    "WEBHOOK_ENDPOINT", "https://webhook.solarfame.com/clickup-webhook"
)
EVENTS = ["taskUpdated", "taskDeleted"]  # receiver due_date filtreler + taskDeleted'i __deleted_raw__ olarak saklar
ENV_FILE = Path("~/clickup-bot/.env").expanduser()
RESPONSE_FILE = Path(
    os.path.dirname(os.path.abspath(__file__))
) / "subscribe_response.json"
SERVICE_NAME = "clickup-webhook"
# -----------------------------------------------------------------------------


def mask(value):
    if not value:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def find_existing(session, endpoint):
    url = f"https://api.clickup.com/api/v2/team/{WORKSPACE_ID}/webhook"
    r = session.get(url, timeout=20)
    if r.status_code != 200:
        print(f"❌ Webhook listesi alınamadı: {r.status_code} {r.text[:300]}")
        sys.exit(1)
    return [w for w in (r.json().get("webhooks") or []) if w.get("endpoint") == endpoint]


def delete_webhook(session, webhook_id):
    url = f"https://api.clickup.com/api/v2/webhook/{webhook_id}"
    r = session.delete(url, timeout=20)
    if r.status_code not in (200, 204):
        print(f"❌ Silme başarısız {webhook_id}: {r.status_code} {r.text[:300]}")
        sys.exit(1)


def extract_secret(resp):
    """Yaygın 3 shape'i sırayla dener."""
    if not isinstance(resp, dict):
        return None
    if isinstance(resp.get("secret"), str):
        return resp["secret"]
    inner = resp.get("webhook")
    if isinstance(inner, dict) and isinstance(inner.get("secret"), str):
        return inner["secret"]
    data = resp.get("data")
    if isinstance(data, dict) and isinstance(data.get("secret"), str):
        return data["secret"]
    return None


def extract_id(resp):
    if not isinstance(resp, dict):
        return None
    if isinstance(resp.get("id"), (str, int)):
        return str(resp["id"])
    inner = resp.get("webhook")
    if isinstance(inner, dict) and isinstance(inner.get("id"), (str, int)):
        return str(inner["id"])
    return None


def write_env_var(env_path: Path, key: str, value: str):
    """KEY=VALUE satırını override et / yoksa ekle. Atomik write+rename, izinleri korur."""
    lines = []
    found = False
    if env_path.exists():
        with env_path.open("r") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                found = True
                break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{key}={value}\n")

    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.writelines(lines)
    if env_path.exists():
        os.chmod(tmp, env_path.stat().st_mode & 0o777)
    else:
        os.chmod(tmp, 0o600)
    tmp.replace(env_path)


def deep_mask_secret(obj):
    """Response objesinin kopyasında secret alanlarını maskele."""
    if isinstance(obj, dict):
        return {
            k: (mask(v) if k == "secret" and isinstance(v, str) else deep_mask_secret(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [deep_mask_secret(x) for x in obj]
    return obj


def try_restart_service():
    """sudo -n ile passwordless restart dener; başarısızsa False."""
    try:
        rc = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", SERVICE_NAME],
            capture_output=True, text=True, timeout=15,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return rc == 0


def is_service_active():
    try:
        rc = subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE_NAME],
            timeout=5,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return rc == 0


def self_test(secret):
    """Yeni secret ile fake imzalı POST → 200 beklenir."""
    body = json.dumps({"event": "selftest", "history_items": []}).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        r = requests.post(
            ENDPOINT,
            data=body,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as e:
        return False, f"request error: {e}"
    return (r.status_code == 200), f"HTTP {r.status_code} body={r.text[:120]}"


def main():
    args = sys.argv[1:]
    replace = "--replace" in args or "--force" in args

    session = get_session()

    print(f"🔍 Mevcut webhook'lar kontrol ediliyor (endpoint={ENDPOINT})...")
    existing = find_existing(session, ENDPOINT)
    if existing:
        print(f"⚠️  Bu endpoint'te zaten {len(existing)} webhook var:")
        for w in existing:
            print(f"     id={w.get('id')}  events={w.get('events')}")
        if not replace:
            print()
            print("--replace olmadan dup yaratmıyorum. Seçenekler:")
            for w in existing:
                print(f"  • python3 webhook/delete_webhook.py {w.get('id')}")
            print("  • python3 webhook/subscribe_webhook.py --replace")
            sys.exit(2)
        print("  --replace verildi, hepsi siliniyor...")
        for w in existing:
            delete_webhook(session, w.get("id"))
            print(f"     silindi: {w.get('id')}")

    print()
    print(f"📡 Subscribe POST → {ENDPOINT}, events={EVENTS}")
    body = {"endpoint": ENDPOINT, "events": EVENTS}
    url = f"https://api.clickup.com/api/v2/team/{WORKSPACE_ID}/webhook"
    r = session.post(url, json=body, timeout=20)
    if r.status_code not in (200, 201):
        print(f"❌ Subscribe HATASI {r.status_code}:")
        print(r.text[:1000])
        sys.exit(1)

    try:
        resp = r.json()
    except ValueError:
        print("❌ Response JSON değil:")
        print(r.text[:600])
        sys.exit(1)

    webhook_id = extract_id(resp)
    secret = extract_secret(resp)

    if not webhook_id:
        print("❌ Response'tan webhook id okunamadı:")
        print(json.dumps(resp, indent=2)[:1000])
        sys.exit(1)
    if not secret:
        print("❌ Response'tan secret okunamadı (KRİTİK):")
        print(json.dumps(resp, indent=2)[:1000])
        print()
        print("Beklenen alanlar: secret / webhook.secret / data.secret")
        sys.exit(1)

    print(f"✓ Subscribe OK")
    print(f"     webhook_id : {webhook_id}")
    print(f"     secret     : {mask(secret)}")

    # 1) Maskeli response'u kaydet
    masked = deep_mask_secret(resp)
    RESPONSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RESPONSE_FILE.open("w") as f:
        json.dump(masked, f, indent=2, ensure_ascii=False)
    print(f"     dump       : {RESPONSE_FILE} (secret maskeli)")

    # 2) .env'e atomik yaz
    print()
    print("📝 .env güncelleniyor (atomik write+rename)...")
    stats_token = secrets.token_hex(32)
    write_env_var(ENV_FILE, "WEBHOOK_SECRET", secret)
    write_env_var(ENV_FILE, "WEBHOOK_STATS_TOKEN", stats_token)

    # 3) doğrula
    env_text = ENV_FILE.read_text()
    if f"WEBHOOK_SECRET={secret}\n" not in env_text:
        print("❌ .env'e WEBHOOK_SECRET yazılmadı!")
        sys.exit(1)
    if f"WEBHOOK_STATS_TOKEN={stats_token}\n" not in env_text:
        print("❌ .env'e WEBHOOK_STATS_TOKEN yazılmadı!")
        sys.exit(1)
    print("     ✓ WEBHOOK_SECRET satırı .env'de mevcut")
    print(f"     ✓ WEBHOOK_STATS_TOKEN satırı .env'de mevcut ({mask(stats_token)})")

    # 4) restart + self-test
    print()
    print(f"🔄 sudo -n systemctl restart {SERVICE_NAME} deneniyor...")
    restarted = try_restart_service()
    if not restarted:
        print(f"  ⚠️ Passwordless sudo yok veya restart başarısız.")
        print(f"     Manuel çalıştır:  sudo systemctl restart {SERVICE_NAME}")
        print(f"     Sonra:             curl https://webhook.solarfame.com/health")
        print()
        print("=" * 50)
        print("FAZ 4 - .env yazıldı, RESTART MANUEL bekleniyor")
        print("=" * 50)
        sys.exit(0)

    print("  ✓ Service restart edildi")
    time.sleep(5)
    if not is_service_active():
        print("❌ Service restart sonrası aktif değil:")
        subprocess.run(["sudo", "journalctl", "-u", SERVICE_NAME, "-n", "30", "--no-pager"])
        sys.exit(1)
    print("  ✓ systemctl is-active: yes")

    # 5) self-test
    print()
    print("🧪 Self-test (yeni secret ile imzalı POST → 200 beklenir)...")
    ok, msg = self_test(secret)
    if ok:
        print(f"  ✓ Self-test PASS: {msg}")
    else:
        print(f"  ✗ Self-test FAIL: {msg}")
        print("     .env restart sonrası okunmamış olabilir, journal kontrol:")
        subprocess.run(["sudo", "journalctl", "-u", SERVICE_NAME, "-n", "20", "--no-pager"])
        sys.exit(1)

    # 6) Bitirme
    print()
    print("=" * 50)
    print("FAZ 4 TAMAM")
    print("=" * 50)
    print(f"  webhook_id            : {webhook_id}")
    print(f"  endpoint              : {ENDPOINT}")
    print(f"  events                : {EVENTS}")
    print(f"  WEBHOOK_SECRET        : {mask(secret)}  (.env'de)")
    print(f"  WEBHOOK_STATS_TOKEN   : {mask(stats_token)}  (.env'de)")
    print(f"  service               : active (restart sonrası)")
    print(f"  self-test             : PASS (HMAC doğrulama çalışıyor)")
    print(f"  response dump         : {RESPONSE_FILE}")
    print()
    print("ŞİMDİ MANUEL DOĞRULAMA TESTİ YAP:")
    print()
    print("  1. ClickUp'ta bir test task aç (tercihen Sandbox list)")
    print("  2. Due date'ini değiştir (örn yarına kaydır)")
    print("  3. ~5 sn sonra sunucuda kontrol et:")
    print()
    print("       tail -f ~/clickup-bot/webhook.log")
    print("       # 'Ingested due_date: task=... before=... after=...' satırı")
    print()
    print("       sudo tail -f ~/clickup-bot/webhook_access.log")
    print("       # POST /clickup-webhook 200 satırı")
    print()
    print("       sqlite3 ~/clickup-bot/webhook_events.db \\")
    print("         'SELECT task_id, user_name, before_value, after_value, \\")
    print("                 datetime(received_at_ms/1000,\"unixepoch\") AS at \\")
    print("          FROM events ORDER BY received_at_ms DESC LIMIT 5;'")
    print()
    print("       # /stats endpoint (token .env'de):")
    print("       TOKEN=$(grep WEBHOOK_STATS_TOKEN ~/clickup-bot/.env | cut -d= -f2)")
    print('       curl -s "https://webhook.solarfame.com/stats?token=$TOKEN" | python3 -m json.tool')
    print()
    print("İlk gerçek event DB'de görününce Faz 5'e (daily report refactor) geçeriz.")


if __name__ == "__main__":
    main()
