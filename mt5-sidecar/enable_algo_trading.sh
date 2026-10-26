#!/usr/bin/env bash
#
# LV Copytrader — drive the MetaTrader Algo Trading / AutoTrading toolbar on DISPLAY=:99.
#
# Ctrl+E is the terminal's toggle. The Python RPC server only asks for a keypress when
# terminal_info().trade_allowed is already false, so a single keypress should turn it on.
# A second press would turn it off again — this script must therefore send Ctrl+E exactly
# once per flag.
#
# On Exness Wine builds windowactivate fails (_NET_ACTIVE_WINDOW unsupported), so Ctrl+E
# often never reaches the terminal. The fallback is a physical click on the red
# "Algo Trading" toolbar control, using root coordinates (no activate).
#
# Two modes:
#   enable_algo_trading.sh           one shot Ctrl+E (used by hand / tests)
#   enable_algo_trading.sh --watch   sit on C:/sp/enable_algo.flag (Wine path
#                                    /config/.wine/drive_c/sp) and fire once per flag.
#                                    Flag body is "ctrl+e" (default) or "click".
#
# Nothing here ever prints MT5_PASSWORD or SIDECAR_TOKEN, window titles, or account numbers.

set -u

DISPLAY="${DISPLAY:-:99}"
export DISPLAY

log() { printf '%s sidecar %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
FINDER="${SCRIPT_DIR}/find_algo_button.py"

find_mt5_window() {
    local id pattern
    for pattern in "MetaTrader" "Exness" "terminal64" "MetaQuotes"; do
        id="$(xdotool search --onlyvisible --name "$pattern" 2>/dev/null | tail -n 1 || true)"
        if [ -n "${id}" ]; then
            printf '%s' "${id}"
            return 0
        fi
    done
    for pattern in "terminal64.exe" "wine"; do
        id="$(xdotool search --onlyvisible --class "$pattern" 2>/dev/null | tail -n 1 || true)"
        if [ -n "${id}" ]; then
            printf '%s' "${id}"
            return 0
        fi
    done
    return 1
}

require_display() {
    if ! command -v xdotool >/dev/null 2>&1; then
        log "algo-enable: xdotool is not installed"
        return 1
    fi
    if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        log "algo-enable: DISPLAY ${DISPLAY} is not answering"
        return 1
    fi
    return 0
}

send_ctrl_e() {
    if ! require_display; then
        return 1
    fi

    local wid=""
    wid="$(find_mt5_window || true)"
    if [ -n "${wid}" ]; then
        # Do NOT windowactivate: Wine does not implement _NET_ACTIVE_WINDOW, the
        # call fails, and a key sent to the "focused" window misses the terminal.
        # --window delivers the key to the X window even when it is not active.
        xdotool windowraise "${wid}" 2>/dev/null || true
        sleep 0.05
        if xdotool key --window "${wid}" --clearmodifiers ctrl+e 2>/dev/null; then
            log "algo-enable: sent Ctrl+E to window ${wid} on ${DISPLAY}"
            return 0
        fi
        log "algo-enable: --window Ctrl+E failed on ${wid}; not sending a focused-window key (would miss under Wine)"
        return 1
    fi
    log "algo-enable: no MetaTrader window found; cannot send Ctrl+E"
    return 1
}

# Click the red Algo Trading toolbar button in root coordinates.
# Wine ignores windowactivate; a click at the visible button still lands.
click_algo_button() {
    if ! require_display; then
        return 1
    fi

    local wid=""
    wid="$(find_mt5_window || true)"
    if [ -z "${wid}" ]; then
        log "algo-enable: no MetaTrader window found; cannot click Algo Trading"
        return 1
    fi

    xdotool windowraise "${wid}" 2>/dev/null || true
    sleep 0.05

    local X=0 Y=0 WIDTH=1280 HEIGHT=800
    eval "$(xdotool getwindowgeometry --shell "${wid}" 2>/dev/null || true)"
    if [ "${WIDTH:-0}" -lt 200 ] 2>/dev/null; then
        WIDTH=1280
    fi
    if [ "${HEIGHT:-0}" -lt 100 ] 2>/dev/null; then
        HEIGHT=800
    fi

    local root_x="" root_y=""
    local strip_h=72
    local strip_y=$((Y + 28))
    local png="/tmp/sp-algo-toolbar.png"
    rm -f "$png"

    if command -v scrot >/dev/null 2>&1; then
        # Crop the toolbar band of the MT5 window (title bar skipped). -o overwrites.
        if scrot -o -a "${X},${strip_y},${WIDTH},${strip_h}" "$png" 2>/dev/null; then
            if [ -f "$FINDER" ] && command -v python3 >/dev/null 2>&1; then
                local coords=""
                coords="$(python3 "$FINDER" --png "$png" --origin-x "$X" --origin-y "$strip_y" 2>/dev/null || true)"
                if [ -n "$coords" ]; then
                    root_x="${coords%% *}"
                    root_y="${coords##* }"
                    log "algo-enable: red Algo Trading control at root ${root_x},${root_y}"
                fi
            fi
        fi
    fi
    rm -f "$png"

    if [ -z "$root_x" ] || [ -z "$root_y" ]; then
        # Known offset: Exness "Algo Trading" sits on the first toolbar, left-of-centre.
        root_x=$((X + WIDTH * 32 / 100))
        root_y=$((Y + 58))
        log "algo-enable: no red cluster in screenshot; clicking known toolbar offset root ${root_x},${root_y}"
    fi

    # Root coords, no --window / no activate: the pointer click hits whatever is visible.
    if xdotool mousemove --sync "${root_x}" "${root_y}" click --clearmodifiers 1 2>/dev/null; then
        log "algo-enable: clicked Algo Trading at root ${root_x},${root_y} on ${DISPLAY}"
        return 0
    fi
    log "algo-enable: xdotool click failed at root ${root_x},${root_y}"
    return 1
}

watch_flag() {
    local dir="${1:-/config/.wine/drive_c/sp}"
    mkdir -p "$dir"
    : > "${dir}/enable_algo.ready"
    log "algo-enable: watching ${dir}/enable_algo.flag"
    while true; do
        if [ -f "${dir}/enable_algo.flag" ]; then
            local action="ctrl+e"
            action="$(tr -d '\r\n' < "${dir}/enable_algo.flag" 2>/dev/null || true)"
            rm -f "${dir}/enable_algo.flag"
            case "${action}" in
                click)
                    click_algo_button || true
                    ;;
                *)
                    send_ctrl_e || true
                    ;;
            esac
            date -u '+%Y-%m-%dT%H:%M:%SZ' > "${dir}/enable_algo.ack"
        fi
        sleep 0.25
    done
}

case "${1:-}" in
    --watch)
        watch_flag "${2:-}"
        ;;
    --click)
        click_algo_button
        ;;
    *)
        send_ctrl_e
        ;;
esac
