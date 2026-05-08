#!/bin/bash
# Faz 3: clickup-webhook'u systemd servisi olarak kur, otomatik restart davranışını doğrula.
#
# Yapılanlar:
#   1) Manuel/önceki gunicorn'ları temizle (port 8080 boş olsun)
#   2) /etc/systemd/system/clickup-webhook.service'i kopyala
#   3) systemctl daemon-reload + enable + start
#   4) Servis aktif mi, dinliyor mu kontrol
#   5) HTTP smoke testleri (/health, POST /clickup-webhook, /stats)
#   6) Restart testi (systemctl restart)
#   7) Auto-recovery testi (kill -9 → systemd Restart=on-failure → yeni MainPID)
#   8) journalctl son satırları
#
# Faz 4'e kadar WEBHOOK_SECRET boş olduğu için POST 401 döner — bu beklenen.

set -e

APP_DIR="${APP_DIR:-$HOME/clickup-bot}"
VENV="$APP_DIR/venv"
ENV_FILE="$APP_DIR/.env"
DOMAIN="${WEBHOOK_DOMAIN:-webhook.solarfame.com}"

SERVICE_NAME="clickup-webhook"
SERVICE_SRC="$APP_DIR/webhook/clickup-webhook.service"
SERVICE_DST="/etc/systemd/system/clickup-webhook.service"

cd "$APP_DIR"

echo ""
echo "=========================================="
echo "  Faz 3 - systemd service"
echo "=========================================="
echo ""

if [ ! -f "$SERVICE_SRC" ]; then
    echo "❌ Service template yok: $SERVICE_SRC"
    exit 1
fi
if [ ! -d "$VENV" ]; then
    echo "❌ Venv yok: $VENV"
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ .env yok: $ENV_FILE"
    exit 1
fi

echo "[1/8] Önceki manuel gunicorn process'leri temizleniyor (varsa)..."
if pgrep -f "webhook.receiver:app" >/dev/null 2>&1; then
    pkill -f "webhook.receiver:app" 2>/dev/null || true
    sleep 2
    if pgrep -f "webhook.receiver:app" >/dev/null 2>&1; then
        pkill -9 -f "webhook.receiver:app" 2>/dev/null || true
        sleep 1
    fi
    echo "  -> manuel gunicorn'lar kapatıldı"
else
    echo "  -> zaten yok"
fi

echo "[2/8] Service file kopyalanıyor: $SERVICE_DST"
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo chmod 644 "$SERVICE_DST"

echo "  Service içeriği:"
sudo sed 's/^/    /' "$SERVICE_DST"

echo ""
echo "[3/8] systemd reload + enable + start..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" 2>&1 | sed 's/^/  /'
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "[4/8] Servis aktif mi kontrol (5 sn bekleme)..."
sleep 5
if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "❌ Servis aktif değil. Status:"
    sudo systemctl status "$SERVICE_NAME" --no-pager -l | sed 's/^/    /'
    echo ""
    echo "  Son journal:"
    sudo journalctl -u "$SERVICE_NAME" --no-pager -n 30 | sed 's/^/    /'
    exit 1
fi
echo "  -> $SERVICE_NAME: active"

MAIN_PID_INITIAL=$(systemctl show -p MainPID --value "$SERVICE_NAME")
echo "  -> MainPID: $MAIN_PID_INITIAL"
ss -tlnp 2>/dev/null | grep ':8080 ' | sed 's/^/    /' || true

echo ""
echo "[5/8] Smoke testleri..."
PASSED=0
FAILED=0
test_http() {
    local name="$1"; local expected="$2"; shift 2
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@" 2>/dev/null || echo "curl_err")
    if [ "$code" = "$expected" ]; then
        echo "  ✓ $name (HTTP $code)"
        PASSED=$((PASSED + 1))
    else
        echo "  ✗ $name (beklenen $expected, gelen $code)"
        FAILED=$((FAILED + 1))
    fi
}

test_http "GET  /health"                    "200"  "https://$DOMAIN/health"
test_http "POST /clickup-webhook (no sig)"  "401"  -X POST -H 'Content-Type: application/json' --data '{}' "https://$DOMAIN/clickup-webhook"
test_http "GET  /stats?token=YANLIS"        "401"  "https://$DOMAIN/stats?token=YANLIS"

HEALTH_BODY=$(curl -fsS --max-time 8 "https://$DOMAIN/health" 2>/dev/null | tr -d '\n' || echo "")
if [ "$HEALTH_BODY" = "ok" ]; then
    echo "  ✓ /health body 'ok'"
    PASSED=$((PASSED + 1))
else
    echo "  ✗ /health body beklenen 'ok', gelen '$HEALTH_BODY'"
    FAILED=$((FAILED + 1))
fi

if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo "  Smoke test BAŞARISIZ; restart/auto-recovery testleri atlanıyor."
    echo "  Journal:"
    sudo journalctl -u "$SERVICE_NAME" --no-pager -n 30 | sed 's/^/    /'
    exit 1
