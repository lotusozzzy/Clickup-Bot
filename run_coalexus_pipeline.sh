#!/usr/bin/env bash
# Coalexus Pipeline Update — cron wrapper (Pzt-Cum 20:00 TRT = 17:00 UTC).
# Log cron satırında redirect ile yazılır:
#   0 17 * * 1-5 /home/ubuntu/clickup-bot/run_coalexus_pipeline.sh >> /home/ubuntu/clickup-bot/coalexus_pipeline.log 2>&1
# DRY_RUN / RECIPIENT_OVERRIDE çağıran ortamdan MİRAS alınır (.env'de yok,
# bu wrapper bunları ezmez) → manuel test için:
#   DRY_RUN=1 /home/ubuntu/clickup-bot/run_coalexus_pipeline.sh
cd /home/ubuntu/clickup-bot || exit 1
set -a
source /home/ubuntu/clickup-bot/.env
set +a
exec /home/ubuntu/clickup-bot/venv/bin/python /home/ubuntu/clickup-bot/coalexus_pipeline_report.py
