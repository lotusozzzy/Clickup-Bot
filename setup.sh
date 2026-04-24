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

echo "[4/7] run.sh, run_and_stop.sh ve run_daily_and_stop.sh yazılıyor..."
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

# Haftalık (Salı) bakiye raporu için - sonunda instance'ı kapatır
cat > "$APP_DIR/run_and_stop.sh" <<EOF
#!/bin/bash
# Haftalık bakiye raporu wrapper'ı.
echo "[\$(date)] run_and_stop başladı"
"$APP_DIR/run.sh"
EXIT=\$?
echo "[\$(date)] bot çıkış kodu=\$EXIT, 60 sn sonra sunucu kapatılıyor."
sudo /sbin/shutdown -h +1 "ClickUp bot tamamlandi, instance stop ediliyor"
EOF
chmod +x "$APP_DIR/run_and_stop.sh"

# Günlük (Pzt-Cum) tarih değişiklik raporu için - sonunda instance'ı kapatır
cat > "$APP_DIR/run_daily_and_stop.sh" <<EOF
#!/bin/bash
# Günlük tarih değişiklik raporu wrapper'ı.
echo "[\$(date)] run_daily_and_stop başladı"
cd "$APP_DIR"
set -a
source "$APP_DIR/.env"
set +a
"$APP_DIR/venv/bin/python" "$APP_DIR/clickup_due_report.py"
EXIT=\$?
echo "[\$(date)] günlük rapor çıkış kodu=\$EXIT, 60 sn sonra sunucu kapatılıyor."
sudo /sbin/shutdown -h +1 "ClickUp gunluk rapor tamamlandi, instance stop ediliyor"
EOF
chmod +x "$APP_DIR/run_daily_and_stop.sh"

CURRENT_USER="$(id -un)"
echo "[5/7] $CURRENT_USER için şifresiz shutdown yetkisi veriliyor..."
SUDOERS_FILE="/etc/sudoers.d/clickup-bot-shutdown"
echo "$CURRENT_USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee "$SUDOERS_FILE" >/dev/null
sudo chmod 440 "$SUDOERS_FILE"

echo "[6/7] Cron işleri ekleniyor (haftalık + günlük)..."
# Haftalık bakiye: Salı 09:00 TR (06:00 UTC)
WEEKLY_LINE="0 6 * * 2 $APP_DIR/run_and_stop.sh >> $APP_DIR/cron.log 2>&1"
# Günlük tarih değişiklik raporu: Pzt-Cum 21:00 TR (18:00 UTC)
DAILY_LINE="0 18 * * 1-5 $APP_DIR/run_daily_and_stop.sh >> $APP_DIR/cron_daily.log 2>&1"
( crontab -l 2>/dev/null | grep -v "$APP_DIR/run" ; echo "$WEEKLY_LINE" ; echo "$DAILY_LINE" ) | crontab -
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
echo "  Bakiye raporu (haftalık):  $APP_DIR/run.sh"
echo "  Tarih değişiklik raporu :  cd $APP_DIR && set -a; source .env; set +a; venv/bin/python clickup_due_report.py"
echo ""
echo "Otomatik cron'lar (TR saati):"
echo "  Salı 09:00            -> run_and_stop.sh         (haftalık bakiye + instance stop)"
echo "  Pzt-Cum 21:00         -> run_daily_and_stop.sh   (günlük tarih raporu + instance stop)"
echo ""
echo "AWS EventBridge Scheduler tarafında olması gerekenler:"
echo "  Clickup-Uyandir       -> Salı 08:50 start"
echo "  Clickup-Uyut          -> Salı 10:00 stop (yedek)"
echo "  Clickup-Daily-Uyandir -> Pzt-Cum 20:50 start"
echo "  Clickup-Daily-Uyut    -> Pzt-Cum 21:30 stop (yedek)"
echo ""
echo "Logları izlemek için:"
echo "  tail -f $APP_DIR/cron.log         (haftalık)"
echo "  tail -f $APP_DIR/cron_daily.log   (günlük)"
echo ""
