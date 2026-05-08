#!/usr/bin/env python3
"""Workspace'teki ClickUp webhook'larını listele.

Kullanım:
    python3 webhook/list_webhooks.py

Endpoint, events, status ve health bilgilerini gösterir. Secret
LIST endpoint'inde gelmez (sadece create response'unda gelir).
"""

import json
import os
import sys

# clickup_bot importundan ÖNCE .env yüklenmeli
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook._envloader import ensure_env  # noqa: E402

ensure_env()

from clickup_bot import WORKSPACE_ID, get_session  # noqa: E402


def main():
    session = get_session()
    url = f"https://api.clickup.com/api/v2/team/{WORKSPACE_ID}/webhook"
    r = session.get(url, timeout=20)
    if r.status_code != 200:
        print(f"❌ HTTP {r.status_code}:")
        print(r.text[:500])
        sys.exit(1)

    data = r.json()
    webhooks = data.get("webhooks") or []

    if not webhooks:
        print("ℹ️  Bu workspace'te kayıtlı webhook yok.")
        return

    print(f"📋 Toplam {len(webhooks)} webhook:\n")
    for i, w in enumerate(webhooks, 1):
        print(f"--- [{i}/{len(webhooks)}] ---")
        print(f"  id        : {w.get('id', '?')}")
        print(f"  endpoint  : {w.get('endpoint', '?')}")
        print(f"  events    : {w.get('events', [])}")
        print(f"  health    : {w.get('health', {})}")
        for k in ("space_id", "folder_id", "list_id", "task_id"):
            if w.get(k):
                print(f"  {k:10s}: {w[k]}")
        # Tam response struct'ı debug için (uzun olabilir):
        # print("  (raw):", json.dumps(w, indent=2)[:500])
        print()


if __name__ == "__main__":
    main()
