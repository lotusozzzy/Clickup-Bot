#!/bin/bash
# ClickUp Bot - EC2 üzerinde tek komutla kurulum.
# Kullanım (EC2 terminalinde):
#   curl -fsSL https://raw.githubusercontent.com/lotusozzzy/clickup-bot/claude/history-based-date-attribution/setup.sh | bash
#
# Repo private ise indirip elle çalıştır:
#   bash setup.sh

set -e

REPO_URL="${REPO_URL:-https://github.com/lotusozzzy/clickup-bot.git}"
BRANCH="${BRANCH:-claude/history-based-date-attribution}"
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

echo "[4/7] run.sh, run_daily.sh, run_and_stop.sh ve run_daily_and_stop.sh yazılıyor..."
# Manuel test / 7/24 açık sunucu cron'u için - kapanmaz (haftalık bakiye)
cat > "$APP_DIR/run.sh" <<EOF
#!/bin/bash
cd "$APP_DIR"
set -a
source "$APP_DIR/.env"
set +a
"$APP_DIR/venv/bin/python" "$APP_DIR/clickup_bot.py"
EOF
chmod +x "$APP_DIR/run.sh"

# Manuel test / 7/24 açık sunucu cron'u için - kapanmaz (günlük tarih raporu)
cat > "$APP_DIR/run_daily.sh" <<EOF
#!/bin/bash
cd "$APP_DIR"
set -a
source "$APP_DIR/.env"
set +a
"$APP_DIR/venv/bin/python" "$APP_DIR/clickup_due_report.py"
EOF
chmod +x "$APP_DIR/run_daily.sh"

# EventBridge ile otomatik açılan/kapanan sunucu için - sonunda instance'ı durdurur
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

# DETACHED ÇALIŞTIRMA: SSH oturumundan bağımsız, terminal kapansa da koşar.
# nohup ile arka plana atılır, çıktı manual.log'a yazılır.
cat > "$APP_DIR/run_detached.sh" <<EOF
#!/bin/bash
# Bakiye raporunu SSH'tan bağımsız (detached) başlatır.
# Bağlantın koparsa bile çalışmaya devam eder.
APP_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
LOG="\$APP_DIR/manual.log"

# Önceki çalışan instance'ları temizle
if pgrep -f "\$APP_DIR/clickup_bot.py" >/dev/null 2>&1; then
    echo "⚠️ Önceden çalışan bir clickup_bot.py süreci var, durduruluyor..."
    pkill -9 -f "\$APP_DIR/clickup_bot.py"
    sleep 2
fi

# Logu sıfırla (eski log otomatik silinir)
: > "\$LOG"

# nohup + setsid ile tamamen detached başlat
setsid nohup "\$APP_DIR/run.sh" > "\$LOG" 2>&1 < /dev/null &
PID=\$!
disown 2>/dev/null || true
sleep 2

if kill -0 "\$PID" 2>/dev/null; then
    echo ""
    echo "✅ Bakiye raporu arka planda başlatıldı."
    echo "   PID: \$PID"
    echo "   Log: \$LOG"
    echo ""
    echo "Şimdi tarayıcı sekmesini KAPATABİLİRSİN, script çalışmaya devam eder."
    echo ""
    echo "Canlı izlemek için (Ctrl+C ile çıkış, script ölmez):"
    echo "    tail -f \$LOG"
    echo ""
    echo "Durum kontrolü :  \$APP_DIR/status.sh"
    echo "Durdurmak için :  \$APP_DIR/stop.sh"
else
    echo "❌ Başlatılamadı. Logu kontrol et:"
    echo "    cat \$LOG"
    exit 1
fi
EOF
chmod +x "$APP_DIR/run_detached.sh"

# Aynı şey günlük rapor için
cat > "$APP_DIR/run_daily_detached.sh" <<EOF
#!/bin/bash
APP_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
LOG="\$APP_DIR/manual_daily.log"

if pgrep -f "\$APP_DIR/clickup_due_report.py" >/dev/null 2>&1; then
    echo "⚠️ Önceden çalışan bir clickup_due_report.py süreci var, durduruluyor..."
    pkill -9 -f "\$APP_DIR/clickup_due_report.py"
    sleep 2
fi

: > "\$LOG"
cd "\$APP_DIR"
set -a; source "\$APP_DIR/.env"; set +a
setsid nohup "\$APP_DIR/venv/bin/python" "\$APP_DIR/clickup_due_report.py" > "\$LOG" 2>&1 < /dev/null &
PID=\$!
disown 2>/dev/null || true
sleep 2

if kill -0 "\$PID" 2>/dev/null; then
    echo "✅ Günlük rapor arka planda başlatıldı (PID \$PID, log: \$LOG)."
    echo "Canlı izle:  tail -f \$LOG"
else
    echo "❌ Başlatılamadı. Log: \$LOG"
    exit 1
