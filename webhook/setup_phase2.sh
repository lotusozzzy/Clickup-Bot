#!/bin/bash
# Faz 2: Flask webhook receiver kurulumu + manuel smoke test.
#
# Yapılanlar:
#   1) Mevcut venv'e flask + gunicorn pip install
#   2) .env'e WEBHOOK_SECRET ve WEBHOOK_STATS_TOKEN yer tutucu (Faz 4'te dolacak)
#   3) SQLite DB init (~/clickup-bot/webhook_events.db)
#   4) Smoke test:
#        - gunicorn arka planda başlat (127.0.0.1:8080)
#        - HTTPS /health → "ok"
#        - POST /clickup-webhook -d '{}' → 401
#        - GET  /stats?token=test → 401
#        - gunicorn kill
#   5) sqlite3 ile schema doğrulama
#
# Faz 3'te systemd service ekleyeceğiz; bu fazda manuel kalıyor.

set -e

APP_DIR="${APP_DIR:-$HOME/clickup-bot}"
VENV="$APP_DIR/venv"
ENV_FILE="$APP_DIR/.env"
DOMAIN="${WEBHOOK_DOMAIN:-webhook.solarfame.com}"

cd "$APP_DIR"

echo ""
echo "=========================================="
echo "  Faz 2 - Flask webhook receiver"
echo "=========================================="
echo ""

if [ ! -d "$VENV" ]; then
    echo "❌ Venv bulunamadı: $VENV. Önce ana setup.sh çalıştırılmalı."
    exit 1
fi

echo "[1/5] flask + gunicorn yükleniyor..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r webhook/requirements.txt
echo "  -> $("$VENV/bin/pip" show flask | grep -E '^Version' | awk '{print "flask " $2}')"
echo "  -> $("$VENV/bin/pip" show gunicorn | grep -E '^Version' | awk '{print "gunicorn " $2}')"

echo "[2/5] .env'e webhook anahtar yer tutucuları..."
if [ ! -f "$ENV_FILE" ]; then
    touch "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi
if ! grep -q "^WEBHOOK_SECRET=" "$ENV_FILE"; then
    {
        echo ""
        echo "# Webhook (Faz 4'te ClickUp Create Webhook yanıtından doldurulacak)"
        echo "WEBHOOK_SECRET="
    } >> "$ENV_FILE"
    echo "  -> WEBHOOK_SECRET= satırı eklendi (boş)"
else
    echo "  -> WEBHOOK_SECRET zaten var, dokunulmadı"
fi
if ! grep -q "^WEBHOOK_STATS_TOKEN=" "$ENV_FILE"; then
    echo "WEBHOOK_STATS_TOKEN=" >> "$ENV_FILE"
    echo "  -> WEBHOOK_STATS_TOKEN= satırı eklendi (boş)"
else
    echo "  -> WEBHOOK_STATS_TOKEN zaten var, dokunulmadı"
fi

echo "[3/5] SQLite DB init..."
"$VENV/bin/python" -c "from webhook.db import init_db; init_db(); print('  -> webhook_events.db hazır')"

echo "[4/5] Smoke test - gunicorn arka planda + curl..."
GLOG=$(mktemp)
# gunicorn'u .env'siz başlatıyoruz (WEBHOOK_SECRET boş kalsın - tüm sig fail olsun)
nohup "$VENV/bin/gunicorn" \
    --chdir "$APP_DIR" \
    --bind 127.0.0.1:8080 \
    --workers 1 \
    --timeout 30 \
    --access-logfile - \
    --error-logfile - \
    "webhook.receiver:app" > "$GLOG" 2>&1 &
GPID=$!
sleep 2

# gunicorn ayağa kalktı mı?
if ! kill -0 "$GPID" 2>/dev/null; then
    echo "❌ gunicorn başlatılamadı. Log:"
    cat "$GLOG"
    rm -f "$GLOG"
    exit 1
fi

echo "  gunicorn PID=$GPID"
echo ""

PASSED=0
FAILED=0

run_test() {
    local name="$1"
    local expected="$2"
    shift 2
    local actual
    actual=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@") || actual="curl_err"
    if [ "$actual" = "$expected" ]; then
        echo "  ✓ $name (HTTP $actual)"
        PASSED=$((PASSED + 1))
    else
        echo "  ✗ $name (beklenen $expected, gelen $actual)"
        FAILED=$((FAILED + 1))
    fi
}

run_test "HTTPS /health (nginx)" "200" \
    "https://$DOMAIN/health"

run_test "POST /clickup-webhook (HMAC fail)" "401" \
    -X POST -H "Content-Type: application/json" --data '{}' \
    "https://$DOMAIN/clickup-webhook"

run_test "GET /stats (yanlış token)" "401" \
    "https://$DOMAIN/stats?token=YANLIS"

# Bonus: HTTP body içeriği /health için "ok"
HEALTH_BODY=$(curl -fsS --max-time 8 "https://$DOMAIN/health" | tr -d '\n')
if [ "$HEALTH_BODY" = "ok" ]; then
    echo "  ✓ /health body: 'ok'"
    PASSED=$((PASSED + 1))
else
    echo "  ✗ /health body beklenen 'ok', gelen '$HEALTH_BODY'"
    FAILED=$((FAILED + 1))
fi

echo ""
echo "  gunicorn durduruluyor (PID=$GPID)..."
kill "$GPID" 2>/dev/null || true
wait "$GPID" 2>/dev/null || true

echo "  Gunicorn log özeti:"
tail -10 "$GLOG" | sed 's/^/    /'
rm -f "$GLOG"

echo ""
echo "[5/5] SQLite schema doğrulama..."
DB_FILE="$APP_DIR/webhook_events.db"
if [ ! -f "$DB_FILE" ]; then
    echo "❌ $DB_FILE oluşmamış"
    exit 1
fi
echo "  events tablosu:"
sqlite3 "$DB_FILE" ".schema events" | sed 's/^/    /'
echo "  index'ler:"
sqlite3 "$DB_FILE" "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events';" | sed 's/^/    /'
ROW_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM events;")
echo "  satır sayısı: $ROW_COUNT (henüz hiç event almadık - 0 olmalı)"

echo ""
echo "=========================================="
if [ "$FAILED" -gt 0 ]; then
    echo "  FAZ 2 - $FAILED test BAŞARISIZ ($PASSED geçti)"
    echo "=========================================="
    exit 1
fi
echo "  FAZ 2 TAMAM ($PASSED test geçti)"
echo "=========================================="
echo ""
echo "  receiver path        : $APP_DIR/webhook/receiver.py"
echo "  db.py path           : $APP_DIR/webhook/db.py"
echo "  logger.py path       : $APP_DIR/webhook/logger.py"
echo "  SQLite DB            : $DB_FILE"
echo "  Receiver log         : $APP_DIR/webhook.log"
echo ""
echo "  Manuel başlatma (gerekirse):"
echo "    cd $APP_DIR && set -a && source .env && set +a && \\"
echo "    venv/bin/gunicorn --chdir $APP_DIR -b 127.0.0.1:8080 -w 2 \\"
echo "      --access-logfile - --error-logfile - webhook.receiver:app"
echo ""
echo "  Faz 3: systemd service kurulumu (autostart + restart on failure)"
echo "  Faz 4: WEBHOOK_SECRET üret + ClickUp Create Webhook API çağrısı"
echo ""
