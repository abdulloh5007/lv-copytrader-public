# LV Copytrader

Current UI, waitlist, security, verification and deployment: [Product guide](docs/PRODUCT.md).


> **Bringing this up for the first time? Read [RUNBOOK.md](RUNBOOK.md) first.**
> Most failures here name the wrong thing — `IPC send failed` is not an IPC problem,
> a `403` is not a bad API key, and `terminal64.exe is running` can be a lie. The
> runbook is the startup sequence plus a symptom table for every trap that was hit
> for real against a live broker.

A self-hosted system where **an LLM is the trader**. It watches a Telegram signal channel through a
user account, reads whatever the channel actually posts — free-form prose, screenshots of charts,
replies to earlier calls — decides what to do, and executes on MetaTrader 5 through tools. A web
dashboard shows what it is thinking, what it did, and lets the operator configure all of it.

The framing this project was built to, and it is not decoration:

> *We are building a plane, and the pilot is the AI. Our job is to build the cockpit — the
> instruments it reads and the controls it pulls. The AI flies.*

Everything in the codebase follows from that division of labour. The model names intent — *buy gold,
stop 20 pips below, first target +30*. The server does every multiplication, every rounding, every
symbol lookup and every broker constraint check. The model never computes a lot size and never
guesses what a pip is worth.

### The three properties that define success

1. **The channel is not templated.** Signals arrive as `buy gold now`, `tp1 hit move sl to be`,
   `+110 pips 🚀`, or a bare screenshot with an entry line drawn on it. There is no regex that parses
   this. The model reads it — text and image together — and decides.
2. **Late is acceptable only inside a hard band.** The AI will often see a signal minutes after it
   was posted, with price already moved. A classic gold entry is skipped for "price already moved"
   only when the live fill is more than `trading.max_entry_drift_usd` (default five dollars) from
   the channel entry or zone. At or inside that distance it must be opened, and fixed lots are not
   a reason to defer. A ladder of 4 or more targets keeps the wider seven-dollar metal band. Either
   way the printed entry must be younger than 10 minutes. Farther than the band, or older than that,
   the server refuses both a market chase and a pending order.
3. **Every position has a stop.** The one trading rule the executor enforces unconditionally, in both
   directions: an order cannot be opened without a stop, and an existing stop cannot be removed.

The armed switch, the stop-loss rule, and the duplicate-signal guard can block an order.
A new open has two more: the fill must sit within the entry band of the printed entry
(classic gold: `trading.max_entry_drift_usd`, default $5; ladders: seven dollars),
and the message that printed that entry must be younger than 10 minutes. Attaching a later
ladder to legs that are already open is not a new open. Position-size limits, daily loss caps
and max-open-position counts all exist as operator settings and all ship **disabled**. That was a
deliberate choice — full autonomy, with an on/off switch and a stop-loss invariant, rather than a
committee of vetoes.

---

## Architecture

Two containers, and the split between them is forced by one fact: **the `MetaTrader5` Python package
is Windows-only.** There is no Linux wheel, and it talks to a running `terminal64.exe` over a single
IPC channel that is not thread-safe.

```
                  ┌──────────────────────────────────────────┐
   browser ──────►│  app  (Linux, Python 3.12)                │
        :8000     │  FastAPI + built SPA, ONE uvicorn worker  │
                  │  all trading logic, all AI, all state     │
                  └───────────────┬──────────────────────────┘
                                  │  JSON-RPC over the compose network
                                  │  POST /rpc  +  X-SP-Token
                                  ▼
                  ┌──────────────────────────────────────────┐
   VNC :5901 ────►│  mt5-sidecar  (Debian + Wine + Xvfb)      │
   (debug only)   │  terminal64.exe, Windows Python 3.11      │
                  │  rpc_server.py — the ONLY MetaTrader5     │
                  │  import in the whole project              │
                  └──────────────────────────────────────────┘
```

**Trading logic never runs inside Wine.** The sidecar is a dumb, explicitly whitelisted RPC surface
in front of the terminal — `account_info`, `symbol_info`, `order_check`, `order_send` and a dozen
others, nothing more. Every decision, every calculation and every piece of persistence happens in the
Linux container, where it is testable, debuggable and not subject to Wine's mood. The backend cannot
`import MetaTrader5` at all; the MT5 integer constants it needs live as literals in
`backend/app/trading/constants.py`.

Consequences of that layout, each of which is load-bearing:

- The sidecar's RPC server is **single-threaded** (`http.server.HTTPServer`, not
  `ThreadingHTTPServer`). That one thread *is* the serialisation of the non-thread-safe MT5 IPC
  channel.
- The sidecar's port is **never published to the host**. It is reachable only on the compose network,
  and every request carries `SIDECAR_TOKEN`, compared with `hmac.compare_digest`. An unauthenticated
  `order_send` on an open port would make every other precaution here pointless.
- MT5 needs an X server even headless — Wine's null graphics driver makes window creation fail — so
  Xvfb runs on `:99`, and a `vnc` service under the `debug` profile can attach to it.

**The SPA is built to static files and served by FastAPI itself.** One container, one port, one
process, and **exactly one uvicorn worker with no `--reload`, in every environment.** That is not a
performance note: a second worker means a second Telethon client and a second ingest queue, which
means the same signal traded twice. `--reload` re-executes module import in a new event loop, which
is precisely the `RuntimeError` Telethon raises when its loop changes.

Persistence is SQLite via SQLAlchemy async, schema created with `Base.metadata.create_all` on
startup. No Alembic. Secrets — MT5 password, Telegram session string, provider API key, bot token —
are Fernet-encrypted at rest, keyed by `SECRET_KEY`, and never appear in logs or in any API response;
settings endpoints return `••••••••` with a `has_value: true` flag beside it.

---

## The file tree

One line per file, generated from the actual repository.

### Root

| File | What it is |
|---|---|
| `README.md` | This document. |
| `docker-compose.yml` | The `app` and `mt5-sidecar` services, the named volumes, and the `vnc` service under the `debug` profile. No `version:` key — obsolete in Compose v2. |
| `.env.example` | Template for `.env`; every key documented with its default. Copy it and fill in three MT5 values to start. |
| `.env` | The operator's real environment file. Git-ignored; the only place plaintext secrets are allowed to live. |
| `.gitignore` | Excludes secrets, the venv, build output, the SQLite database, downloaded signal images, Telethon sessions and the Wine prefix. |
| `.dockerignore` | Keeps secrets, `node_modules`, build output and local state out of every image layer. The build context is the repository root, so this covers both stages. |
| `docs/BUILD_PROMPT.md` | The original specification: behaviour, the MT5/Telegram/provider traps, prohibitions, build order, acceptance criteria. |
| `docs/CONTRACTS.md` | The interface contract every module was written against: module names, signatures, ORM columns, event names, tool names, REST routes, JSON shapes. |

### `backend/`

| File | What it is |
|---|---|
| `Dockerfile` | Two-stage: Node builds the SPA, Python runs the backend and copies the bundle to `/app/static`. `CMD` is uvicorn with `--workers 1` and no `--reload`. |
| `pyproject.toml` | Packaging and pytest configuration. Import root is `backend/`, so every module is `app.<pkg>.<mod>`. `MetaTrader5` is deliberately absent from the dependencies. |

**`backend/app/` — application core**

| File | What it is |
|---|---|
| `__init__.py` | Package marker; carries `__version__`. |
| `main.py` | The FastAPI app: lifespan wiring, one error envelope, router mounting, and the built SPA. Every startup step is guarded so one failure leaves a red dot rather than an unreachable dashboard. |
| `config.py` | Every tunable constant in one module, as both `settings.NAME` and a module-level alias. Secrets are reachable only through `settings`, because they can be bootstrapped or rotated at runtime. |
| `runtime.py` | **An addition to the spec's tree** — see the note below. A typed service container (`Runtime`) held on `app.state.runtime`, holding the shared MT5/Telethon/provider/notifier handles, the status map and the arm flag. |
| `db.py` | Async engine, session factory, `create_all` on startup, WAL pragma, and the encrypted settings key-value store. A decrypt failure returns the default and flags the row `needs_reentry` rather than raising. |
| `models.py` | The whole persisted schema as SQLAlchemy 2.0 declarative models: settings, sources, messages, images, runs, run steps, trades, trade events, stopless episodes, notifications, probe cache, system state. |
| `schemas.py` | Pydantic v2 request and response models for every REST route — the authoritative projection of the contract onto JSON. |
| `crypto.py` | Fernet encrypt/decrypt, `SECRET_KEY` bootstrap (generates and writes to `.env` when missing), the `••••••••` mask, and `needs_reentry` handling. |
| `errors.py` | The error hierarchy, the MT5 retcode → human-sentence table, and the HTTP mapping. Imports nothing from `app.*`, so it is the leaf of the dependency graph. |
| `events.py` | In-process event bus: typed envelopes (`{type, ts, seq, data}`) fanned out to every WebSocket subscriber, dropping the oldest event on a full queue rather than blocking. |

