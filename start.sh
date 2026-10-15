#!/bin/bash
# Boom - Unified startup script
# Usage: ./start.sh [api|web|gateway|all]
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-all}"

export PYTHONPATH="$ROOT:$ROOT/agent/src:$PYTHONPATH"

start_api() {
    echo "[boom] Starting API server on :18080..."
    python3 -m gateway.api &
    echo $! > /tmp/boom_api.pid
}

start_web() {
    echo "[boom] Starting web UI on :5173..."
    cd "$ROOT/apps/web"
    if [ ! -d "node_modules" ]; then
        echo "[boom] Installing web dependencies..."
        npm install --no-audit --no-fund
    fi
    if [ ! -d "dist" ]; then
        echo "[boom] Building web UI..."
        npm run build
    fi
    npx vite preview --port 5173 --host 127.0.0.1 &
    echo $! > /tmp/boom_web.pid
    cd "$ROOT"
}

start_gateway() {
    echo "[boom] Starting gateway daemon..."
    python3 boom.py --gateway &
    echo $! > /tmp/boom_gateway.pid
}

case "$MODE" in
    api) start_api ;;
    web) start_web ;;
    gateway) start_gateway ;;
    all)
        start_api
        sleep 2
        start_web
        echo ""
        echo "[boom] All services running:"
        echo "  API:     http://127.0.0.1:18080"
        echo "  Web UI:  http://127.0.0.1:5173"
        echo ""
        echo "Stop with: ./stop.sh"
        ;;
    *) echo "Usage: $0 [api|web|gateway|all]"; exit 1 ;;
esac
