#!/bin/bash
# VPS experimental: requires Python venv, Playwright chromium and Xvfb.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then echo 'Создайте .env с APP_PASSWORD и Telegram-полями' >&2; exit 1; fi
if ! grep -Eq '^APP_PASSWORD=.{12,}' .env; then echo 'Для VPS задайте APP_PASSWORD длиной минимум 12 символов' >&2; exit 1; fi
if ! command -v xvfb-run >/dev/null; then echo 'Установите xvfb: sudo apt install xvfb' >&2; exit 1; fi
export OZON_HEADLESS=false
unset PRICE_TRACKER_CDP_URL
exec xvfb-run -a .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