**`backend/app/api/` — REST and WebSocket**

| File | What it is |
|---|---|
| `__init__.py` | Router package marker. |
| `auth.py` | `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/status` — the dashboard sign-in, the signed cookie it hands out, and the per-IP throttle behind it. The three routes exempt from the gate, so `status` answers the login question and nothing else. |
| `settings.py` | The masked settings tree and every section that writes to it: MT5, provider, notifier, limits, general. A PUT that echoes the mask leaves the stored secret untouched. |
| `telegram.py` | The headless login flow (code → 2FA), the channel picker, source selection, and image bytes. Every Telethon failure gets its own status and machine-readable code, because "400: error" is not good enough for a login screen. |
| `provider.py` | Test the connection, list models, probe one model's capabilities — and report *which* of the four things went wrong (key rejected / wrong base_url / host unreachable / non-JSON body). |
| `trading.py` | Read-only views, the `/manual/*` operator overrides, arm/disarm and the kill switch. Read routes answer 200 with an `mt5_unavailable` flag when the sidecar is down, never a 500. |
| `history.py` | Closed trades with filters, and the per-source aggregates: win rate, average R, total P/L, per-symbol breakdown. |
| `stream.py` | `GET /api/stream` — the dashboard WebSocket, the `hello` frame carrying status plus recent events, and the account ticker behind it. |

**`backend/app/telegram/` — ingest and notifications**

| File | What it is |
|---|---|
| `__init__.py` | Package marker. |
| `client.py` | Telethon lifecycle bound to uvicorn's loop: connect inside `lifespan`, never `client.start()`, never `run_until_disconnected()`, `get_me()` after connect so updates actually flow. Login, dialog listing, source selection, handler registration, image download, catch-up fetch. |
| `classify.py` | Message kind detection. Branches on media *type* first, because `message.photo` is truthy for link previews and `message.video` is truthy for GIFs and video notes — and a chart "sent as file" arrives as a document and must be processed, not skipped. |
| `ingest.py` | Dedup, persist, enqueue, catch-up. One bounded queue and three workers (`INGEST_WORKERS`). A free worker starts the next post while the others are still deciding or trading. Broker writes stay one at a time. |
| `links.py` | `t.me` permalink construction — `/username/id` for public channels, `/c/<short_id>/id` for private ones, `None` where no per-message web link exists. |
| `notifier.py` | The operator's own bot (HTTP Bot API via httpx, not Telethon): opened / modified / closed / rejected / attention messages, locale-aware, fire-and-forget with retries so a notification failure never delays a trade. |

**`backend/app/ai/` — the provider layer and the agent loop**

| File | What it is |
|---|---|
| `__init__.py` | Package marker. |
| `base.py` | The provider-agnostic types: normalized content blocks, the streaming `Delta` union, the `Provider` protocol, base-URL normalization and `build_provider`. |
| `anthropic_provider.py` | Anthropic Messages API: streaming turns, model listing with capabilities read from the response, adaptive thinking, and thinking blocks echoed back with their signatures untouched. Never sends `temperature`/`top_p`/`top_k`. |
| `openai_provider.py` | Any endpoint serving `/v1/chat/completions` — OpenRouter, Groq, Together, LiteLLM, vLLM, LM Studio, Ollama. Operator-supplied extra headers are merged last. Unparsable `function.arguments` become an error tool result, not a crash. |
| `catalog.py` | Model listing, the one-shot capability probe (`max_tokens: 16`, a real 1×1 PNG, a one-parameter tool) and the database-backed probe cache with operator overrides. |
| `tools.py` | The tool surface offered to the model — the single source of truth for every tool name and schema, in spec order, `finish` last. |
| `prompts.py` | The system prompt and the per-run context builder: the post, its images, its age and staleness flag, its permalink, the parent it replies to, the revision it supersedes, recent signals, and live account state. |
| `loop.py` | The agent loop: one call is one persisted `runs` row with every deliberation and tool call recorded and streamed. Enforces the iteration cap, forces a final tool-less turn on it, and records `incomplete` when `finish` never came. Up to three runs share the queue; a reply chain still waits on its own lock. |
| `triage.py` | Cheap yes/no/uncertain call on the Settings provider before the tool loop. When a channel playbook exists, that text is appended to this prompt only. |

**`backend/app/reviewer/` — channel playbook, separate from the three signal workers**

| File | What it is |
|---|---|
| `worker.py` | One background task (`channel_reviewer`). It does not take an ingest slot. It studies the active source once, not on a timer. |
| `study.py` | Builds a sample from stored messages, or from the last 3–4 days of the Telegram channel when that sample is thin, and asks the Settings provider for a short playbook. |
| `archive.py` | Writes the active brief and, when the source chat changes, moves the previous brief plus study notes into `channel-briefs/backups/`. |
| `brief.py` | The only reader triage may use. It opens `channel-briefs/active/brief.md` and refuses `backups/` and `reviewer/`. |

**`backend/app/trading/` — the money path**

| File | What it is |
|---|---|
| `__init__.py` | Package marker. |
| `constants.py` | Every MT5 integer the backend needs, as literals under its MQL5 name — including the bit-packed timeframes and the two different filling-mode numbering schemes. |
| `rpc.py` | The async JSON-RPC client for the sidecar, and the recursive `dict → SimpleNamespace` conversion that makes the rest of the backend read `si.digits` rather than `si["digits"]`. |
| `symbols.py` | Symbol resolution as a ranked candidate list (exact → suffix-stripped → alias → description), pip conventions with an auditable name for the rule that fired, quotes with a zero-tick retry, broker-clock arithmetic, and `catalog()` — the whole symbol table, filtered and ranked from the cached snapshot, which is what the settings page's symbol picker reads. |
| `pricing.py` | The arithmetic the model must never do: distance conversion, volume from risk (rounding **down**, never up), stop validation, filling-mode choice, pending-type inference, volume splitting by largest remainder. Every function is pure. |
| `executor.py` | The tool implementations that touch the account. Read tools hand the model numbers; write tools run one fixed gauntlet of checks before anything reaches the broker. A broker rejection is a result, never an exception. |
| `guards.py` | The unconditional gates (stop present, stop not removable, correct side, stops level, armed, duplicate signal), the operator's optional limits, and the stopless-position watchdog with its fallback stop. Guards return a refusal string; they never raise. |
| `reconcile.py` | Startup and reconnect recovery: snapshot MT5, match by ticket, recover `run_id` from the `SP:` comment, close rows whose tickets are gone, and leave foreign-magic positions untouched and marked `external`. |

**`backend/app/util/`**

| File | What it is |
|---|---|
| `__init__.py` | Package marker. |
| `auth.py` | The pure-ASGI gate over every HTTP request and every WebSocket, the HMAC-signed session cookie, and the login throttle. Accepts HTTP Basic but never *demands* it: no `WWW-Authenticate` means no native browser prompt, and an unauthenticated navigation gets the SPA shell so the app can render its own login screen. |
| `images.py` | Image preparation for vision input: decode, downscale to the long-edge cap, re-encode as JPEG, and step quality down until it fits the byte cap. Pillow work runs in a thread. |
| `logging.py` | Structured single-line logging with secret redaction applied to every formatted line, including secrets registered at runtime. |

**`backend/tests/`**

