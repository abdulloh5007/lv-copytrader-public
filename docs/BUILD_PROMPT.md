# Build prompt: LV Copytrader — an AI trader that reads a Telegram signal channel and drives MetaTrader 5

> Paste everything below the divider into your coding model as a single message.

---

## ROLE

You are a senior backend engineer who has shipped production trading automation: broker API integration, order lifecycle management, and LLM agent loops with real side effects. You know that a trading bot fails in the gaps — a filling mode the broker rejects, a symbol name with a suffix, a duplicate message replayed on reconnect — not in the happy path. You write complete, runnable code with real error handling, not a sketch.

## TASK

Build **LV Copytrader**: a self-hosted system where an LLM acts as the trader. It watches a Telegram signal channel through a user account, reads whatever the channel posts — free-form prose, screenshots of charts, replies to earlier calls — decides what to do, and executes on MetaTrader 5 through tools you give it. A web dashboard shows what it is thinking, what it did, and lets the operator configure everything.

The owner's framing, keep it: *we are building a plane, and the pilot is the AI. Our job is to build the cockpit — the instruments it reads and the controls it pulls. The AI flies.*

Three properties define success:

1. **The channel is not templated.** Signals arrive as `buy gold now`, `tp1 hit move sl to be`, `+110 pips 🚀`, or a bare screenshot with an entry line drawn on it. There is no regex that parses this. The model reads it — text and image together — and decides.
2. **Late is acceptable, wrong is not.** The AI will often see a signal minutes after it was posted and price has already moved. It must be able to enter late *knowingly*, with the current distance from the stated entry in front of it as a number, and skip when that distance has eaten the trade.
3. **Do not invent a stop, and do not remove one.** A channel post with no stop opens without one (incomplete entry, fixed ladder volume). An existing stop cannot be cleared. Orphans stay with the stopless watchdog.

## ASSUMPTIONS

Load-bearing choices the owner made or approved. Do not silently change them.

- **Backend** is Linux-native Python 3.12 + FastAPI. MetaTrader 5 runs in a **separate Wine container** reachable over the compose network at `http://mt5-sidecar:18812`. Trading logic never runs inside Wine.
- **Frontend** is a Vite + React + TypeScript SPA, built to static files and served by FastAPI itself. One container, one port, one process for the app.
- **Autonomy is full.** No approval step and no risk-based veto ships enabled. Position-size limits, daily loss caps and max-open-positions all exist as settings that ship **disabled**. Unconditional gates: the `armed` switch, a posted stop on the correct side when one is sent, the refusal to remove an existing stop (§6), and the duplicate-signal guard (§5.5). A missing stop does not refuse the open. Nothing else may block an order by default.
- **Take profit is optional.** A signal without a TP opens a position with `tp = 0.0` and the AI manages it later.
- **One signal source** is active at a time, chosen in the UI. The database schema still carries `source_id` on every row from day one, so adding sources later is a UI change, not a migration of the whole model.
- **Dark + light theme, RU + EN locale**, both resolved before first paint (§10).
- **Persistence is SQLite** via SQLAlchemy async, schema created with `Base.metadata.create_all` on startup. No Alembic, no migration tooling. Exactly one uvicorn worker, no `--reload`, in every environment (§3.6).

---

## BRIEF — facts supplied by the owner, use verbatim

Seed MT5 account (an Exness **demo**; the operator changes it in the UI later). These values live in `.env` only — `.env` is git-ignored and `.env.example` carries placeholders:

```
MT5_SERVER=Exness-MT5Trial15
MT5_LOGIN=474436674
MT5_PASSWORD=<redacted — the real value belongs in .env, which is git-ignored>
```

Broker credentials, the Telegram session string, provider API keys and the notification bot token are encrypted at rest in the database with Fernet (`cryptography`), keyed by `SECRET_KEY` from `.env`, and are **never** written to logs or returned by any API endpoint — settings endpoints return `"••••••••"` with a `has_value: true` flag. If `SECRET_KEY` is missing at startup, generate one, write it to `.env`, and log that fact. If it is present but fails to decrypt existing rows, do not crash: mark those settings `needs_reentry`, surface a banner in the UI naming which ones, and keep running.

Message types the AI must process: **plain text, photo, photo with caption, an image sent as an uncompressed document, and replies to any of those**. Message types it must skip without invoking the model: voice, video, video note, audio, sticker, animation/GIF, non-image document, poll, dice, contact, location, and service messages. Skipped messages are still recorded and shown in the dashboard feed marked `skipped` with the detected kind, so the operator can see nothing was lost.

## OUTPUT FORMAT

Deliver a complete project. One concern per file. No 900-line modules. Every package directory gets an `__init__.py`.

```
lv-copytrader/
  README.md
  docker-compose.yml           # app + mt5-sidecar; vnc under the `debug` profile
  .env.example
  .gitignore
  backend/
    Dockerfile                 # CMD: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
    pyproject.toml
    app/
      main.py                  # FastAPI app, lifespan wiring only
      config.py                # EVERY tunable constant, one place (parameter table below)
      db.py                    # async engine, session factory, create_all on startup
      models.py                # SQLAlchemy ORM models
      schemas.py               # Pydantic request/response models
      crypto.py                # Fernet encrypt/decrypt, key bootstrap, needs_reentry handling
      errors.py                # TradingError, ProviderError, IngestError + retcode->message map
      events.py                # in-process event bus -> WebSocket fan-out
      api/
        settings.py            # provider, telegram, mt5, notifier, optional-limit CRUD
        telegram.py            # login flow, dialog list, source selection
        provider.py            # test connection, list models, probe capabilities
        trading.py             # read-only views, manual override, arm/disarm, kill switch
        history.py             # closed trades, filters, per-source aggregates
        stream.py              # GET /api/stream (WebSocket)
      telegram/
        client.py              # Telethon lifecycle bound to uvicorn's loop
        classify.py            # message kind detection
        ingest.py              # dedup, persist, enqueue, catch-up
        links.py               # t.me permalink construction
        notifier.py            # Bot API sender (httpx, NOT Telethon)
      ai/
        base.py                # Provider protocol + normalized types
        anthropic_provider.py
        openai_provider.py
        catalog.py             # model listing + capability probe + probe cache
        tools.py               # tool schemas — single source of truth
        prompts.py             # system prompt, context builder
        loop.py                # the agent loop
      trading/
        constants.py           # every MT5 integer literal the backend needs (§1.2)
        rpc.py                 # JSON-RPC client for the sidecar; dict -> SimpleNamespace
        symbols.py             # symbol resolution, suffix matching, pip convention, caching
        pricing.py             # distance conversion, volume sizing, stop validation
        executor.py            # tool implementations that touch the account
        guards.py              # the SL invariant + optional operator limits
        reconcile.py           # startup recovery, position <-> signal linkage
      util/
        images.py              # downscale, re-encode, size cap, data-URL
        logging.py             # structured logs with secret redaction
  mt5-sidecar/
    Dockerfile
    entrypoint.sh              # Xvfb, terminal launch, watchdog
    common.ini.tmpl            # placeholders only, rendered from env at boot
    rpc_server.py              # runs under WINDOWS Python inside Wine, stdlib only
  frontend/
    package.json
    vite.config.ts
    tsconfig.json
    index.html                 # contains the pre-paint theme/locale bootstrap
    src/
      main.tsx
      App.tsx                  # router + layout shell
      api/client.ts            # typed fetch wrapper, error normalization
      api/stream.ts            # WebSocket with reconnect + backoff
      i18n/{index.ts,ru.ts,en.ts}
      styles/{tokens.css,base.css,components.css}
      components/
        ArmSwitch.tsx  StatusDots.tsx  AccountPanel.tsx  SignalFeed.tsx
        SignalCard.tsx  RunStream.tsx  ThinkingBlock.tsx  ToolCallCard.tsx
        PositionsTable.tsx  OrdersTable.tsx  HistoryTable.tsx
        SecretField.tsx  ModelPicker.tsx  DialogPicker.tsx  ConfirmDialog.tsx
      pages/{Cockpit,Positions,History,Settings}.tsx
```

**Dependencies.** Backend: `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `aiosqlite`, `pydantic-settings`, `telethon`, `httpx`, `cryptography`, `pillow`, `anthropic>=1.0`. The sidecar uses **only the Python standard library plus `MetaTrader5`** — nothing else installs cleanly under Wine and you do not need it. Frontend: `react`, `react-dom`, `react-router-dom`, `vite`, `typescript`. Fonts load from `https://fonts.googleapis.com`; no other CDN.

**README.md** at the root: what LV Copytrader is, the tree with one line per file, how to run it (`docker compose up --build`), the first-run checklist (MT5 login → Telegram login → provider setup → pick channel → watch disarmed → arm), the parameter table, the honest limitations from §12, and the current state. Update it whenever structure or behaviour changes.

