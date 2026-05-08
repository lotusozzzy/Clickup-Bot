#!/usr/bin/env python3
"""Bir ClickUp webhook'unu webhook_id ile sil.

Kullanım:
    python3 webhook/delete_webhook.py <webhook_id>

ID'yi list_webhooks.py çıktısından al.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook._envloader import ensure_env  # noqa: E402

ensure_env()

from clickup_bot import get_session  # noqa: E402


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("Kullanım: delete_webhook.py <webhook_id>")
        sys.exit(2)
    webhook_id = sys.argv[1].strip()

    session = get_session()
    url = f"https://api.clickup.com/api/v2/webhook/{webhook_id}"
    r = session.delete(url, timeout=20)

    if r.status_code in (200, 204):
        print(f"✓ Webhook silindi: {webhook_id}")
        return

    print(f"❌ HTTP {r.status_code}:")
    print(r.text[:500])
    sys.exit(1)


if __name__ == "__main__":
    main()