| File | What it is |
|---|---|
| `__init__.py` | Test package marker. |
| `conftest.py` | Isolates the run from the operator's real data and network *before* `app.config` is imported: a throwaway `DATA_DIR` and a fixed `SECRET_KEY`. |
| `fake_mt5.py` | An in-process fake of the sidecar's whole RPC surface, with an Exness-flavoured symbol table. Models what actually breaks real code: `10008` vs `10009`, `order_send` returning `None`, filling bitmasks, hedging vs netting. |
| `fake_rpc.py` | A drop-in stand-in for `Mt5Rpc` backed by `FakeMt5`, returning `SimpleNamespace` results exactly like the real client. |
| `test_pricing.py` | The arithmetic that costs money: pip conventions, distance units, risk sizing, rounding direction, stop validation, filling choice, volume splits. |
| `test_symbols.py` | Resolution ranking, broker suffixes, aliases, pip conventions, quotes and zero-tick retry, broker-clock offset. |
| `test_executor.py` | The paths where money moves: the stop-loss invariant in both directions, `10008` success, multi-TP on hedging and netting, the disarmed gate, the kill switch, partial close, duplicate guard, comment ownership. |
| `test_classify.py` | Message classification against real `telethon.tl.types` instances, plus permalink construction. |
| `test_api_smoke.py` | The whole backend booted and driven over HTTP with no MT5 and no Telegram — every dot red, every read route still 200, every secret masked. |
| `test_startup.py` | That the lifespan does not wait for the broker, so the port opens before MT5 does. |
| `test_auth.py` | That the gate holds over routes, writes and the `/api/stream` WebSocket — and that the three deliberate holes in it (the login routes, the bundle, the shell served to a navigation) let nothing else through. |
| `test_run_serialisation.py` | That only one agent run happens at a time process-wide, and that an `sl_watchdog` run is never shown a position-opening tool. |
| `test_rpc_breaker.py` | That an unreachable sidecar is written off after two failures and every later call fails instantly, and that a broker-side refusal never opens the breaker. |
| `test_sidecar.py` | The sidecar's RPC layer tested on Linux with `MetaTrader5` stubbed in `sys.modules`: the method whitelist, single-threaded server, namedtuple and numpy serialisation, constant-time token comparison. |
| `test_ingest.py` | Ingest behaviour: an edit is a revision sharing one `signal_ref`, skipped kinds are recorded but never enqueued, two messages 200 ms apart run strictly in sequence, the replay guard is the unique index, albums store every image. |
| `test_reconcile.py` | Startup recovery: re-link by ticket, `run_id` recovered from the `SP:` comment, foreign magics marked `external` and spared by the kill switch, tickets closed while down reconciled from deal history, and reconcile degrading rather than raising when the sidecar is unreachable. |

### `mt5-sidecar/`

| File | What it is |
|---|---|
| `Dockerfile` | Debian + `winehq-stable` + Xvfb. The Wine prefix, the terminal and Windows Python live on the `/config` volume rather than in an image layer, so the broker's first-login state survives a rebuild. |
| `entrypoint.sh` | Per-start: Xvfb on `:99`; a one-time bootstrap (Wine prefix, 64-bit Windows Python 3.11, `pip install MetaTrader5`, silent MT5 install); ini rendering; terminal launch; the RPC server; and the watchdog that relaunches a dead terminal a bounded number of times. |
| `common.ini.tmpl` | The terminal startup ini with placeholders only. Rendered from env and converted to **UTF-16LE** at boot, because MT5 reads a UTF-8 ini unreliably. Carries `Account=0` / `Profile=0`, which stop MT5 disabling algo trading when the account changes. |
| `rpc_server.py` | The JSON-RPC server, running under Windows Python inside Wine. The only file permitted to `import MetaTrader5`. Stdlib only, single-threaded, explicit method whitelist, token-checked. |

### `frontend/`

