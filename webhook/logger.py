"""Webhook receiver için tek FileHandler logger.

Faz 6'da log rotation eklenecek; şimdilik sade bir FileHandler.
Modül seviyesinde get_logger() çağrısı idempotent — handler tek seferlik eklenir.
"""

import logging
import os

LOG_PATH = os.environ.get(
    "WEBHOOK_LOG_PATH",
    os.path.expanduser("~/clickup-bot/webhook.log"),
)

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

_logger = None


def get_logger():
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("webhook")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # root logger'a düşmesin

    # Idempotent handler kontrolü (gunicorn worker'ları yeniden başlayınca
    # modül yeniden import olabilir).
    has_file_handler = any(
        isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(LOG_PATH)
        for h in logger.handlers
    )
    if not has_file_handler:
        parent = os.path.dirname(LOG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(fh)

    _logger = logger
    return logger
