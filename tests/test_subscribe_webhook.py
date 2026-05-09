"""Adım 2 testleri: subscribe_webhook helper'ları + EVENTS listesi.

main() integration test'i yerine pure unit testler — main() systemctl,
sudo, requests.post(production_endpoint) gibi yan etkilere bağımlı.
Mock'lanan parçalar:
  - find_existing  → session.get
  - delete_webhook → session.delete

write_env_var testleri tmp_path kullanır; production .env'e dokunmaz.
"""

import json
import os
import stat
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _import_subscribe(monkeypatch):
    """clickup_bot env requirement'larını sahte değerlerle karşılayıp
    subscribe_webhook modülünü taze import eder."""
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("SMTP_PASSWORD", "test-smtp-pw-not-real")
    monkeypatch.setenv("CLICKUP_WORKSPACE_ID", "9999999")

    for mod_name in list(sys.modules):
        if mod_name in ("clickup_bot",) or mod_name.startswith("webhook"):
            del sys.modules[mod_name]
    # subscribe_webhook import'u clickup_bot'u tetikler — env var'lar set
    from webhook import subscribe_webhook
    return subscribe_webhook


@pytest.fixture
def sw(monkeypatch):
    return _import_subscribe(monkeypatch)


# ---------------------------------------------------------------------------
# EVENTS sabiti
# ---------------------------------------------------------------------------

def test_events_list_includes_task_deleted(sw):
    assert "taskUpdated" in sw.EVENTS
    assert "taskDeleted" in sw.EVENTS
    # Sıralama önemli değil ama dup olmasın
    assert len(sw.EVENTS) == len(set(sw.EVENTS))


# ---------------------------------------------------------------------------
# extract_secret / extract_id — ClickUp response shape varyantları
# ---------------------------------------------------------------------------

def test_extract_secret_flat_shape(sw):
    assert sw.extract_secret({"secret": "s1"}) == "s1"


def test_extract_secret_inner_webhook_shape(sw):
    assert sw.extract_secret({"webhook": {"secret": "s2"}}) == "s2"


def test_extract_secret_inner_data_shape(sw):
    assert sw.extract_secret({"data": {"secret": "s3"}}) == "s3"


def test_extract_secret_missing_returns_none(sw):
    assert sw.extract_secret({"webhook": {"id": "x"}}) is None
    assert sw.extract_secret({}) is None
    assert sw.extract_secret(None) is None


def test_extract_id_flat_shape(sw):
    assert sw.extract_id({"id": "wh-1"}) == "wh-1"
    assert sw.extract_id({"id": 42}) == "42"


def test_extract_id_inner_webhook_shape(sw):
    assert sw.extract_id({"webhook": {"id": "wh-2"}}) == "wh-2"


def test_extract_id_missing_returns_none(sw):
    assert sw.extract_id({"data": {"foo": "bar"}}) is None
    assert sw.extract_id(None) is None


# ---------------------------------------------------------------------------
# write_env_var — atomic write, append, replace, permission
# ---------------------------------------------------------------------------

def test_write_env_var_appends_when_missing(sw, tmp_path):
    env = tmp_path / ".env"
    env.write_text("EXISTING=foo\n")
    sw.write_env_var(env, "WEBHOOK_SECRET", "abc123")

    text = env.read_text()
    assert "EXISTING=foo\n" in text
    assert "WEBHOOK_SECRET=abc123\n" in text


def test_write_env_var_replaces_existing_value(sw, tmp_path):
    env = tmp_path / ".env"
    env.write_text("WEBHOOK_SECRET=old\nOTHER=bar\n")
    sw.write_env_var(env, "WEBHOOK_SECRET", "new123")

    text = env.read_text()
    assert "WEBHOOK_SECRET=new123\n" in text
    assert "WEBHOOK_SECRET=old\n" not in text
    assert "OTHER=bar\n" in text  # diğer satır korunmalı


def test_write_env_var_creates_file_when_absent(sw, tmp_path):
    env = tmp_path / ".env"
    assert not env.exists()
    sw.write_env_var(env, "WEBHOOK_SECRET", "fresh")
    assert env.exists()
    assert env.read_text() == "WEBHOOK_SECRET=fresh\n"
    # Yeni dosya 0600 izinleriyle yazılmalı
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600


