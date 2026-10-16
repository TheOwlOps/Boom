#!/bin/bash
# Boom - Unified startup script
#
# Usage: ./start.sh [api|web|gateway|all|status] [--help]
#
# Services (all bind to 127.0.0.1 by default):
#   api      Python HTTP API for the web UI   (port 18080, override with BOOM_API_PORT)
#   web      Web UI served by vite preview    (port 5173,  override with BOOM_WEB_PORT)
#   gateway  Multi-channel gateway daemon     (ingress port 8788, override with BOOM_INGRESS_PORT)
#   all      api + web + gateway
#   status   Show which services are running
#
# Ports, logs and pid files: ports can be overridden with the env vars above.
# Logs and pid files live in /tmp/boom-<repohash>/ (namespaced by repo path so
# two checkouts on one machine never clobber each other).

ROOT="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-all}"

API_PORT="${BOOM_API_PORT:-18080}"
WEB_PORT="${BOOM_WEB_PORT:-5173}"
INGRESS_PORT="${BOOM_INGRESS_PORT:-8788}"

# Namespace pid/log files by repo path hash: two checkouts on one machine
# must not share /tmp/boom_api.pid.
PID_TAG="$(printf '%s' "$ROOT" | md5sum | cut -c1-8)"
RUNDIR="/tmp/boom-${PID_TAG}"
mkdir -p "$RUNDIR"

API_PIDFILE="$RUNDIR/api.pid"
WEB_PIDFILE="$RUNDIR/web.pid"
GATEWAY_PIDFILE="$RUNDIR/gateway.pid"

log()  { echo "[boom] $*"; }
fail() { echo "[boom] ERROR: $*" >&2; return 1; }

# --- prerequisites -----------------------------------------------------------

need_cmd() { # need_cmd <cmd> <install-hint>
    command -v "$1" >/dev/null 2>&1 || {
        fail "missing required command '$1'. $2"
        return 1
    }
}

check_python() {
    need_cmd python3 "Install Python >= 3.11 (https://www.python.org/downloads/) or use devbox: 'devbox shell'." || return 1
    local ver
    ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$ver" in
        3.1[1-9]*|3.[2-9][0-9]*) ;; # 3.11+
        *) fail "python3 is $ver but Boom requires Python >= 3.11."; return 1 ;;
    esac
    if ! python3 -c 'import httpx, pydantic' 2>/dev/null; then
        log "Installing missing Python dependencies (httpx, pydantic, pyyaml)..."
        pip_install httpx "pydantic>=2" pyyaml || return 1
    fi
}

pip_install() { # pip_install <pkgs...>: pip install with PEP 668 fallback
    if python3 -m pip install --quiet "$@" 2>/tmp/boom-pip-err.log; then
        return 0
    fi
    if grep -q "externally-managed-environment" /tmp/boom-pip-err.log 2>/dev/null; then
        log "System Python is externally managed (PEP 668); retrying with --break-system-packages..."
        python3 -m pip install --quiet --break-system-packages "$@" 2>/tmp/boom-pip-err.log || {
            fail "pip install failed. Create a venv first: python3 -m venv .venv && source .venv/bin/activate"
            return 1
        }
        return 0
    fi
    if ! python3 -m pip --version >/dev/null 2>&1; then
        fail "pip is not available for python3. Install it (e.g. 'python3 -m ensurepip' or your OS package)."
        return 1
    fi
    fail "pip install $* failed. See /tmp/boom-pip-err.log for details."
    return 1
}

check_node() {
    need_cmd node "Install Node.js >= 20 (https://nodejs.org/) or use devbox: 'devbox shell'." || return 1
    local major
    major="$(node --version | sed 's/^v//; s/\..*//')"
    if [ "$major" -lt 20 ] 2>/dev/null; then
        fail "node is $(node --version) but the web UI requires Node >= 20."
        return 1
    fi
    need_cmd npm "Node.js ships with npm; reinstall Node.js from https://nodejs.org/." || return 1
}

# --- port / process helpers --------------------------------------------------

port_in_use() { # port_in_use <port> -> 0 if something listens on 127.0.0.1:<port>
    python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', $1))" 2>/dev/null
}

