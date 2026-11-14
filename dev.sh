#!/usr/bin/env bash
# lymow-console dev launcher: backend (uvicorn --reload) + frontend (vite).
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

# ── backend venv ────────────────────────────────────────────────
if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -e "backend[dev]"
fi

backend/.venv/bin/python -m uvicorn app.main:app \
  --app-dir backend --host 127.0.0.1 --port 8384 --reload &
BACK_PID=$!

# ── frontend dev server (proxies /api and /ws to 8384) ─────────
(cd frontend && npm install --silent && npm run dev) &
FRONT_PID=$!

trap 'kill $BACK_PID $FRONT_PID 2>/dev/null || true' EXIT INT TERM
echo
echo "backend  → http://127.0.0.1:8384  (API + /healthz)"
echo "frontend → http://127.0.0.1:5173  (dev UI)"
wait