| File | What it is |
|---|---|
| `index.html` | The document shell, and the inline pre-paint script that applies the stored theme and locale to `<html>` before any bundle loads — so a reload never flashes light-then-dark or English-then-Russian. |
| `package.json` | Scripts (`dev`, `build`, `preview`, `typecheck`) and the four runtime dependencies: react, react-dom, react-router-dom, plus vite/typescript as dev tooling. |
| `package-lock.json` | The resolved dependency lockfile. |
| `vite.config.ts` | The React plugin, the `@/…` path alias (which must match `tsconfig.json`'s `paths`), and the build output directory. |
| `tsconfig.json` | Strict TypeScript configuration for the app. |
| `tsconfig.node.json` | Covers `vite.config.ts` only; typechecks without `@types/node`, which is deliberately not a dependency. |
| `src/vite-env.d.ts` | The Vite client type reference. |
| `src/main.tsx` | The entry point and nothing else. The three stylesheet imports come first, in token → base → components order. |
| `src/App.tsx` | The router and layout shell: the top bar with the four routes, the three status dots, and the theme and locale toggles. |
| `src/api/client.ts` | The typed fetch wrapper. One exported function per REST route, one exported type per JSON shape, and `ApiError` as the single failure model. |
| `src/api/stream.ts` | The single WebSocket, reconnecting with exponential backoff from 1 s to 30 s and refetching REST state on every (re)connect — the stream is a live tail, never a source of truth. |
| `src/i18n/index.ts` | The i18n runtime: `t(key, vars?)`, locale switching, and the `Intl`-backed number, price, currency and date formatters. A missing key renders the key itself in `--warn`. |
| `src/i18n/en.ts` | The English dictionary — a flat map of dotted keys. Every user-visible string in the dashboard is here. |
| `src/i18n/ru.ts` | The Russian dictionary, typed as `Record<TranslationKey, string>` so a key added to `en.ts` and forgotten here breaks the build instead of the UI. |
| `src/styles/tokens.css` | The only file in the project permitted to contain a colour literal. Both `[data-theme]` blocks plus the theme-independent type, spacing, shape, density and motion tokens. |
| `src/styles/base.css` | The reset, the document surface, the type faces, the focus ring and the scrollbars. Nothing else. |
| `src/styles/components.css` | Reusable primitives only — `.card`, `.btn`, `.badge`, `.table`, `.field`, `.dot`, `.mono` — each reading tokens exclusively. |
| `src/components/ArmSwitch.tsx` | The largest control on the page: ARM / DISARM, with the kill switch beside it behind a typed confirmation. |
| `src/components/StatusDots.tsx` | The three health dots — MT5, Telegram, provider — each carrying its state as a **word** as well as a colour. |
| `src/components/AccountPanel.tsx` | Balance, equity, free margin, open P/L, today's realized P/L, leverage, margin mode as a literal word, open position count. |
| `src/components/SignalFeed.tsx` | The incoming-message column, newest first, skipped messages included; seeds from REST and prepends on `signal.received`. |
| `src/components/SignalCard.tsx` | One message: its text as the channel wrote it, a kind badge, image thumbnails, its permalink, and its outcome. |
| `src/components/RunStream.tsx` | The live-reasoning column: thinking, then each tool call as a card, in arrival order, with finished runs collapsing to a one-line summary. |
| `src/components/ThinkingBlock.tsx` | The model's reasoning, in `--thinking` at 13 px behind a left rule, so a glance separates deliberation from action. |
| `src/components/ToolCallCard.tsx` | One tool invocation: name, status chip, formatted arguments and result, colour-coded by outcome. |
| `src/components/PositionsTable.tsx` | Open positions, one dense row each, with manual close and modify controls; `external` rows visibly read-only. |
| `src/components/OrdersTable.tsx` | Pending orders, the same treatment, with cancel and modify. |
| `src/components/HistoryTable.tsx` | Closed trades with realized P/L, the signal that caused each, the filter bar and the aggregates above them. |
| `src/components/SecretField.tsx` | A password-style input for a secret the backend stores and never returns — renders the mask with its `has_<field>` flag and only sends a value the operator actually retyped. |
| `src/components/ModelPicker.tsx` | The second half of the provider flow: the returned model list, searchable and selectable, with probed capabilities and their operator overrides. |
| `src/components/DialogPicker.tsx` | The Telegram channel list, searchable; picking a row makes it the active signal source. |
| `src/components/ConfirmDialog.tsx` | The only modal shape in the dashboard, including the typed-confirmation variant the kill switch uses. |
| `src/pages/Cockpit.tsx` | The landing page: signal feed, live reasoning, and the account column with the arm switch. |
| `src/pages/Positions.tsx` | Open positions and pending orders, and the data plane behind their tables. |
| `src/pages/History.tsx` | Closed trades and per-source aggregates, with date and symbol filters. |
| `src/pages/Settings.tsx` | The five sections in the order an operator fills them in — Telegram, MT5, provider, notification bot, optional limits — plus general preferences. |

### A deliberate addition to the spec's tree: `backend/app/runtime.py`

The tree in `docs/BUILD_PROMPT.md` has no home for the handles that every part of the system shares —
the MT5 RPC client, the symbol resolver, the executor, the Telethon service, the ingestor, the
notifier, the event bus, the background task table and the status map. They are created once in
`main.py`'s lifespan and needed everywhere.

The two options were an untyped `dict` on `app.state`, or every module reaching back into `main.py`
and importing it circularly. `runtime.py` is the third: a `@dataclass Runtime` held on
`app.state.runtime`, with `get_runtime(request)` as the FastAPI dependency. It is typed, it has one
owner, it has no import cycle, and it is the only addition of its kind. This is recorded in
`docs/CONTRACTS.md` §0 as well.

The other two additions are mechanical and required by the toolchain: `frontend/src/vite-env.d.ts`
and `frontend/tsconfig.node.json`.

---

## How to run it

### From a clean checkout

```bash
git clone <your-remote> lv-copytrader && cd lv-copytrader
cp .env.example .env
```

Edit `.env` and set at minimum:

```
MT5_SERVER=Exness-MT5Trial15
MT5_LOGIN=<your demo account number>
MT5_PASSWORD=<its password>
SIDECAR_TOKEN=<any long random string>
```

Leave `SECRET_KEY` empty; it is generated on first start and written back to `.env`, and the log line
says so. Then:

```bash
docker compose up --build
```

The dashboard is on **http://localhost:8000**.

The first build is slow — the sidecar downloads and installs Wine, a 64-bit Windows Python 3.11 and
the MT5 terminal into the `/config` volume. The app container does **not** wait for it: the port opens
immediately and the dashboard renders with the MT5 dot red, so you can watch the bring-up rather than
stare at a connection refused. That behaviour is asserted by `backend/tests/test_startup.py`.

### First-run checklist, in this order

| # | Step | Where | What you should see |
|---|---|---|---|
| 1 | **MT5 login** | Nothing to do — it comes from `.env` alone | The MT5 dot goes green on its own. Settings → MetaTrader 5 → *Test connection* reports the real balance, account name and margin mode. |
| 2 | **Telegram login** | Settings → Telegram account | Enter `api_id` / `api_hash` from my.telegram.org and your phone in E.164. A code arrives in Telegram; enter it, then your 2FA password if you have one. The Telegram dot goes green. |
| 3 | **Provider setup** | Settings → AI provider | Pick Anthropic or an OpenAI-compatible base URL, paste the key, press *Test & list models*, pick a model. The probe reports vision and tool support. The provider dot goes green. |
| 4 | **Pick the channel** | Settings → Telegram account → channel picker | Your dialogs, searchable. Choosing one makes it the active source. Nothing is ingested before this point — no handler is even registered. Selecting a channel does **not** replay its backlog; the current latest message id becomes the baseline. |
| 5 | **Watch it disarmed** | Cockpit | Signals arrive in the feed, the model reasons in the centre column, tool calls render as cards — and every write tool answers *"System is disarmed — no order was sent"*. Nothing reaches the broker. |
| 6 | **Arm it** | The ARM switch, top right of the Cockpit | Write tools now reach MT5. |

**Run it disarmed for a while, and actually read the reasoning.** This is the single most useful thing
you can do with this system before risking money. Disarmed, the loop runs in full: you see how the
model interprets that particular channel's shorthand, whether it calls `evaluate_entry` before
committing, whether it reads price levels off screenshots correctly, and what it would have done. A
day of that tells you more about whether this channel and this model work together than any amount of
reading the code. It is also the emergency stop — disarming takes one click and leaves open positions
alone.

### Choosing which symbols the AI may trade

Settings → Optional limits carries `symbol_allowlist`, and the list is **imported from the connected
account** rather than typed from memory: `GET /api/trading/symbols` reads the broker's own table —
names with their real suffixes, the broker's Market Watch group, digits, pip size and the convention
that decided it, the live spread, and the smallest lot the broker will accept — so what gets stored is
a name MT5 will actually accept. Search matches the name and the description case-insensitively and
also matches across a broker suffix, so typing `XAUUSD` finds this account's `XAUUSDr`; the same
alias table the model resolves against ranks next, so typing `gold` leads with `XAUUSDr` rather
than with `BTCXAUr`, whose description merely mentions gold. A real Exness
demo carries 312 symbols across 8 groups, well past the 300-row default page, so the response reports
`total` and `truncated` and the picker says *"showing 300 of 312 — refine the search"* rather than
implying the list is complete.

**Selecting nothing is not a restriction.** `symbol_allowlist` is an optional operator limit and it
ships **empty**; an empty list means every symbol is allowed and the limit is not evaluated at all
(BUILD §6). Making it easy to fill in does not make it easy to fill in by accident: the picker prints
the state as a sentence — *"No restriction — the AI may trade any symbol the broker offers"* against
*"Restricted to 3 symbols"* — and nothing in the settings path turns "no entries" into an entry. The
three unconditional gates remain exactly three: `armed`, the stop-loss rule, and the duplicate-signal
guard. When the list *is* populated the match stays suffix-insensitive, so `XAUUSD` on the list still
permits an order on `XAUUSDr`, and a symbol outside it is refused with a message naming the limit.

If MT5 is unreachable the route answers 200 with `mt5_unavailable: true` and no rows, because Settings
is the page where a broken terminal gets fixed and it has to render in order to be used.

### When a broker dialog blocks headless startup

MT5 self-updates and occasionally puts up a modal that nobody can click, and a fresh Wine prefix has
no broker server list at all, so the very first login may need one pass through a GUI:

```bash
docker compose --profile debug up
```

This additionally starts a VNC server on **5901** attached to the sidecar's Xvfb display. Connect with
any VNC client, dismiss the dialog or complete the login by hand, and it is done — the `/config`
volume keeps `Config/servers.dat` and `Config/accounts.dat`, so this happens once, not every restart.

### Exness (and any broker the generic installer does not list)

`MT5_INSTALLER_URL` defaults to the generic MetaQuotes installer:

```
https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe
```

**The MetaQuotes broker directory frequently does not list Exness servers**, so a login against
`Exness-MT5Trial15` fails with the server simply not existing in the terminal's list. The fix is not a
code change: take the **Exness-branded installer URL** from your Exness Personal Area and set

```
MT5_INSTALLER_URL=<the Exness-branded installer URL>
```

It is the same terminal with a bundled server list. The same applies to any other broker whose servers
the generic build does not carry.

---

## Parameters

Every tunable constant lives in `backend/app/config.py` and is overridable from `.env`. Generated from
`app.config.PARAMETERS`.

| Name | Default | What it controls |
|---|---|---|
| `SECRET_KEY` | `None` | Fernet key that encrypts every secret stored in the database; generated on first start and written to `.env` when it is missing. |
| `MT5_SERVER` | `'Exness-MT5Trial15'` | Broker server name the MT5 terminal logs into; seeds the database setting the operator can later change in the UI. |
| `MT5_LOGIN` | `None` | MT5 account number used for the initial login, so the MT5 status dot can go green from `.env` alone without any interactive step. |
| `MT5_PASSWORD` | `''` | Password for that MT5 account; it is rendered into the terminal ini inside the sidecar and is never logged or returned by any endpoint. |
| `MT5_INSTALLER_URL` | `'https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe'` | URL the sidecar downloads the MT5 installer from; point it at the broker-branded installer when the generic one does not list your server. |
| `SIDECAR_URL` | `'http://mt5-sidecar:18812'` | Base URL of the Wine sidecar's JSON-RPC endpoint on the compose network; the port is deliberately not published to the host. |
| `SIDECAR_TOKEN` | `'change-me'` | Shared secret sent as the `X-SP-Token` header on every RPC call and compared with `hmac.compare_digest` by the sidecar. |
| `DATA_DIR` | `'/data'` | Directory holding the SQLite database, downloaded signal images and any Telethon session material; mount it as a volume to survive restarts. |
| `LOG_LEVEL` | `'INFO'` | Logging verbosity for the backend; accepts the standard logging level names and is upper-cased before use. |
| `FRONTEND_DIST` | `'/app/static'` | Directory of the built Vite bundle that FastAPI serves as the dashboard. |
| `RPC_TIMEOUT_SECONDS` | `30.0` | How long a single RPC call to the MT5 sidecar may take before it is treated as a transport failure. |
| `RPC_CONNECT_TIMEOUT_SECONDS` | `10.0` | How long the RPC client waits to establish a connection to the sidecar. |
| `RPC_HEALTH_SECONDS` | `10.0` | Interval between sidecar ping calls that drive the MT5 status dot. |
| `MAGIC_NUMBER` | `770077` | Magic number stamped on every order this system sends; positions carrying a different magic are shown read-only as external and are never touched. |
| `DEVIATION_POINTS` | `20` | Slippage tolerance in points allowed on every market order and market close. |
| `REQUOTE_RETRIES` | `2` | How many times a market order is re-quoted and resent after a requote (10004) or price-changed (10020) retcode before the failure is reported to the model. |
| `SYMBOL_CACHE_SECONDS` | `3600` | How long the cached broker symbol list is reused before it is refreshed. |
| `MAX_SYMBOL_CANDIDATES` | `8` | Maximum number of ranked symbol candidates `resolve_symbol` hands back to the model, which always picks rather than being given one silent guess. |
| `SYMBOL_LIST_LIMIT_DEFAULT` | `300` | How many symbol rows `GET /api/trading/symbols` returns when the caller names no limit; the response reports `truncated` honestly whenever it clips. |
| `SYMBOL_LIST_LIMIT_MAX` | `1000` | Hard ceiling on that route's `limit` parameter, so one request can never ask the backend to serialise an unbounded symbol table. |
| `PIP_OVERRIDES` | `{}` | Per-symbol pip sizes that override the automatic pip convention, as a JSON object such as `{"XAUUSDm": 0.1}`; lets the operator correct any symbol without code changes. |
| `DEFAULT_RISK_PERCENT` | `1.0` | Percentage of balance risked when the model supplies neither a volume nor a risk percent; the value actually used is echoed in every tool result. |
| `MARGIN_HEADROOM` | `0.8` | Fraction of free margin an order may consume, checked with `order_calc_margin` before the order is sent. |
| `FORCE_MARGIN_MODE` | `None` | Overrides the margin mode detected from the account with `"hedging"` or `"netting"` so both multi-TP branches can be tested against one demo account; empty means detect. |
| `MAX_TP_SPLITS` | `3` | Maximum number of take-profit targets a signal may be split across; extra targets are stored on the signal and reported as unmanaged. |
| `SL_WATCHDOG_SECONDS` | `60` | Interval of the watchdog that hunts for open positions with our magic and no stop loss. |
| `SL_FALLBACK_ATTEMPTS` | `3` | How many watchdog cycles the AI is asked to attach a missing stop before the executor stops asking and attaches one itself. |
| `SL_FALLBACK_POINTS` | `500` | Distance in points from entry used for that fallback stop, or the broker's minimum stop distance when that is larger. |
| `INGEST_QUEUE_MAX` | `50` | Bounded depth of the per-source ingest queue; on overflow the oldest message is dropped and a system warning is emitted. |
| `CATCHUP_LIMIT` | `20` | Maximum number of messages fetched after a restart to cover the gap; on the very first run for a source nothing is fetched and the latest id becomes the baseline. |
| `TELEGRAM_FLOOD_SLEEP_THRESHOLD` | `60` | Telethon sleeps through flood waits shorter than this many seconds and raises anything longer so it reaches the dashboard instead of hanging for hours. |
| `INGEST_WORKERS` | `3` | How many ingest workers may run triage and the full agent at once. Broker writes stay one at a time. The channel reviewer does not use one of these slots. |
| `CHANNEL_BRIEF_MAX_LINES` | `60` | Maximum lines kept in the active channel playbook that triage may see. |
| `CHANNEL_BRIEF_HISTORY_DAYS` | `4` | Days of Telegram history fetched when the stored sample for the active source is thin. Clamped to 3–4. |
| `CHANNEL_BRIEF_MIN_MESSAGES` | `12` | Stored posts below this count, or spanning under two days, count as a thin sample. |
| `CHANNEL_REVIEWER_MAX_TOKENS` | `2048` | Output budget for the one-shot channel study. Not the full agent's `MAX_TOKENS`. |
| `MAX_TOOL_ITERATIONS` | `32` | Maximum tool-calling turns in one full agent run; on the cap a final tool-less turn is forced so every run ends with a recorded decision. Not lowered for speed. |
| `MAX_TOKENS` | `32768` | `max_tokens` on every full-agent provider request; Anthropic requires it. Settings `provider.max_tokens` overrides it. Triage uses `TRIAGE_MAX_TOKENS` (256) and does not lower this. |
| `FAST_ENTRY_METAL_MARKET_DISTANCE` | `7.0` | Fallback metal band, in price units, for a ladder of 4 or more targets. Classic gold and silver entries use the operator setting `trading.max_entry_drift_usd` (default 5) instead. Farther than the band that applies, the fast path does not place a pending order. Explicit buy now / sell now with no printed price is still a market open. A printed price outside the band is a miss even when the post says now. A stale post is not chased. Either a far quote or a stale post refuses the open. Does not change `MAX_TOKENS` or `MAX_TOOL_ITERATIONS`. |
| `FAST_ENTRY_DEFAULT_MARKET_PIPS` | `5.0` | How many pips count as at-market for a non-metal fast entry. Metals use `FAST_ENTRY_METAL_MARKET_DISTANCE`. |
| `MAX_IMAGES_PER_RUN` | `4` | Maximum number of images attached to one run's context; when an album is truncated the context states how many were dropped. |
| `STALE_SIGNAL_SECONDS` | `600` | Age (10 minutes), from the earliest chain message that printed the entry, past which a new open is refused and the post is flagged stale. A ladder reply does not reset the clock. Attaching levels to legs already open is not blocked. |
| `CONTEXT_SIGNALS` | `10` | Number of recent messages from the active source included in every run's context. |
| `RECENT_SIGNALS_TOOL_MAX` | `25` | Upper bound on the number of messages the `get_recent_signals` tool may return. |
| `IMAGE_MAX_EDGE` | `1568` | Longest edge in pixels an image is downscaled to before it is sent to the model; lower it to cut token cost, raise it for legibility. |
| `IMAGE_JPEG_QUALITY` | `85` | JPEG quality images are first re-encoded at. |
| `IMAGE_MIN_QUALITY` | `40` | Lowest JPEG quality the re-encode loop will drop to while trying to fit the byte cap. |
| `IMAGE_MAX_BYTES` | `3500000` | Maximum encoded size of a single image; quality is reduced in steps of ten until the image fits. |
| `PROBE_CACHE_HOURS` | `24` | How long a model's probed vision and tool capabilities are cached per base URL and model before it is probed again. |
| `PROVIDER_TIMEOUT_SECONDS` | `180.0` | Timeout for a streaming provider request, generous because thinking models are slow. |
| `NOTIFY_RETRY_DELAYS` | `(1.0, 4.0)` | Delays in seconds between retries of a failed Telegram notification, after which a system warning with reason `notify_failed` is emitted. |
| `NOTIFY_DETECT_SECONDS` | `60` | How long the notifier polls `getUpdates` while detecting the operator's chat id after they press *Test and detect chat*. |
| `NOTIFY_POLL_SECONDS` | `2.0` | Interval between those `getUpdates` polls. |
| `ACCOUNT_TICK_SECONDS` | `3.0` | Interval at which account balance, equity and open profit are refreshed and pushed to the dashboard as `account.tick` events. |
| `EVENT_QUEUE_MAX` | `500` | Depth of each WebSocket subscriber's event queue; the oldest event is dropped when a slow client fills it. |
| `MAX_CANDLES` | `200` | Maximum number of OHLC bars the `get_candles` tool may return in one call. |
| `DB_ECHO` | `False` | Echoes every SQL statement to the log; useful when debugging, noisy otherwise. |

### Design tokens

`frontend/src/styles/tokens.css` is the only file in the project permitted to contain a colour
literal; everything else reads `var(--token)`. The first 26 tokens are declared once on `:root` and
are identical in both themes — `same` in the Light column means the same single declaration, not a
duplicated value.

| Token | Dark | Light | What it controls |
|---|---|---|---|
| `--font-ui` | `Inter, system-ui, -apple-system, "Segoe UI", sans-serif` | same | The interface face: all prose, labels and headings. |
| `--font-mono` | `"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace` | same | Every number — prices, volumes, tickets, P/L — paired with `tabular-nums` so a streaming price does not make the row jitter. |
| `--fs-1` | `12px` | same | Meta text: badges, hints, table headers, field labels. |
| `--fs-2` | `13px` | same | Dense body: table cells, buttons, and the AI's thinking text. |
| `--fs-3` | `14px` | same | The base interface size. |
| `--fs-4` | `16px` | same | Card titles and emphasised numbers. |
| `--fs-5` | `20px` | same | The few headline numbers: equity, open P/L. |
| `--lh` | `1.5` | same | Base line height. |
| `--lh-tight` | `1.25` | same | Headings and single-line numeric readouts. |
| `--sp-1` | `4px` | same | The half-step of the spacing scale: gaps inside a chip or a badge. |
| `--sp-2` | `8px` | same | The base spacing unit: padding inside controls, gaps between related elements. |
| `--sp-3` | `12px` | same | Table cell padding and the gap between a label and its field. |
| `--sp-4` | `16px` | same | Card padding and the gap between cards. |
| `--sp-5` | `24px` | same | Section spacing inside a panel. |
| `--sp-6` | `32px` | same | Spacing between major page sections. |
| `--sp-7` | `48px` | same | Page-level vertical rhythm. |
| `--sp-8` | `64px` | same | The largest gap: empty-state padding, page margins on wide screens. |
| `--radius` | `8px` | same | Surfaces: cards, modals, panels, image thumbnails. |
| `--radius-sm` | `4px` | same | Controls: buttons, inputs, badges, chips. |
| `--border-w` | `1px` | same | The single hairline border width used everywhere; there are no drop shadows on data surfaces. |
| `--row-h` | `28px` | same | Table row height — the cockpit reads dense, so this is a token rather than a guess per table. |
| `--control-h` | `30px` | same | Height of a button, input or select. |
| `--control-h-sm` | `24px` | same | Height of the small button variant. |
| `--dur-fast` | `120ms` | same | Hover and press feedback. |
| `--dur` | `200ms` | same | Panel, modal and disclosure transitions. |
| `--ease` | `cubic-bezier(0.2, 0, 0, 1)` | same | The easing curve on every transition. |
| `--bg` | `#0B0E13` | `#F7F8FA` | The page ground — and the ink colour on any solid `--accent` or `--long` fill, which is why it inverts with the theme. |
| `--surface` | `#141922` | `#FFFFFF` | The first layer above the ground: cards, panels, tables. |
| `--surface-2` | `#1C2230` | `#EEF1F6` | The second layer: table headers, inset panels, tool-call bodies. Depth comes from this against `--surface`, not from shadows. |
| `--surface-3` | `#232B3B` | `#E3E8F0` | One step further: hovered rows, kbd caps, skeletons, inset wells. |
| `--border` | `#2A3242` | `#D8DEE8` | The default hairline between surfaces. |
| `--border-strong` | `#3A4457` | `#BCC5D3` | Borders that must read at a glance: input outlines, the scrollbar thumb, structural dividers. |
| `--text` | `#E6EAF2` | `#10141C` | Primary text. |
| `--text-dim` | `#8B95A8` | `#5C6678` | Secondary text: labels, timestamps, hints, disabled states. |
| `--accent` | `#4C9AFF` | `#1B6FE0` | The interactive colour: links, primary buttons, the focus ring, selected rows. |
| `--accent-dim` | `rgba(76, 154, 255, 0.16)` | `rgba(27, 111, 224, 0.12)` | The accent as a wash: text selection, the selected row, ghost-button hover. |
| `--long` | `#2ECC8F` | `#0F7A57` | The BUY side, profits, healthy state — always printed alongside the word `BUY` or a ▲, never colour alone. |
| `--short` | `#FF5C7A` | `#E0526E` | The SELL side, losses, destructive actions — always alongside `SELL` or a ▼. Split from `--long` in lightness so direction survives greyscale. |
| `--ok` | `var(--long)` | `var(--long)` | The healthy status dot; the same green as long, named for its role so status and direction can diverge later. |
| `--danger` | `var(--short)` | `var(--short)` | Destructive controls, error text and losses; the same red as short, named for its role. |
| `--warn` | `#F5A623` | `#B87400` | Warnings: the stale-signal flag, queue overflow, degraded state, and a missing i18n key. |
| `--thinking` | `#A78BFA` | `#6D4AC7` | The model's reasoning text, visually distinct from tool calls and results so a glance separates deliberation from action. |
| `--overlay` | `rgba(5, 7, 11, 0.66)` | `rgba(16, 20, 28, 0.40)` | The modal scrim — and the only colour any shadow in the project is allowed to use. |
| `--focus` | `rgba(76, 154, 255, 0.45)` | `rgba(27, 111, 224, 0.38)` | The halo outside the `--accent` focus-visible ring. |

Two rules govern edits to the palette, and both are written into the file itself. **Any change to
`--long`, `--short`, `--ok`, `--danger` or `--warn` must be re-checked under a greyscale filter**; if
the swatches converge the edit is wrong however good it looks in colour. And `--short` and `--warn`
sit mid-scale in the light theme, so they are never painted as solid fills — those states use the
outline treatment (coloured text plus a coloured 1 px border) in `components.css`.

---

## Development without Docker

Docker is only needed for the Wine sidecar. Everything on the Linux side runs directly.

```bash
# Python environment (already present at .venv in this checkout)
python3.12 -m venv .venv
./.venv/bin/pip install -e backend[dev]
```

**Run the tests:**

```bash
cd backend && SP_ENV_FILE=/path/to/lv-copytrader/.env \
  /path/to/lv-copytrader/.venv/bin/python -m pytest tests/ -q
```

**Build the SPA:**

```bash
cd frontend
npm install
npx tsc --noEmit      # typecheck
npm run build         # -> frontend/dist, which the Docker image copies to /app/static
npm run dev           # or the Vite dev server against a backend on :8000
```

**Run the backend alone** (it comes up degraded but serving, with the MT5 dot red):

```bash
cd backend && SP_ENV_FILE=/path/to/.env DATA_DIR=/tmp/sp-dev FRONTEND_DIST=../frontend/dist \
  ../.venv/bin/python -m uvicorn app.main:app --port 8000 --workers 1
```

One worker, no `--reload` — here as much as in production. See the Architecture section for why.

### The whole trading layer is testable with no Wine, no broker and no network

`backend/tests/fake_mt5.py` is an in-process fake of the sidecar's entire RPC surface, with an
Exness-flavoured symbol table carrying real broker suffixes, real digit counts, real tick values and
real filling bitmasks. `backend/tests/fake_rpc.py` wraps it in the same public contract as the real
`Mt5Rpc`, returning `SimpleNamespace` objects exactly as the HTTP client does.

Together they mean `symbols.py`, `pricing.py`, `guards.py`, `executor.py` and `reconcile.py` — every
module where money moves — run under test against a broker that deliberately reproduces the behaviour
that breaks real code: a pending order succeeding at `10008` while a market order succeeds at `10009`,
`order_send` returning `None` instead of a result object, the two incompatible filling-mode numbering
schemes, and hedging versus netting semantics on a second order. No sockets are opened and the
`MetaTrader5` package is never imported.

`backend/tests/test_sidecar.py` covers the one file that *does* import it, by installing a stub
`MetaTrader5` in `sys.modules` before loading `mt5-sidecar/rpc_server.py` as written — so its
serialisation, its method whitelist and its constant-time token check are pinned on Linux even though
the file itself only ever runs under Wine.

---

## Limitations

Read these before you arm it.

- **Automating a Telegram user account carries account risk.** Telegram's API terms do not forbid it,
  and `api_id`/`api_hash` are issued for exactly this, but anti-spam heuristics can limit or ban
  accounts on suspicion with no guaranteed appeal. Reading a channel you belong to is the low-risk
  end; do not add bulk messaging to this system.

- **The AI will misread signals.** Vision models misjudge price levels on chart screenshots,
  especially unlabelled ones. `evaluate_entry` and `get_candles` exist to ground it against real
  prices, and the operator should run disarmed for a while and read the reasoning before arming.

- **Redistributing signals from a paid channel may violate its terms.** This tool is for the
  operator's own account.

- **Slippage and latency are real.** Signals arrive late; the AI adds seconds; the broker adds more.
  Executed prices will differ from posted ones. The dashboard reports the difference on every fill.

- **MT5 under Wine is a moving target.** MT5 self-updates and can present a dialog that blocks
  headless startup. `docker compose --profile debug up` exists for exactly this.

- **This is not investment advice and carries real financial risk.** Run it on the demo account
  first.

---

## Current state

**Verified in this environment, by running it:**

- The backend test suite passes: **335 tests**, no failures, no skips, in about 60 seconds.
  It uses no network, no Wine and no MT5 package. By file:
  `test_pricing` 99, `test_symbols` 62, `test_classify` 39, `test_auth` 30, `test_api_smoke` 26,
  `test_symbol_allowlist` 23, `test_executor` 17, `test_ingest` 12, `test_sidecar` 9,
  `test_reconcile` 9, `test_rpc_breaker` 4, `test_run_serialisation` 3, `test_startup` 2.
- The SPA typechecks clean under `npx tsc --noEmit` and builds clean under `npm run build`
  (50 modules, ~480 kB JS / ~18 kB CSS before gzip).

**Not verified, because it could not be:** Docker was not available in the build environment. So
`docker compose up --build` has never been executed, the Wine sidecar has never run against a real
`terminal64.exe`, and no order has ever reached a broker from this code. The live Telegram path
(Telethon login, real channel updates, albums, edits) and the live provider path (a real Anthropic or
OpenAI-compatible endpoint answering a streaming request) have likewise never been exercised
end to end. Everything below the API boundary is covered by fakes; everything at and beyond it is not.

### Acceptance criteria, honestly

| # | Criterion | Status |
|---|---|---|
| 1 | `docker compose up --build` brings up both containers and serves the dashboard; MT5 dot green from `.env` alone | **Verified live.** Both containers healthy, dashboard served, and `GET /api/health` reports `mt5.ok: true` with no interactive step — `/api/trading/account` returns login 474436674, balance 1000.00 USD, hedging. |
| 2 | All three dots green after login, and green again across a restart | **Needs a real broker, Telegram account and provider.** |
| 3 | `resolve_symbol("gold")` returns the broker's actual symbol, suffix, `pip_size`, `pip_convention`, `stops_level_points` | **Verified live** against Exness-MT5Trial15: `XAUUSDr`, `digits 3`, `pip_size 0.10`, `metal_gold_0.10`. The live feed is what proved the original `point * 10` rule wrong — see the live-broker findings above. |
| 4 | EURUSD sizing rounds **down** only, is an exact multiple of `volume_step`, and never risks more than intended | **Covered** by `test_pricing.py`. |
| 5 | A pending order returns `ok: true` at retcode `10008` and appears exactly once | **Covered** by `test_executor.py`. |
| 6 | Three-TP signal splits into three positions on hedging; on netting opens one and names the unmanaged targets | **Covered** by `test_executor.py`, both branches. |
| 7 | `open_market` without `sl` refused; `modify_position` with `sl: 0` refused; no stopless position past the fallback attempts | **Partly covered.** Both refusals are tested in `test_executor.py`, including that a stopless order never reaches the broker, and `test_run_serialisation.py` pins that a watchdog run is never even *shown* a position-opening tool. The fallback-stop cycle itself still has no automated test. |
| 8 | Voice note / video note / sticker produce `skipped` rows and **zero** provider calls; a chart sent as a document produces one | **Covered.** `test_classify.py` pins classification against real Telethon types; `test_ingest.py` pins that skipped rows are recorded but **never enqueued** (queue empty) while a chart sent as an uncompressed document *is* — the mechanical form of "zero provider calls". |
| 9 | Editing a signal creates a revision, triggers one run with the previous revision in context, opens no second position | **Covered.** `test_ingest.py` pins revision 2 of the same `(source_id, message_id)`, both revisions sharing one `signal_ref` (which is what makes the §5.5 duplicate guard stop the second position), and an edit of a *skipped* row being re-classified into its own chain at 1. |
| 10 | Two messages 200 ms apart produce two runs that execute strictly in sequence | **Covered.** `test_ingest.py` holds the worker 400 ms and delivers the second message 200 ms in, creating a real overlap window, then asserts the recorded run intervals do **not** overlap. Mutation-checked: making the worker fire runs as background tasks fails this test. |
| 11 | Killing the backend mid-position reconciles by ticket and magic and leaves foreign-magic positions untouched | **Covered.** `test_reconcile.py` pins re-link by ticket, `run_id` recovery from the `SP:` comment, an unmatched comment still becoming a managed row, a foreign magic marked `external` and surviving the kill switch byte-identical, a ticket closed while down reconciled from deal history with the §2 clock padding asserted, and a vanished pending order becoming `cancelled` not `closed`. |
| 12 | Disarmed, a valid signal streams full reasoning and sends nothing; the kill switch still closes | **Covered** by `test_executor.py`. |
| 13 | Dark + Russian reload with no flash, cold cache, CPU throttled 6× | **Not executed.** Needs a browser. The pre-paint bootstrap is in `index.html` as specified. |
| 14 | OpenRouter's non-standard envelope lists models; a local Ollama runs a text signal end to end | **Not covered.** The provider layer has no automated tests and no network was available. |
| 15 | Every secret absent from logs and from every API response | **Partly covered.** `test_api_smoke.py` asserts the masked tree and that a PUT echoing the mask preserves the stored value. The `docker compose logs` half needs a real run. |

### Defects found by adversarial audit, and fixed

An independent audit measured this build against its own spec rather than reading it. What it
found, and what changed:

| Severity | Defect | Fix |
|---|---|---|
| **Severe** | The SL watchdog called `run_agent` from its own task, so a channel post and a watchdog tick 50 ms apart produced two **overlapping** agent runs — the exact double-entry hazard §3.5 exists to prevent. The ingest queue serialises posts; the watchdog walked around it. `rpc.py`'s write lock was not enough: it orders individual `order_send` calls, but run B reads, decides and writes while run A sits between *its* read and *its* write. | A process-wide agent-run mutex in `ai/loop.py`. `test_run_serialisation.py` reproduces the scenario and asserts non-overlap; removing the mutex fails it with `peak=2`. |
| **Severe** | That watchdog run was handed all 17 tools, including `open_market`. Because it carries no `signal_ref`, the §5.5 duplicate guard returns immediately — so its §6.4 scope ("attach a stop") was enforced by nothing but prose in a prompt. A scripted run opened a position and nothing refused it. | `tools_for_kind("sl_watchdog")` shows it nine tools; `open_market`, `place_pending`, `close_position` and `cancel_order` are not among them. A tool the model is never shown is a tool it cannot call. |
| **High** | `resolve_symbol` answered *"no symbol matches 'US30'"* for tradable symbols sitting in the table. Any symbol whose `digits` is outside `{2,3,4,5}` — indices quoted to one decimal, JP225 and many BTCUSD feeds to zero — had its pip convention raise, and the candidate was dropped. §2's alias table mandates exactly those instruments, and the remedy was buried in a swallowed log. | Such a symbol is now carried with `pip_size: 0.0`, `pip_convention: "unresolved"` and a `pip_note` naming `PIP_OVERRIDES`. Pip-denominated distances refuse loudly; absolute prices, points, quotes and orders work. `distance_units` reports `pips: null` instead of raising. |
| **High** | `PUT /api/settings/mt5` stored a new account and changed nothing at the broker — the sidecar reads `MT5_*` from its environment once, at boot, and exposed no re-login. The UI reported success. | A `relogin` method on the sidecar (whitelisted, serialised with the order methods, never retried, rolling back to the working account if the new one fails) and an apply step on save. A broker-side failure comes back as `mt5.apply_error` rather than a false success. |
| **High** | Found by running the app rather than testing it: with no sidecar reachable, `GET /api/trading/positions` returned nothing for over a minute. The route's DB fallback was correct — it was sitting behind `RPC_TIMEOUT_SECONDS` × three attempts, on *every* request. A dashboard that hangs is worse than one that says "MT5 is down". | A circuit breaker in `trading/rpc.py`: two consecutive transport failures write the sidecar off for 15 s, so every later caller fails in microseconds until a trial call succeeds. A broker-side refusal never opens it — an `ok:false` body means the sidecar answered. `test_rpc_breaker.py`. |
| **Medium** | The test double omitted `price_current` on order rows, so `get_orders()` raised `AttributeError` and **no test in the suite ever called it** — leaving criterion 5's second clause, the live Orders route and reconcile's order branch unexecuted behind a green suite. | The double carries `price_current` and `reason`; `test_executor.py` now drives the Orders path end to end. |

### Found by running it against a live broker

Docker was installed after the build, and the stack was brought up against the real
Exness-MT5Trial15 demo. Six defects surfaced that no amount of testing against fakes could
have found, because every one of them lives exactly where the fakes end:

| Defect | Why the tests could not see it |
|---|---|
| **A metal's pip was `point * 10`.** The live demo quotes `XAUUSDr` at `digits=3`, so gold's pip came out `0.01` instead of `0.10` — **every gold stop ten times tighter than the model asked for**, which is the exact failure §2 warns about. The pip is now anchored to the metal (`metal_gold_0.10`, `metal_silver_0.01`), not to a multiple of `point`. | The fake table quoted gold at `digits=2`, where `point * 10` happens to be right. |
| **`BROKER_SUFFIXES` had no `"r"`.** Exness Raw-Spread accounts quote `XAUUSDr`/`EURUSDr` and nothing with `m`, so `resolve_symbol("gold")` answered *"no symbol matches"* for an instrument sitting in `symbols_get()`. | The fake used `m`, the suffix that was in the list. |
| **`mt5.initialize(path=…)` launches its own terminal** when it cannot attach, and `entrypoint.sh` had already launched one. Two terminals on one data folder do not coexist: one exits `code 0`, the watchdog relaunches, and the pair never settles. The sidecar now waits for the terminal's own log to report an authorisation before the first `initialize()`, and the watchdog stays out until the channel has worked once. | There is no MT5 in the test suite at all — this is the Wine boundary. |
| **`/config:C:/MT5/common.ini` was truncated to `C:\`.** Forward slashes are right for `initialize(path=…)` and wrong on the command line: the terminal applied a bogus "start config", logged in, and exited 0. Backslashes fixed it. | Only a real `terminal64.exe` parses that argument. |
| **Wine refused the prefix**: `cp -a` preserved uid 1000 and the container runs as root. | No container in the suite. |
| **Read routes hung for over a minute** with MT5 down — `RPC_TIMEOUT_SECONDS` × three attempts on *every* request, behind a correct DB fallback. A circuit breaker now writes the sidecar off after two transport failures. | The fakes answer instantly; only a genuinely unreachable host has a timeout. |

One deliberate deviation from the spec came out of the same run. §1 says to refuse when
`terminal_info().trade_allowed` is false. Measured on the live demo, that flag tracks the
**AutoTrading toolbar button**, which cannot be on in a headless terminal with no chart —
while trading works regardless:

```
terminal_info().trade_allowed     = False      <- toolbar
terminal_info().tradeapi_disabled = False      <- the flag that actually returns 10027
account_info().trade_allowed      = True       <- the broker
order_check(0.01 XAUUSDr buy + stop) -> retcode 0, "Done", margin_free 997.68
```

So the gate *was* `tradeapi_disabled` plus the account's own permission, and the toolbar flag
was logged rather than fatal.

**Update (Exness builds):** that measurement does **not** hold once the toolbar is off on a
broker-branded terminal. `order_send` then returns `10027` / "AutoTrading disabled by client"
until Algo Trading is on (`Ctrl+E`). The sidecar now sends that key itself after
`initialize()` (via `xdotool` on `DISPLAY=:99`, **one** press, then a several-second poll
of `trade_allowed`). Helper ack is not success: if a real `order_send` probe is still
10027, it sends one `user32.keybd_event` Ctrl+E (the path that actually works on Exness
Wine) and then one click on the red toolbar button. `order_check` is **not** the probe —
on Exness it can return 10013 while live `order_send` is still 10027. Enable succeeds
only when `trade_allowed` is true or a guarded far pending `order_send` places and is
cancelled. A 10027 `order_send` is retried once after a successful enable. `common.ini`
must stay UTF-16LE or the `[Experts]` flags never apply. See RUNBOOK.

### Fixed: the Python-to-MT5 channel inside the compose sidecar

**Symptom.** `mt5.initialize()` returned `(-10001, 'IPC send failed')` on every rung of the
ladder, forever, but *only* inside the `mt5-sidecar` **compose service**. The same image, same
volume, same env and same script under a plain `docker run` connected 5 times out of 5. The
terminal looked healthy — its own log showed `'474436674': authorized on Exness-MT5Trial15`
and `order_check` on a 0.01 XAUUSDr order returned `retcode 0` — so the failure read as an IPC
problem for a long time.

**Root cause. There was no X server, so there was no terminal.**

`/tmp/.X11-unix` is the `sp-x11` **named volume**, shared with the `debug` profile's `vnc`
service. A named volume persists, and a unix-socket inode persists with it: the `X99` socket
that the *first* container's Xvfb created was still sitting in the volume when every later
container started. `start_xvfb()` tested `[ -S "$socket" ]`, saw the file, logged
`X display :99 is already up`, and **never started Xvfb**. `DISPLAY=:99` then pointed at a dead
inode, and Wine failed at the first window:

```
err:winediag:nodrv_CreateWindow Application tried to create a window, but no driver could be loaded.
err:winediag:nodrv_CreateWindow Make sure that your display server is running and that its variables are set.
```

`terminal64.exe` exited before it ever created its IPC endpoint, so `-10001` did not mean "the
channel broke" — it meant **there was nothing on the other end of the channel at all**. The
`authorized on` log line and the good `order_check` came from the one early run whose Xvfb did
start; they were evidence of a working *past*, not a working present.

Two things kept this disguised as an IPC bug:

* `launch_terminal()` trusted the first `pgrep -f terminal64.exe` hit. `wine <exe>` leaves a
  wrapper process matching that pattern for a moment even when the terminal dies instantly, so
  the log said `terminal64.exe is running` zero seconds after a launch that had already failed.
* A plain `docker run` never mounts `sp-x11`. It gets an empty `/tmp/.X11-unix` from the image
  layer, starts Xvfb normally, and connects — which made the difference look like a property of
  compose (namespaces, `init`, shm size, ulimits) rather than of one extra volume. It is neither
  namespaces nor shared memory: it is that one mount, and it only bites from the **second**
  container onward, which is why the very first `docker compose up` on a fresh volume worked.

Evidence, taken from the failing container itself: `ps` showed `wineserver`, `services.exe`,
`explorer.exe` and `rpc_server.py` but **no `Xvfb` and no `terminal64.exe`**; `/tmp/.X11-unix/X99`
was dated 19:46 the previous day while the container had started at 04:37; and running
`terminal64.exe` in the foreground with `WINEDEBUG=` printed the `nodrv_CreateWindow` pair above.

**The fix.**

* `start_xvfb()` now probes the display with `xdpyinfo` instead of stat-ing the socket, removes a
  stale socket when nothing answers, starts Xvfb, and then waits for the display to *answer*
  rather than for the file to appear. It `die`s if the display never comes up, so the container
  fails loudly instead of running on headless for an hour.
* `x11-utils` (for `xdpyinfo`) was added to the sidecar image.
* `launch_terminal()` re-checks after 8 s and only reports success if the terminal is *still*
  there, so a terminal that exits at window creation can no longer masquerade as a healthy start.
* The `vnc` role waits for a display that answers, for the same reason.
* `docker-compose.yml` documents the hazard on both the mount and the volume.

**Regression watch.** If `-10001` ever returns, check these in order before touching anything
about IPC:

```bash
docker compose exec mt5-sidecar pgrep -a Xvfb          # must print a process
docker compose exec mt5-sidecar xdpyinfo -display :99  # must succeed
docker compose exec mt5-sidecar pgrep -af terminal64   # must show C:\MT5\terminal64.exe
docker compose logs mt5-sidecar | grep -E "Xvfb|X display"
```

The sidecar log must say `Xvfb is up and answering on :99`. If it ever says
`X display :99 is already up and answering`, something else really is serving that display — and
if a start fails with `Xvfb ... never answered`, clear the runtime volume while the stack is down:
`docker compose down && docker volume rm signalpilot_sp-x11`. That volume holds only sockets;
deleting it loses nothing.

The four-rung ladder in `rpc_server.py` stays as it is. With a live display the connection lands
on the `credentials` rung, exactly as it did under `docker run`.

### Which modules have no automated coverage

`ai/loop.py`, `ai/anthropic_provider.py`, `ai/openai_provider.py`, `ai/catalog.py`, `ai/prompts.py`,
`telegram/client.py` and `telegram/notifier.py` are imported and exercised at import time when
`test_api_smoke.py` boots the real application, so an import error or a signature mismatch would
surface — but their behaviour is not tested. The provider layer is the largest gap: it needs a live
endpoint (or a recorded HTTP fixture) to test properly, and neither was available here. Those are the
first places to look if something misbehaves in a live run, and the first places to add tests.

`telegram/ingest.py` and `trading/reconcile.py` **are** now covered behaviourally, by
`test_ingest.py` and `test_reconcile.py`.

### What to do first

Bring it up with Docker on the demo account, complete the first-run checklist, and **leave it
disarmed for a day or more**. Read what the model does with your channel's actual shorthand before
you let it touch an order.

## License

[Apache License 2.0](LICENSE). Chosen over MIT deliberately: this software places real orders on
a real brokerage account, and Apache 2.0 carries dedicated *Disclaimer of Warranty* and
*Limitation of Liability* sections rather than a single sentence, plus an explicit patent grant.

A software licence disclaims warranties about code, not about money — [`NOTICE`](NOTICE) covers
the trading risk, the Telegram account risk, and the paid-channel terms that are not this
licence's to grant. Read it before pointing this at an account.

## ARM64 hosts

For the verified FEX + Wine MT5 deployment on Ubuntu ARM64, see
[deploy/arm64/README.md](deploy/arm64/README.md) and [compose.arm64.yml](compose.arm64.yml).
The backend stays native ARM64; the trading terminal runs locally through FEX.
