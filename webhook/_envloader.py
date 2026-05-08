"""Webhook CLI scriptleri için ortak yardımcılar.

list_webhooks.py / delete_webhook.py / subscribe_webhook.py modüllerinden
çağrılır. Tek görevi: clickup_bot'u import etmeden önce ~/clickup-bot/.env
içeriğini os.environ'a yüklemek (CLICKUP_API_TOKEN, SMTP_PASSWORD vs).

Sebep: clickup_bot modülü yüklenirken zorunlu env var'ları okur ve
eksikse RuntimeError fırlatır. Webhook scriptleri ad-hoc çalıştırılır;
kullanıcının her seferinde 'set -a && source .env && set +a' yapmasını
beklemek hatalı kullanım riski yaratır.
"""

import os
from pathlib import Path


DEFAULT_ENV_PATH = "~/clickup-bot/.env"


def ensure_env(env_path=DEFAULT_ENV_PATH):
    """KEY=VALUE satırlarını os.environ.setdefault ile yükle."""
    p = Path(env_path).expanduser()
    if not p.exists():
        return False
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    return True
