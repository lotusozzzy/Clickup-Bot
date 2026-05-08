#!/bin/bash
# Faz 1: webhook.solarfame.com için nginx + Let's Encrypt kurulumu.
#
# Ön koşullar (kullanıcı tarafından zaten tamam):
#   - DNS A kaydı EC2 public IP'sine bağlı, propagasyon tamam
#   - AWS Security Group'ta 80 ve 443 açık
#   - Ubuntu EC2 + sudo yetkisi
#
# Kullanım:
#   WEBHOOK_EMAIL=ozan@solarfame.com bash webhook/setup_phase1.sh
#
# Tamamlandığında /health endpoint'i hem HTTP hem HTTPS üzerinden 'ok' döner.
# Flask receiver Faz 2'de gelir; şu an proxy_pass için yer tutucu var (502 verir,
# bu beklenen).

set -e

DOMAIN="${WEBHOOK_DOMAIN:-webhook.solarfame.com}"
EMAIL="${WEBHOOK_EMAIL:-}"
SITE_FILE="/etc/nginx/sites-available/webhook"
SITE_LINK="/etc/nginx/sites-enabled/webhook"
DEFAULT_LINK="/etc/nginx/sites-enabled/default"

echo ""
echo "=========================================="
echo "  Faz 1 - $DOMAIN için nginx + HTTPS"
echo "=========================================="
echo ""

if [ -z "$EMAIL" ]; then
    echo "❌ WEBHOOK_EMAIL set edilmedi (Let's Encrypt için zorunlu)."
    echo "   Çalıştır:  WEBHOOK_EMAIL=siz@solarfame.com bash webhook/setup_phase1.sh"
    exit 1
fi

echo "[1/9] nginx ve certbot kuruluyor..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx >/dev/null
echo "  -> nginx $(nginx -v 2>&1), certbot $(certbot --version 2>&1)"

echo "[2/9] nginx site config yazılıyor: $SITE_FILE"
sudo tee "$SITE_FILE" > /dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /health {
        return 200 'ok\n';
        add_header Content-Type text/plain;
    }

    # Faz 2'de Flask receiver 8080'de dinleyecek.
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
    }
}
EOF

echo "[3/9] sites-enabled symlink + default site temizliği..."
sudo ln -sf "$SITE_FILE" "$SITE_LINK"
if [ -L "$DEFAULT_LINK" ] || [ -e "$DEFAULT_LINK" ]; then
    sudo rm -f "$DEFAULT_LINK"
    echo "  -> /etc/nginx/sites-enabled/default kaldırıldı"
fi

echo "[4/9] nginx -t (config doğrulama)..."
sudo nginx -t

echo "[5/9] nginx reload..."
sudo systemctl reload nginx
sudo systemctl enable nginx >/dev/null 2>&1 || true

echo "[6/9] UFW firewall kontrol..."
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
    echo "  UFW aktif - 80/443 izin veriliyor."
    sudo ufw allow 80/tcp >/dev/null
    sudo ufw allow 443/tcp >/dev/null
    sudo ufw reload >/dev/null
else
    echo "  UFW pasif veya yok, atlanıyor (AWS Security Group zaten açık)."
fi

echo "[7/9] HTTP /health smoke test..."
sleep 1
HTTP_BODY=$(curl -fsS --max-time 10 "http://$DOMAIN/health" 2>&1) || {
    echo "❌ HTTP /health başarısız. nginx logu:"
    sudo tail -20 /var/log/nginx/error.log
    exit 1
}
echo "  HTTP /health  → '$(echo "$HTTP_BODY" | tr -d '\n')'"

echo "[8/9] Let's Encrypt sertifikası alınıyor (certbot --nginx)..."
sudo certbot --nginx -d "$DOMAIN" \
    --non-interactive --agree-tos -m "$EMAIL" \
    --redirect

echo "[9/9] HTTPS /health + renew dry-run..."
sleep 1
HTTPS_BODY=$(curl -fsS --max-time 10 "https://$DOMAIN/health" 2>&1) || {
    echo "❌ HTTPS /health başarısız. nginx logu:"
    sudo tail -20 /var/log/nginx/error.log
    exit 1
}
echo "  HTTPS /health → '$(echo "$HTTPS_BODY" | tr -d '\n')'"

echo ""
echo "  Renew dry-run:"
sudo certbot renew --dry-run 2>&1 | grep -E "Congratulations|Cert|simulation|success" | sed 's/^/    /'

CERT_DIR="/etc/letsencrypt/live/$DOMAIN"

echo ""
echo "=========================================="
echo "  FAZ 1 TAMAM"
echo "=========================================="
echo "  nginx config         : $SITE_FILE"
echo "  enabled symlink      : $SITE_LINK"
echo "  cert dizini          : $CERT_DIR"
echo "  fullchain.pem        : $CERT_DIR/fullchain.pem"
echo "  privkey.pem          : $CERT_DIR/privkey.pem"
echo "  HTTP  /health çıktısı: '$(echo "$HTTP_BODY" | tr -d '\n')'"
echo "  HTTPS /health çıktısı: '$(echo "$HTTPS_BODY" | tr -d '\n')'"
echo ""
echo "  Renewal: certbot.timer ile otomatik yönetiliyor (systemctl status certbot.timer)."
echo "  Faz 2: webhook/setup_phase2.sh - Flask receiver'ı 8080'de başlatacak."
echo ""
