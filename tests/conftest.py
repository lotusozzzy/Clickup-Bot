"""Webhook receiver testleri için shared pytest fixture'ları.

Her test izole tmp DB ile çalışır: WEBHOOK_DB_PATH env var'ı
geçici bir dosyaya işaret eder ve `webhook` modülleri taze import
edilir, böylece module-level WEBHOOK_SECRET / DB_PATH yenilenir.

Production DB'ye (~/clickup-bot/webhook_events.db) DOKUNULMAZ.
"""

import hashlib
import hmac
import json
import sys

import pytest


SECRET = "test-secret-12345"


@pytest.fixture
def webhook_env(tmp_path, monkeypatch):
    db_file = tmp_path / "test_webhook.db"
    snapshot_file = tmp_path / "test_snapshot.json"
    monkeypatch.setenv("WEBHOOK_DB_PATH", str(db_file))
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("WEBHOOK_STATS_TOKEN", "test-stats-token")
    monkeypatch.setenv("WEBHOOK_SNAPSHOT_PATH", str(snapshot_file))
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-clickup-token")

    # Module-level WEBHOOK_SECRET / DB_PATH / SNAPSHOT_PATH değerleri import
    # sırasında okunduğu için fresh import zorla.
    for mod_name in list(sys.modules):
        if mod_name == "webhook" or mod_name.startswith("webhook."):
            del sys.modules[mod_name]

    from webhook import db, receiver

    receiver.app.config["TESTING"] = True
    return {
        "client": receiver.app.test_client(),
        "receiver": receiver,
        "db": db,
        "db_path": str(db_file),
        "snapshot_path": str(snapshot_file),
    }


@pytest.fixture
def signed_post(webhook_env):
    """HMAC imzalı POST helper'ı.

    secret=None geçilirse imzasız gönderilir (HMAC fail testi için).
    secret=<custom> ile yanlış secret testi yapılabilir.
    """
    client = webhook_env["client"]

    def _do(payload, secret=SECRET, sig_header="X-Signature"):
        raw = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            sig = hmac.new(
                secret.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
            headers[sig_header] = sig
        return client.post("/clickup-webhook", data=raw, headers=headers)

    return _do
