# LV Copytrader — mt5-sidecar.
#
# Debian + winehq-stable + Xvfb. Inside the Wine prefix (which lives on the /config volume,
# not in the image): the MetaTrader 5 terminal, 64-bit Windows Python 3.11 and the
# MetaTrader5 package. entrypoint.sh installs those on first run, because the terminal and
# its Config/servers.dat + Config/accounts.dat must survive a rebuild — putting them in an
# image layer would mean re-doing the broker's first-login dance on every image change.
#
# Wine Mono is deliberately NOT installed: WINEDLLOVERRIDES=mscoree= suppresses the prompt,
# and nothing in this container is .NET.
FROM debian:trixie-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

# --------------------------------------------------------------------------------------
# WineHQ repository, then Wine itself. i386 is enabled because the winehq-stable package
# still pulls a 32-bit half, and because both the MT5 and the Python installers are 32-bit
# bootstrappers even though everything they install is 64-bit.
# --------------------------------------------------------------------------------------
RUN set -euxo pipefail; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://dl.winehq.org/wine-builds/winehq.key -o /etc/apt/keyrings/winehq-archive.key; \
    . /etc/os-release; \
    codename="${VERSION_CODENAME:-bookworm}"; \
    if ! curl -fsI "https://dl.winehq.org/wine-builds/debian/dists/${codename}/Release" >/dev/null 2>&1; then \
        echo "WineHQ has no dists/${codename}; falling back to bookworm"; \
        codename=bookworm; \
    fi; \
    echo "deb [signed-by=/etc/apt/keyrings/winehq-archive.key] https://dl.winehq.org/wine-builds/debian/ ${codename} main" \
        > /etc/apt/sources.list.d/winehq.list; \
    dpkg --add-architecture i386; \
    apt-get update; \
    apt-get install -y --install-recommends winehq-stable; \
    rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------------------------
# Everything else the entrypoint uses:
#   xvfb         Wine's null graphics driver cannot create a window; MT5 needs a real X
#                server or it dies with "no driver could be loaded".
#   x11-utils    xdpyinfo, the only honest test of "is there an X server on :99". The socket
#                file is NOT that test: /tmp/.X11-unix is a shared named volume, so its X99
#                inode outlives the Xvfb that made it, and entrypoint.sh trusting the file
#                is what left the terminal with no display and MT5 with (-10001, 'IPC send
#                failed'). See start_xvfb() in entrypoint.sh.
#   x11vnc       the compose `debug` profile's window onto that display, for the broker
#                dialog that will eventually appear.
#   cabextract   Wine's own dependency for unpacking Windows installers.
#   winbind      ntlm_auth; without it Wine logs authentication errors on every start.
#   libc-bin     iconv, which turns the rendered common.ini into UTF-16LE.
#   gettext-base envsubst, which renders it from the template.
#   procps       pgrep, used to wait for terminal64.exe.
#   curl         the MT5 and Python installer downloads, and the healthcheck below.
#   xdotool      sends Ctrl+E to the terminal on DISPLAY=:99 so Algo Trading can be
#                turned on without a VNC click. Exness builds return 10027 when the
#                toolbar toggle is off even if common.ini AllowLiveTrading=1 is set.
#   scrot        captures the toolbar band so we can click the red Algo Trading
#                control by root coordinates (Wine windowactivate is unsupported).
#   Pillow       pinned in an isolated venv; finds the red control in the screenshot.
# --------------------------------------------------------------------------------------
RUN set -euxo pipefail; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        xvfb \
        x11-utils \
        x11vnc \
        xdotool \
        scrot \
        python3 \
        python3-venv \
        cabextract \
        winbind \
        libc-bin \
        gettext-base \
        procps \
        curl \
        ca-certificates; \
    python3 -m venv /opt/pixel-tools; \
    /opt/pixel-tools/bin/pip install --no-cache-dir Pillow==13.0.0; \
    /opt/pixel-tools/bin/pip uninstall -y pip; \
    apt-get upgrade -y; \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/pixel-tools/bin:${PATH}" \
    WINEARCH=win64 \
    WINEPREFIX=/config/.wine \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES="mscoree=" \
    DISPLAY=:99 \
    SP_APP_DIR=/opt/signalpilot \
    SIDECAR_HOST=0.0.0.0 \
    SIDECAR_PORT=18812 \
    WATCHDOG_SECONDS=15 \
    WATCHDOG_MAX_RESTARTS=5

# The Wine prefix, the installed terminal and its Config/servers.dat + Config/accounts.dat.
# A fresh prefix has no server list, so the very first login may need one pass through VNC;
# this volume is why that happens once, forever.
VOLUME ["/config"]

# Documentation only. docker-compose.yml must NOT publish this port to the host: the RPC
# endpoint is order_send, and it is reachable on the compose network alone.
EXPOSE 18812

COPY common.ini.tmpl rpc_server.py enable_algo_trading.sh find_algo_button.py /opt/signalpilot/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/signalpilot/enable_algo_trading.sh /opt/signalpilot/find_algo_button.py

# The first run downloads and installs the terminal, Python and the MetaTrader5 wheel, which
# takes minutes — hence the long start period. The check reads only the watchdog's cached
# flags (GET /health needs no token and never touches the IPC channel), but the server is
# single-threaded on purpose, so a long order_send can delay a reply; retries cover that.
HEALTHCHECK --interval=30s --timeout=10s --start-period=900s --retries=5 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:18812/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