**Standing bans:** no `TODO`, no `pass  # implement later`, no "simplified for brevity", no code elided with an ellipsis. Every file in that tree and every function you name, you write in full.

---

## 1. The MT5 sidecar and order semantics — start here, this decides whether anything works

The `MetaTrader5` Python package is Windows-only and there is no Linux wheel. It talks to a running `terminal64.exe` over a **single IPC channel that is not thread-safe**. So:

**Container layout.** `mt5-sidecar` runs Debian + `winehq-stable`, `WINEARCH=win64`, `WINEPREFIX=/config/.wine`, `WINEDEBUG=-all`, `WINEDLLOVERRIDES=mscoree=` (this suppresses the Wine Mono install prompt outright — do not install Mono, you do not need .NET). Inside the prefix: **64-bit** Windows Python 3.11 (`python-3.11.9-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0`) and `pip install MetaTrader5`.

The bitness matters and is a trap several public images fell into: `MetaTrader5` shipped `win32` wheels only up to `5.0.37`; from `5.0.43` onward it is `win_amd64` only. A 32-bit Windows Python silently pins you to a 2022-era build.

**Display.** Wine's null graphics driver makes window creation fail with `Application tried to create a window, but no driver could be loaded`. MT5 needs an X server. Run `Xvfb :99 -screen 0 1280x1024x24` and export `DISPLAY=:99`. Add `x11vnc -display :99 -forever -rfbport 5901 -nopw` as a second compose service under `profiles: ["debug"]` publishing `5901:5901`, so `docker compose --profile debug up` lets the operator look at the terminal when a broker dialog appears. It will happen.

**Terminal install and launch.** Install silently with `wine mt5setup.exe /auto /path:"C:/MT5"`. The installer URL is `MT5_INSTALLER_URL` in `.env`, defaulting to `https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe`. Note in the README that the generic MetaQuotes broker directory frequently does not list Exness servers, and that the fix is to take the Exness-branded installer URL from the Exness Personal Area and set `MT5_INSTALLER_URL` to it — it is the same terminal with a bundled server list.

MetaQuotes documents exactly four command-line keys for `terminal64.exe`: `/login:`, `/config:`, `/profile:`, `/portable`. **There is no `/password:` and no `/server:` switch** — those live in the ini's `[Common]` section. `common.ini.tmpl` holds placeholders only; `entrypoint.sh` substitutes env values and converts the result to **UTF-16LE** (`iconv -f UTF-8 -t UTF-16LE`), because MT5 reads a UTF-8 ini unreliably.

```ini
[Common]
Login=${MT5_LOGIN}
Password=${MT5_PASSWORD}
Server=${MT5_SERVER}
KeepPrivate=1
NewsEnable=0
[Experts]
AllowLiveTrading=1
Enabled=1
Account=0
Profile=0
```

`Account=0` and `Profile=0` are load-bearing: they mean "do **not** disable algo trading when the account or profile changes". At their defaults, switching accounts silently kills automated trading and every `order_send` comes back `10027`.

Launch: `wine64 'C:/MT5/terminal64.exe' /portable /config:'C:/MT5/common.ini'`, wait for the process, then `mt5.initialize(path="C:/MT5/terminal64.exe", login=..., password=..., server=..., portable=True, timeout=60000)`.

Use **forward slashes** in `path=`. Backslashes in this argument are a known cause of `initialize()` returning `False` with `last_error() == (-10005, 'IPC timeout')`.

Immediately after a successful `initialize()`, assert the terminal will actually trade:

```python
ti = mt5.terminal_info()
if not ti.trade_allowed or ti.tradeapi_disabled:
    raise RuntimeError("Algo trading disabled in the terminal")
```
`tradeapi_disabled` is the *"Disable automatic trading through the external Python API"* option. When it is on, orders come back `TRADE_RETCODE_CLIENT_DISABLES_AT` (10027) and nothing in your code is wrong.

Mount `/config` as a named volume so `Config/servers.dat` and `Config/accounts.dat` survive restarts. A fresh Wine prefix has no server list, so the very first login may need one pass through VNC; the volume means that happens once, forever.

**`rpc_server.py`** — stdlib `http.server.ThreadingHTTPServer` is wrong here; use `http.server.HTTPServer`, which is single-threaded. That single thread *is* your serialization of the MT5 IPC channel and it costs you nothing to get right.

```
POST /rpc   header X-SP-Token: <SIDECAR_TOKEN>
            {"method": "<name>", "params": {...}}
  200 -> {"ok": true,  "result": <json>}
  200 -> {"ok": false, "error": {"code": <int>, "message": "<str>", "retcode": <int|null>}}
  401 -> missing or wrong token
```

`SIDECAR_TOKEN` is a shared secret from `.env`, checked on every request with `hmac.compare_digest`. The sidecar port is **not** published to the host in `docker-compose.yml` — it is reachable only on the compose network. An unauthenticated `order_send` on an open port is the one hole that would make everything else in this spec pointless.

Whitelist methods explicitly — never `getattr(mt5, params["method"])`. Namedtuples convert with `._asdict()`; numpy arrays from `copy_rates_*` convert with `.astype(object).tolist()`. Exposed methods: `account_info`, `terminal_info`, `symbols_get`, `symbol_info`, `symbol_select`, `symbol_info_tick`, `copy_rates_from_pos`, `positions_get`, `orders_get`, `history_deals_get`, `order_check`, `order_send`, `order_calc_margin`, `ping`.

A **watchdog** thread checks `pgrep -f terminal64.exe` every `WATCHDOG_SECONDS = 15`; if the terminal is gone it relaunches and re-runs `initialize()`, at most `WATCHDOG_MAX_RESTARTS = 5` times per 10 minutes, after which it reports permanently unhealthy rather than thrashing. MT5 self-updates and occasionally dies; the sidecar must come back on its own, but a crash loop must be visible.

### 1.1 The backend sees objects, not dicts

`rpc.py` recursively converts every JSON object in a result into `types.SimpleNamespace` and every array of objects into a tuple of them. That is why every sketch in this document reads `si.digits` and `p.ticket` rather than `si["digits"]`. Attribute form is the contract on the Linux side; only `rpc_server.py` touches the real `MetaTrader5` objects. A missing attribute raises `AttributeError`, which is what you want — a silent `None` from `.get()` in the money path is worse.

### 1.2 `trading/constants.py`

The backend cannot `import MetaTrader5`; it is Windows-only and correctly absent from the backend's dependencies. Every integer the backend needs lives in one module, written as literals with the MQL5 name as the identifier:

```python
TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP = 1, 5, 6
TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE                   = 7, 8
ORDER_TYPE_BUY, ORDER_TYPE_SELL                            = 0, 1
ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT                = 2, 3
ORDER_TYPE_BUY_STOP,  ORDER_TYPE_SELL_STOP                 = 4, 5
ORDER_FILLING_FOK, ORDER_FILLING_IOC = 0, 1
ORDER_FILLING_RETURN, ORDER_FILLING_BOC = 2, 3
SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC, SYMBOL_FILLING_BOC = 1, 2, 4   # bitmask, different scheme
ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
MARGIN_MODE_NETTING, MARGIN_MODE_EXCHANGE, MARGIN_MODE_HEDGING = 0, 1, 2
TIMEFRAMES = {"M1":1,"M5":5,"M15":15,"M30":30,"H1":16385,"H4":16388,"D1":16408}
RETCODE = {10004:"REQUOTE", 10008:"PLACED", 10009:"DONE", 10010:"DONE_PARTIAL",
           10013:"INVALID", 10014:"INVALID_VOLUME", 10015:"INVALID_PRICE",
           10016:"INVALID_STOPS", 10017:"TRADE_DISABLED", 10018:"MARKET_CLOSED",
           10019:"NO_MONEY", 10020:"PRICE_CHANGED", 10021:"PRICE_OFF",
           10027:"CLIENT_DISABLES_AT", 10030:"INVALID_FILL",
           10038:"INVALID_CLOSE_VOLUME", 10045:"FIFO_CLOSE"}
```
Timeframes are bit-packed — `H1` really is `16385`, not `60`. Never compute them.

### 1.3 Six order-semantics traps

**Success is not always 10009.** A market order that filled returns `TRADE_RETCODE_DONE` = **10009**. A *pending* order that was accepted returns `TRADE_RETCODE_PLACED` = **10008**. Code that checks `retcode == 10009` marks every limit order the AI places as a failure, then retries it, and now there are two. Treat `{10008, 10009}` as success and `10010` as partial success that must be reported with the filled volume.

**`order_send` can return `None`.** On a transport or validation failure before the server is reached it returns `None`, not a result object. Null-check before touching `.retcode` or you get an `AttributeError` in the one code path that matters.

