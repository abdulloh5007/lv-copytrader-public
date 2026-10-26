# ARM64 deployment: FEX + Wine 11

The backend runs natively on ARM64. Windows MT5, its Python SDK, and wineserver
run through FEX. The existing x86-64 sidecar image supplies Wine and Linux
libraries; QEMU/binfmt runs Linux helpers. No remote x86 executor is needed.

Verified on Ubuntu 24.04, 4 KiB pages, Ampere/Neoverse N1, 2 CPUs / 12 GiB,
Docker Compose 2.40.3. FEX ARMv8.2 package `2610-1~n` and Wine 11.0 are pinned
in `runtime.json`. Do not silently replace these with newer runtime versions.

## Why this configuration exists

Box64 could report `connected=true` and pass `order_check`, while every real
`order_send` failed with broker retcode 10031. Changing CPU flags and moving
wineserver to Box64 did not resolve it. QEMU with Wine 11.20 did not establish
a reliable Python/terminal IPC connection. FEX with the existing Wine 11.0
successfully opened and closed a minimum-volume DEMO order on the ARM host.

The final verification repeated the open/close from the application's network
namespace **after restarting the same production MT5 container**, with the
temporary x86 executor stopped and its SSH tunnel disabled. Both broker
responses were 10009, and history confirmed the round trip. No diagnostic
position or pending order remained. Existing credentials, data and risk
settings were retained; old rejected signals were not replayed.

## Prepare an ARM host

1. Install Docker Engine, Compose, `qemu-user-static`, `binfmt-support`,
   `software-properties-common`, and Python 3 on Ubuntu 24.04 ARM64. Enable
   Docker and binfmt-support on boot. Check that both x86-64 and i386 handlers
   are enabled with `update-binfmts --display`.
2. Supply the tested **amd64 Wine 11.0 sidecar image** as
   `signalpilot/mt5-sidecar:local`. An archived image or an image built on an
   x86-64 build machine can be loaded with `docker image load`. Do not commit
   Docker images or Wine profiles. On the tested QEMU version, an amd64 apt
   build on ARM failed signature verification; signatures must remain enabled.
   Building an image elsewhere is a build step, not a runtime dependency.
3. Keep credentials in the untracked `.env`. On a migration, preserve the
   working database, matching `SECRET_KEY`, Telegram session and MT5 volume.
   When using the tunnel profile, trust only its fixed proxy address with
   `SP_TRUSTED_PROXY_IPS=127.0.0.1,172.19.0.2`.
   Take a consistent backup and stop the old ingest/trading application before
   starting another instance. Never start an old database against the account.
4. Prepare the pinned runtime from this repository:

   ```sh
   sudo python3 deploy/arm64/prepare_runtime.py --install
   sudo python3 deploy/arm64/prepare_runtime.py --check
   ```

The preparer downloads from the official signed FEX PPA and verifies package
and binary SHA256 values. It extracts FEX into `/opt/copytrader-compat/fex`,
copies the wrappers and RPC source, and extracts the original x86-64 wineserver
from the image. It refuses different existing files. It never reads `.env`,
starts MT5, arms the application or sends an order. Its Wine version check
runs only `wine --version` in a temporary network-disabled container.
It does not install a new global binfmt handler or fetch another root filesystem:
the sidecar's own filesystem is the FEX guest root.

`--check` verifies prepared files without changing them. For a different
installation directory, use `--root /absolute/path` and set `ARM_COMPAT_DIR`
to the same path for Compose. The extracted FEX binaries use the host's ARM64
libc/libstdc++; those libraries and their loader are mounted read-only.

## Build and start

The base Compose declares existing external volumes: `lvct_lvCT-data`,
`lvct_lvCT-mt5-config`, `lvct_lvCT-x11`. Verify these names and their contents
before starting; create empty volumes only for a genuinely new installation.
An empty Wine profile may need the broker's initial server selection.

```sh
docker tag signalpilot/mt5-sidecar:local signalpilot/mt5-sidecar:arm-fex-wine11.0-20261023
docker compose -f docker-compose.yml -f compose.arm64.yml build app
docker compose -f docker-compose.yml -f compose.arm64.yml --profile tunnel up -d --no-build
```

The ARM overlay retains the deployed app image name for compatibility. Its
portable build uses the repository's normal multi-stage `backend/Dockerfile`,
which builds the frontend and installs the locked backend dependencies natively.
The live migration originally reused an already built frontend; neither those
private migration artifacts nor host paths are prerequisites for this build.

For an already running host with the prepared images, use `up -d --no-build`.
Do not rebuild or replace production as part of a read-only check. If the ARM
host already links `docker-compose.override.yml` to `compose.arm64.yml`, normal
Compose commands load the overlay automatically. Keep that link local so an
x86 clone does not accidentally load the ARM configuration.

## Verify and maintain

```sh
docker compose -f docker-compose.yml -f compose.arm64.yml ps
docker compose -f docker-compose.yml -f compose.arm64.yml logs --tail 40 mt5-sidecar
docker stats --no-stream
```

Allow roughly two minutes for Wine, broker login, and automatic Algo Trading
enablement. Check the authenticated `/api/health`: MT5 and Telegram should be
healthy. Check the configured account identity and trading mode before any
write. A green connection flag alone is insufficient evidence of execution.
The deployment was verified on DEMO; no automated live-account test is supplied.

App, MT5 and tunnel use `restart: unless-stopped`. MT5 has a 4 GiB memory limit
and a 1.8 CPU limit. The entry guard clears a stale X99 socket/lock only when
no X server answers and no Xvfb process is alive. RPC still requires its token,
is reachable only in the Docker network, and has no published host port.

The ARM overlay enables `SP_INIT_CREDENTIALS_FIRST=1`. It prioritizes the
credentials rung during initial attach, retaining the default ordering on
other deployments. An explicit account change still requires credentials and
cannot fall back to a previously connected account. This does not retry
orders or bypass any trade/risk checks.

Pause trading in the panel before maintenance. After restarting MT5, reconcile
positions and orders before rearming. Never blindly repeat an opening request
after an ambiguous response; inspect broker positions and history first.

Back up `.env`, both data volumes, Compose files and `/opt/copytrader-compat`.
Keep those backups private. Full VM reboot and a weeks-long soak were not part
of the verification; the same-container restart and broker execution were.
