#!/usr/bin/env bash
#
# LV Copytrader — mt5-sidecar entrypoint.
#
# Order of business, once per container start:
#   1. Xvfb on :99, because Wine's null graphics driver makes MT5's window creation fail
#      with "Application tried to create a window, but no driver could be loaded".
#   2. A one-time bootstrap guarded by marker files inside the /config volume: Wine prefix,
#      the MT5 terminal, 64-bit Windows Python 3.11 and the MetaTrader5 package. Because
#      /config is a named volume, this happens once per volume, not once per container.
#   3. Render common.ini from the template and convert it to UTF-16LE.
#   4. Launch terminal64.exe /portable /config:C:/MT5/common.ini in the background.
#   5. Run rpc_server.py under the Wine Python in the foreground.
#
# Nothing here ever prints MT5_PASSWORD or SIDECAR_TOKEN.

set -euo pipefail

# --------------------------------------------------------------------------------------
# Wine environment, exported before anything touches the prefix.
# WINEDLLOVERRIDES=mscoree= suppresses the Wine Mono installer prompt outright. Mono is
# deliberately not installed: nothing in this container is .NET, and the prompt is a modal
# dialog that would block a headless first run forever.
# --------------------------------------------------------------------------------------
export WINEARCH="${WINEARCH:-win64}"
export WINEPREFIX="${WINEPREFIX:-/config/.wine}"
export WINEDEBUG="${WINEDEBUG:--all}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree=}"
export DISPLAY="${DISPLAY:-:99}"

APP_DIR="${SP_APP_DIR:-/opt/signalpilot}"
DRIVE_C="${WINEPREFIX}/drive_c"
MT5_DIR="${DRIVE_C}/MT5"
SP_DIR="${DRIVE_C}/sp"
BOOT_DIR="/config/.bootstrap"

MT5_INSTALLER_URL="${MT5_INSTALLER_URL:-https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe}"
# 64-bit on purpose — see bootstrap_python().
PYTHON_INSTALLER_URL="${PYTHON_INSTALLER_URL:-https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe}"

SP_ROLE="${SP_ROLE:-sidecar}"                 # "sidecar" or "vnc" (the compose debug profile)
ENABLE_VNC="${ENABLE_VNC:-0}"                 # start x11vnc inside this container too
VNC_PORT="${VNC_PORT:-5901}"
XVFB_GEOMETRY="${XVFB_GEOMETRY:-1280x1024x24}"
MT5_INSTALL_TIMEOUT="${MT5_INSTALL_TIMEOUT:-900}"
MT5_START_TIMEOUT="${MT5_START_TIMEOUT:-180}"
MT5_INI_BOM="${MT5_INI_BOM:-0}"               # set to 1 if a terminal build wants the BOM

# Windows-side paths. Forward slashes everywhere they reach mt5.initialize(path=...):
# backslashes there are a documented cause of initialize() returning False with
# last_error() == (-10005, 'IPC timeout').
# BACKSLASHES here too, for the same reason as the ini path below and NOT the same rule as
# mt5.initialize(path=...), which wants forward slashes. Measured: launching
# `wine C:/MT5/terminal64.exe` leaves a wine wrapper process alive just long enough for
# `pgrep -f terminal64.exe` to report success, while the terminal itself never starts and
# never writes a line to its own log -- so the sidecar waited for an authorisation that
# could never come, and the only terminals that ever ran were the ones initialize() spawned.
TERMINAL_EXE_WIN='C:\MT5\terminal64.exe'
# The SAME executable, spelled the other way, because the two consumers disagree and both
# are documented: the shell launch above takes backslashes, while mt5.initialize(path=...)
# takes FORWARD slashes -- backslashes there are a known cause of an IPC failure. Measured:
# the working connection used 'C:/MT5/terminal64.exe'. Keep these two in step.
TERMINAL_EXE_INIT='C:/MT5/terminal64.exe'
# BACKSLASHES, and this is not the same rule as mt5.initialize(path=...).
#
# initialize() wants FORWARD slashes -- backslashes there are a documented cause of
# (-10005, 'IPC timeout'). The COMMAND LINE is the opposite: terminal64.exe's parser
# truncates /config:C:/MT5/common.ini at the first colon and receives "C:\", then
# reports `Startup: successfully initialized from start config "C:\"`, logs the account
# in, and exits 0 -- so the watchdog relaunches it forever and initialize() only ever
# sees (-10001, 'IPC send failed'). Measured against a live Exness demo.
TERMINAL_INI_WIN='C:\MT5\common.ini'
RPC_SCRIPT_WIN='C:/sp/rpc_server.py'
LAUNCHER_WIN='C:/sp/launch_terminal.cmd'