wait_for_port() { # wait_for_port <port> <timeout_secs> -> 0 when listening
    local port="$1" timeout="${2:-15}" i=0
    while [ "$i" -lt "$timeout" ]; do
        port_in_use "$port" && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

service_pid() { # service_pid <pidfile>: echo pid if the recorded process is alive
    local pidfile="$1" pid=""
    [ -f "$pidfile" ] && pid="$(head -n 1 "$pidfile" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
        return 0
    fi
    return 1
}

# --- service starters ---------------------------------------------------------

start_api() {
    check_python || return 1
    if service_pid "$API_PIDFILE" >/dev/null; then
        log "API already running (pid $(service_pid "$API_PIDFILE"))."
        return 0
    fi
    if port_in_use "$API_PORT"; then
        fail "port $API_PORT is already in use. Stop the other service or set BOOM_API_PORT to a free port."
        return 1
    fi
    log "Starting API server on 127.0.0.1:${API_PORT}..."
    cd "$ROOT" || return 1
    # api.py adds the repo root and agent/src to sys.path itself, so no PYTHONPATH needed.
    nohup python3 -m gateway.api >>"$RUNDIR/api.log" 2>&1 &
    local pid=$!
    printf '%s\n%s\n' "$pid" "$ROOT" > "$API_PIDFILE"
    if ! wait_for_port "$API_PORT" 20; then
        log "API failed to listen on :$API_PORT. Last log lines:"
        tail -n 20 "$RUNDIR/api.log" >&2
        rm -f "$API_PIDFILE"
        return 1
    fi
    log "API running (pid $pid): http://127.0.0.1:${API_PORT}"
}

start_web() {
    check_node || return 1
    if service_pid "$WEB_PIDFILE" >/dev/null; then
        log "Web UI already running (pid $(service_pid "$WEB_PIDFILE"))."
        return 0
    fi
    if port_in_use "$WEB_PORT"; then
        fail "port $WEB_PORT is already in use. Stop the other service or set BOOM_WEB_PORT to a free port."
        return 1
    fi
    (
        cd "$ROOT/apps/web" || exit 1
        if [ ! -x "node_modules/.bin/vite" ]; then
            log "Installing web dependencies (npm install)..."
            npm install --no-audit --no-fund --no-progress || exit 1
        fi
        if [ ! -f "dist/index.html" ]; then
            log "Building web UI (npm run build)..."
            npm run build || exit 1
        fi
        log "Starting web UI on 127.0.0.1:${WEB_PORT}..."
        # Use the local vite binary directly: npx would prompt to download vite
        # when node_modules is incomplete and hang on headless machines.
        # --strictPort: fail loudly instead of silently moving to another port.
        nohup ./node_modules/.bin/vite preview \
            --port "$WEB_PORT" --host 127.0.0.1 --strictPort \
            >>"$RUNDIR/web.log" 2>&1 &
        printf '%s\n%s\n' "$!" "$ROOT" > "$WEB_PIDFILE"
    ) || return 1
    if ! wait_for_port "$WEB_PORT" 20; then
        log "Web UI failed to listen on :$WEB_PORT. Last log lines:"
        tail -n 20 "$RUNDIR/web.log" >&2
        rm -f "$WEB_PIDFILE"
        return 1
    fi
    log "Web UI running (pid $(service_pid "$WEB_PIDFILE")): http://127.0.0.1:${WEB_PORT}"
}

start_gateway() {
    check_python || return 1
    if service_pid "$GATEWAY_PIDFILE" >/dev/null; then
        log "Gateway already running (pid $(service_pid "$GATEWAY_PIDFILE"))."
        return 0
    fi
    if port_in_use "$INGRESS_PORT"; then
        fail "port $INGRESS_PORT is already in use. Stop the other service or set BOOM_INGRESS_PORT to a free port."
        return 1
    fi
    log "Starting gateway daemon (ingress 127.0.0.1:${INGRESS_PORT})..."
    log "Tip: run 'python3 boom.py setup' first to configure providers and channel tokens."
    cd "$ROOT" || return 1
    nohup python3 boom.py --gateway >>"$RUNDIR/gateway.log" 2>&1 &
    local pid=$!
    printf '%s\n%s\n' "$pid" "$ROOT" > "$GATEWAY_PIDFILE"
    # The daemon prints its banner unbuffered only on a TTY; give it a moment,
    # then verify the process survived startup (ingress binds inside asyncio).
    sleep 3
    if ! kill -0 "$pid" 2>/dev/null; then
        log "Gateway exited during startup. Last log lines:"
        tail -n 20 "$RUNDIR/gateway.log" >&2
        rm -f "$GATEWAY_PIDFILE"
        return 1
    fi
    if ! port_in_use "$INGRESS_PORT"; then
        log "Warning: gateway is running (pid $pid) but nothing listens on :$INGRESS_PORT yet."
    fi
    log "Gateway running (pid $pid)."
}

show_status() {
    local name pidfile port pid
    for spec in "API:$API_PIDFILE:$API_PORT" "Web UI:$WEB_PIDFILE:$WEB_PORT" "Gateway:$GATEWAY_PIDFILE:$INGRESS_PORT"; do
        name="${spec%%:*}"; rest="${spec#*:}"
        pidfile="${rest%%:*}"; port="${rest##*:}"
        if pid="$(service_pid "$pidfile")"; then
            if port_in_use "$port"; then
                echo "  $name: RUNNING (pid $pid, port $port)"
            else
                echo "  $name: STARTING/UNHEALTHY (pid $pid alive, port $port not listening)"
            fi
        else
            echo "  $name: stopped"
        fi
    done
}

print_help() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    echo ""
    echo "Environment overrides:"
    echo "  BOOM_API_PORT       API port (default 18080)"
    echo "  BOOM_WEB_PORT       Web UI port (default 5173)"
    echo "  BOOM_INGRESS_PORT   Gateway ingress port (default 8788)"
    echo "  BOOM_HOME           Config/state directory (default ~/.boom); run 'python3 boom.py setup' to configure."
    echo ""
    echo "Stop with: ./stop.sh"
}

rc=0
case "$MODE" in
    api)     start_api || rc=1 ;;
    web)     start_web || rc=1 ;;
    gateway) start_gateway || rc=1 ;;
    all)
        start_api || rc=1
        start_web || rc=1
        start_gateway || rc=1
        echo ""
        if [ "$rc" -eq 0 ]; then
            log "All services running:"
            echo "  API:      http://127.0.0.1:${API_PORT}"
            echo "  Web UI:   http://127.0.0.1:${WEB_PORT}"
            echo "  Gateway:  ingress on 127.0.0.1:${INGRESS_PORT}"
        else
            fail "Some services failed to start. Check the logs in $RUNDIR/."
        fi
        echo ""
        echo "Stop with: ./stop.sh"
        ;;
    status)  show_status ;;
    help|--help|-h) print_help ;;
    *)
        echo "Usage: $0 [api|web|gateway|all|status] [--help]" >&2
        exit 1
        ;;
esac
exit "$rc"
