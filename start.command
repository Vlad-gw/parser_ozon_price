#!/bin/bash
# Local macOS startup; intentionally bound to loopback only.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo 'Сначала: bash setup.command' >&2; exit 1; fi
if [ ! -f .env ]; then cp .env.example .env; fi
PORT=9223
CDP="http://localhost:$PORT"
CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
PROFILE="$HOME/.ozon-price-tracker-chrome"
if ! curl --noproxy '*' -fsS --max-time 2 "$CDP/json/version" >/dev/null 2>&1; then
  if [ ! -x "$CHROME" ]; then echo 'Chrome не найден. Установите Google Chrome.' >&2; exit 1; fi
  echo 'Запускаю отдельный Chrome для чтения Ozon...'
  "$CHROME" --remote-debugging-port="$PORT" --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
    > /tmp/ozon-price-tracker-chrome.log 2>&1 &
  ready=0
  for i in {1..20}; do
    if curl --noproxy '*' -fsS --max-time 2 "$CDP/json/version" >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then echo 'Chrome CDP не отвечает. Лог: /tmp/ozon-price-tracker-chrome.log' >&2; exit 1; fi
fi
export OZON_HEADLESS=false
export PRICE_TRACKER_CDP_URL="$CDP"
echo 'Сайт: http://127.0.0.1:8000 (откройте в обычном Chrome рядом с ChatGPT)'
echo 'Остановка: Ctrl+C. Специальный Chrome можно свернуть.'
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
