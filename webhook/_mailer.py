"""Operasyonel uyarı maili gönderimi.

clickup_bot.py'deki SMTP yapılandırmasıyla aynı env var'ları kullanır:
    SMTP_HOST, SMTP_PORT, SMTP_FROM, SMTP_PASSWORD

Alıcı adresi aşağıdaki sırayla çözülür:
    explicit recipient param  →  ALERT_EMAIL  →  MAIL_TO_DAILY  →  MAIL_TO
    →  son çare 'solarframemuhasebe@solarfame.com'

ALERT_EMAIL env var ile operasyonel uyarıları yönetim/teknik adresinden
ayırabilirsin (.env'de tanımlanmadıysa MAIL_TO_DAILY'ye düşer).
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _resolve_recipient(explicit=None):
    if explicit:
        return explicit
    return (
        os.environ.get("ALERT_EMAIL")
        or os.environ.get("MAIL_TO_DAILY")
        or os.environ.get("MAIL_TO")
        or "solarframemuhasebe@solarfame.com"
    )


def send_alert(subject, body_html, recipient=None):
    """Operasyonel uyarı maili gönder.

    SMTP_PASSWORD eksikse RuntimeError fırlatır (sessizce yutmuyoruz —
    çağıran taraf log'a yazıp pas geçmek isterse exception'ı yakalar).
    """
    smtp_host = os.environ.get("SMTP_HOST", "smtp.yandex.com.tr")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    sender = os.environ.get("SMTP_FROM", "clickup@solarfame.com")
    password = os.environ.get("SMTP_PASSWORD", "")
    if not password:
        raise RuntimeError("SMTP_PASSWORD set edilmedi - .env yüklendi mi?")

    to_addr = _resolve_recipient(recipient)

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html", _charset="utf-8"))

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as s:
        s.login(sender, password)
        s.send_message(msg)

    return to_addr
