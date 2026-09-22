#!/usr/bin/env bash
# Gramma — quick environment bootstrap (sandbox/nodule resets lose .venv & node_modules)
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Python venv + deps"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Mini-App deps + build (VITE_API_BASE='' same-origin)"
cd miniapp
npm install
VITE_API_BASE="" npm run build
cd ..

echo "==> Tests"
.venv/bin/python -m pytest tests/ -q

echo "✅ All set."