**Closing a position needs `position`.** There is no close action. Send `TRADE_ACTION_DEAL` with the *opposite* type, the closing side's price, and the position ticket:

```python
{"action": TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": vol,
 "type": ORDER_TYPE_SELL if p.type == POSITION_TYPE_BUY else ORDER_TYPE_BUY,
 "position": p.ticket,                       # omit this and you OPEN an opposite position
 "price": tick.bid if p.type == POSITION_TYPE_BUY else tick.ask,
 "deviation": DEVIATION_POINTS, "magic": MAGIC_NUMBER,
 "type_time": ORDER_TIME_GTC, "type_filling": pick_filling(si, TRADE_ACTION_DEAL, partial=True)}
```
Partial close is the same request with a smaller `volume`; over-sending gives `10038`. `DEVIATION_POINTS = 20` is the slippage tolerance on every market order and lives in `config.py`; on `10004` (requote) or `10020` (price changed), re-read the tick and retry at most `REQUOTE_RETRIES = 2` times before returning the failure to the model with the price movement stated.

**Filling mode: two different numbering schemes.** `symbol_info().filling_mode` is a *bitmask* (`FOK=1`, `IOC=2`, `BOC=4`). `request["type_filling"]` takes an *enum* (`FOK=0`, `IOC=1`, `RETURN=2`, `BOC=3`). Passing one into the other is the single most common bug in MT5 Python code, and it fails as `10030` with the broker comment `Unsupported filling mode`. The `SYMBOL_FILLING_*` names **do not exist** in the Python package — `mt5.SYMBOL_FILLING_IOC` raises `AttributeError` — which is why they are literals in `constants.py`.

```python
def pick_filling(si, action, partial=False):
    if action == TRADE_ACTION_PENDING:
        return ORDER_FILLING_RETURN            # pending orders are RETURN; FOK/IOC gives 10030
    fm = si.filling_mode
    if partial and (fm & SYMBOL_FILLING_IOC): return ORDER_FILLING_IOC   # FOK forbids partial fills
    if fm & SYMBOL_FILLING_FOK:  return ORDER_FILLING_FOK
    if fm & SYMBOL_FILLING_IOC:  return ORDER_FILLING_IOC
    if fm & SYMBOL_FILLING_BOC:  return ORDER_FILLING_BOC
    if si.trade_exemode != 2:    return ORDER_FILLING_RETURN   # banned under Market execution
    raise TradingError(f"no valid filling mode for {si.name} (filling_mode={fm}, exemode={si.trade_exemode})")
```
`ORDER_FILLING_RETURN` is never a bit in `filling_mode`, so `filling_mode == 0` does not mean "nothing is allowed". Preferring FOK on a *partial* close is wrong — FOK means all-or-nothing.

**`order_check` succeeds at retcode `0`, not `10009`.** Run it before every `order_send` — it surfaces `10030`, `10016`, `10014` and `10019` without touching the market, and it returns the projected `margin_free` so you can report a real reason to the model. Its success retcode being `0` catches people out.

**Netting vs hedging changes what a second order means.** Read `account_info().margin_mode` at startup: `MARGIN_MODE_HEDGING == 2` allows several independent positions on one symbol; `NETTING == 0` and `EXCHANGE == 1` merge them, so a second buy adds to the position and a sell reduces it. Multi-TP splitting (§5.3) is only legal on hedging. `FORCE_MARGIN_MODE` in config (`"hedging"` / `"netting"` / `None`, default `None`) overrides the detected value so both branches can be tested against one demo account.

## 2. Symbols, distances, and volume — the arithmetic the model must never do

The model reads `buy gold, sl 20 pips, tp1 +30`. It does not know the broker's symbol is `XAUUSDm`, does not know what a "pip" means on that symbol, and cannot reliably multiply floats. All of it happens in `symbols.py` / `pricing.py`.

**Symbol resolution.** Cache `symbols_get()` at startup and refresh every `SYMBOL_CACHE_SECONDS = 3600`. Score every symbol against the query across four passes and return the union, ranked by pass order then by name length: exact case-insensitive name match → exact match after stripping a broker suffix (`m`, `c`, `.raw`, `_i`, `#`, `.pro`, `-ECN`, `.stp`) → alias table (`GOLD`/`XAU` → `XAUUSD`, `SILVER` → `XAGUSD`, `US30`/`DOW`/`DJ30`, `NAS100`/`USTEC`/`NDX`, `GER40`/`DAX`, `BTC` → `BTCUSD`, `OIL`/`WTI` → `USOIL`/`XTIUSD`) → substring match on `symbol_info().description`. Return the top `MAX_SYMBOL_CANDIDATES = 8`, never one silent guess; the tool hands the model the ranked list and it picks. Zero candidates returns `{"ok": false, "message": "no symbol matches 'X'", "hint": "<3 closest names by edit distance>"}` — the model can then try a different word.

Always `symbol_select(name, True)` before quoting. An unselected symbol returns a tick of zeros rather than an error; after selecting, if `tick.bid == 0 or tick.ask == 0`, retry once after 500 ms and then return `{"ok": false, "message": "no quotes for X — market closed or symbol unavailable"}`.

**Pip convention is explicit and auditable, never inferred by the model.**

```python
def pip_size(si):
    if si.name.upper() in PIP_OVERRIDES:  return PIP_OVERRIDES[si.name.upper()]
    if si.digits in (3, 5):               return si.point * 10   # FX 5-digit / JPY 3-digit
    if _is_metal(si):                     return si.point * 10   # XAU 2-digit: 1 pip = 0.10
    if si.digits in (2, 4):               return si.point
    raise TradingError(f"no pip convention for {si.name} (digits={si.digits})")
```
"Pip" is trader slang with no broker-side definition, and getting it wrong scales every stop by ten. So `resolve_symbol` returns `pip_size`, `point`, `digits` **and** a `pip_convention` string naming which branch fired, and `PIP_OVERRIDES` in config lets the operator correct any symbol without touching code. Every distance the model is shown is reported in **all three** units — `points`, `pips`, and price — so the model never has to pick.

**The model supplies absolute prices, and a helper converts relative ones.** When a signal says "sl 20 pips below entry", the model calls `price_from_distance(symbol, anchor, direction, distance, unit)` with `unit ∈ {"pips","points","price","percent"}`; the tool returns the resulting price, rounded to `si.digits`, plus the convention it used. The model reads chart levels and quotes distances; the server does every multiplication. That is the whole division of labour.

**Stop distance is a broker constraint, not an opinion.** `si.trade_stops_level` is the minimum distance in **points** between the current price and any SL or TP; `si.trade_freeze_level` is the zone in which an existing order cannot be modified at all. Validate before sending and return a specific error naming the required distance and the actual one — this is what `10016` means, and a bare "invalid stops" tells the model nothing it can act on. **Round stops away from the market, then re-validate**: rounding a buy's stop up moves it toward price and can breach `trade_stops_level` after the check already passed.

**Volume from risk, computed server-side.**
```python
tick_value = si.trade_tick_value_loss or si.trade_tick_value      # the losing side is what a stop costs
for name, v in (("trade_tick_size", si.trade_tick_size), ("tick_value", tick_value),
                ("volume_step", si.volume_step), ("sl_distance", abs(entry - sl))):
    if not v:
        return tool_error(f"cannot size the trade: {name} is {v} for {si.name}")

sl_distance  = abs(entry - sl)
loss_per_lot = (sl_distance / si.trade_tick_size) * tick_value    # account currency
raw          = (balance * risk_percent / 100.0) / loss_per_lot
volume       = round(math.floor(raw / si.volume_step + 1e-9) * si.volume_step, 8)

if volume < si.volume_min:
    return tool_error(f"{risk_percent}% risk gives {volume} lots, below the {si.volume_min} minimum; "
                      f"the minimum lot would risk {si.volume_min * loss_per_lot:.2f} "
                      f"{account.currency} ({si.volume_min * loss_per_lot / balance * 100:.2f}%)")
if volume > si.volume_max:
    volume = si.volume_max                                        # report the reduction in the result
```
Round **down**, never up. Clamping *up* to `volume_min` behind the model's back silently multiplies the intended risk, so it is an error the model must resolve, not a correction. Clamping *down* to `volume_max` is safe but must be stated in the tool result. Then confirm with `order_calc_margin` that `required_margin <= MARGIN_HEADROOM * margin_free` with `MARGIN_HEADROOM = 0.8`, and report the shortfall in account currency if it fails.

`risk_percent` has no upper bound — the owner chose full autonomy deliberately, and a cap here would be a risk veto. `DEFAULT_RISK_PERCENT = 1.0` applies when the model supplies neither `volume` nor `risk_percent`, and the value used is always echoed in the tool result.

