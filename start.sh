#!/usr/bin/env sh
# Start AI Phone from this folder, setting up the Python environment on first run.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv and installing packages..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example. Edit it, then run ./start.sh again."; exit 0; }

exec .venv/bin/python -m server "$@"
