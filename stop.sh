#!/bin/bash
# Boom - Stop all services started by ./start.sh
#
# Only touches processes that this repo's start.sh launched: a recorded PID is
# killed only after verifying the process still belongs to this checkout
# (via /proc/<pid>/cwd). The pkill fallback likewise only kills processes
# whose working directory is this repo, so other checkouts or other users'
# services are never harmed.

ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_TAG="$(printf '%s' "$ROOT" | md5sum | cut -c1-8)"
RUNDIR="/tmp/boom-${PID_TAG}"

belongs_to_repo() { # belongs_to_repo <pid> -> 0 if the process runs from $ROOT
    local pid="$1" cwd
    [ -n "$pid" ] || return 1
    [ -d "/proc/$pid" ] || return 1
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)" || return 1
    [ "$cwd" = "$ROOT" ]
}

stop_pidfile() { # stop_pidfile <pidfile> <label>
    local pidfile="$1" label="$2" pid="" root=""
    [ -f "$pidfile" ] || return 0
    pid="$(sed -n '1p' "$pidfile" 2>/dev/null)"
    root="$(sed -n '2p' "$pidfile" 2>/dev/null)"
    rm -f "$pidfile"
    if [ -z "$pid" ]; then
        return 0
    fi
    if [ "$root" != "$ROOT" ]; then
        echo "[boom] $label: pid file belongs to another checkout ($root); skipping pid $pid."
        return 0
    fi
    if ! belongs_to_repo "$pid"; then
        echo "[boom] $label: pid $pid is not a Boom process from this repo (stale pid file); skipping."
        return 0
    fi
    kill "$pid" 2>/dev/null
    # Give it a moment to exit gracefully, then escalate.
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        echo "[boom] $label: force-killed pid $pid."
    else
        echo "[boom] $label: stopped pid $pid."
    fi
}

# Fallback: kill by command pattern, but ONLY processes running from this repo.
# This catches services whose pid file was lost, without touching other
# checkouts or other users' processes.
pkill_in_repo() { # pkill_in_repo <label> <pgrep-pattern>
    local label="$1" pattern="$2" pid
    for pid in $(pgrep -f "$pattern" 2>/dev/null); do
        [ "$pid" != "$$" ] || continue
        if belongs_to_repo "$pid"; then
            kill "$pid" 2>/dev/null && echo "[boom] $label: stopped stray pid $pid."
        fi
    done
}

stop_pidfile "$RUNDIR/api.pid"     "API"
stop_pidfile "$RUNDIR/gateway.pid" "Gateway"

pkill_in_repo "API"     "gateway\.api"
pkill_in_repo "Gateway" "boom\.py --gateway"

echo "[boom] All services stopped."
