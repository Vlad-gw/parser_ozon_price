"""Create a consistent SQLite snapshot even while the service uses WAL mode.

Run: .venv/bin/python backup_db.py
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def database_path() -> Path:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    path = Path(os.getenv('DB_PATH', str(ROOT / 'data' / 'prices.sqlite3'))).expanduser()
    return path if path.is_absolute() else ROOT / path


def backup_database(source: Path, destination: Path):
    if not source.exists():
        raise FileNotFoundError(f'База не найдена: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix('.tmp')
    try:
        if tmp.exists():
            tmp.unlink()
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=20) as db:
            with sqlite3.connect(tmp) as copy:
                db.backup(copy, pages=100, sleep=.1)
                result = copy.execute('PRAGMA integrity_check').fetchone()[0]
                if result != 'ok':
                    raise RuntimeError(f'Проверка копии: {result}')
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            tmp.unlink()
    return destination


if __name__ == '__main__':
    now = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = backup_database(database_path(), ROOT / 'data' / 'backups' / f'prices-{now}.sqlite3')
    print(f'Резервная копия создана и проверена: {path}')