fi

echo ""
echo "[6/8] Restart testi (systemctl restart)..."
PID_BEFORE_RESTART=$(systemctl show -p MainPID --value "$SERVICE_NAME")
sudo systemctl restart "$SERVICE_NAME"
sleep 3

PID_AFTER_RESTART=$(systemctl show -p MainPID --value "$SERVICE_NAME")
if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "  ✗ Restart sonrası servis aktif değil"
    exit 1
fi
if [ "$PID_BEFORE_RESTART" = "$PID_AFTER_RESTART" ] || [ -z "$PID_AFTER_RESTART" ]; then
    echo "  ✗ Restart sonrası MainPID değişmedi (eski=$PID_BEFORE_RESTART yeni=$PID_AFTER_RESTART)"
    exit 1
fi
RESTART_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "https://$DOMAIN/health")
echo "  ✓ Restart: PID $PID_BEFORE_RESTART → $PID_AFTER_RESTART, /health = $RESTART_HEALTH"

echo ""
echo "[7/8] Auto-recovery testi (kill -9 master PID)..."
PID_BEFORE_KILL=$(systemctl show -p MainPID --value "$SERVICE_NAME")
echo "  Killing PID $PID_BEFORE_KILL with SIGKILL..."
sudo kill -9 "$PID_BEFORE_KILL" 2>/dev/null || {
    echo "  ✗ kill -9 başarısız (PID hala $PID_BEFORE_KILL?)"
    exit 1
}

# RestartSec=5, biraz pay bırak
echo "  10 sn bekleniyor (RestartSec=5)..."
RECOVERED=0
NEW_PID=""
RECOVER_HEALTH="(timeout)"
RECOVER_DURATION=0
for i in $(seq 1 12); do
    sleep 1
    NEW_PID=$(systemctl show -p MainPID --value "$SERVICE_NAME")
    if [ -n "$NEW_PID" ] && [ "$NEW_PID" != "0" ] && [ "$NEW_PID" != "$PID_BEFORE_KILL" ]; then
        # MainPID değişti ama process gerçekten dinliyor mu?
        if curl -s -o /dev/null -w "%{http_code}" --max-time 3 "https://$DOMAIN/health" 2>/dev/null | grep -q "200"; then
            RECOVERED=1
            RECOVER_DURATION=$i
            break
        fi
    fi
done

if [ "$RECOVERED" = "1" ]; then
    RECOVER_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "https://$DOMAIN/health")
    echo "  ✓ Auto-recovery: $PID_BEFORE_KILL → $NEW_PID, /health = $RECOVER_HEALTH, süre ~${RECOVER_DURATION}s"
else
    echo "  ✗ Auto-recovery 12 sn içinde gerçekleşmedi (eski_pid=$PID_BEFORE_KILL yeni_pid=$NEW_PID)"
    sudo systemctl status "$SERVICE_NAME" --no-pager -l | sed 's/^/    /'
    sudo journalctl -u "$SERVICE_NAME" --no-pager -n 30 | sed 's/^/    /'
    exit 1
fi

echo ""
echo "[8/8] systemctl status + journalctl son 30..."
echo ""
echo "  --- systemctl status (özet) ---"
sudo systemctl status "$SERVICE_NAME" --no-pager -l --lines=0 | sed 's/^/    /'
echo ""
echo "  --- journalctl -u $SERVICE_NAME -n 30 ---"
sudo journalctl -u "$SERVICE_NAME" --no-pager -n 30 | sed 's/^/    /'

echo ""
echo "=========================================="
echo "  FAZ 3 TAMAM"
echo "=========================================="
echo ""
echo "  service file        : $SERVICE_DST"
echo "  service durum       : $(systemctl is-active $SERVICE_NAME) ($(systemctl is-enabled $SERVICE_NAME))"
echo "  şu anki MainPID     : $(systemctl show -p MainPID --value $SERVICE_NAME)"
echo "  access log          : $APP_DIR/webhook_access.log"
echo "  error log           : $APP_DIR/webhook_error.log"
echo "  app log (logger.py) : $APP_DIR/webhook.log"
echo ""
echo "  HTTP testleri:"
echo "    /health                       → 200 (body 'ok')"
echo "    POST /clickup-webhook (no sig)→ 401 (HMAC fail, beklenen)"
echo "    /stats?token=YANLIS           → 401 (auth fail, beklenen)"
echo ""
echo "  Restart testi      : PID $PID_BEFORE_RESTART → $PID_AFTER_RESTART"
echo "  Auto-recovery testi: PID $PID_BEFORE_KILL → $NEW_PID (~${RECOVER_DURATION}s)"
echo ""
echo "  Boot autostart aktif (enable). Server reboot sonrası servis kalkar."
echo ""
echo "  Faz 4: WEBHOOK_SECRET üret + ClickUp Create Webhook API çağrısı"
echo ""