SHUTTING_DOWN=0
XVFB_PID=""
VNC_PID=""
RPC_PID=""
ALGO_WATCH_PID=""

# --------------------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------------------
log() { printf '%s sidecar %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "FATAL: $*"; exit 1; }

wait_for_path() {
    # wait_for_path <path> <seconds>
    local target="$1" limit="${2:-30}" waited=0
    while [ "$waited" -lt "$limit" ]; do
        if [ -e "$target" ]; then return 0; fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

wait_for_pid_exit() {
    local pid="$1" limit="${2:-15}" waited=0
    while [ "$waited" -lt "$limit" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then return 0; fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

display_number() {
    local value="${DISPLAY#*:}"
    printf '%s' "${value%%.*}"
}

mark()   { mkdir -p "$BOOT_DIR"; : > "${BOOT_DIR}/$1"; }
marked() { [ -f "${BOOT_DIR}/$1" ]; }

fetch() {
    local url="$1" dest="$2"
    log "downloading $(basename "$dest")"
    curl -fSL --retry 3 --retry-delay 5 --connect-timeout 30 -o "$dest" "$url" \
        || die "download failed: ${url}"
}

# Two Wine entry points on purpose:
#   $WINE   plain `wine`, which dispatches to the right loader. The MT5 and Python
#           installers are 32-bit bootstrappers and `wine64` refuses those outright.
#   $WINE64 `wine64` where the build still ships it (Wine 9+ merged it into `wine`), used
#           for terminal64.exe exactly as BUILD_PROMPT §1 spells the launch line.
WINE="$(command -v wine || true)"
[ -n "$WINE" ] || die "wine is not installed in this image"
WINE64="$(command -v wine64 || printf '%s' "$WINE")"

# --------------------------------------------------------------------------------------
# X server and the optional VNC view.
# --------------------------------------------------------------------------------------
# Does an X server actually ANSWER on $DISPLAY? The socket FILE existing proves nothing,
# and that distinction was the single root cause of the compose sidecar's MT5 failure.
#
# /tmp/.X11-unix is the sp-x11 NAMED VOLUME (shared with the compose `debug` profile's vnc
# service), so the X99 socket inode one container's Xvfb creates survives on disk long after
# that Xvfb is gone. The old test here was `[ -S "$socket" ]`, which therefore reported
# "already up" on every container start after the very first one, skipped Xvfb entirely, and
# left DISPLAY=:99 pointing at a dead inode. Wine then died at window creation:
#
#     err:winediag:nodrv_CreateWindow Application tried to create a window, but no driver
#     could be loaded.
#
# terminal64.exe exited before it ever created its IPC endpoint, so every rung of
# mt5.initialize() came back (-10001, 'IPC send failed') -- an error that reads like a broken
# channel but actually means "there is no terminal on the other end at all".
#
# A plain `docker run` does not mount sp-x11: it gets an empty /tmp/.X11-unix from the image
# layer, starts Xvfb normally, and connects on the credentials rung. THAT is the whole
# compose-vs-docker-run contrast, and it is a missing display, not an IPC problem.
x_display_answers() {
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1
}

start_xvfb() {
    local socket waited
    socket="/tmp/.X11-unix/X$(display_number)"

    if x_display_answers; then
        log "X display ${DISPLAY} is already up and answering"
        return 0
    fi

    if [ -e "$socket" ]; then
        # Nothing answers on it. Leaving it would hand the same lie to the next container.
        log "removing a stale X socket at ${socket} — nothing answers on ${DISPLAY}"
        rm -f "$socket"
    fi

    log "starting Xvfb on ${DISPLAY} (${XVFB_GEOMETRY})"
    Xvfb "$DISPLAY" -screen 0 "$XVFB_GEOMETRY" -nolisten tcp &
    XVFB_PID=$!
    wait_for_path "$socket" 30 || die "Xvfb did not come up on ${DISPLAY}"

    # Wait for it to ANSWER, not merely to have created its socket — the same mistake in a
    # smaller form.
    waited=0
    while [ "$waited" -lt 30 ]; do
        if x_display_answers; then
            log "Xvfb is up and answering on ${DISPLAY} (pid ${XVFB_PID})"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    die "Xvfb (pid ${XVFB_PID}) created ${socket} but ${DISPLAY} never answered — \
MT5 cannot create its window without a live display, and a terminal that cannot open a window \
never serves the Python IPC channel"
}

start_vnc() {
    log "starting x11vnc on port ${VNC_PORT} — a broker dialog is now visible over VNC"
    x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport "$VNC_PORT" -quiet >/dev/null 2>&1 &
    VNC_PID=$!
}

# --------------------------------------------------------------------------------------
# One-time bootstrap, one marker per stage so a half-finished first run resumes.
# --------------------------------------------------------------------------------------
bootstrap_wineprefix() {
    if marked v1-wineboot && [ -d "$DRIVE_C" ]; then
        return 0
    fi
    log "first run: initialising the Wine prefix at ${WINEPREFIX} (takes a minute)"
    wineboot --init >/dev/null 2>&1 || die "wineboot --init failed"
    wineserver -w
    mark v1-wineboot
    log "Wine prefix ready"
}

bootstrap_mt5() {
    if marked v1-mt5 && [ -f "${MT5_DIR}/terminal64.exe" ]; then
        return 0
    fi
    fetch "$MT5_INSTALLER_URL" /tmp/mt5setup.exe
    log "installing MetaTrader 5 into C:/MT5 (silent)"
    # The generic MetaQuotes installer frequently does not list Exness servers. The fix is
    # the broker-branded installer URL from the Personal Area in MT5_INSTALLER_URL — same
    # terminal, bundled server list.
    "$WINE" /tmp/mt5setup.exe /auto '/path:C:/MT5' >/dev/null 2>&1 &
    local installer_pid=$!
    if ! wait_for_path "${MT5_DIR}/terminal64.exe" "$MT5_INSTALL_TIMEOUT"; then
        die "the installer never produced ${MT5_DIR}/terminal64.exe within ${MT5_INSTALL_TIMEOUT}s — \
run 'docker compose --profile debug up' and look at the installer over VNC"
    fi
    # Give it a moment to finish writing MQL5/ and Config/, then stop the terminal the
    # installer starts on its own; the real launch happens after the ini is rendered.
    sleep 20
    wait "$installer_pid" 2>/dev/null || true
    stop_terminal
    rm -f /tmp/mt5setup.exe
    mark v1-mt5
    log "MetaTrader 5 installed"
}

bootstrap_python() {
    if marked v1-python && find_wine_python >/dev/null 2>&1; then
        return 0
    fi
    fetch "$PYTHON_INSTALLER_URL" /tmp/python-amd64.exe
    # 64-BIT ON PURPOSE, and this is a trap several public images fell into: the
    # MetaTrader5 wheels are win32 only up to 5.0.37 and win_amd64 only from 5.0.43 onward.
    # A 32-bit Windows Python still "works" — it silently pins the sidecar to a 2022-era
    # build of the package, and you find out much later.
    log "installing 64-bit Windows Python 3.11 inside the prefix"
    "$WINE" /tmp/python-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 >/dev/null 2>&1 || true
    wineserver -w
    find_wine_python >/dev/null 2>&1 \
        || die "the Python installer finished but no python.exe appeared under ${DRIVE_C}"
    rm -f /tmp/python-amd64.exe
    mark v1-python
    log "Windows Python installed at $(find_wine_python)"
}

find_wine_python() {
    local candidate
    for candidate in \
        "${DRIVE_C}/Program Files/Python311/python.exe" \
        "${DRIVE_C}/Python311/python.exe" \
        "${DRIVE_C}/users/${USER:-root}/AppData/Local/Programs/Python/Python311/python.exe" \
        "${DRIVE_C}/users/crossover/AppData/Local/Programs/Python/Python311/python.exe"
    do
        if [ -f "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    candidate="$(find "$DRIVE_C" -maxdepth 6 -name python.exe -type f 2>/dev/null | head -n 1 || true)"
    if [ -n "$candidate" ]; then
        printf '%s' "$candidate"
        return 0
    fi
    return 1
}

bootstrap_mt5py() {
    local python_exe
    python_exe="$(find_wine_python)" || die "no Windows Python in the prefix"
    if marked v1-mt5py && "$WINE" "$python_exe" -c "import MetaTrader5" >/dev/null 2>&1; then
        return 0
    fi
    log "installing the MetaTrader5 package into the Wine Python"
    "$WINE" "$python_exe" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$WINE" "$python_exe" -m pip install --no-cache-dir MetaTrader5 \
        || die "pip install MetaTrader5 failed"
    # Print the version and the bitness: this single line is what makes the win32/win_amd64
    # trap visible in the logs instead of two years later in a missing feature.
    "$WINE" "$python_exe" -c \
        'import struct, MetaTrader5 as m; print("MetaTrader5", m.__version__, str(struct.calcsize("P") * 8) + "-bit python")' \
        || die "the MetaTrader5 package does not import under the Wine Python"
    mark v1-mt5py
}

# --------------------------------------------------------------------------------------
# Configuration rendered at every start, because the operator can change the account.
# --------------------------------------------------------------------------------------
render_common_ini() {
    mkdir -p "$MT5_DIR"
    if [ -z "${MT5_LOGIN:-}" ]; then
        log "warning: MT5_LOGIN is empty — the terminal will start without an account"
    fi
    export MT5_LOGIN="${MT5_LOGIN:-0}"
    export MT5_PASSWORD="${MT5_PASSWORD:-}"
    export MT5_SERVER="${MT5_SERVER:-}"

    local rendered
    rendered="$(mktemp)"
    # envsubst is given an explicit variable list so nothing else in the template is
    # touched and nothing inside the password is ever re-expanded.
    envsubst '${MT5_LOGIN} ${MT5_PASSWORD} ${MT5_SERVER}' < "${APP_DIR}/common.ini.tmpl" > "$rendered"

    # UTF-16LE: MT5 reads a UTF-8 ini unreliably and simply ignores the [Experts]
    # flags — AllowLiveTrading never applies, and every order_send comes back 10027.
    # A UTF-8 write here is a silent production failure, so we verify the bytes
    # (and a round-trip of the Experts keys) before the terminal is launched.
    local dest="${MT5_DIR}/common.ini"
    if [ "$MT5_INI_BOM" = "1" ]; then
        printf '\xff\xfe' > "$dest"
        iconv -f UTF-8 -t UTF-16LE < "$rendered" >> "$dest"
    else
        iconv -f UTF-8 -t UTF-16LE < "$rendered" > "$dest"
    fi
    rm -f "$rendered"
    chmod 600 "$dest"
    assert_common_ini_utf16le "$dest"
    log "rendered C:/MT5/common.ini (UTF-16LE, $(wc -c < "$dest") bytes) for server '${MT5_SERVER}' login ${MT5_LOGIN}"
}

assert_common_ini_utf16le() {
    # assert_common_ini_utf16le <path>
    # Confirms the file is UTF-16LE (BOM or ASCII-in-UTF-16LE) and that the Experts
    # keys survived the conversion. The decoded copy is grepped, never printed —
    # it still carries the account password.
    local dest="$1" size head decoded
    [ -f "$dest" ] || die "common.ini was not written at ${dest}"
    size="$(wc -c < "$dest")"
    if [ "$((size % 2))" -ne 0 ]; then
        die "common.ini is ${size} bytes (odd) — UTF-16LE must be even; [Experts] flags would be ignored"
    fi
    head="$(od -An -tx1 -N 2 "$dest" | tr -d ' \n')"
    case "$head" in
        fffe|3b00|5b00) ;;
        *)
            # UTF-16LE of ASCII has 00 as the second byte. UTF-8 `[Common]` is 5b 43.
            local b2="${head:2:2}"
            if [ "$b2" = "00" ] && [ "${#head}" -eq 4 ]; then
                :
            else
                die "common.ini is not UTF-16LE (first two bytes ${head}) — [Experts] AllowLiveTrading would be ignored and order_send would return 10027"
            fi
            ;;
    esac
    decoded="$(mktemp)"
    if ! iconv -f UTF-16LE -t UTF-8 < "$dest" > "$decoded" 2>/dev/null; then
        rm -f "$decoded"
        die "common.ini is not valid UTF-16LE (iconv refused it)"
    fi
    # grep the keys only — never cat the decoded file, it contains MT5_PASSWORD.
    local missing=""
    grep -q '^AllowLiveTrading=1' "$decoded" || missing="${missing} AllowLiveTrading=1"
    grep -q '^Enabled=1' "$decoded" || missing="${missing} Enabled=1"
    grep -q '^Account=0' "$decoded" || missing="${missing} Account=0"
    grep -q '^Profile=0' "$decoded" || missing="${missing} Profile=0"
    rm -f "$decoded"
    if [ -n "$missing" ]; then
        die "common.ini UTF-16LE round-trip dropped Experts keys:${missing}"
    fi
}

install_sp_files() {
    mkdir -p "$SP_DIR"
    # Copied into the prefix so the Wine Python is handed an unambiguous C: path rather
    # than a unix path it would resolve against whatever drive the CWD maps to.
    cp -f "${APP_DIR}/rpc_server.py" "${SP_DIR}/rpc_server.py"
    cp -f "${APP_DIR}/enable_algo_trading.sh" "${SP_DIR}/enable_algo_trading.sh"
    cp -f "${APP_DIR}/find_algo_button.py" "${SP_DIR}/find_algo_button.py"
    chmod +x "${APP_DIR}/enable_algo_trading.sh" "${SP_DIR}/enable_algo_trading.sh" \
             "${APP_DIR}/find_algo_button.py" "${SP_DIR}/find_algo_button.py" 2>/dev/null || true
    # The watchdog inside rpc_server.py relaunches the terminal through this script.
    # `start ""` detaches the terminal so the cmd.exe it spawns returns immediately.
    # CRLF line endings because it is a Windows batch file.
    printf '@echo off\r\nstart "" "C:\\MT5\\terminal64.exe" /portable "/config:C:\\MT5\\common.ini"\r\n' \
        > "${SP_DIR}/launch_terminal.cmd"
}

# --------------------------------------------------------------------------------------
# Terminal lifecycle.
# --------------------------------------------------------------------------------------
terminal_running() { pgrep -f 'terminal64.exe' >/dev/null 2>&1; }

wait_for_terminal_login() {
    # Poll the terminal's own log for an authorisation newer than this launch.
    #
    # This is the one sequence measured to work end to end: launch the terminal, let it
    # actually log in, and only then let mt5.initialize() attach. Starting the RPC server
    # first and initialising immediately returns (-10001, 'IPC send failed') forever -- the
    # terminal is running but its IPC channel is not servable yet, and every retry that
    # passes `path` starts another terminal on the same data folder.
    local timeout="${1:-180}" started waited=0 logdir hit
    started="$(date -u +%H%M%S)"
    logdir="${MT5_DIR}/logs"
    log "waiting up to ${timeout}s for the terminal to authorise"
    while [ "$waited" -lt "$timeout" ]; do
        hit="$(cat "${logdir}"/*.log 2>/dev/null \
              | iconv -f UTF-16LE -t UTF-8 2>/dev/null \
              | grep -a "authorized on" | tail -1 || true)"
        if [ -n "$hit" ]; then
            local stamp
            stamp="$(printf '%s' "$hit" | grep -aoE '[0-9]{2}:[0-9]{2}:[0-9]{2}' | head -1 | tr -d ':')"
            if [ -n "$stamp" ] && [ "$stamp" -ge "$((10#$started - 5))" ] 2>/dev/null; then
                log "terminal authorised at ${stamp}; giving the IPC channel a moment"
                sleep 10
                return 0
            fi
        fi
        sleep 3
        waited=$((waited + 3))
    done
    log "warning: no fresh authorisation within ${timeout}s — starting the RPC server anyway"
    return 1
}

launch_terminal() {
    # Exactly one terminal, always. A container restart can leave the previous instance
    # alive inside the same /config volume, and MT5 permits one instance per data folder:
    # a second one authorises, then one of the pair exits 0, the survivor is unpredictable,
    # and the Python channel never settles. Measured on the live demo as three racing
    # terminals. Clearing first costs nothing and removes the whole class of problem.
    if pgrep -f 'terminal64.exe' >/dev/null 2>&1; then
        log "a terminal is already running in this prefix — stopping it before launching"
        pkill -f 'terminal64.exe' >/dev/null 2>&1 || true
        sleep 4
        pkill -9 -f 'terminal64.exe' >/dev/null 2>&1 || true
        sleep 1
    fi
    log "launching terminal64.exe /portable /config:${TERMINAL_INI_WIN}"
    "$WINE64" "$TERMINAL_EXE_WIN" /portable "/config:${TERMINAL_INI_WIN}" >/dev/null 2>&1 &
    local waited=0 seen=0
    while [ "$waited" -lt "$MT5_START_TIMEOUT" ]; do
        if terminal_running; then
            # The FIRST hit means nothing: `wine <exe>` leaves a wrapper process matching
            # this pattern for a moment even when the terminal dies on the spot. With a dead
            # X display this branch used to log "terminal64.exe is running" zero seconds
            # after the launch, while the terminal had already exited at window creation —
            # which is how a missing display disguised itself as a healthy start for hours.
            seen=1
            sleep 8
            waited=$((waited + 8))
            if terminal_running; then
                log "terminal64.exe is running"
                return 0
            fi
            log "terminal64.exe appeared and exited immediately — it is not staying up"
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if [ "$seen" = "1" ]; then
        log "warning: terminal64.exe does not stay running — the RPC watchdog will keep trying"
    else
        log "warning: terminal64.exe did not appear within ${MT5_START_TIMEOUT}s — the RPC watchdog will keep trying"
    fi
    return 0
}

stop_terminal() {
    if ! terminal_running; then
        return 0
    fi
    log "asking terminal64.exe to close"
    # taskkill without /F sends WM_CLOSE, so the terminal saves Config/ on the way out.
    "$WINE" taskkill.exe /IM terminal64.exe >/dev/null 2>&1 || true
    local waited=0
    while [ "$waited" -lt 15 ] && terminal_running; do
        sleep 1
        waited=$((waited + 1))
    done
    if terminal_running; then
        log "terminal64.exe did not close in time — killing the wine session"
        wineserver -k >/dev/null 2>&1 || true
        sleep 2
    fi
    return 0
}

start_algo_watch() {
    # Linux-side xdotool helper. rpc_server.py (Windows Python under Wine) cannot
    # exec an ELF binary, so it writes C:/sp/enable_algo.flag when Algo Trading is
    # off; this watcher sends one Ctrl+E or one toolbar click per flag. DISPLAY is
    # the Xvfb we started above.
    if ! command -v xdotool >/dev/null 2>&1; then
        log "warning: xdotool is not in this image — Algo Trading cannot be enabled without VNC"
        return 0
    fi
    log "starting the Algo Trading helper (xdotool Ctrl+E on ${DISPLAY})"
    "${APP_DIR}/enable_algo_trading.sh" --watch "$SP_DIR" &
    ALGO_WATCH_PID=$!
}

on_signal() {
    if [ "$SHUTTING_DOWN" = "1" ]; then
        return 0
    fi
    SHUTTING_DOWN=1
    log "termination signal received — shutting down"
    if [ -n "$RPC_PID" ]; then
        kill -TERM "$RPC_PID" 2>/dev/null || true
        wait_for_pid_exit "$RPC_PID" 15 || true
    fi
    stop_terminal
    if [ -n "$ALGO_WATCH_PID" ]; then kill -TERM "$ALGO_WATCH_PID" 2>/dev/null || true; fi
    if [ -n "$VNC_PID" ]; then kill -TERM "$VNC_PID" 2>/dev/null || true; fi
    if [ -n "$XVFB_PID" ]; then kill -TERM "$XVFB_PID" 2>/dev/null || true; fi
    log "stopped"
    exit 0
}

run_rpc_server() {
    local python_exe status
    python_exe="$(find_wine_python)" || die "no Windows Python in the prefix"
    log "starting the RPC server under the Wine Python on 0.0.0.0:${SIDECAR_PORT:-18812}"
    # Deliberately NOT `exec`: the SIGTERM trap has to survive so `docker stop` closes
    # terminal64.exe cleanly. exec would replace this shell, drop the trap, and leave the
    # terminal to be SIGKILLed with its Config/ half-written — which is exactly the state
    # that makes the next start ask for a server list again.
    MT5_PATH="$TERMINAL_EXE_INIT" \
    MT5_CONFIG="$TERMINAL_INI_WIN" \
    MT5_LAUNCHER="$LAUNCHER_WIN" \
        "$WINE" "$python_exe" -u "$RPC_SCRIPT_WIN" &
    RPC_PID=$!

    set +e
    wait "$RPC_PID"
    status=$?
    set -e

    if [ "$SHUTTING_DOWN" = "1" ]; then
        exit 0
    fi
    log "the RPC server exited with status ${status} — stopping the terminal"
    stop_terminal
    exit "$status"
}

# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
main() {
    trap on_signal TERM INT

    if [ "$SP_ROLE" = "vnc" ]; then
        # The compose `debug` profile runs a second container from this same image that
        # shares the sidecar's /tmp/.X11-unix socket and only serves the screen.
        log "role=vnc — attaching x11vnc to ${DISPLAY} on port ${VNC_PORT}"
        # Same rule as start_xvfb: the shared sp-x11 volume can hold a socket inode that no
        # server is behind, so wait for an ANSWER rather than for the file to exist.
        vnc_waited=0
        while [ "$vnc_waited" -lt 300 ] && ! x_display_answers; do
            sleep 2
            vnc_waited=$((vnc_waited + 2))
        done
        x_display_answers \
            || die "no X display answering at ${DISPLAY}; is the sidecar container up and is /tmp/.X11-unix shared?"
        exec x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport "$VNC_PORT"
    fi

    [ -n "${SIDECAR_TOKEN:-}" ] \
        || die "SIDECAR_TOKEN is empty — refusing to start. An unauthenticated /rpc endpoint is order_send on an open port."
    # The shipped placeholder is refused for the same reason an empty one is. A default that
    # everyone can read from .env.example is not a shared secret, and this channel is the one
    # that places orders. Emptiness was checked; being *known* was not.
    case "${SIDECAR_TOKEN}" in
        change-me|changeme|CHANGE-ME|change_me|your-token|secret)
            die "SIDECAR_TOKEN is still the placeholder from .env.example — refusing to start.
Generate one and put it in .env:  openssl rand -base64 32" ;;
    esac

    mkdir -p /config "$BOOT_DIR"
    start_xvfb
    if [ "$ENABLE_VNC" = "1" ]; then start_vnc; fi

    bootstrap_wineprefix
    bootstrap_mt5
    bootstrap_python
    bootstrap_mt5py

    render_common_ini
    install_sp_files
    start_algo_watch
    # The terminal IS launched here, and start_xvfb() above has already proved the display
    # answers -- without that proof this launch produces a terminal that exits at window
    # creation while pgrep still shows a wine wrapper, which is indistinguishable from
    # success in the log and turns into (-10001, 'IPC send failed') minutes later.
    #
    # launch_terminal() clears any terminal already in this prefix first: MT5 permits one
    # instance per data folder, and a second one authorises, then one of the pair exits 0
    # with the survivor unpredictable. One owner, one terminal.
    launch_terminal
    # A flat settle, not a log watch. The terminal writes one log per DAY, so scanning it for
    # "authorized on" matched the PREVIOUS run's line and returned instantly -- which is worse
    # than not waiting at all, because it looks like it worked. The measured recipe simply
    # gives the terminal time before the first initialize(); rpc_server.py's ladder handles
    # the rest and retries anyway if this is short.
    log "letting the terminal settle for ${MT5_SETTLE_SECONDS:-55}s before the RPC server starts"
    sleep "${MT5_SETTLE_SECONDS:-55}"
    run_rpc_server
}

main "$@"
