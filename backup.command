#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo 'Сначала выполните bash setup.command' >&2; exit 1; fi
.venv/bin/python backup_db.py