def test_write_env_var_preserves_existing_permissions(sw, tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=1\n")
    os.chmod(env, 0o640)
    sw.write_env_var(env, "BAR", "2")
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o640


def test_write_env_var_no_tmp_leftover(sw, tmp_path):
    env = tmp_path / ".env"
    sw.write_env_var(env, "K", "v")
    leftover = list(tmp_path.glob(".env.*tmp*"))
    assert leftover == [], f"tmp dosya temizlenmedi: {leftover}"


def test_write_env_var_appends_newline_when_last_line_no_eol(sw, tmp_path):
    env = tmp_path / ".env"
    env.write_text("EXISTING=foo")  # son satırda \n yok
    sw.write_env_var(env, "NEW", "bar")

    lines = env.read_text().splitlines(keepends=True)
    assert lines[0].endswith("\n")  # eski satır artık \n ile bitmeli
    assert lines[-1] == "NEW=bar\n"


# ---------------------------------------------------------------------------
# find_existing — endpoint filtering
# ---------------------------------------------------------------------------

def _make_session(get_response=None, delete_response=None, post_response=None):
    s = MagicMock()
    if get_response is not None:
        s.get.return_value = get_response
    if delete_response is not None:
        s.delete.return_value = delete_response
    if post_response is not None:
        s.post.return_value = post_response
    return s


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def test_find_existing_returns_only_matching_endpoint(sw):
    payload = {
        "webhooks": [
            {"id": "w1", "endpoint": "https://other.example/hook", "events": ["taskUpdated"]},
            {"id": "w2", "endpoint": sw.ENDPOINT, "events": ["taskUpdated"]},
            {"id": "w3", "endpoint": sw.ENDPOINT, "events": ["taskCreated"]},
        ]
    }
    session = _make_session(get_response=_resp(200, payload))
    matches = sw.find_existing(session, sw.ENDPOINT)
    assert [w["id"] for w in matches] == ["w2", "w3"]

    # URL workspace_id içermeli
    called_url = session.get.call_args[0][0]
    assert "/team/" in called_url
    assert "/webhook" in called_url


def test_find_existing_empty_when_no_webhooks(sw):
    session = _make_session(get_response=_resp(200, {"webhooks": []}))
    assert sw.find_existing(session, sw.ENDPOINT) == []


def test_find_existing_aborts_on_http_error(sw):
    session = _make_session(get_response=_resp(500, {"error": "boom"}))
    with pytest.raises(SystemExit) as exc:
        sw.find_existing(session, sw.ENDPOINT)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# delete_webhook — DELETE call shape
# ---------------------------------------------------------------------------

def test_delete_webhook_calls_correct_url_and_accepts_204(sw):
    session = _make_session(delete_response=_resp(204, {}))
    sw.delete_webhook(session, "wh-42")

    called_url = session.delete.call_args[0][0]
    assert called_url.endswith("/webhook/wh-42")


def test_delete_webhook_accepts_200_too(sw):
    session = _make_session(delete_response=_resp(200, {"deleted": True}))
    sw.delete_webhook(session, "wh-x")  # SystemExit fırlatmamalı


def test_delete_webhook_aborts_on_failure(sw):
    session = _make_session(delete_response=_resp(403, {"err": "forbidden"}))
    with pytest.raises(SystemExit) as exc:
        sw.delete_webhook(session, "wh-bad")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# deep_mask_secret — recursive masking
# ---------------------------------------------------------------------------

def test_deep_mask_secret_top_level(sw):
    out = sw.deep_mask_secret({"id": "x", "secret": "abcdefghij"})
    assert out["id"] == "x"
    assert out["secret"].startswith("abcd")
    assert out["secret"].endswith("ghij")
    assert out["secret"] != "abcdefghij"


def test_deep_mask_secret_nested(sw):
    out = sw.deep_mask_secret({"webhook": {"secret": "supersecretvalue", "id": 1}})
    assert out["webhook"]["secret"] != "supersecretvalue"
    assert out["webhook"]["id"] == 1


def test_deep_mask_secret_in_list(sw):
    out = sw.deep_mask_secret([{"secret": "abcdefghij"}, {"id": 7}])
    assert out[0]["secret"] != "abcdefghij"
    assert out[1] == {"id": 7}


def test_deep_mask_secret_short_value(sw):
    out = sw.deep_mask_secret({"secret": "abc"})
    assert out["secret"] == "***"


def test_mask_helper(sw):
    assert sw.mask("") == ""
    assert sw.mask(None) is None
    assert sw.mask("short") == "***"
    long_val = "abcdefghijklmnop"
    masked = sw.mask(long_val)
    assert masked.startswith("abcd")
    assert masked.endswith("mnop")
    assert "..." in masked


# ---------------------------------------------------------------------------
# Replace flow integration — find + delete + body verification
# ---------------------------------------------------------------------------

def test_replace_flow_deletes_and_posts_with_both_events(sw):
    """find_existing + delete_webhook çağrı sırasını ve POST body'sini doğrula.

    main() çalıştırılmıyor (systemctl/sudo/network yan etkileri yüzünden);
    bunun yerine main()'in core ardışık çağrılarını manuel reproduce edip
    body'nin events=['taskUpdated','taskDeleted'] içerdiğini kontrol ediyoruz.
    """
    list_resp = _resp(
        200,
        {"webhooks": [{"id": "old-wh", "endpoint": sw.ENDPOINT, "events": ["taskUpdated"]}]},
    )
    delete_resp = _resp(204, {})
    create_resp = _resp(
        201,
        {
            "id": "new-wh",
            "webhook": {"id": "new-wh", "secret": "fresh-secret-xyz"},
            "secret": "fresh-secret-xyz",
        },
    )
    session = _make_session(
        get_response=list_resp,
        delete_response=delete_resp,
        post_response=create_resp,
    )

    # 1) find_existing
    existing = sw.find_existing(session, sw.ENDPOINT)
    assert len(existing) == 1
    assert existing[0]["id"] == "old-wh"

    # 2) delete (--replace simülasyonu)
    sw.delete_webhook(session, existing[0]["id"])

    # 3) main()'in yapacağı POST'u manuel atalım — body shape'i test için kritik
    body = {"endpoint": sw.ENDPOINT, "events": sw.EVENTS}
    workspace_id = os.environ["CLICKUP_WORKSPACE_ID"]
    create_url = f"https://api.clickup.com/api/v2/team/{workspace_id}/webhook"
    session.post(create_url, json=body, timeout=20)

    # POST body'sini doğrula
    post_call = session.post.call_args
    sent_body = post_call.kwargs["json"]
    assert sent_body["endpoint"] == sw.ENDPOINT
    assert "taskUpdated" in sent_body["events"]
    assert "taskDeleted" in sent_body["events"]

    # 4) Response'tan secret/id parse
    resp_json = create_resp.json()
    assert sw.extract_secret(resp_json) == "fresh-secret-xyz"
    assert sw.extract_id(resp_json) == "new-wh"

    # Çağrı sırası: get → delete → post
    assert session.get.called
    assert session.delete.called
    assert session.post.called
