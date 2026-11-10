# Bringing LV Copytrader up, and the traps that waste a day

Every failure below was hit for real against a live Exness demo. They share one property that
makes them expensive: **the error names the wrong thing.** `IPC send failed` is not an IPC bug.
A `403` is not a bad API key. `terminal64.exe is running` can be a lie. Read the symptom table
before you start changing code, because the obvious fix is almost always for the wrong problem.

---

## 1. The startup sequence

```bash
git clone <repo> lv-copytrader && cd lv-copytrader
cp .env.example .env      # then edit it — see §2
docker compose up -d --build
```

First build takes **40+ minutes**: the sidecar installs WineHQ (420+ packages, including i386
multiarch). That is normal and happens once. Later builds reuse the layer.

First *boot* then does the rest inside the Wine prefix — downloads MetaTrader 5, installs a
64-bit Windows Python, `pip install MetaTrader5`. Several more minutes, also once: it is cached
in the `sp-mt5-config` volume, so it survives `docker compose down`.

**The app answers immediately, before any of that finishes.** That is deliberate — the broker
bring-up runs in the background, because the dashboard is the only place you can watch the
bring-up or fix the credentials blocking it. A red MT5 dot during the first ten minutes is
expected, not a fault.

### Verify each stage instead of assuming

Do not judge by "no error appeared". Check the thing itself:

```bash
# 1. Both containers exist and the app is healthy
docker compose ps

# 2. The dashboard answers (401 without credentials once SP_AUTH_* is set — that is correct)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/health

# 3. The sidecar got an X server, launched the terminal, and CONNECTED
docker compose logs mt5-sidecar | grep -E "Xvfb is up|terminal64.exe is running|connected on the|MT5 initialized"

# 4. The broker is really reachable — this is the only proof that matters
curl -s -u "$SP_AUTH_USER:$SP_AUTH_PASSWORD" localhost:8000/api/trading/account
```

Step 4 returning a real balance is the finish line. Everything before it can look fine while
the channel is dead.

---

## 2. What you must put in `.env`

| Variable | Why it bites |
|---|---|
| `SIDECAR_TOKEN` | Leaving the shipped `change-me` is refused at boot. It guards the endpoint that places orders; a default everyone can read is not a secret. `openssl rand -base64 32` |
| `MT5_SERVER` / `MT5_LOGIN` / `MT5_PASSWORD` | The MT5 dot goes green from these alone, with no interactive step |
| `MT5_INSTALLER_URL` | **Exness (and most brokers): set this to the broker-branded installer from your Personal Area.** The generic MetaQuotes installer frequently ships without your broker in its server list, and the login then fails looking exactly like wrong credentials |
| `SP_AUTH_USER` / `SP_AUTH_PASSWORD` | Missing credentials lock private APIs. Set both; only isolated local development may explicitly set SP_ALLOW_UNAUTHENTICATED=true |
| `SECRET_KEY` | Leave empty — it is generated on first start and written back. It decrypts every stored secret, so once real data exists, **do not lose it** |

---

## 3. Symptom → what it actually means

### `mt5.initialize()` returns `(-10001, 'IPC send failed')`

**Not an IPC problem.** It means there is nothing on the other end: `terminal64.exe` died before
it created its endpoint. Under Wine the usual cause is **no X server**.

```bash
docker compose exec mt5-sidecar sh -c 'DISPLAY=:99 WINEPREFIX=/config/.wine wine "C:\\MT5\\terminal64.exe" /portable'
# err:winediag:nodrv_CreateWindow Application tried to create a window, but no driver could be loaded.
```

Why it recurs: `/tmp/.X11-unix` is the **named volume** `sp-x11` (shared with the VNC profile).
A named volume outlives the container, and so does a unix-socket inode. Any check shaped like
`[ -S /tmp/.X11-unix/X99 ]` sees a corpse and reports "X is already up", so Xvfb never starts.
**Probe whether the display answers (`xdpyinfo`), never whether the file exists.** This is
handled in `entrypoint.sh`; if you rewrite that file, keep it.

