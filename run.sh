#!/usr/bin/env bash
# SGS MLBB Guide — production launcher.
# Creates/refreshes venv, installs deps, warms cache, then starts gunicorn.
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PORT="${PORT:-8085}"
WORKERS="${WORKERS:-2}"
TIMEOUT="${TIMEOUT:-60}"

if [ ! -d venv ]; then
  python3 -m venv venv
fi

# shellcheck disable=SC1091
. venv/bin/activate

pip install --upgrade pip >/dev/null
pip install -r requirements.txt

python -c "from app import warm_cache; warm_cache()" || echo "[run.sh] Warm cache failed — continuing with lazy loads."

exec gunicorn \
  -b "0.0.0.0:${PORT}" \
  -w "${WORKERS}" \
  --timeout "${TIMEOUT}" \
  --access-logfile - \
  --error-logfile - \
  app:app
