#!/bin/bash
# macOS: run once in Terminal
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Нужен Python 3.12 или новее.' >&2; exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3,12):
    raise SystemExit('Нужен Python 3.12 или новее')
PY
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
if [ ! -f .env ]; then
  cp .env.example .env
  echo 'Создан .env. Впишите Telegram-токен и Chat ID через nano .env'
else
  echo 'Существующий .env сохранён.'
fi
mkdir -p data/backups
if [ ! -x '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' ]; then
  echo 'Внимание: установите Google Chrome для локального браузерного режима.'
fi
echo 'Готово. Запуск: bash start.command'
