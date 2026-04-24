#!/bin/bash
# ClickUp Bot - EC2 üzerinde tek komutla kurulum.
# Kullanım (EC2 terminalinde):
#   curl -fsSL https://raw.githubusercontent.com/lotusozzzy/clickup-bot/claude/fix-script-freezing-issue-QME50/setup.sh | bash
#
# Repo private ise indirip elle çalıştır:
#   bash setup.sh

set -e

REPO_URL="${REPO_URL:-https://github.com/lotusozzzy/clickup-bot.git}"
BRANCH="${BRANCH:-claude/fix-script-freezing-issue-QME50}"
APP_DIR="${APP_DIR:-$HOME/clickup-bot}"

echo ""
echo "=========================================="
echo "  ClickUp Bot - Otomatik Kurulum"
echo "=========================================="
echo ""

echo "[1/6] Sistem paketleri kuruluyor (python3, git, nano, cronie)..."
if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip git nano cronie >/dev/null
    sudo systemctl enable --now crond
elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-pip git nano cronie >/dev/null
    sudo systemctl enable --now crond
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv git nano cron >/dev/null
    sudo systemctl enable --now cron
else
    echo "HATA: paket yöneticisi tanınmadı (dnf/yum/apt-get bekleniyordu)"
    exit 1
fi

echo "[2/6] Repo $APP_DIR konumuna alınıyor..."
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    git fetch origin "$BRANCH"
    git checkout "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

echo "[3/6] Python sanal ortamı oluşturuluyor..."
python3 -m venv "$APP_DIR/venv"
# shellcheck disable=SC1091
source "$APP_DIR/venv/bin/activate"
pip install --upgrade pip --quiet
pip install --quiet requests openpyxl

echo "[4/7] run.sh (elle test) ve run_and_stop.sh (zamanlı) yazılıyor..."
# Manuel test için - kapanmaz
cat > "$APP_DIR/run.sh" <<EOF
#!/bin/bash
cd "$APP_DIR"
set -a
source "$APP_DIR/.env"
set +a
"$APP_DIR/venv/bin/python" "$APP_DIR/clickup_bot.py"
EOF
chmod +x "$APP_DIR/run.sh"

# Zamanlı çalıştırma için - iş bitince (başarılı ya da hatalı) instance'ı stop eder
cat > "$APP_DIR/run_and_stop.sh" <<EOF
#!/bin/bash
# Bu script cron tarafından çağrılır. Amacı: bot çalışsın, sonra instance kapansın.
# shutdown komutu çalışırken varsayılan kapanış davranışı "stop" olduğu için
# instance sonlandırılmaz, sadece durdurulur - veriler kalır.
echo "[\$(date)] run_and_stop başladı"
"$APP_DIR/run.sh"
EXIT=\$?
echo "[\$(date)] bot çıkış kodu=\$EXIT, 60 sn sonra sunucu kapatılıyor."
# 60 sn gecikme: cron log yazımı ve varsa mail gönderimi tamamlansın
sudo /sbin/shutdown -h +1 "ClickUp bot tamamlandi, instance stop ediliyor"
EOF
chmod +x "$APP_DIR/run_and_stop.sh"

CURRENT_USER="$(id -un)"
echo "[5/7] $CURRENT_USER için şifresiz shutdown yetkisi veriliyor..."
SUDOERS_FILE="/etc/sudoers.d/clickup-bot-shutdown"
echo "$CURRENT_USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee "$SUDOERS_FILE" >/dev/null
sudo chmod 440 "$SUDOERS_FILE"

echo "[6/7] Cron işi ekleniyor (Salı 09:00 TR / 06:00 UTC)..."
CRON_LINE="0 6 * * 2 $APP_DIR/run_and_stop.sh >> $APP_DIR/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "$APP_DIR/run" ; echo "$CRON_LINE" ) | crontab -
echo "  -> Kurulu cron satırları:"
crontab -l 2>/dev/null | grep "$APP_DIR/run" || echo "  (cron satırı bulunamadı - lütfen 'crontab -l' ile manuel kontrol et)"

echo "[7/7] .env dosyası hazırlanıyor..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

echo ""
echo "=========================================="
echo "  KURULUM TAMAM!"
echo "=========================================="
echo ""
echo "Sırada: .env dosyasına ClickUp token'ı ve Yandex app password'ünü yaz."
echo "Şu komutu çalıştır:"
echo ""
echo "    nano $APP_DIR/.env"
echo ""
echo "Açılan ekranda CLICKUP_API_TOKEN= ve SMTP_PASSWORD= satırlarını doldur."
echo "Kaydetmek için:  Ctrl+O  Enter  Ctrl+X"
echo ""
echo "ELLE test için (instance kapanmaz):"
echo "    $APP_DIR/run.sh"
echo ""
echo "Cron: her Salı 09:00 TR'de run_and_stop.sh çalışır -> mail atılır -> instance stop."
echo "Instance'ın Salı 08:50'de başlaması için EventBridge Scheduler'ı AWS Console'dan kur."
echo "Logları izlemek için:  tail -f $APP_DIR/cron.log"
echo ""
