# LVCT instructions for automation and AI agents

Use `lvct --help` and the subcommand's `--help` from the installed version first.
This guide applies to managed CLI installations. It does not adopt a manually
configured owner deployment or its Docker volumes. Never run a second live runtime
against an existing broker account/Telegram session as a diagnostic.

## Read-only investigation

1. `lvct --version`; choose the correct `--home PATH` if there are several installations.
2. `lvct doctor --json` checks configuration, engine and readiness. It starts nothing.
3. `lvct status --json` shows container lifecycle; `lvct health --json` checks API and
   component readiness. MT5 being healthy is not proof of an order's outcome.
4. `lvct errors --since 1h --json` groups ERROR/CRITICAL messages with likely causes.
5. `lvct logs --service app --since 30m --level error --tail 300` gives context.
   Use `--level critical` for the most severe level, or `--contains TEXT` for a literal
   case-insensitive filter. The sidecar service name is `mt5-sidecar`.
6. Fix the identified cause; do not retry ambiguous broker operations, delete volumes,
   regenerate encryption keys, or toggle trading as a troubleshooting shortcut.

Diagnostics redact configured environment secrets. Arbitrary application messages
can still contain sensitive data: review logs/reports before sharing them. Never
print runtime.env, raw Compose configuration, Docker inspect environment, database
settings, broker sessions or SMTP credentials.

## Command lifetimes and exit codes

| Command | Typical budget / completion |
| --- | --- |
| help, version, config | Usually under a second |
| doctor / health / status / errors / bounded logs | Usually seconds; Docker/SSH calls have 20–60 second timeouts; multiple checks can total several minutes |
| `update --check` | Network check; allow 1–2 minutes |
| `report` | Local collection; allow 2–3 minutes with a slow Docker/SSH endpoint |
| `report --send --file FILE` | Challenge plus HTTPS upload; usually seconds, allow a minute |
| init / menu / password | Interactive until completed; automation needs the documented stdin modes |
| start --build / update --apply / deps --install | Network-dependent; first Wine build can exceed 40 minutes. Keep the process/session and read output; do not kill it at a short generic timeout |
| `logs -f`, `health --watch`, remote `open` | Intentionally stay in foreground until Ctrl+C |

Exit code 0 means the command completed its check/action. `health` returns 1 for
unhealthy **or degraded** state; `doctor` returns 1 for a failed check (warnings alone
are not failure). `errors` returns 0 after a successful read even when errors exist; unavailable runtime
logs return 1 while still showing locally recorded CLI failures.
General failures return 1, argument parsing returns 2, Ctrl+C returns 130. A process
exit is not proof of broker execution. JSON status is schema 1; parse fields, not colours.
`health --watch --json` emits one JSON object per line; its interval is 5–300 seconds.

## Changes and updates

`start` is idempotent for a running stack and never recreates running services.
`stop`, `restart` and installing dependencies require confirmation or an explicit
`--yes` from an already authorized user action. Do not add `--yes` to avoid obtaining
required authorization. Trading/position management pauses while stopped even if
broker-side SL/TP remains active.

`update` defaults to read-only `--check`. `update --apply` refuses running or paused
services, preserves the installation identity, credentials and volumes, builds new
images and creates **stopped** containers. The user must arrange the maintenance stop;
there is no automatic stop/start. Optional `--ref TAG_OR_SHA` selects the official
public revision; `--source PATH` uses an explicitly reviewed offline product tree.

On interruption, do not delete maintenance.json. Run `lvct update --rollback` while
services are stopped. The journal retains previous source, CLI and tagged images;
rollback does not restore/migrate application database contents. Back up volumes
before starting a version that may migrate the database. Then use doctor and an
explicit `start`. Do not prune rollback image tags or release directories during
maintenance. The per-installation operation lock prevents concurrent CLI mutations.

## Support reports

`lvct report --note "Describe the issue"` saves private JSON and prints its path. It
works before setup too. It contains only version/platform, readiness, error categories
and bounded recent ERROR/CRITICAL text, not the environment/database or broker files.
`--contact EMAIL` explicitly includes an optional reply address.

Read the saved JSON. Upload only when the user asks to send a support report:
`lvct report --send --file PATH` prompts; `--yes` confirms an already authorized upload.
The destination is the official HTTPS support receiver. A successful response means
accepted/queued, not that email and Telegram already arrived. There are no automatic
background uploads. On a timeout keep the same report ID/file; do not generate a flood
of new reports. IP/rate restrictions or a 429 mean wait, not bypass the limit.

CLI failures are retained in the protected local `logs/cli.jsonl` (last 200 events,
bounded size). Critical unexpected exceptions store only their type, not secret-bearing
traceback locals. `errors` includes recent CLI events. Runtime Docker logs remain the
source for detailed application tracebacks.