fi
EOF
chmod +x "$APP_DIR/run_daily_detached.sh"

# Durum görüntüleme
cat > "$APP_DIR/status.sh" <<EOF
#!/bin/bash
APP_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
echo "===== ClickUp Bot Durumu ====="
echo ""

for SCRIPT in clickup_bot.py clickup_due_report.py; do
    PID=\$(pgrep -f "\$APP_DIR/\$SCRIPT" | head -1)
    if [ -n "\$PID" ]; then
        ELAPSED=\$(ps -o etime= -p "\$PID" 2>/dev/null | tr -d ' ')
        echo "✅ \$SCRIPT — ÇALIŞIYOR (PID \$PID, süre \$ELAPSED)"
    else
        echo "⏸️  \$SCRIPT — çalışmıyor"
    fi
done

echo ""
echo "----- Son loglar -----"
for LOG in manual.log manual_daily.log cron.log cron_daily.log; do
    if [ -f "\$APP_DIR/\$LOG" ]; then
        SIZE=\$(stat -c%s "\$APP_DIR/\$LOG" 2>/dev/null || stat -f%z "\$APP_DIR/\$LOG")
        echo ""
        echo "[\$LOG] (\$SIZE bayt) son 5 satır:"
        tail -5 "\$APP_DIR/\$LOG" | sed 's/^/  /'
    fi
done
EOF
chmod +x "$APP_DIR/status.sh"

# Manuel durdurma
cat > "$APP_DIR/stop.sh" <<EOF
#!/bin/bash
APP_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
KILLED=0
for SCRIPT in clickup_bot.py clickup_due_report.py; do
    if pgrep -f "\$APP_DIR/\$SCRIPT" >/dev/null 2>&1; then
        pkill -9 -f "\$APP_DIR/\$SCRIPT"
        KILLED=1
        echo "⛔ \$SCRIPT durduruldu."
    fi
done
if [ \$KILLED -eq 0 ]; then
    echo "ℹ️  Çalışan ClickUp bot süreci yoktu."
fi
EOF
chmod +x "$APP_DIR/stop.sh"

CURRENT_USER="$(id -un)"
echo "[5/7] $CURRENT_USER için şifresiz shutdown yetkisi veriliyor..."
SUDOERS_FILE="/etc/sudoers.d/clickup-bot-shutdown"
echo "$CURRENT_USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee "$SUDOERS_FILE" >/dev/null
sudo chmod 440 "$SUDOERS_FILE"

echo "[6/7] Cron işleri ekleniyor (haftalık + günlük) — 7/24 açık sunucu için kapatmasız wrapper'lar..."
# Haftalık bakiye: Salı 09:00 TR (06:00 UTC) — kapatmasız
WEEKLY_LINE="0 6 * * 2 $APP_DIR/run.sh >> $APP_DIR/cron.log 2>&1"
# Günlük tarih değişiklik raporu: Pzt-Cum 21:00 TR (18:00 UTC) — kapatmasız
DAILY_LINE="0 18 * * 1-5 $APP_DIR/run_daily.sh >> $APP_DIR/cron_daily.log 2>&1"
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
echo "ELLE TEST (önerilen — bağlantı koparsa bile çalışır):"
echo "  Bakiye raporu        :  $APP_DIR/run_detached.sh"
echo "  Günlük tarih raporu  :  $APP_DIR/run_daily_detached.sh"
echo "  Durumu gör           :  $APP_DIR/status.sh"
echo "  Durdur               :  $APP_DIR/stop.sh"
echo "  Canlı log            :  tail -f $APP_DIR/manual.log"
echo ""
echo "(Foreground çalıştırma — bağlantı kopunca ölür):"
echo "  $APP_DIR/run.sh"
echo ""
echo "Otomatik cron'lar (TR saati) — 7/24 açık sunucu modu:"
echo "  Salı 09:00            -> run.sh         (haftalık bakiye)"
echo "  Pzt-Cum 21:00         -> run_daily.sh   (günlük tarih raporu)"
echo ""
echo "Eğer EventBridge ile sunucuyu otomatik açıp kapatmak istersen:"
echo "  cron'u manuel olarak run_and_stop.sh / run_daily_and_stop.sh'e çevir"
echo "  ve şu schedule'ları kur:"
echo "    Clickup-Uyandir       -> Salı 08:50 start"
echo "    Clickup-Uyut          -> Salı 10:00 stop (yedek)"
echo "    Clickup-Daily-Uyandir -> Pzt-Cum 20:50 start"
echo "    Clickup-Daily-Uyut    -> Pzt-Cum 21:30 stop (yedek)"
echo ""
echo "Logları izlemek için:"
echo "  tail -f $APP_DIR/cron.log         (haftalık)"
echo "  tail -f $APP_DIR/cron_daily.log   (günlük)"
echo ""
