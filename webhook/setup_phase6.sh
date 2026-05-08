#!/bin/bash
# Faz 6: Operasyonel sertifikasyon kurulumu.
#
# Yaptıkları:
#   - 4 yeni Python modülünün syntax check'i
#   - venv'de requests modülünün varlığını teyit
#   - health_check'i bir kez çalıştır (real /health endpoint'ine bakar)
#   - db_cleanup --dry-run (temizliği uygulamaz, sayar)
#   - subscription_monitor bir kez çalıştır (mevcut webhook görmeli)
#   - 3 yeni cron satırı ekle (saatlik / haftalık / 6saatlik)
#   - /etc/logrotate.d/clickup-webhook'u kopyala + dry-run
#
# YAPMAZ:
#   - daily report cron'unu re-enable etmez (kullanıcı manuel)
#   - webhook'u recreate etmez

set -e

APP_DIR="${APP_DIR:-$HOME/clickup-bot}"
VENV="$APP_DIR/venv"
LOGROTATE_SRC="$APP_DIR/webhook/logrotate.clickup-webhook"
LOGROTATE_DST="/etc/logrotate.d/clickup-webhook"

cd "$APP_DIR"

echo ""
echo "=========================================="
echo "  Faz 6 - Operasyonel sertifikasyon"
echo "=========================================="
echo ""

if [ ! -d "$VENV" ]; then
    echo "❌ Venv yok: $VENV"; exit 1
fi
if [ ! -f "$LOGROTATE_SRC" ]; then
    echo "❌ logrotate template yok: $LOGROTATE_SRC"; exit 1
fi

echo "[1/8] Python syntax check..."
"$VENV/bin/python" -m py_compile \
    webhook/_mailer.py \
    webhook/health_check.py \
    webhook/db_cleanup.py \
    webhook/subscription_monitor.py
echo "  OK"

echo ""
echo "[2/8] requests modülü venv'de mi..."
if "$VENV/bin/python" -c "import requests" 2>/dev/null; then
    echo "  OK"
else
    echo "  yükleniyor..."
    "$VENV/bin/pip" install --quiet requests
fi

echo ""
echo "[3/8] health_check.py manuel çalıştırma (real /health pingleme)..."
"$VENV/bin/python" webhook/health_check.py
HC_RC=$?
if [ "$HC_RC" -ne 0 ]; then
    echo "  ⚠️ health_check exit=$HC_RC (fail counter artmış olabilir)"
fi

# Counter dosyasının durumunu göster
COUNTER_FILE="$APP_DIR/health_fail_count.txt"
if [ -f "$COUNTER_FILE" ]; then
    echo "  fail_count: $(cat $COUNTER_FILE)"
else
    echo "  fail_count: (dosya yok = 0)"
fi

echo ""
echo "[4/8] db_cleanup.py --dry-run..."
"$VENV/bin/python" webhook/db_cleanup.py --dry-run

echo ""
echo "[5/8] subscription_monitor.py manuel çalıştırma..."
"$VENV/bin/python" webhook/subscription_monitor.py || true

echo ""
echo "[6/8] Crontab güncelleniyor (3 yeni satır)..."
HEALTH_LINE="0 * * * * cd $APP_DIR && $VENV/bin/python webhook/health_check.py >> $APP_DIR/health_check.log 2>&1"
CLEANUP_LINE="0 3 * * 0 cd $APP_DIR && $VENV/bin/python webhook/db_cleanup.py >> $APP_DIR/db_cleanup.log 2>&1"
MONITOR_LINE="0 */6 * * * cd $APP_DIR && $VENV/bin/python webhook/subscription_monitor.py >> $APP_DIR/sub_monitor.log 2>&1"

(
    crontab -l 2>/dev/null | \
        grep -v "webhook/health_check\.py" | \
        grep -v "webhook/db_cleanup\.py" | \
        grep -v "webhook/subscription_monitor\.py"
    echo "$HEALTH_LINE"
    echo "$CLEANUP_LINE"
    echo "$MONITOR_LINE"
) | crontab -

echo "  -> kurulu cron satırları (webhook/* eşleşmeleri):"
crontab -l 2>/dev/null | grep "webhook/" | sed 's/^/    /' || echo "    (boş!?)"

echo ""
echo "[7/8] logrotate config yükleniyor..."
sudo cp "$LOGROTATE_SRC" "$LOGROTATE_DST"
sudo chmod 644 "$LOGROTATE_DST"
echo "  -> $LOGROTATE_DST"
echo "  içerik:"
sudo cat "$LOGROTATE_DST" | sed 's/^/    /'

echo ""
echo "[8/8] logrotate dry-run (-d)..."
sudo logrotate -d "$LOGROTATE_DST" 2>&1 | tail -30 | sed 's/^/    /'

echo ""
echo "=========================================="
echo "  FAZ 6 TAMAM"
echo "=========================================="
echo ""
echo "Kurulan dosyalar:"
echo "  $APP_DIR/webhook/_mailer.py"
echo "  $APP_DIR/webhook/health_check.py"
echo "  $APP_DIR/webhook/db_cleanup.py"
echo "  $APP_DIR/webhook/subscription_monitor.py"
echo "  $LOGROTATE_DST"
echo ""
echo "Cron satırları (crontab -l):"
crontab -l 2>/dev/null | grep -E "webhook/|run_daily|run\.sh" | sed 's/^/  /'
echo ""
echo "Manuel komutlar (test/troubleshooting):"
echo "  $VENV/bin/python webhook/health_check.py"
echo "  $VENV/bin/python webhook/db_cleanup.py --dry-run"
echo "  $VENV/bin/python webhook/subscription_monitor.py"
echo "  rm $COUNTER_FILE  # health alert susmuşsa reset"
echo "  rm $APP_DIR/sub_monitor_alert.txt  # sub_monitor cooldown reset"
echo ""
echo "Log dosyaları (logrotate günlük rotate eder, 14 gün tutar):"
echo "  $APP_DIR/webhook.log"
echo "  $APP_DIR/webhook_access.log"
echo "  $APP_DIR/webhook_error.log"
echo "  $APP_DIR/health_check.log"
echo "  $APP_DIR/sub_monitor.log"
echo "  $APP_DIR/db_cleanup.log"
echo ""