**History times are broker-server time**, usually UTC+2/+3, and a naive `datetime` is treated as UTC by the package. Pad `date_to` a day into the future when calling `history_deals_get` or you will silently miss the deals you just made. "Today's realized P/L" means since midnight **broker-server time**, derived from the offset between `symbol_info_tick().time` and `time.time()`.

## 3. Telegram ingest

### 3.1 Login, headless
`TelegramClient(StringSession(saved or None), api_id, api_hash)` — never `client.start()`, which prompts on stdin. Flow: `await client.connect()` → `await client.is_user_authorized()` → `sent = await client.send_code_request(phone)` → persist `sent.phone_code_hash` **in the database**, because Telethon's hash cache is per-process and the code arrives in a second HTTP request → `await client.sign_in(phone=phone, code=code, phone_code_hash=hash)` → catch `SessionPasswordNeededError` and call `await client.sign_in(password=twofa)`. Persist `client.session.save()` (encrypted) on success.

All exceptions import from `telethon.errors`: `SessionPasswordNeededError`, `PhoneCodeInvalidError`, `PhoneCodeExpiredError`, `PhoneNumberInvalidError`, `PasswordHashInvalidError`, `FloodWaitError` (has `.seconds`). Each maps to a distinct message the UI shows next to the field. A 400 with `"error"` is not good enough for a login screen.

### 3.2 Message kind — `message.photo` lies
`message.photo` is also truthy for a link preview containing an image and for a `MessageActionChatEditPhoto` service message. `message.video` is truthy for GIFs and video notes, since all three carry `DocumentAttributeVideo`. And signal channels routinely "send as file" to preserve chart quality, which arrives as a *document* with an image mime type and must be processed, not skipped. Branch on the media type first:

```python
KINDS = {'text','photo','photo_caption','voice','video','video_note','audio',
         'sticker','gif','document','service','empty','unsupported'}

def classify(m):
    if isinstance(m, types.MessageService):               return 'service'
    if m.media is None:                                   return 'text' if m.message else 'empty'
    if isinstance(m.media, types.MessageMediaWebPage):    return 'text'          # link preview
    if isinstance(m.media, types.MessageMediaPhoto):      return 'photo_caption' if m.message else 'photo'
    if isinstance(m.media, types.MessageMediaDocument):
        if m.sticker:    return 'sticker'
        if m.voice:      return 'voice'
        if m.video_note: return 'video_note'
        if m.gif:        return 'gif'
        if m.video:      return 'video'
        if m.audio:      return 'audio'
        mime = getattr(m.document, 'mime_type', '') or ''
        if mime.startswith('image/'):                     # chart sent as an uncompressed file
            return 'photo_caption' if m.message else 'photo'
        return 'document'
    return 'unsupported'
```
`{'text','photo','photo_caption'}` proceed; every other value is stored with `status='skipped'` and the kind shown as a badge. The set is closed — `unsupported` is the catch-all, so the feed never renders a raw Telethon class name. There is no separate caption field: the caption *is* `message.message` / `message.raw_text`. Service messages arrive through `events.ChatAction`, not `events.NewMessage`; register both so they are recorded rather than lost.

### 3.3 Replies and edits are signal, not noise
A reply is how a channel says *"this one hit TP1"*. On `event.is_reply`, call `await event.get_reply_message()`, look the parent up by `(source_id, message_id)`, and attach both the parent text and **any trades already opened from it** to the agent context. Without that link the model sees `move sl to be` with no idea which trade. If the parent is not in the database — it predates source selection — pass the fetched parent's text anyway, flagged `parent_not_tracked`, and say in the context that no trade is linked to it.

Register `events.MessageEdited` alongside `events.NewMessage`. An edit is a *revision*, not a new signal: store it as a new row with `revision = previous + 1` and the same `message_id`, and run the agent with the previous revision and any resulting trades in context. Treating an edit as a fresh signal is how you get two positions from one call. An edit whose text is byte-identical to the previous revision and which adds no new image is stored as `skipped` / `identical_edit` and is not enqueued. An edit to a message that was recorded `skipped` for a non-processable kind (a voice note replaced by text) is re-classified from scratch and starts its own revision chain at 1. An `identical_edit` row does not restart that chain.

### 3.4 Albums
Telegram delivers an album as separate updates; Telethon's `events.Album` reassembles them behind a 0.5 s delay. Two facts that break naive handlers: a one-item "album" never fires `Album` at all (it arrives as `NewMessage`), and `Album.Event` has **no attribute proxy** — `event.id`, `event.media` and `event.date` do not exist on it. Use `event.messages[0].id`, and iterate `event.messages`. Store **every** image in the album; the `MAX_IMAGES_PER_RUN = 4` cap is applied at context-build time (§5.4), not at ingest, and when it truncates, the run context says how many were dropped. Capping at ingest would throw away data you cannot get back.

### 3.5 Dedup, permalinks, ordering
Unique index on `(source_id, message_id, edit_date)`, storing `edit_date = 0` for un-edited messages. **Do not leave it NULL** — SQLite treats every NULL as distinct, so a Telethon reconnect replay would insert a second row and open the same trade twice, which is exactly the case this index exists to prevent.

Permalink: for a channel with a `username`, `https://t.me/{username}/{id}`; for a private channel, `https://t.me/c/{abs(chat_id) - 1000000000000}/{id}` — that subtraction is exactly Telethon's `resolve_id` inverse of the `-100…` marker. Basic groups and DMs have no permalink; return `None` and let the notifier omit the link rather than emitting a broken one.

Each source has **one queue** and up to **three workers** (`INGEST_WORKERS`, ceiling 3). Two messages 200 ms apart may run triage and the agent together. A free worker takes the next queued post without waiting for a busy one to finish. Broker writes stay strictly one at a time inside the executor, which is what stops a double entry and a stop modified from underneath itself. Queue depth is bounded (`INGEST_QUEUE_MAX = 50`); on overflow, drop the oldest and emit `system.warning`. Before the operator has selected a source, no handler is registered at all and nothing is ingested.

A fourth task, the channel reviewer, is not one of those three slots. It studies the active source once, writes a short playbook under `DATA_DIR/channel-briefs/active/brief.md`, and triage appends that file when it exists. Changing the source backs the previous playbook up under `channel-briefs/backups/`, which signal workers cannot read, and starts a fresh study. It does not re-study on a timer.

### 3.6 Living inside uvicorn's event loop
Telethon binds itself to the loop that ran `connect()` and re-checks on **every** request, raising `RuntimeError: The asyncio event loop must not change after connection`. Consequences, all mandatory:

- Create and connect the client inside FastAPI's `lifespan`, never at import time and never in a separate `asyncio.run()`.
- After `connect()`, call `await client.get_me()` — updates do not start flowing until a high-level request is made. Skipping this produces a client that connects fine and receives nothing, which is a miserable bug to find.
- Do **not** call `run_until_disconnected()` — under a running loop it returns a coroutine, and awaiting it inside `lifespan` blocks startup forever. Uvicorn's loop already keeps you alive.
- Never `import telethon.sync`.
- Run uvicorn with **exactly one worker and no `--reload`, in every environment**. A second process means a second ingest queue and duplicate trades, and `--reload` re-executes module import in a new loop, which is the `RuntimeError` above.
- Set `flood_sleep_threshold=60`; let `FloodWaitError` above that propagate to the dashboard instead of sleeping for hours.

## 4. Provider layer — Anthropic and anything OpenAI-compatible

`ai/base.py` defines one protocol both implementations satisfy, so `loop.py` never branches on vendor:

```python
class Provider(Protocol):
    async def list_models(self) -> list[ModelInfo]: ...
    async def probe(self, model: str) -> Capabilities: ...        # vision?, tools?
    def  run(self, *, system: str, messages: list[Msg], tools: list[ToolSpec],
             max_tokens: int) -> AsyncIterator[Delta]: ...
```
`run` **streams**. `Delta` is a tagged union of `ThinkingDelta`, `TextDelta`, `ToolCallDelta`, `UsageDelta`, `DoneDelta(stop_reason)`. The dashboard's live-reasoning panel is fed straight from this iterator; a `run()` that returns a finished object cannot produce it. `max_tokens` is required on Anthropic's endpoint and must be a real parameter, defaulting to `MAX_TOKENS = 32768` (Settings `provider.max_tokens` overrides it; the yes/no triage call uses `TRIAGE_MAX_TOKENS` and does not lower this).

### 4.1 Anthropic — three current facts that break older code
`POST https://api.anthropic.com/v1/messages`, headers `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`.