Distinguish it from `-10005 'IPC timeout'`, which means the opposite: the terminal IS there and
the call is waiting. `-10005` on the first attempts is normal and usually resolves.

### `terminal64.exe is running` but nothing works

`pgrep -f terminal64.exe` matches the `wine`/`start.exe` wrapper, which lingers for a moment
even when the terminal dies instantly. It will report success zero seconds after a launch that
already failed. Re-check a few seconds later, or read the terminal's own log:
`/config/.wine/drive_c/MT5/logs/YYYYMMDD.log` — **UTF-16LE**, so `iconv -f UTF-16LE -t UTF-8`.

### Terminal starts, logs `authorized`, then `exit with code 0`, forever

The `/config:` argument was truncated. On the **command line** MT5 needs **backslashes**:

```
terminal64.exe /portable "/config:C:\MT5\common.ini"     ✅
terminal64.exe /portable "/config:C:/MT5/common.ini"     ❌ parsed as C:\ — applies a bogus
                                                            "start config", logs in, exits 0
```

**The opposite rule applies to `mt5.initialize(path=...)`, which needs FORWARD slashes.** Same
file, two consumers, two conventions. Keep the two constants separate and labelled.

### `wine: '/config/.wine' is not owned by you`

The prefix files carry a different uid than the container user. If you seeded the volume from a
host copy, `cp -a` preserved your uid while the container runs as root. `chown -R 0:0` the volume.

### `initialize()` fails on every attempt with the same arguments

Calling one variant repeatedly does not connect. Walking several in **one process** does —
reproduced 5/5 against a live demo:

```
bare              -> (-10005, 'IPC timeout')
portable=True     -> (-10005, 'IPC timeout')
portable=False    -> (-10005, 'IPC timeout')
+ credentials     -> (1, 'Success')
```

`rpc_server.py` implements this ladder. **Do not "simplify" it to the last rung** — on its own it
does not connect. Give the terminal ~55 s to settle before the first attempt; starting the RPC
server earlier just burns the ladder against a terminal that is not servable yet.

### `terminal_info().trade_allowed` is False / `order_send` returns 10027

On Exness (and other broker-branded) builds the AutoTrading **toolbar** is the gate, not
just `common.ini`. `AllowLiveTrading=1` in `[Experts]` is required and must be written as
**UTF-16LE** (a UTF-8 `common.ini` is silently ignored). After that the toolbar toggle still
has to be on: `order_send` comes back `TRADE_RETCODE_CLIENT_DISABLES_AT` (10027) with
"AutoTrading disabled by client" until it is.

The sidecar turns it on itself after `initialize()`: **one** `Ctrl+E` via the Linux
xdotool helper, then it polls `trade_allowed` for ~10s. **Helper ack is not success.**
On Exness Wine, xdotool Ctrl+E and a toolbar click can both leave AutoTrading off while
`user32.keybd_event Ctrl+E` turns it on. If the real `order_send` probe is still 10027,
the sidecar sends **one** user32 Ctrl+E (even when `enable_algo.ready` exists), then one
click on the red **Algo Trading** control. **Do not trust `order_check`**: it can return
10013 while live `order_send` is 10027. Effectiveness is `trade_allowed == true`, or a
guarded far pending `order_send` that places (10008/10009) and is cancelled. A second
Ctrl+E or a second click would turn it off, so nothing else may toggle until either (a)
`trade_allowed` was seen True and then False — a real disable — or (b) the 120s cooldown
elapsed **and** a real `order_send` probe is still 10027. The watchdog observes; it does
not spam-toggle. If `order_send` returns 10027, it polls / maybe one enable, then retries
the same request once (10027 means the first request was not accepted, so that retry
cannot duplicate an order).

