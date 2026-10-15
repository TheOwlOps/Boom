#!/bin/bash
# Boom - Stop all services
for pid in /tmp/boom_api.pid /tmp/boom_web.pid /tmp/boom_gateway.pid; do
    if [ -f "$pid" ]; then
        kill "$(cat "$pid")" 2>/dev/null || true
        rm -f "$pid"
    fi
done
# Fallback: kill by pattern
pkill -f "gateway.api" 2>/dev/null || true
pkill -f "vite preview" 2>/dev/null || true
echo "[boom] All services stopped."