1. **Do not send `temperature`, `top_p` or `top_k`.** On the current Opus/Sonnet/Fable generation these return **400 on every request**, thinking or not. The `anthropic` Python SDK v1 removed them from the `messages.create()` signature entirely, so passing them is a `TypeError` before you reach the wire. Omit them; do not expose them in the UI.
2. **Thinking config is `{"type": "adaptive"}`.** The older `{"type": "enabled", "budget_tokens": N}` is rejected with 400 on current models, and on the newest ones thinking is on by default. Send `{"thinking": {"type": "adaptive", "display": "summarized"}}` — `display` defaults to `omitted`, which returns *empty* thinking text and leaves the reasoning panel blank. Probe once per model at setup: try `adaptive`, and only if that 400s fall back to `{"type": "enabled", "budget_tokens": 4096}` with `max_tokens > budget_tokens`. Cache which form the model accepts alongside its capabilities. That fallback is the **only** permitted use of the `enabled` form.
3. **Thinking blocks must be echoed back unmodified.** Each carries a `signature`. When you return a `tool_result` you must include the assistant turn's `thinking` blocks exactly as received, in order, alongside their `tool_use` block. Editing, reordering or dropping them is a 400. With the `enabled` fallback, `tool_choice` supports only `auto` and `none`.

Content blocks: tools as `{"name", "description", "input_schema"}`; a call arrives as `{"type":"tool_use","id":"toolu_…","name":…,"input":{…}}` with `stop_reason: "tool_use"`; results go back in a `user` message as `{"type":"tool_result","tool_use_id":…,"content":…,"is_error":bool}`. Images: `{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":"…"}}`.

Model list: `GET /v1/models`. Its envelope is **not** OpenAI's — there is no `object: "list"`; it is `{"data":[…], "has_more":bool, "first_id":…, "last_id":…}` with `limit`/`after_id` cursors. Each entry carries a real `capabilities` object (`image_input.supported`, `thinking.types`, `effort`), so on Anthropic you read capability directly instead of probing.

Watch `stop_reason` on a **200** response: `"model_context_window_exceeded"` arrives with HTTP 200, not an error status. Treat it as a hard stop, trim the context, and surface it. Error bodies are `{"type":"error","error":{"type":…,"message":…}}` with `authentication_error` 401, `not_found_error` 404, `rate_limit_error` 429, `overloaded_error` **529** (not 503).

### 4.2 OpenAI-compatible — target `/v1/chat/completions`
Not `/v1/responses`. Chat Completions is what every gateway implements — OpenRouter and Ollama do not serve `/v1/responses` at all — and it is not deprecated. Base URL, API key, model, and an **extra-headers map** are all operator-supplied; the extra-headers field is what lets a self-hosted gateway work without code changes.

Tools are `{"type":"function","function":{"name","description","parameters"}}`; calls come back in `tool_calls[]` where **`function.arguments` is a JSON string not guaranteed to be valid JSON**. Parse it inside a `try`; on failure return a `tool` message describing the parse error so the model retries, rather than crashing the run. Branch on `tool_calls[i].type == "function"` and skip unknown types — a `custom` variant now exists. Results go back as `{"role":"tool","tool_call_id":…,"content":…}`.

Images: `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…","detail":"auto"}}`. **Always send a data URL, never an https URL** — Ollama rejects remote URLs outright with `image URLs are not currently supported`.

### 4.3 The "Test provider" button, which is the feature the operator sees
When `base_url` and `api_key` are both filled, `POST /api/provider/test` does, in order:

1. `GET {base_url}/v1/models` (or Anthropic's, per provider kind). Normalize the base URL first: accept `https://host`, `https://host/`, and `https://host/v1` and produce the same request.
2. **Normalize the response defensively.** OpenRouter breaks the OpenAI envelope: no top-level `object`, no per-entry `object` or `owned_by`, plus extra fields `architecture.input_modalities`, `supported_parameters`, `context_length`. Treat **only `data[i].id` as guaranteed** and read everything else with `.get()`. Where richer metadata exists, prefer it to a probe: `"image" in architecture.input_modalities` and `"tools" in supported_parameters` are facts, not guesses.
3. Return the list to the UI, which renders it searchable and selectable.
4. On selection, `POST /api/provider/probe` sends one throwaway request with `max_tokens: 16`, thinking disabled, a 1×1 PNG data URL, and a one-parameter tool. HTTP 400/422 means unsupported; 200 means supported. **`max_tokens: 1` is wrong here** — it 400s on every thinking-capable model regardless of vision or tools, so the probe would report "unsupported" for exactly the models you want. Cache the verdict per `(base_url, model)` for `PROBE_CACHE_HOURS = 24` and expose `supports_vision` / `supports_tools` as operator-overridable switches, because self-hosted endpoints defeat any heuristic.

Never infer capability from the model name string. Every gateway renames models.

Failures report the real cause: 401 → "key rejected", 404 → "endpoint or model not found — check base_url", connection error → "host unreachable", non-JSON body → the first 200 characters. "Failed to connect" tells the operator nothing.

### 4.4 Subscriptions — say this out loud in the UI and README
Consumer Claude and ChatGPT subscriptions have **no public API**. There is no supported OAuth path that lets this application trade on a subscription plan, and any code that pretends otherwise is fiction that will break. What the settings page offers instead: an API key from either vendor, or any OpenAI-compatible base URL — which covers OpenRouter, Groq, Together, LiteLLM, vLLM, LM Studio and Ollama, including a local model on the same machine. The custom-headers field covers a gateway the operator runs themselves. Write this as one calm sentence in the settings page, not as an error.

## 5. The cockpit — tools, context, and the loop

### 5.1 The model calls tools; it does not emit JSON for you to parse
The tempting design is: ask the model to return `{"action":"buy","symbol":"XAUUSD","lot":0.1,"sl":3320}`, parse it, execute. Do not build that. It puts float arithmetic, symbol resolution and broker constraints inside the model's head, where they are unreliable and unauditable, and it gives the model one shot with no way to look before it leaps.

Instead the model runs a real tool loop: it reads, it *checks*, it acts, it sees the result, it reports. Every step is persisted and streamed. Cap it at `MAX_TOOL_ITERATIONS = 32` per full agent run; on the cap, force a final turn with tools disabled so the run always ends with a recorded decision. Do not lower the cap to buy speed.

### 5.2 Read tools
- `get_account()` → balance, equity, margin_free, currency, leverage, `margin_mode` as the literal string `"hedging"`/`"netting"`, open position count, today's realized P/L (broker-server day).
- `resolve_symbol(query)` → up to 8 ranked candidates, each with `name`, `description`, `digits`, `point`, `pip_size`, `pip_convention`, `volume_min/step/max`, `stops_level_points`, `spread_points`, `tradable`.
- `get_quote(symbol)` → `bid`, `ask`, `spread_points`, `spread_pips`, `server_time`.
- `price_from_distance(symbol, anchor, direction, distance, unit)` → an absolute price plus the convention used (§2). This is how "sl 20 pips below" becomes a number without the model multiplying anything.
- `evaluate_entry(symbol, side, signal_entry, sl, tp?)` → **the late-entry instrument.** Returns current price, `distance_from_signal_entry` in points/pips/price, `distance_as_fraction_of_sl` (how much of the intended stop the move already ate), the R:R if entered now versus as posted, `signal_age_seconds`, and the volume `DEFAULT_RISK_PERCENT` would produce at both prices. The AI does not do this arithmetic; it reads the number and judges. This tool is what makes "enter late, knowingly" a real capability rather than a hope.
- `get_positions()` / `get_orders()` → live from MT5, each row joined to its originating `signal_ref` and the source message excerpt.
- `get_candles(symbol, timeframe, count)` → recent OHLC, capped at 200 bars. A chart screenshot cannot tell the model where price is *now*; this is how it grounds an image against reality.
- `get_recent_signals(limit)` → the last N messages from this source with their outcomes, so `tp1 hit` resolves to the right trade. Do **not** use this to invent ladder runner-mode stats.
- `get_channel_ladder_stats()` → server-computed last-N fully-closed ladder outcomes for this source (`raw_SL`, `BE_stopouts`, `TP4+`, clean streak, `suggested_mode`, `suggested_runner_tp_bias`) plus a ready-made blurb. The model must not recount channel history.

### 5.3 Write tools
Every write tool takes a `reason` string, which is what the notification and the history row quote. **`comment` is not a tool parameter** — the executor owns the MT5 comment field, because §8 recovers `run_id` from it and a model-written comment would destroy the reconciliation tag.

- `open_market(symbol, side, sl, tp[], volume?, risk_percent?, reason, runner_mode?, runner_tp_index?)` — exactly one of `volume` / `risk_percent`, or neither (then `DEFAULT_RISK_PERCENT`); both is a validation error returned to the model, not an exception. On a 4+ TP ladder, `runner_mode` is `be_after_cover` or `hold_channel_sl` (omit to use the server's suggested mode from channel stats). `runner_tp_index` is the 1-based numbered TP for the runner (LONG excluded from N); omit to use `min(ceil((N+1)/2)+bias, N-1)`, or shift that suggestion by at most ±1.
- `place_pending(symbol, side, order_type, price, sl, tp[], expiry_minutes?, volume?, risk_percent?, reason)` where `order_type ∈ {limit, stop}`. The executor picks `BUY_LIMIT` / `SELL_LIMIT` / `BUY_STOP` / `SELL_STOP` from `side` + `order_type` + the price's position relative to the market, and rejects an impossible combination by naming the type it inferred and why it is invalid.
- `modify_position(ticket, sl?, tp?, reason)` — `TRADE_ACTION_SLTP`, requires `position`.
- `modify_order(ticket, price?, sl?, tp?, reason)` — `TRADE_ACTION_MODIFY`, requires `order`.
- `close_position(ticket, volume?, reason)` — full or partial, per §1.3.
- `cancel_order(ticket, reason)` — `TRADE_ACTION_REMOVE`.
- `notify(text)` — pushes to the operator's Telegram bot (§7).
- `finish(decision, summary)` where `decision ∈ {"acted","ignored","deferred"}` — **every run must end with this call.** A run that ends without it is recorded as `incomplete` and shown in red; that visibility is the point.

**Multi-TP is a first-class case, not an afterthought.** Signals routinely give TP1/TP2/TP3 and MT5 allows one TP per position, so `tp` is a *list*, capped at `MAX_TP_SPLITS = 3` (extra targets are stored on the signal and reported as unmanaged).

- `tp == []` → one position with `tp = 0.0`. Legal, and the AI manages the exit later.
- **Hedging** → split the volume into `len(tp)` sub-positions by largest remainder, so the parts sum exactly to the whole and each respects `volume_step` and `volume_min`, each with its own TP, all sharing one `signal_ref` and one `magic`. If `volume < len(tp) * volume_min`, reduce the split count to `floor(volume / volume_min)` and say so in the tool result.
- **Netting** → one position with `tp[0]`; persist the rest as unmanaged targets and tell the model so in the result.

Silently keeping `tp[0]` and discarding the rest is the failure this is written to prevent.

Every write tool returns `{ok, ticket(s)?, filled_volume?, fill_price?, slippage_points?, retcode?, retcode_name?, message}`. A rejection is never an exception that kills the run — `10019` comes back as *"not enough free margin: need 412.30 USD, have 180.05"*, so the model can retry smaller.

### 5.4 Context
The system prompt (`prompts.py`, one place, English) tells the model: it is the trader; the channel's style; that signals may be minutes old and it should call `evaluate_entry` before deciding; that stops are mandatory and `open_market` will reject a missing one; that it must not open a duplicate for a `signal_ref` that already has a live position; and that it must always end with `finish`.

Per-run user turn: the message text, its images, `signal_age_seconds`, a `stale: true` flag when that exceeds `STALE_SIGNAL_SECONDS = 600`, its permalink, the parent message and that parent's trades if it is a reply, the previous revision if it is an edit, the last `CONTEXT_SIGNALS = 10` messages from this source, current account state, and open positions with their `signal_ref`s.

**Images.** Downscale so the long edge is ≤ `IMAGE_MAX_EDGE = 1568` px, re-encode as JPEG quality 85, and if the result still exceeds `IMAGE_MAX_BYTES = 3_500_000` drop quality in steps of 10 down to 40. Sizing matters: image cost is `⌈w/28⌉ × ⌈h/28⌉` tokens, so an unresized 1920×1080 screenshot is 69 × 39 = **2 691** tokens while 1568 × 882 is 56 × 32 = **1 792** — about two-thirds, and four of them still dominate the request. Lower `IMAGE_MAX_EDGE` if cost matters more than legibility; 1 100 px roughly halves it again. Cap at `MAX_IMAGES_PER_RUN = 4`, and when an album is truncated say in the context how many were dropped.

### 5.5 Idempotency, the duplicate guard, and armed state
Every run gets `run_id = uuid4()`. Every order carries `magic = MAGIC_NUMBER` (config, default `770077`) and `comment = f"SP:{run_id.hex[:8]}"`, which is how §8 re-links positions after a restart. **Before any write tool executes**, re-check that no position or pending order already exists with this `signal_ref`; if one does, return that fact to the model instead of executing and let it decide. This guard is unconditional — it is a correctness rule, not a risk limit.

A global **`armed`** flag (default `false`, toggled in the UI header, persisted) gates every write tool except the kill switch's own closes. Disarmed, the loop still runs and shows its full reasoning and intended actions, but write tools return `{"ok": false, "message": "System is disarmed — no order was sent"}`. This is how the operator watches the AI think for a day before letting it trade, and it is also the emergency stop.

`kill_switch` is separate: it closes every position **and** cancels every pending order carrying `MAGIC_NUMBER`, **then** sets `armed = false`. That order matters — disarming first would make its own closes return "system is disarmed" and the kill switch would do nothing. It sits behind a typed confirmation in the UI.

## 6. Stops: do not invent one, do not remove one

The operator chose full autonomy, so `guards.py` is deliberately thin. Opening without a posted stop is allowed. Removing a stop that is already on is not.

1. `sl` is **optional** on `open_market` and `place_pending`. Omit it, or send `0`, when the channel has not posted a stop: the server opens an incomplete entry at `trading.ladder_volume` (two legs when the broker allows, otherwise one) with no stop and no take-profit. Do not invent a level to get the order through. `check_sl_present` still exists and still returns `SL_REQUIRED`; the executor does not call it on open.
2. `modify_position` and `modify_order` **reject `sl == 0`** with *"the stop cannot be removed; move it instead"*. A stop that is already on can be moved. It cannot be cleared.
3. The SL must be on the correct side of the entry (below for a buy, above for a sell) and at least `trade_stops_level` points away. A wrong side is rejected naming the corrected side.
4. A **watchdog** runs every `SL_WATCHDOG_SECONDS = 60`. Any open position with `magic == MAGIC_NUMBER` and `sl == 0.0` starts a *stopless episode*: notify the operator **once** per episode (not once per tick), and start an agent run whose only job is to attach a stop. After `SL_FALLBACK_ATTEMPTS = 3` failed cycles the executor stops asking and acts itself — it attaches a stop at `SL_FALLBACK_POINTS = 500` points from entry (or `trade_stops_level`, whichever is larger) and notifies that it did so. The fallback bypasses the `armed` gate, because a naked position is a worse state than an unwanted order. Positions lose their stops for real reasons — a partial close, a manual edit in the terminal, a broker action — and finding out at the next drawdown is not acceptable.

Everything else is an **optional operator limit**, stored in settings and shipped **off**: `max_volume_per_trade` (null), `max_open_positions` (null), `max_daily_loss_percent` (null), `symbol_allowlist` (empty = all allowed), `trading_hours` (empty = always). A null or empty limit is not evaluated at all. When one is set and a call would breach it, the write tool returns a refusal naming the limit and its value, and the model reports it in `finish`. The settings page states plainly that these ship disabled.

## 7. Notifications through the operator's own bot

Separate from the Telethon user account. The operator creates a bot with @BotFather, pastes the token, and presses **Test & detect chat**: the backend calls `getMe` to validate the token, tells the operator to send `/start` to the bot, then polls `getUpdates` every 2 s for up to 60 s and fills `chat_id` from the first incoming message. Making the operator find their own numeric chat id by hand is the kind of small friction that gets a project abandoned.

Send via `httpx` to `https://api.telegram.org/bot{token}/sendMessage` with `parse_mode: "HTML"` — HTML escapes cleanly, while MarkdownV2 breaks on the first `.` in a price. Fire-and-forget in a background task; a notification failure must never abort or delay a trade. Retry at 1 s and 4 s, then emit `system.warning` with reason `notify_failed`.

Messages, each carrying the source-post link when one exists:

- **Opened** — symbol, side, volume, entry, SL, TP(s), risk percent, the model's `reason`, link to the signal post.
- **Modified** — what changed, old → new.
- **Closed** — cause (TP / SL / by the AI / manual / kill switch), realized P/L in account currency and in points.
- **Rejected** — the retcode name and the human message.
- **Attention** — stopless episode, SL fallback applied, MT5 disconnected, provider failing, queue overflow.

Every type has its own on/off switch in settings. Notifications respect the current UI locale.

## 8. Reconciliation on startup and reconnect

The process will restart while positions are open. On startup, and after any MT5 reconnect:

1. Pull `positions_get()` and `orders_get()`.
2. Match each against the database by ticket; for unknown tickets, recover the `run_id` from `comment` (`SP:xxxxxxxx`) and re-link to its signal. Positions with a different `magic` belong to someone else — display them read-only, marked `external`, and never modify or close them, including from the kill switch.
3. Rows marked open in the database whose tickets no longer exist were closed while we were down: call `history_deals_get` from `last_seen_close_time - 1h` to `now + 1 day` (the padding is the broker-time issue from §2), compute realized P/L, close the row, and notify.
4. Messages that arrived while the process was down: fetch up to `CATCHUP_LIMIT = 20` messages newer than the last stored `message_id` for the active source, run them through the same classify/dedup path, and mark them `catchup`. On the very first run for a source there is no stored `message_id`; fetch nothing and record the current latest id as the baseline, so selecting a channel does not replay its backlog as live signals. Every catch-up message carries its true `signal_age_seconds` and the `stale` flag into the model's context — a 40-minute-old entry must not look live.

## 9. The dashboard

One WebSocket, `GET /api/stream`, carrying typed events: `signal.received`, `run.started`, `run.thinking`, `run.tool_call`, `run.tool_result`, `run.finished`, `provider.request`, `order.placed`, `order.cancelled`, `position.opened`, `position.modified`, `position.closed`, `account.tick`, `system.warning`. `provider.request` carries model, token usage and latency — it is what makes "zero provider calls for a voice note" checkable rather than a claim. The client reconnects with exponential backoff from 1 s to 30 s and, on reconnect, refetches state over REST rather than assuming it missed nothing.

**Cockpit** (landing page) — three columns on desktop, stacked on mobile:
- *Signal feed*: incoming messages with kind badges, image thumbnails, skipped messages greyed with their reason.
- *Live reasoning*: the current run streaming — thinking text, then each tool call as a card with its arguments and result, colour-coded by outcome. Finished runs collapse to a one-line summary that expands.
- *Account*: balance, equity, free margin, open P/L, three explicit status dots for MT5 / Telegram / provider, and the **ARM / DISARM** switch, the largest control on the page, with the kill switch beside it behind a typed confirmation.

**Positions** — open positions and pending orders, each row linking to the signal that created it, with manual close and modify controls, and `external` rows visibly read-only.

**History** — closed trades with realized P/L, the signal that caused each, and per-source aggregates: win rate, average R, total P/L. Filter by date range and symbol.

**Settings** — Telegram account (api_id, api_hash, phone, code, 2FA, then a dialog picker listing the user's channels by title), MT5 account (server, login, password, **Test connection** reporting the real balance and account name), AI provider (kind, base_url, api_key, extra headers, then the Test → model list → select → probe flow of §4.3), notification bot, and the optional limits of §6 with their "off by default" note.

Every input is validated on both sides with the same rules and the same messages: `api_id` a positive integer, phone E.164, MT5 login a positive integer, `base_url` a URL normalized per §4.3, numeric limits positive. Errors render next to the field, never as a toast that disappears.

All prices, volumes, tickets and P/L render in the monospace face with `font-variant-numeric: tabular-nums`, so a streaming price does not make the row jitter.

## 10. Theme and locale, resolved before first paint

Both preferences are read and applied by a small **inline script in `index.html`'s `<head>`**, before any bundle loads:

```html
<script>
  try {
    var t = localStorage.getItem('sp-theme') ||
            (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    var l = localStorage.getItem('sp-locale') ||
            ((navigator.language || 'en').toLowerCase().startsWith('ru') ? 'ru' : 'en');
    document.documentElement.setAttribute('data-theme', t);
    document.documentElement.setAttribute('lang', l);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.documentElement.setAttribute('lang', 'en');
  }
</script>
```

Applying either in a React effect means every reload flashes white before going dark, or English before switching to Russian — invisible on a fast dev machine, obvious to the person who uses this daily. The `try/catch` matters: `localStorage` throws outright in some privacy modes, and an unguarded read leaves the page unthemed.

CSS reads `:root[data-theme="dark"]` / `:root[data-theme="light"]` and defines **every** colour as a token in `tokens.css`. No colour literal appears in any other file.

Copy lives in `src/i18n/{ru,en}.ts` as a flat key dictionary from the first line of markup — both locales complete, no fallback holes. `t('key')` is the only way any string reaches the DOM; a missing key renders the key itself in `--warn` so gaps are visible rather than silent. Retrofitting i18n into finished markup means touching every file, which is why this is not optional even for the first commit. Numbers and dates go through `Intl.NumberFormat` / `Intl.DateTimeFormat` with the active locale; account currency uses `style: 'currency'`.

## 11. Look and feel

One direction, committed to: **instrument-panel dark, calm, dense**. It should read as a cockpit — quiet until something matters.

```css
:root[data-theme="dark"] {
  --bg:     #0B0E13;  --surface: #141922;  --surface-2: #1C2230;  --border: #2A3242;
  --text:   #E6EAF2;  --text-dim:#8B95A8;  --accent:    #4C9AFF;
  --long:   #2ECC8F;  --short:   #FF5C7A;  --warn:      #F5A623;  --thinking:#A78BFA;
}
:root[data-theme="light"] {
  --bg:     #F7F8FA;  --surface: #FFFFFF;  --surface-2: #EEF1F6;  --border: #D8DEE8;
  --text:   #10141C;  --text-dim:#5C6678;  --accent:    #1B6FE0;
  --long:   #0F7A57;  --short:   #E0526E;  --warn:      #B87400;  --thinking:#6D4AC7;
}
```

Long and short are green and red because traders expect it, but colour is never the only carrier: **every** buy/sell indicator pairs its colour with the word `BUY`/`SELL` or a directional arrow. Both pairs are separated in lightness so direction survives a monochrome screenshot — dark `#2ECC8F` (L ≈ 0.46) against `#FF5C7A` (L ≈ 0.30), light `#0F7A57` (L ≈ 0.19) against `#E0526E` (L ≈ 0.28). Check any palette edit against a greyscale filter before committing it; the earlier light pair failed this and looked identical desaturated.

Type: **Inter** for the interface, **JetBrains Mono** for every number, both from Google Fonts with real fallbacks (`Inter, system-ui, -apple-system, "Segoe UI", sans-serif` and `"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace`). Base 14 px, 1.5 line height, an 8 px spacing scale, 8 px radii, one-pixel borders and no drop shadows on data surfaces — depth comes from `--surface` against `--surface-2`.

The AI's thinking text renders in `--thinking` at 13 px behind a left rule, visually distinct from tool calls and results, so a glance separates deliberation from action.

`base.css` holds the reset, the `html`/`body` background and colour from tokens, font-family declarations, focus-visible rings on `--accent`, and the scrollbar treatment — nothing else. `components.css` holds only reusable primitives (`.card`, `.btn`, `.btn--danger`, `.badge`, `.table`, `.field`, `.dot`, `.mono`), each reading tokens exclusively. Anything page-specific lives with its component.

## 12. Limitations — write these into the README honestly

- **Automating a Telegram user account carries account risk.** Telegram's API terms do not forbid it, and `api_id`/`api_hash` are issued for exactly this, but anti-spam heuristics can limit or ban accounts on suspicion with no guaranteed appeal. Reading a channel you belong to is the low-risk end; do not add bulk messaging to this system.
- **The AI will misread signals.** Vision models misjudge price levels on chart screenshots, especially unlabelled ones. `evaluate_entry` and `get_candles` exist to ground it against real prices, and the operator should run disarmed for a while and read the reasoning before arming.
- **Redistributing signals from a paid channel may violate its terms.** This tool is for the operator's own account.
- **Slippage and latency are real.** Signals arrive late; the AI adds seconds; the broker adds more. Executed prices will differ from posted ones. The dashboard reports the difference on every fill.
- **MT5 under Wine is a moving target.** MT5 self-updates and can present a dialog that blocks headless startup. `docker compose --profile debug up` exists for exactly this.
- **This is not investment advice and carries real financial risk.** Run it on the demo account first.

---

## Build order

Each stage runs on its own and has something to check before you continue. Every check is observable without the later stages.

**Stage 0 — skeleton.** Repo tree, `config.py`, `constants.py`, ORM models, `docker-compose.yml`, Fernet helpers, Vite build served by FastAPI, dashboard shell with theme and locale switches.
*Check:* `docker compose up --build`; in the browser console set `localStorage['sp-theme']='dark'` and `localStorage['sp-locale']='ru'`, reload with the cache cold and CPU throttled 6×, and see the shell render dark and Russian with no flash of either default.

**Stage 1 — MT5 sidecar alone.** Wine image, terminal install, ini rendering, Xvfb, `rpc_server.py`, token auth, watchdog.
*Check:* from the app container, `curl -s -H 'X-SP-Token: …' http://mt5-sidecar:18812/rpc -d '{"method":"account_info","params":{}}'` returns the real balance of Exness demo `474436674`, and the same call without the header returns 401. Restart the sidecar; it comes back without human input.

**Stage 2 — trading layer, no AI.** `rpc.py`, `symbols.py`, `pricing.py`, `executor.py`, `guards.py`, and REST endpoints under `/api/trading/manual/*`.
*Check:* over JSON only, no UI — resolve `gold` and get the Exness symbol with its real suffix, `pip_size` and `pip_convention`; open 0.01 lot with a stop 200 points away; `GET /api/trading/positions` shows it with the right distance in all three units; modify the SL; partially close; close. No `10030` and no `10016` anywhere. Then try to open without `sl` and get the §6 refusal.

**Stage 3 — Telegram ingest, no AI.** Login flow, dialog picker, classify, dedup, albums, edits, permalinks, queue, catch-up baseline.
*Check:* in a private test channel post text, a photo, a photo with caption, a chart sent as an uncompressed file, a reply, an album, and a voice note. All appear in the feed with correct kinds; the voice note is `skipped` and the uncompressed chart is **not**; edit the text message and a revision row appears rather than a duplicate; restart the backend and nothing is double-counted.

**Stage 4 — provider layer.** Both providers, catalog, probe with `max_tokens: 16`, settings UI.
*Check:* an Anthropic key lists models with capabilities read from the response; an OpenAI-compatible base URL lists models; an OpenRouter URL lists models without crashing on the non-standard envelope; a wrong key says "key rejected", not "failed to connect".

**Stage 5 — the loop.** Tools, prompts, streaming agent loop, `finish`, idempotency, duplicate guard, armed gating.
*Check:* disarmed, post `buy gold now, sl 20 pips below, tp1 +30 tp2 +60`: thinking streams to the dashboard, `resolve_symbol` → `price_from_distance` → `evaluate_entry` → `open_market` appear as tool cards, and the write returns "system is disarmed". Arm it and repeat: on a hedging account two positions open with split TPs, on netting one opens and the result names the unmanaged target. Both have stops.

**Stage 6 — notifications, history, reconciliation, polish.** Bot setup with chat detection, all notification types, history aggregates, startup reconciliation, catch-up, SL watchdog and fallback, both locales complete.
*Check:* kill the backend with a position open, close that position by hand in the terminal, restart: it reconciles to closed with correct realized P/L and the operator gets a Telegram message carrying the source-post link. Separately, clear a position's SL in the terminal and confirm one notification, then the fallback stop after three cycles.

## Acceptance criteria

1. `docker compose up --build` from a clean checkout brings up both containers and serves the dashboard; the MT5 dot goes green from `.env` alone, without any interactive step.
2. After the operator completes the Telegram login and provider setup, all three status dots are green and stay green across a backend restart.
3. `resolve_symbol("gold")` on the Exness demo returns the broker's actual symbol with its real suffix, its `pip_size`, its `pip_convention`, and its `stops_level_points` — not a hardcoded `XAUUSD`.
4. On EURUSD (`digits = 5`, `point = 0.00001`, `pip = 0.0001`) with a 3 000 USD balance, `risk_percent: 1` and a stop 20 pips away produces a volume whose loss at the stop is 30 USD **minus at most `volume_step × loss_per_lot`, and never more than 30 USD** — the sizing rounds down only. The volume is an exact multiple of `volume_step`.
5. A pending order returns `ok: true` even though its retcode is `10008`, and appears exactly once in the Orders table.
6. A three-TP signal on a hedging account opens three positions whose volumes sum exactly to the intended total with every part ≥ `volume_min`, each with its own TP, all sharing one `signal_ref`. With `FORCE_MARGIN_MODE="netting"` the same signal opens one position and the tool result names the unmanaged targets.
7. `open_market` without `sl` is refused with a message the model can act on and the run continues; `modify_position` with `sl: 0` is refused the same way. No position with `MAGIC_NUMBER` stays stopless past `SL_FALLBACK_ATTEMPTS` watchdog cycles without a fallback stop and a notification.
8. Posting a voice note, a video note and a sticker produces three `skipped` rows and **zero** `provider.request` events on the stream, while a chart sent as an uncompressed document produces exactly one.
9. Editing a signal message creates a revision row, triggers one agent run with the previous revision in context, and does **not** open a second position for the same signal.
10. Two messages posted 200 ms apart produce two runs that execute strictly one after the other; the second run's context contains the first run's position.
11. Killing the backend mid-position and restarting reconciles that position from MT5 by ticket and `magic`, re-links it via the `SP:` comment, and leaves positions with a foreign `magic` untouched and marked `external` — including when the kill switch fires.
12. With `armed: false`, a valid signal streams complete reasoning and tool calls and sends nothing to the broker; `positions_get()` is unchanged. The kill switch still closes positions while disarmed.
13. Reloading the dashboard in dark Russian shows no flash of light theme and no flash of English, with a cold cache and CPU throttled 6×.
14. Pointing the provider at an OpenRouter base URL lists models despite the non-standard envelope; pointing it at a local Ollama URL runs a text-only signal end to end.
15. Every secret — MT5 password, Telegram session string, provider key, bot token, sidecar token — is absent from `docker compose logs` and from every API response, which return `"••••••••"` with `has_value: true`.

## Prohibited

Each line names a specific wrong path and the section that explains it. Where a rule appears in both places, the section is authoritative.

- **Do not pass `symbol_info().filling_mode` into `request["type_filling"]`** (§1.3). Different numbering schemes — bitmask `1` means "FOK allowed", enum `1` means IOC. Fails only as retcode `10030` at the broker.
- **Do not write `mt5.SYMBOL_FILLING_FOK` / `_IOC` / `_BOC`** — those names do not exist in the package and raise `AttributeError`. Use the literals in `constants.py` (§1.2).
- **Do not treat `retcode != 10009` as failure** (§1.3). Pending orders succeed at `10008`; getting this wrong makes the system retry orders that already exist.
- **Do not omit `position` when closing** (§1.3). On a hedging account the close silently becomes a new opposite position and exposure doubles with no error anywhere.
- **Do not let the model compute lot sizes, pip values, or price arithmetic** (§2). It names price levels and relative distances; `pricing.py` does every multiplication and every rounding.
- **Do not clamp a computed volume up to `volume_min`** (§2). It silently multiplies the intended risk. Return the error and let the model choose.
- **Do not `import MetaTrader5` anywhere in `backend/`** (§1.2). There is no Linux wheel; the constants live in `constants.py` and the calls go over RPC.
- **Do not publish the sidecar port to the host or accept an unauthenticated `/rpc` call** (§1). It is `order_send` on an open port.
- **Do not send `temperature`, `top_p` or `top_k` to Anthropic**, and do not use `thinking: {"type":"enabled"}` as the first attempt — the §4.1 fallback is its only permitted use. Both are hard 400s on the current generation.
- **Do not modify, reorder or drop Anthropic `thinking` blocks** when returning tool results (§4.1). They carry a `signature` and a mismatch is a 400.
- **Do not assume the OpenAI `/v1/models` envelope** (§4.3). Only `data[i].id` is guaranteed; OpenRouter omits `object` and `owned_by` entirely.
- **Do not send https image URLs to an OpenAI-compatible endpoint** (§4.2). Ollama and several gateways reject them; always inline a `data:` URL.
- **Do not probe capabilities with `max_tokens: 1`** (§4.3). It 400s on every thinking-capable model and reports them all unsupported.
- **Do not use `client.start()`, `run_until_disconnected()` or `import telethon.sync`**, and do not create the Telethon client outside FastAPI's `lifespan` (§3.6). Each binds the client to the wrong loop or blocks startup.
- **Do not trust `message.photo` or `message.video` for classification** (§3.2). `photo` is truthy for link previews; `video` is truthy for GIFs and video notes; an image sent as a document must still be processed.
- **Do not leave `edit_date` NULL in the dedup index** (§3.5). SQLite treats every NULL as distinct and the replay guard stops working.
- **Do not run more than one uvicorn worker, and do not use `--reload`, in any environment** (§3.6).
- **Do not give `open_market` or `place_pending` a `comment` parameter** (§5.3). The executor owns that field; a model-written comment breaks reconciliation.
- **Do not add a per-trade approval step or any risk-based veto that ships enabled** (§6). Unconditional gates are `armed`, a posted stop on the correct side, the refusal to remove an existing stop, and the duplicate-signal guard. A missing stop does not refuse the open. The operator's limits ship disabled.
- **Do not disarm before closing in the kill switch** (§5.5). The closes would be refused by the arm gate and the kill switch would do nothing.
- **Do not implement an OAuth or "log in with your Claude/ChatGPT subscription" flow** (§4.4). No such public API exists; anything written will be fiction that breaks.
- No `TODO`, no stubbed functions, no "simplified for brevity", no code replaced by ellipses, and no file in the tree left uncreated.

---

**Deliver in this order:** the decisions you made and why → full code, one block per file, complete → run instructions from a clean checkout → a parameter table listing every constant in `config.py` and every token in `tokens.css` with its default and what it controls.