```bash
# Did the helper start, and did the toolbar come on?
docker compose logs mt5-sidecar | grep -E "algo-enable|Algo Trading|trade_allowed|UTF-16LE|10027"

# common.ini encoding (must be UTF-16LE; never cat it — it holds MT5_PASSWORD)
docker compose exec mt5-sidecar bash -lc \
  'od -An -tx1 -N 2 /config/.wine/drive_c/MT5/common.ini'
# expect: ff fe   (BOM)  or  3b 00 / 5b 00  (ASCII ';' / '[' in UTF-16LE)
```

If it is still off after boot, `docker compose --profile debug up` and press `Ctrl+E` (or
click **Algo Trading**) in VNC. Then `terminal_info().trade_allowed` should be true and a
pending `order_send` should return `10009` / `10008`, not `10027`.

`tradeapi_disabled` (Tools → Options → Expert Advisors → "Disable automatic trading through
the external Python API") is a different switch. Ctrl+E cannot clear it; the UTF-16LE ini
and the Options dialog can.

### The AI says "no symbol matches 'gold'" for a symbol that exists

Your broker's suffix is not in `BROKER_SUFFIXES` (`symbols.py`). Exness Raw-Spread accounts quote
`XAUUSDr`, `EURUSDr` — with only `m` in the list, resolution fails silently and totally. Check
what your account actually quotes:

```bash
curl -s -u "$SP_AUTH_USER:$SP_AUTH_PASSWORD" 'localhost:8000/api/trading/symbols?query=XAU'
```

### Stops are ten times too tight (or too loose)

Pip conventions. **Gold is quoted to 3 digits at some brokers and 2 at others**, so a rule like
`pip = point * 10` gives 0.01 on one feed and 0.10 on the other — the same instrument, a pip
differing by a factor of ten, and every stop wrong by the same factor. The pip is anchored to the
*instrument*, never to a multiple of `point`. `resolve_symbol` reports `pip_convention` naming
which rule fired; read it. `PIP_OVERRIDES` in `config.py` corrects any symbol without code changes.

### The AI provider returns 403 and you assume the key is wrong

Check whether the body is HTML. A gateway behind Cloudflare answers a non-browser `User-Agent`
with a block page, and the literal `python-httpx/x.y` is a known bot signature:

```
User-Agent: python-httpx/0.28.1  -> 403 "Attention Required"
User-Agent: lv-copytrader/1.1.0    -> 200
```

Both providers already send an honest non-library UA. If a gateway still refuses, put a browser
`User-Agent` in the provider's **extra headers** field — that is what it exists for. Do not
hardcode a browser UA globally; every other endpoint then gets lied to.

**Test with the client the app actually uses.** A `curl` matrix here is misleading: curl has its
own TLS fingerprint and is blocked independently, which makes it look like only a full Chrome UA
works when `lv-copytrader/1.1.0` is fine through httpx.

### Live ingest goes quiet while a second client uses the same StringSession

The app holds one Telethon `StringSession`. A second client — a script, a notebook, another
process — logged in with that same session string takes the update channel. Telegram delivers
live updates to one connection, so the running app stops seeing new posts until it is
restarted. The next start runs catch-up and replays at most `CATCHUP_LIMIT`
(default 20) messages newer than the stored `last_message_id`, oldest first.
History at or before that id is not fetched. If more than the limit arrived
while the app was down, only that oldest slice is replayed and the watermark
advances to the end of the slice, so the rest of the gap is not replayed.

Post test messages from a different Telegram account, or from the channel's own admin client.
Do not open a second Telethon session on the string stored in Settings while the app is
running. Posting through the app's already-connected client would be the same connection and
would not cause this; a helper that built another client from the saved string would. There
is no in-app "post as the channel" sender for that reason.

### `Telegram is not connected — finish the login` right after a restart

The session is stored and survives restarts; the connect happens in the background, and that
window is normal. The current build says "reconnecting" instead. If you genuinely need to
re-login, `has_session` in `GET /api/settings` tells you whether a session exists at all.

### Notifications go nowhere

Three separate things, and the UI shows all three: the bot token, a bound `chat_id`, and the
`enabled` switch. A saved token alone sends nothing. **Telegram forbids a bot messaging first** —
you must message the bot, then run "Test & detect chat". Set `admin_ids` to your numeric
Telegram id first, or the bind takes whoever messages the bot first, and your trade
notifications — symbol, volume, entry, stop, P/L — go to them.

---

## 4. Things that look like bugs and are not

- **Two rows for one Telegram message** with different `edit_date`. The channel edited its post;
  each edit is a revision by design (§3.3). The unique index on
  `(source_id, message_id, edit_date)` is what stops a genuine replay.
- **Revision restarting at 1** for a `skipped` message. An edit of a skipped row is
  re-classified from scratch — a voice note replaced by text becomes a real signal.
- **A run marked `manual`.** Something hit `/api/trading/manual/*`, not the model. The badge is
  deliberately loud: telling AI decisions from human ones is the whole point of the feed.
- **`GET /api/health` returning 401.** Authentication is on. That is the desired state.
- **A red MT5 dot for the first minutes.** The bring-up is backgrounded on purpose so the port
  opens immediately.

---

## 5. When verifying, watch your tools

Two verification failures cost real time here, and both would have produced a confident wrong
answer:

- **`grep -r` may skip dotfiles** if a shell function shadows it (Claude Code installs one).
  Scanning a tree for secrets that way reports "clean" while `.env` sits in it. Use
  `/usr/bin/grep` explicitly for anything security-shaped.
- **`pgrep -f <pattern>` matches your own command line**, so a check for "is X running" can find
  itself. Match on the process name, or exclude the checker.

---

## 6. Shutting down

```bash
docker compose down          # containers go, volumes stay
```

Volumes `sp-data` (database), `sp-mt5-config` (the ~2.6 GB Wine prefix) and `sp-x11` persist, so
the next start skips the long install. **Disarm before stopping**, or the next start resumes
trading on stale signals before anyone has looked at it. Stopping the app does **not** close open
positions — they live at the broker under their own stops. Use the kill switch first if you want
them flat.

---

## 7. Signal workers and the channel playbook

`INGEST_WORKERS` defaults to **3** (ceiling 3). One queue, three consumers. If one worker is
inside triage or a tool loop, the next queued post is taken by a free worker immediately.
What still serialises, on purpose:

- a teaser and the ladder reply that quotes it share one chain lock, so the reply sees the
  tickets the teaser opened;
- broker writes (open, modify, close, cancel) stay one at a time inside the executor.

The channel reviewer is a **separate** task named `channel_reviewer`. It does not take a
signal slot and runs only when `reviewer.enabled` is on. It writes the active source's
playbook to `/data/channel-briefs/active/brief.md`, capped at 60 lines and 12,000 characters.
Triage and the trading agent receive it as untrusted context, only when its source marker
matches their Telegram chat. If no matching playbook exists they proceed without one.

Every 15 minutes the reviewer checks the active source. If its recent sample changed, it
replaces the playbook; it does not append older versions. The newest posts are retained in
its bounded sample. A failed refresh retains the previous matching playbook and retries
later. The sample comes from the database and, on refresh or when the sample is thin, the last
3–4 days of Telegram history. Fresh Telegram edits take precedence on refresh. Study reads do not ingest signals or move `last_message_id`.

Changing the active source archives the previous playbook and notes separately so it can be
reviewed or restored. Those archives are never injected into active AI runs. Initial studies
retry after the same delay if the model or Telegram is unavailable. Three signal workers
process different posts concurrently. For the XAUUAD source, each entry is also classified
in three independent, tool-free model sessions; two matching votes are required for an
automatic fast entry or a skip. Verified follow-up protection does not wait for AI votes.


### Opt-in gold source strategy

The private `trading.uz_gold_policy` setting remains disabled unless the owner
explicitly configures it. The source must match the private research-only marker;
all direct AI writes for that source remain blocked. The deterministic path
requires a live quote, current unedited entry, two hedging legs, owner-set total
volume, a 15-unit gold-price stop on both orders, three independent AI checks and
a confirmed broker stop. Targets may be added by editing the same signal, and a
reply requesting break-even touches only tracked positions of that signal.

The 15-unit stop is relative to the requested quote: fills, slippage, commissions
and gaps can make realised losses different. Missed zones, old/edited entries,
re-entry requests and ambiguous/unthreaded management instructions are not
automatically turned into new orders. An edited direction disarms new entries;
the system never automatically closes or reverses those existing positions.

### Research-only sources

A private `ingest.research_only_chat_id` runtime setting marks a source as
research-only. Its posts are recorded and its channel playbook can be studied,
but the signal loop defers them and the executor refuses AI trade writes even
if trading is armed. The stop-loss watchdog and manual controls remain separate.
Selecting a source does not authorize or configure its order size, SL, targets,
reentries, or handling of edited messages. Configure and test a complete source
profile before removing this guard. Never hard-code private chat identifiers in
source or public exports.

### Codex provider (optional)

The `codex` Compose profile runs a local, capability-token-protected app-server in the
application's network namespace. Prepare its executable and separate ChatGPT device-login
session in the `lvCT-data` volume before enabling the profile, then select Codex and
`gpt-6.1-terra` in Settings. Its reasoning effort is medium. The port is not published;
keep the session and access-token files private. A ChatGPT session may need a fresh device
login after expiration or revocation. The private runtime snapshot does not include this
separate session directory, so do not assume a cloned repository restores the login.

The source-specific XAUUAD profile uses two 0.20-lot entries and applies a later verified SL,
TP2 and TP4 to the same tickets; opening without SL exposes the position until protection
arrives. A missing or contradictory follow-up is rejected rather than guessed.

### MT5 isolation and security maintenance

The standard Compose configuration puts the terminal on a separate `trading` bridge;
only the application also joins that bridge. Do not attach public/tunnel services there.
Pass only MT5/RPC settings to the terminal, never the entire backend environment file.
Keep RPC unpublished and use a random token. Restrict privileges and rotate container logs.

For an existing deployment using fixed container IPs on one bridge, install an equivalent
host bridge firewall rule allowing the RPC destination/port only from the backend IP.
`DOCKER-USER` alone is insufficient when bridge netfilter is unavailable. Persist rules
with a dedicated service and verify both allowed and denied container namespaces after
recreation. Update the rule when changing network addresses.

Before replacing a terminal image, pause new entries and confirm no running execution
pipeline or open positions/pending orders. Keep a rollback image and a private terminal
volume backup. Preserve the Wine/FEX version and compatibility mounts. Verify MT5 and
Telegram readiness before restoring the previous armed state. Never submit test orders.
Audit the Wine image separately from the backend; vendor-unfixed packages require tracking.
See [the dated audit](docs/SECURITY-AUDIT.md) for scope and remaining limitations.


## Managed CLI and waitlist operations

New users should start with [the CLI guide](docs/CLI.md); agents should read
[CLI command contracts](docs/CLI-AGENTS.md). Manual Compose examples above retain
the legacy port; the CLI chooses a free port starting at 19471.

Waitlist maintenance requires local Docker owner access. Run these from the manually
configured Compose checkout (for a managed installation, use its explicit Docker
context, project, environment and source paths from `lvct config`):

```sh
docker compose exec -T app python -m app.cli.waitlist count
# Export contains personal data; keep it private and remove unneeded copies.
umask 077
docker compose exec -T app python -m app.cli.waitlist export > /private/path/waitlist.csv
docker compose exec -T app python -m app.cli.waitlist delete --email person@example.com
```

Keep the original encryption key with the database. Regenerating it makes stored
waitlist addresses and broker/provider/Telegram secrets unreadable. CSV export
escapes spreadsheet formulas. This is data administration, not a mail subscription
sender. Public support reports are a separate service: see [Support](docs/SUPPORT.md).
