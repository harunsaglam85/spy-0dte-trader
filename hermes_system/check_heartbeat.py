#!/usr/bin/env python3
"""
check_heartbeat.py — External liveness check for the Hermes engine (audit U3).

The engine touches /root/hermes_system/heartbeat.txt on every loop iteration
during market hours. systemd (hermes-heartbeat.timer) runs this script every
5 minutes; if the file is missing or its mtime is more than 5 minutes old
during market hours, a Telegram alert is sent. This catches a *hung* process
(stuck socket, deadlock) that systemd's crash supervision cannot see.

Outside market hours the engine does not touch the file, so the check is a
no-op. The check starts at 9:36 ET so yesterday's heartbeat does not
false-alarm before the engine's first in-hours iteration.
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import time
from datetime import datetime
from pathlib import Path

import pytz
from dotenv import load_dotenv

load_dotenv('/root/spy-0dte-trader/.env')

from core.telegram_alerts import send as tg_send

ET = pytz.timezone('America/New_York')
HEARTBEAT_FILE = Path('/root/hermes_system/heartbeat.txt')
STALE_SECS = 300


def main() -> None:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    if not ((9, 36) <= (now.hour, now.minute) <= (16, 0)):
        return

    if not HEARTBEAT_FILE.exists():
        tg_send(
            '🚨 Hermes heartbeat MISSING during market hours — '
            'engine has never written heartbeat.txt. Check hermes-engine.'
        )
        return

    age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    if age > STALE_SECS:
        tg_send(
            f'🚨 Hermes heartbeat STALE — last update {int(age // 60)} min ago '
            f'during market hours. Engine may be hung (not crashed); '
            f'check hermes-engine / consider systemctl restart.'
        )


if __name__ == '__main__':
    main()
