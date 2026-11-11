"""Scheduled, atomic SQLite snapshots using the online backup API."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from .config import DB_PATH
from backup_db import backup_database

log = logging.getLogger(__name__)


def _backup_and_prune():
    folder = DB_PATH.parent / 'backups'
    now = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup_database(DB_PATH, folder / f'prices-{now}.sqlite3')
    # Keep the last 14 snapshots to avoid filling the disk.
    files = sorted(folder.glob('prices-*.sqlite3'), reverse=True)
    for old in files[14:]:
        old.unlink()


async def scheduled_backup():
    try:
        await asyncio.to_thread(_backup_and_prune)
    except Exception:
        log.exception('Не удалось создать регулярную резервную копию SQLite')
