# Copyright 2026 Abdulloh (github.com/abdulloh5007)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE at the repository
# root. This software places real orders on a real brokerage account; NOTICE states the
# trading risk the licence itself does not cover.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LV Copytrader — the MT5 sidecar's JSON-RPC server.

This file runs under **Windows** Python 3.11 inside Wine and is the only place in the
whole project allowed to ``import MetaTrader5``. There is no Linux wheel for that package;
the Linux backend reaches the terminal exclusively through the HTTP surface below and
keeps its MT5 integer literals in ``backend/app/trading/constants.py``.

Dependencies: the Python standard library plus ``MetaTrader5``. Nothing else installs
cleanly under Wine and nothing else is needed here.

Wire protocol (BUILD_PROMPT §1)::

    POST /rpc     header X-SP-Token: <SIDECAR_TOKEN>
                  {"method": "<name>", "params": {...}}
      200 -> {"ok": true,  "result": <json>}
      200 -> {"ok": false, "error": {"code": <int>, "message": "<str>", "retcode": <int|null>}}
      401 -> missing or wrong token

    GET  /health  (no token — this is the compose healthcheck)
      200 -> {"ok": true,  "initialized": true, "terminal": true}
      503 -> {"ok": false, "initialized": <bool>, "terminal": <bool>}

Error codes mirror ``RpcError.rpc_code`` on the backend (CONTRACTS.md §9):

    -1  transport failure          (emitted by the backend's client, never by this server)
    -2  authentication failure     (also the body of the 401)
    -3  an mt5 call returned None  (message carries mt5.last_error())
    -4  unknown / not-whitelisted method
    -5  malformed params for a known method
    -6  MT5 is not initialized (or the sidecar is permanently unhealthy)
    -7  the mt5 call raised

Secrets: the MT5 password and the sidecar token are read from the environment, are never
written to a log line (a logging filter masks them even if some future edit passes one in),
and are never part of any response body.
"""

from __future__ import annotations

import calendar
import ctypes
import hmac
import inspect
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

# The one permitted import of the Windows-only package, in the one process that may do it.
import MetaTrader5 as mt5  # noqa: E402  (import order is deliberate: stdlib first, then the broker API)

# ======================================================================================
# Configuration — every value comes from the environment that entrypoint.sh exported.
# ======================================================================================


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else str(value)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


HOST = _env_str("SIDECAR_HOST", "0.0.0.0")
PORT = _env_int("SIDECAR_PORT", 18812)
TOKEN = _env_str("SIDECAR_TOKEN", "")
INSECURE_TOKENS = {"change-me", "changeme", "your-token"}

# Forward slashes, always. Backslashes in mt5.initialize(path=...) are a documented cause of
# initialize() returning False with last_error() == (-10005, 'IPC timeout') — BUILD_PROMPT §1.
MT5_PATH = _env_str("MT5_PATH", "C:/MT5/terminal64.exe").replace("\\", "/")
MT5_CONFIG = _env_str("MT5_CONFIG", "C:/MT5/common.ini").replace("\\", "/")
MT5_LAUNCHER = _env_str("MT5_LAUNCHER", "C:/sp/launch_terminal.cmd").replace("\\", "/")

MT5_LOGIN = _env_int("MT5_LOGIN", 0)
MT5_PASSWORD = _env_str("MT5_PASSWORD", "")
MT5_SERVER = _env_str("MT5_SERVER", "")

# mt5.initialize(timeout=...) is milliseconds. 60 s covers a cold terminal start under Wine.
INIT_TIMEOUT_MS = _env_int("MT5_INIT_TIMEOUT_MS", 60000)
INIT_MAX_ATTEMPTS = _env_int("MT5_INIT_ATTEMPTS", 12)
#: How long to wait for the terminal to authorise before the first initialize() attempt.
TERMINAL_LOGIN_TIMEOUT = _env_float("MT5_LOGIN_TIMEOUT", 120.0)
#: The terminal's install directory as Windows sees it, for reading its own log.
MT5_DIR_WIN = _env_str("MT5_DIR_WIN", "C:\\MT5")
INIT_BACKOFF_START = _env_float("MT5_INIT_BACKOFF_START", 2.0)
INIT_BACKOFF_MAX = _env_float("MT5_INIT_BACKOFF_MAX", 30.0)

WATCHDOG_SECONDS = _env_float("WATCHDOG_SECONDS", 15.0)
WATCHDOG_MAX_RESTARTS = _env_int("WATCHDOG_MAX_RESTARTS", 5)
WATCHDOG_RESTART_WINDOW = _env_float("WATCHDOG_RESTART_WINDOW", 600.0)  # ten minutes
# How long a relaunched terminal is given to answer terminal_info() before the tick gives up.
RELAUNCH_WAIT_SECONDS = _env_float("MT5_RELAUNCH_WAIT_SECONDS", 120.0)

#: TRADE_RETCODE_CLIENT_DISABLES_AT — "AutoTrading disabled by client".
CLIENT_DISABLES_AT = 10027
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_ACTION_PENDING = 5
TRADE_ACTION_REMOVE = 8
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_STOP = 5
ORDER_FILLING_RETURN = 2
ORDER_TIME_GTC = 0
SYMBOL_TRADE_MODE_FULL = 4
#: Distinct from the backend MAGIC_NUMBER (770077) so a leftover probe is never reconciled.
ALGO_PROBE_MAGIC = 770070
ALGO_PROBE_COMMENT = "algo-probe"
#: After one Ctrl+E, poll trade_allowed this long before deciding the toggle failed.
#: The toolbar flag lags the keypress; a second Ctrl+E in that window turns it back OFF.
ALGO_SETTLE_SECONDS = _env_float("ALGO_SETTLE_SECONDS", 10.0)
#: Sleep between trade_allowed polls during settle.
ALGO_POLL_INTERVAL = _env_float("ALGO_POLL_INTERVAL", 0.5)
#: Never send another Ctrl+E inside this window unless trade_allowed was observed
#: True and then became False (a real disable, not "still false after the first send").
ALGO_TOGGLE_COOLDOWN = _env_float("ALGO_TOGGLE_COOLDOWN", 120.0)
#: Linux helper that entrypoint.sh started; writes enable_algo.ack after one Ctrl+E.
ALGO_FLAG_WAIT = _env_float("ALGO_FLAG_WAIT", 4.0)

MAX_BODY_BYTES = _env_int("SIDECAR_MAX_BODY_BYTES", 1024 * 1024)
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()

# history_deals_get is padded by one day: broker-server time runs UTC+2/+3 and a naive
# datetime is treated as UTC by the package, so an un-padded date_to silently drops the
# deals that were just made (BUILD_PROMPT §2).
HISTORY_PAD_SECONDS = 86400.0

# order_send / order_check are the two methods whose real None must reach the backend as a
# documented value rather than an error: the package returns None on a transport or
# validation failure before the server is reached, and executor.py null-checks for exactly
# that (BUILD_PROMPT §1.3). Every other method returning None is an error (-3).
NULLABLE_RESULT_METHODS = frozenset({"order_send", "order_check"})

# mt5.last_error() codes that mean "the IPC channel to terminal64.exe is gone". Seeing one
# wakes the watchdog immediately instead of waiting out WATCHDOG_SECONDS.
IPC_ERROR_CODES = frozenset({-10000, -10001, -10002, -10003, -10004, -10005})


# ======================================================================================
# Logging — a filter masks the password and the token even if a future edit logs one.
# ======================================================================================


class _SecretFilter(logging.Filter):
    """Replace every registered secret literal with ``***`` in the formatted message."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._known: set[str] = set()
        self._secrets: tuple[str, ...] = ()
        self.add(*secrets)

    def add(self, *secrets: str) -> None:
        """Register more secrets at runtime -- a relogin brings a new password with it."""
        self._known.update(s for s in secrets if s and len(s) >= 4)
        # Longest first, so a secret containing another is masked before its substring.
        self._secrets = tuple(sorted(self._known, key=len, reverse=True))

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record must not kill the server
            return True
        masked = message
        for secret in self._secrets:
            if secret in masked:
                masked = masked.replace(secret, "***")
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


#: The live redaction filter, so a password supplied after boot is masked too.
_SECRET_FILTER: "_SecretFilter | None" = None


def _register_secret(value: str) -> None:
    """Mask a secret that arrived after start-up (a relogin password)."""
    if _SECRET_FILTER is not None and value:
        _SECRET_FILTER.add(value)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(fmt="%(asctime)s %(levelname)-7s sidecar %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    )
    handler.formatter.converter = time.gmtime  # type: ignore[union-attr]
    global _SECRET_FILTER
    _SECRET_FILTER = _SecretFilter((TOKEN, MT5_PASSWORD))
    handler.addFilter(_SECRET_FILTER)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    return logging.getLogger("rpc")


log = _setup_logging()


# ======================================================================================
# The IPC lock.
#
# BUILD_PROMPT §1: the MetaTrader5 package talks to terminal64.exe over a single IPC
# channel that is NOT thread-safe. That is why the server below is http.server.HTTPServer
# and NOT ThreadingHTTPServer — the single request thread IS the serialisation of that
# channel, and it costs nothing to get right.
#
# The watchdog is the only other thread that touches mt5, so one mutex closes the gap.
# The watchdog never blocks on it: if it cannot take the lock quickly, a call is in flight,
# which is itself proof the channel is alive.
# ======================================================================================

MT5_LOCK = threading.RLock()


class TerminalTradeDisabled(RuntimeError):
    """The terminal answered but will refuse to trade — the 10027 condition.

    A subclass of RuntimeError so BUILD_PROMPT §1's "raise RuntimeError" holds, but a
    distinguishable one: the IPC channel is usable for reads, so the sidecar keeps serving
    and reports the condition through /health instead of pretending MT5 is absent.
    """


class _State:
    """Health facts the HTTP thread reads and the watchdog thread writes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.initialized = False
        self.terminal = False
        self.trade_ready = False
        self.unhealthy = False  # permanent: the restart budget is spent
        # Latched once the IPC channel has been established at least once. Until then the
        # watchdog must not relaunch: the terminal is up but not yet servable, and a second
        # instance on the same data folder makes MT5 shut one down.
        self.ever_initialized = False
        self.reason = "starting"
        self.restarts: deque[float] = deque()
        self.init_failures = 0
        self.wake = threading.Event()

    def set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ok = self.initialized and self.terminal and self.trade_ready and not self.unhealthy
            return {
                "ok": bool(ok),
                "initialized": bool(self.initialized),
                "terminal": bool(self.terminal),
                "trade_ready": bool(self.trade_ready),
                "unhealthy": bool(self.unhealthy),
                "init_failures": int(self.init_failures),
                "reason": str(self.reason),
            }

    def readiness(self) -> tuple[bool, str]:
        """Can an RPC call touch MT5 right now?"""
        with self._lock:
            if self.unhealthy:
                return False, f"sidecar is permanently unhealthy: {self.reason}"
            if not self.initialized:
                return False, f"MT5 is not initialized: {self.reason}"
            return True, ""

    def note_ipc_failure(self) -> None:
        """A call came back with an IPC-level error: run the next watchdog tick now."""
        self.wake.set()

    def restart_budget_left(self) -> int:
        now = time.time()
        with self._lock:
            while self.restarts and now - self.restarts[0] > WATCHDOG_RESTART_WINDOW:
                self.restarts.popleft()
            return max(0, WATCHDOG_MAX_RESTARTS - len(self.restarts))

    def record_restart(self) -> int:
        now = time.time()
        with self._lock:
            self.restarts.append(now)
            return len(self.restarts)


STATE = _State()
STOP = threading.Event()
# Popen handles of relaunched terminals, kept referenced so they are not garbage collected
# mid-start; the terminal outlives this process either way.
_LAUNCHED: list[subprocess.Popen] = []


# ======================================================================================
# JSON serialisation of MetaTrader5 results.
# ======================================================================================


def _datetime_to_unix(value: datetime) -> float:
    """Convert an MT5 datetime to a unix float.

    The package builds naive datetimes with ``utcfromtimestamp``, so ``calendar.timegm`` is
    its exact inverse. ``datetime.timestamp()`` on a naive value would apply the container's
    local zone instead and shift every history timestamp.
    """
    if value.tzinfo is not None:
        return float(value.timestamp())
    return float(calendar.timegm(value.timetuple())) + value.microsecond / 1_000_000.0


def _to_jsonable(value: Any, depth: int = 0) -> Any:
    """Turn any MetaTrader5 result into JSON.

    namedtuple-ish objects (every MT5 structure) convert with ``._asdict()``; a tuple of
    them becomes a list of dicts; the numpy arrays returned by ``copy_rates_*`` convert with
    ``.astype(object).tolist()``; datetimes become unix floats; anything unknown falls back
    to ``repr`` so a result can never fail to serialise.
    """
    if depth > 12:
        return repr(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not JSON; a non-finite price is meaningless to the backend anyway.
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return _datetime_to_unix(value)

    as_dict = getattr(value, "_asdict", None)
    if callable(as_dict):
        try:
            mapping = as_dict()
        except Exception:  # pragma: no cover - defensive
            mapping = None
        if isinstance(mapping, dict):
            return {str(k): _to_jsonable(v, depth + 1) for k, v in mapping.items()}

    # numpy without importing numpy: copy_rates_* returns a structured ndarray, and single
    # fields come back as numpy scalars. Both are duck-typed here so the sidecar's only
    # third-party import stays MetaTrader5.
    if getattr(value, "dtype", None) is not None and hasattr(value, "tolist"):
        if getattr(value, "shape", ()) == ():
            return _to_jsonable(value.item(), depth + 1)
        astype = getattr(value, "astype", None)
        if callable(astype):
            return _to_jsonable(astype(object).tolist(), depth + 1)
        return _to_jsonable(value.tolist(), depth + 1)

    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(item, depth + 1) for item in value]
    return repr(value)


def _last_error() -> tuple[int, str]:
    """mt5.last_error() as (code, description), never raising."""
    try:
        err = mt5.last_error()
    except Exception as exc:  # pragma: no cover - defensive
        return 0, f"last_error() unavailable: {exc}"
    if isinstance(err, (list, tuple)) and len(err) >= 2:
        try:
            return int(err[0]), str(err[1])
        except (TypeError, ValueError):
            return 0, str(err)
    return 0, str(err)


# ======================================================================================
# Algo Trading / AutoTrading toolbar.
#
# On Exness (and other broker-branded) builds, common.ini `[Experts] AllowLiveTrading=1`
# is not enough: order_send returns TRADE_RETCODE_CLIENT_DISABLES_AT (10027) with
# "AutoTrading disabled by client" until the toolbar Algo Trading toggle is on.
# terminal_info().trade_allowed tracks that toggle. Ctrl+E (or clicking the button) turns
# it on; a second Ctrl+E turns it off again.
#
# Production failure we hit: sending Ctrl+E, seeing trade_allowed still false 0.8s later,
# and sending again — which flipped the toolbar OFF. The flag lags the keypress by
# several seconds. So: at most one Ctrl+E, then poll for ALGO_SETTLE_SECONDS. A second
# press is allowed only after we observed trade_allowed True and then False (a real
# disable), or after the cooldown on the 10027 path if a *real* order_send probe is
# still 10027. The watchdog observes; it does not spam-toggle while the flag is catching up.
#
# Production failure after that: a dry order_check returning 10013 (INVALID) was treated
# as "AT is on". On Exness Wine builds order_check does NOT fail with 10027 when the
# toolbar is off — live order_send still returns 10027. Effectiveness is therefore
# trade_allowed == True, or a guarded far pending order_send that actually places
# (10008/10009) and is cancelled immediately. order_check is never the probe.
#
# Ctrl+E via xdotool often never arrives: Wine does not implement _NET_ACTIVE_WINDOW.
# Production: linux helper Ctrl+E *and* a toolbar click left AutoTrading off (order_send
# still 10027), while user32.keybd_event Ctrl+E turned it on. Helper ack ≠ effective.
# So: one helper Ctrl+E, poll + real probe; if still 10027, one user32 Ctrl+E (even when
# enable_algo.ready exists); then one toolbar click. Never two of the same toggle.
#
# rpc_server.py runs under Windows Python inside Wine, so it cannot exec Linux xdotool
# directly. entrypoint.sh starts enable_algo_trading.sh --watch, which sends one Ctrl+E
# or one toolbar click per C:/sp/enable_algo.flag.
# ======================================================================================

#: Unix time of the last toggle we actually sent (Ctrl+E or toolbar click). 0 means never.
_ALGO_LAST_SENT_AT = 0.0
#: True once terminal_info().trade_allowed was True after ``_ALGO_LAST_SENT_AT``.
_ALGO_SAW_ALLOWED_SINCE_SEND = False
#: True when a guarded order_send probe actually placed (then cancelled), or the
#: toolbar flag was observed True. Never set from order_check.
_ALGO_PROBE_OK = False
#: True once we have tried the toolbar-click fallback after Ctrl+E in this cycle.
_ALGO_CLICK_TRIED = False
#: True once we have tried user32.keybd_event Ctrl+E in this cycle.
_ALGO_USER32_TRIED = False
#: Tests replace this to observe toggles without touching X11.
_SEND_CTRL_E: Callable[[], bool] | None = None
#: Tests replace this to observe toolbar clicks without touching X11.
_SEND_TOOLBAR_CLICK: Callable[[], bool] | None = None
#: Tests replace this to observe the user32 fallback without ctypes.
_SEND_USER32_CTRL_E: Callable[[], bool] | None = None


def _sp_dir() -> str:
    """Directory that holds rpc_server.py — C:/sp inside the prefix, the flag-file home."""
    return os.path.dirname(os.path.abspath(__file__))


def _toolbar_off(info: Any) -> bool:
    """True when the AutoTrading toolbar toggle is off (the Exness 10027 condition)."""
    if info is None:
        return True
    return not bool(getattr(info, "trade_allowed", False))


def _trade_ready_from(info: Any, account: Any) -> bool:
    """Whether order_send is expected to be accepted, not 10027 / broker-blocked."""
    if info is None:
        return False
    if bool(getattr(info, "tradeapi_disabled", False)):
        return False
    if account is not None and not bool(getattr(account, "trade_allowed", True)):
        return False
    if bool(getattr(info, "trade_allowed", False)):
        return True
    # Toolbar flag can lag a successful enable; only a *real* order_send probe
    # that placed (and was cancelled) is allowed to stand in for the flag.
    # order_check 10013 is a false positive on Exness Wine builds.
    return bool(_ALGO_PROBE_OK)


def _is_client_disables_at(result: Any) -> bool:
    """True when an order_* result is TRADE_RETCODE_CLIENT_DISABLES_AT (10027)."""
    if result is None:
        return False
    retcode = getattr(result, "retcode", None)
    try:
        if int(retcode) == CLIENT_DISABLES_AT:
            return True
    except (TypeError, ValueError):
        pass
    comment = str(getattr(result, "comment", "") or "").lower()
    return "autotrading disabled" in comment


def _assert_common_ini_utf16le() -> None:
    """Log if common.ini does not look like UTF-16LE — Experts flags would be ignored."""
    candidates = (
        MT5_CONFIG,
        MT5_CONFIG.replace("/", "\\"),
        os.path.join("C:\\MT5", "common.ini"),
    )
    raw = b""
    path_used = ""
    for path in candidates:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(8)
            path_used = path
            break
        except OSError:
            continue
    if not raw:
        log.debug("common.ini not readable yet; skipping UTF-16LE check")
        return
    if raw.startswith(b"\xff\xfe"):
        log.info("common.ini encoding=UTF-16LE (BOM) path=%s", path_used)
        return
    if len(raw) >= 4 and raw[1] == 0 and raw[0] != 0:
        log.info("common.ini encoding=UTF-16LE path=%s", path_used)
        return
    if raw.startswith(b"[") or raw.startswith(b";") or raw.startswith(b"\xef\xbb"):
        log.error(
            "common.ini is not UTF-16LE (looks like UTF-8) path=%s — [Experts] flags will "
            "be ignored and order_send will return 10027",
            path_used,
        )
        return
    log.warning("common.ini encoding could not be confirmed path=%s head=%r", path_used, raw[:4])


def _request_linux_helper(action: str = "ctrl+e", timeout: float | None = None) -> bool:
    """Ask the Linux xdotool watcher to run ``action`` (ctrl+e or click). Returns True if it acked."""
    wait_for = ALGO_FLAG_WAIT if timeout is None else float(timeout)
    directory = _sp_dir()
    ready = os.path.join(directory, "enable_algo.ready")
    if not os.path.exists(ready):
        return False
    flag = os.path.join(directory, "enable_algo.flag")
    ack = os.path.join(directory, "enable_algo.ack")
    before = 0.0
    try:
        before = os.path.getmtime(ack)
    except OSError:
        before = 0.0
    body = "click" if action == "click" else "ctrl+e"
    try:
        with open(flag, "w", encoding="ascii") as handle:
            handle.write(body)
    except OSError as exc:
        log.warning("could not write algo-enable flag: %s", exc)
        return False
    deadline = time.time() + wait_for
    while time.time() < deadline:
        try:
            if os.path.getmtime(ack) > before:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    log.warning("linux xdotool helper did not ack %s within %.1fs", body, wait_for)
    return False


def _request_linux_ctrl_e(timeout: float | None = None) -> bool:
    """Ask the Linux xdotool watcher to send one Ctrl+E. Returns True if it acked."""
    return _request_linux_helper("ctrl+e", timeout=timeout)


def _user32_ctrl_e() -> bool:
    """Send Ctrl+E through Wine's user32 (keybd_event after focusing the MT5 window)."""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    except AttributeError:
        return False

    hwnd = _find_mt5_hwnd(user32)
    if hwnd:
        try:
            user32.SetForegroundWindow(hwnd)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        except Exception:  # pragma: no cover - Wine window manager can refuse
            pass
        time.sleep(0.15)

    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_E = 0x45
    try:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_E, 0, 0, 0)
        user32.keybd_event(VK_E, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    except Exception as exc:
        log.warning("user32 keybd_event Ctrl+E failed: %s", exc)
        return False
    log.info("algo-enable: sent Ctrl+E via user32 keybd_event")
    return True


def _find_mt5_hwnd(user32: Any) -> int:
    """Return the first visible window whose title/class looks like MetaTrader, or 0."""
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = int(user32.GetWindowTextLengthW(hwnd))
            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            blob = f"{title_buf.value} {class_buf.value}".lower()
        except Exception:
            return True
        if any(token in blob for token in ("metatrader", "metaquotes", "terminal64", "exness")):
            found.append(int(hwnd))
            return False
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_callback), 0)
    except Exception:
        return 0
    return found[0] if found else 0


def _send_one_ctrl_e() -> bool:
    """Send exactly one Ctrl+E via the Linux helper if it is up, else user32.

    Do not fall through to user32 in *this* call after a helper ack or a missed
    ack — if the helper actually delivered the key, a second press would flip
    AutoTrading off. user32 is the *next* step, after a real order_send probe
    shows 10027 (ack ≠ effective). See ``_try_user32_ctrl_e``.
    """
    global _ALGO_USER32_TRIED
    if _SEND_CTRL_E is not None:
        return bool(_SEND_CTRL_E())
    ready = os.path.join(_sp_dir(), "enable_algo.ready")
    if os.path.exists(ready):
        acked = _request_linux_ctrl_e()
        if not acked:
            log.warning(
                "algo-enable: linux xdotool helper did not ack — not sending user32 in "
                "the same step (will after a 10027 probe if the key never landed)"
            )
        return acked
    sent = _user32_ctrl_e()
    if sent:
        _ALGO_USER32_TRIED = True
        return True
    log.warning("algo-enable: neither xdotool helper nor user32 could send Ctrl+E")
    return False


def _send_toolbar_click() -> bool:
    """Click the red Algo Trading toolbar control once via the Linux helper.

    Never falls through to Ctrl+E or user32 — that would be a second toggle.
    """
    if _SEND_TOOLBAR_CLICK is not None:
        return bool(_SEND_TOOLBAR_CLICK())
    ready = os.path.join(_sp_dir(), "enable_algo.ready")
    if os.path.exists(ready):
        acked = _request_linux_helper("click")
        if not acked:
            log.warning("algo-enable: linux helper did not ack toolbar click")
        return acked
    log.warning("algo-enable: linux helper is not running; cannot click Algo Trading")
    return False


def _reset_algo_state() -> None:
    """Clear toggle bookkeeping. Call when the terminal process itself is replaced."""
    global _ALGO_LAST_SENT_AT, _ALGO_SAW_ALLOWED_SINCE_SEND, _ALGO_PROBE_OK
    global _ALGO_CLICK_TRIED, _ALGO_USER32_TRIED
    _ALGO_LAST_SENT_AT = 0.0
    _ALGO_SAW_ALLOWED_SINCE_SEND = False
    _ALGO_PROBE_OK = False
    _ALGO_CLICK_TRIED = False
    _ALGO_USER32_TRIED = False


def _read_trade_allowed() -> tuple[Any, bool]:
    """Return (terminal_info, trade_allowed). info may be None."""
    try:
        info = mt5.terminal_info()
    except Exception as exc:
        log.warning("terminal_info() raised while checking Algo Trading: %s", exc)
        return None, False
    return info, not _toolbar_off(info)


def _observe_trade_allowed(allowed: bool) -> None:
    """Record a trade_allowed sample. Seeing True after a send is what unlocks a later resend."""
    global _ALGO_SAW_ALLOWED_SINCE_SEND, _ALGO_PROBE_OK
    if allowed:
        _ALGO_SAW_ALLOWED_SINCE_SEND = True
        _ALGO_PROBE_OK = True


def _ctrl_e_allowed(currently_off: bool, *, reason: str = "") -> bool:
    """May we send Ctrl+E? One shot per cooldown unless we saw ON then OFF.

    The watchdog never sends a second press just because the flag is still false
    or the cooldown elapsed — that is how the production toggle flip-flopped.
    A later ``order_send`` 10027 is the path that may retry after cooldown.
    """
    if not currently_off:
        return False
    if _ALGO_PROBE_OK and not _ALGO_SAW_ALLOWED_SINCE_SEND:
        # A real order_send probe placed (or the flag was True). Another Ctrl+E would
        # flip a working terminal OFF.
        return False
    if _ALGO_LAST_SENT_AT <= 0:
        return True
    if _ALGO_SAW_ALLOWED_SINCE_SEND:
        # Observed True after the last send; now it is False — a real disable.
        return True
    if reason == "watchdog":
        return False
    return (time.time() - _ALGO_LAST_SENT_AT) >= max(0.0, ALGO_TOGGLE_COOLDOWN)


def _probe_placed(result: Any) -> bool:
    """True when order_send actually accepted the request (pending placed or deal done)."""
    if result is None:
        return False
    try:
        return int(getattr(result, "retcode", -1)) in (
            TRADE_RETCODE_PLACED,
            TRADE_RETCODE_DONE,
            TRADE_RETCODE_DONE_PARTIAL,
        )
    except (TypeError, ValueError):
        return False


def _far_buy_limit_price(bid: float, point: float, digits: int, stops_level: int) -> float:
    """A BUY LIMIT well below market so the probe cannot fill."""
    point = point if point and point > 0 else 0.00001
    digits = int(digits) if digits else 5
    min_dist = max(int(stops_level or 0), 50) * point
    candidate = bid * 0.5
    floor = bid - max(min_dist * 20, abs(bid) * 0.2)
    far = min(candidate, floor) if bid > 0 else point * 10
    if far <= 0:
        far = point * 10
    if bid - far < min_dist:
        far = max(point, bid - min_dist * 20)
    return round(far, digits)


def _pick_probe_symbol() -> tuple[str, float, float, float] | None:
    """Pick a tradable symbol and a far BUY LIMIT price. None if the terminal has none."""
    try:
        symbols = mt5.symbols_get()
    except Exception as exc:
        log.debug("algo-enable: symbols_get raised: %s", exc)
        symbols = None
    names: list[str] = []
    if symbols:
        for item in symbols:
            name = str(getattr(item, "name", "") or "")
            if name:
                names.append(name)
    preferred = [n for n in names if any(tok in n.upper() for tok in ("XAU", "EURUSD", "GBPUSD", "USDJPY"))]
    ordered = preferred + [n for n in names if n not in preferred]
    if not ordered:
        ordered = ["XAUUSDm", "EURUSDm", "EURUSD"]
    for name in ordered:
        try:
            mt5.symbol_select(name, True)
        except Exception:
            pass
        try:
            info = mt5.symbol_info(name)
            tick = mt5.symbol_info_tick(name)
        except Exception:
            continue
        if info is None or tick is None:
            continue
        mode = int(getattr(info, "trade_mode", SYMBOL_TRADE_MODE_FULL) or SYMBOL_TRADE_MODE_FULL)
        if mode not in (SYMBOL_TRADE_MODE_FULL, 1, 2):  # FULL / long-only / short-only
            continue
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 and ask <= 0:
            continue
        if bid <= 0:
            bid = ask
        volume = float(getattr(info, "volume_min", 0.01) or 0.01)
        point = float(getattr(info, "point", 0.0) or 0.0)
        digits = int(getattr(info, "digits", 5) or 5)
        stops = int(getattr(info, "trade_stops_level", 0) or 0)
        price = _far_buy_limit_price(bid, point, digits, stops)
        return name, volume, price, ask
    return None


def _cancel_probe_order(ticket: int) -> None:
    try:
        result = mt5.order_send({"action": TRADE_ACTION_REMOVE, "order": int(ticket)})
    except Exception as exc:
        log.warning("algo-enable: cancel probe ticket=%s raised: %s", ticket, exc)
        return
    log.info(
        "algo-enable: cancelled probe ticket=%s retcode=%s",
        ticket,
        getattr(result, "retcode", None),
    )


def _cancel_leftover_probes() -> None:
    try:
        orders = mt5.orders_get() or ()
    except Exception:
        return
    for order in orders:
        try:
            if int(getattr(order, "magic", 0) or 0) != ALGO_PROBE_MAGIC:
                continue
        except (TypeError, ValueError):
            continue
        ticket = int(getattr(order, "ticket", 0) or 0)
        if ticket:
            _cancel_probe_order(ticket)


def _probe_algo_api() -> bool:
    """Guarded far pending order_send. Success only if it places, then we cancel.

    order_check is a false positive on Exness Wine builds (10013 while live
    order_send is still 10027). 10027 here means AutoTrading is still off.
    Any other retcode is inconclusive — not success.
    """
    global _ALGO_PROBE_OK
    spec = _pick_probe_symbol()
    if spec is None:
        log.warning("algo-enable: no tradable symbol for order_send probe")
        return False
    symbol, volume, price, _ask = spec
    request = {
        "action": TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": volume,
        "type": ORDER_TYPE_BUY_LIMIT,
        "price": price,
        "magic": ALGO_PROBE_MAGIC,
        "comment": ALGO_PROBE_COMMENT,
        "type_filling": ORDER_FILLING_RETURN,
        "type_time": ORDER_TIME_GTC,
    }
    try:
        result = mt5.order_send(request)
    except Exception as exc:
        log.warning("algo-enable: order_send probe raised: %s", exc)
        return False
    if result is None:
        log.info("algo-enable: order_send probe returned None")
        return False
    retcode = getattr(result, "retcode", None)
    comment = getattr(result, "comment", "")
    if _is_client_disables_at(result):
        _ALGO_PROBE_OK = False
        log.info(
            "algo-enable: order_send probe retcode=%s — AutoTrading still blocked",
            retcode,
        )
        return False
    if _probe_placed(result):
        ticket = 0
        try:
            ticket = int(getattr(result, "order", 0) or 0)
        except (TypeError, ValueError):
            ticket = 0
        log.info(
            "algo-enable: order_send probe PLACED retcode=%s ticket=%s symbol=%s — cancelling",
            retcode,
            ticket,
            symbol,
        )
        if ticket:
            _cancel_probe_order(ticket)
        else:
            _cancel_leftover_probes()
        _ALGO_PROBE_OK = True
        return True
    log.info(
        "algo-enable: order_send probe retcode=%s comment=%s — inconclusive "
        "(not treating as enabled; order_check-style false positives are ignored)",
        retcode,
        comment,
    )
    return False


def _poll_until_allowed(timeout: float, *, probe: bool = True) -> bool:
    """Poll trade_allowed for `timeout` seconds. Optionally finish with a real order_send probe."""
    deadline = time.time() + max(0.0, float(timeout))
    interval = max(0.0, ALGO_POLL_INTERVAL)
    while True:
        _info, allowed = _read_trade_allowed()
        _observe_trade_allowed(allowed)
        if allowed:
            log.info("algo-enable: trade_allowed=true")
            return True
        if time.time() >= deadline:
            if probe and _probe_algo_api():
                return True
            return False
        STOP.wait(interval)
        if STOP.is_set():
            _info, allowed = _read_trade_allowed()
            _observe_trade_allowed(allowed)
            return allowed


def _try_user32_ctrl_e(reason: str) -> bool:
    """One user32 Ctrl+E after the linux helper acked but a real probe is still 10027.

    Helper ack is not effectiveness. Production: xdotool Ctrl+E left AutoTrading off;
    user32.keybd_event turned it on. This is still one toggle — the helper key did not
    land — so it does not flip a working toolbar off.
    """
    global _ALGO_LAST_SENT_AT, _ALGO_SAW_ALLOWED_SINCE_SEND, _ALGO_PROBE_OK, _ALGO_USER32_TRIED
    if _ALGO_USER32_TRIED:
        return False
    log.warning(
        "algo-enable: linux helper Ctrl+E was not effective (trade_allowed still false / "
        "order_send probe still 10027) — sending one user32 keybd_event Ctrl+E reason=%s",
        reason,
    )
    if _SEND_USER32_CTRL_E is not None:
        sent = bool(_SEND_USER32_CTRL_E())
    else:
        sent = _user32_ctrl_e()
    _ALGO_USER32_TRIED = True
    if sent:
        _ALGO_LAST_SENT_AT = time.time()
        _ALGO_SAW_ALLOWED_SINCE_SEND = False
        _ALGO_PROBE_OK = False
    log.info("algo-enable: user32 Ctrl+E sent=%s reason=%s — waiting for trade_allowed", sent, reason)
    if _poll_until_allowed(ALGO_SETTLE_SECONDS):
        log.info("algo-enable: Algo Trading is effective after user32 Ctrl+E reason=%s", reason)
        return True
    return False


def _fallback_after_ineffective_toggle(reason: str) -> bool:
    """After helper Ctrl+E failed a real probe: user32 once, then toolbar click once."""
    if reason == "watchdog":
        return False
    if not _ALGO_USER32_TRIED and _try_user32_ctrl_e(reason):
        return True
    if not _ALGO_CLICK_TRIED and _try_toolbar_click(reason):
        return True
    return False


def _try_toolbar_click(reason: str) -> bool:
    """One toolbar click after Ctrl+E failed a real order_send probe. Click is also a toggle."""
    global _ALGO_LAST_SENT_AT, _ALGO_SAW_ALLOWED_SINCE_SEND, _ALGO_PROBE_OK, _ALGO_CLICK_TRIED
    if _ALGO_CLICK_TRIED:
        return False
    log.warning(
        "algo-enable: Ctrl+E did not enable AutoTrading (trade_allowed still false / "
        "order_send probe still 10027) — clicking the toolbar button reason=%s",
        reason,
    )
    sent = _send_toolbar_click()
    _ALGO_CLICK_TRIED = True
    if sent:
        _ALGO_LAST_SENT_AT = time.time()
        _ALGO_SAW_ALLOWED_SINCE_SEND = False
        _ALGO_PROBE_OK = False
    log.info("algo-enable: toolbar click sent=%s reason=%s — waiting for trade_allowed", sent, reason)
    if _poll_until_allowed(ALGO_SETTLE_SECONDS):
        log.info("algo-enable: Algo Trading is effective after toolbar click reason=%s", reason)
        return True
    log.error(
        "algo-enable: trade_allowed still false after Ctrl+E and toolbar click reason=%s — "
        "VNC: click the red Algo Trading control once. order_send will return 10027 until it is on",
        reason,
    )
    return False


def _ensure_algo_trading(reason: str = "boot") -> bool:
    """Enable Algo Trading: one helper Ctrl+E, then one user32 Ctrl+E, then one click.

    Must be called with MT5_LOCK held. A toolbar that is already on is left alone.
    Returns True only when ``trade_allowed`` is true, or a guarded order_send probe
    actually placed (and was cancelled). ``order_check`` is never used as the probe.

    Caller: ``_initialize`` (one-shot), the watchdog (observe / True→False only), and
    a single 10027 retry on order_send (poll, send only if cooldown allows).
    """
    global _ALGO_LAST_SENT_AT, _ALGO_SAW_ALLOWED_SINCE_SEND, _ALGO_PROBE_OK
    global _ALGO_CLICK_TRIED, _ALGO_USER32_TRIED

    info, allowed = _read_trade_allowed()
    _observe_trade_allowed(allowed)

    if info is not None and bool(getattr(info, "tradeapi_disabled", False)):
        log.error(
            "algo-enable: tradeapi_disabled=true — Ctrl+E cannot clear Tools > Options > "
            "'Disable automatic trading through the external Python API' (reason=%s)",
            reason,
        )

    if allowed:
        log.info("algo trading already enabled (trade_allowed=true) reason=%s", reason)
        return True

    if not _ctrl_e_allowed(currently_off=True, reason=reason):
        elapsed = time.time() - _ALGO_LAST_SENT_AT if _ALGO_LAST_SENT_AT else 0.0
        remaining = max(0.0, ALGO_TOGGLE_COOLDOWN - elapsed)
        log.log(
            logging.DEBUG if reason == "watchdog" else logging.INFO,
            "algo-enable: not sending Ctrl+E reason=%s elapsed_since_send=%.1fs "
            "cooldown_left=%.0fs saw_allowed_since_send=%s probe_ok=%s click_tried=%s "
            "user32_tried=%s (a second press would flip Algo Trading off)",
            reason,
            elapsed,
            remaining,
            _ALGO_SAW_ALLOWED_SINCE_SEND,
            _ALGO_PROBE_OK,
            _ALGO_CLICK_TRIED,
            _ALGO_USER32_TRIED,
        )
        if reason == "watchdog":
            # Do not hold the IPC lock for a long poll on every tick.
            return bool(_ALGO_PROBE_OK)
        leftover = 0.0
        if _ALGO_LAST_SENT_AT:
            leftover = max(0.0, ALGO_SETTLE_SECONDS - elapsed)
        if _poll_until_allowed(leftover):
            return True
        return _fallback_after_ineffective_toggle(reason)

    # Not the first send: confirm with a real order_send probe. order_check 10013
    # is NOT a reason to skip the toggle — that was the production false positive.
    if _ALGO_LAST_SENT_AT > 0:
        if _probe_algo_api():
            log.info(
                "algo-enable: not sending another toggle reason=%s — order_send probe placed "
                "(toolbar flag is stale)",
                reason,
            )
            return True
        if _fallback_after_ineffective_toggle(reason):
            return True

    if _ALGO_SAW_ALLOWED_SINCE_SEND:
        # Real disable (True → False): fallbacks may be used again this cycle.
        _ALGO_CLICK_TRIED = False
        _ALGO_USER32_TRIED = False

    log.warning(
        "algo-enable: AutoTrading toolbar is off (trade_allowed=false) reason=%s — "
        "sending one Ctrl+E, then polling up to %.0fs",
        reason,
        ALGO_SETTLE_SECONDS,
    )
    sent = _send_one_ctrl_e()
    _ALGO_LAST_SENT_AT = time.time()
    _ALGO_SAW_ALLOWED_SINCE_SEND = False
    _ALGO_PROBE_OK = False
    log.info("algo-enable: Ctrl+E sent=%s reason=%s — waiting for trade_allowed", sent, reason)

    if _poll_until_allowed(ALGO_SETTLE_SECONDS):
        log.info("algo-enable: Algo Trading is effective after one Ctrl+E reason=%s", reason)
        return True

    if _fallback_after_ineffective_toggle(reason):
        return True

    log.error(
        "algo-enable: trade_allowed still false after helper Ctrl+E, user32 Ctrl+E, "
        "toolbar click, and %.0fs poll reason=%s — not sending another (cooldown %.0fs). "
        "VNC: one Ctrl+E / Algo Trading. order_send will return 10027 until the toolbar is on",
        ALGO_SETTLE_SECONDS,
        reason,
        ALGO_TOGGLE_COOLDOWN,
    )
    return False


# ======================================================================================
# initialize() and the watchdog.
# ======================================================================================


def _initialize(allow_launch: bool = False, require_credentials: bool = False) -> None:
    """Attach to terminal64.exe and prove the terminal will actually trade.

    Raises RuntimeError when the channel cannot be opened, and TerminalTradeDisabled when
    it opens but algo trading is off — the condition that makes every order_send come back
    10027 with nothing wrong in this code.
    """
    with MT5_LOCK:
        # Drop a half-open channel first; shutdown() on a never-initialized package is a no-op.
        try:
            mt5.shutdown()
        except Exception:  # pragma: no cover - defensive
            pass

        # THE LADDER. Reproduced twice against a live Exness demo, and it is not cosmetic:
        # calling the same variant repeatedly returns (-10001, 'IPC send failed') forever,
        # while walking these four in ONE process ends in (1, 'Success').
        #
        #     bare            -> (-10005, 'IPC timeout')
        #     portable=True   -> (-10005, 'IPC timeout')
        #     portable=False  -> (-10005, 'IPC timeout')
        #     + credentials   -> (1, 'Success')     account 474436674, balance 1000.00
        #
        # Note the error CHANGES between the two shapes: -10005 is "still trying to reach the
        # terminal", -10001 is the dead end the single-variant loop sits in. Each rung is
        # separated by mt5.shutdown(), exactly as measured. Do not "simplify" this to the last
        # rung -- on its own it does not connect.
        #
        # `path` is what makes initialize() START a terminal when it cannot attach to one --
        # and entrypoint.sh has already started ours. Passing it on every retry therefore
        # spawns a fresh terminal per attempt: measured on the live demo, three instances
        # were racing on one data folder, each authorising and then exiting 0 as MT5 enforces
        # a single instance, so the channel never stabilised. Attaching is the default; the
        # path is offered only as a last resort, when every attach has failed and the
        # terminal entrypoint.sh launched is evidently gone.
        creds: dict[str, Any] = {}
        if MT5_LOGIN:
            creds["login"] = int(MT5_LOGIN)
        if MT5_PASSWORD:
            creds["password"] = MT5_PASSWORD
        if MT5_SERVER:
            creds["server"] = MT5_SERVER

        ladder: list[tuple[str, dict[str, Any]]] = [
            ("bare", {}),
            ("portable", {"path": MT5_PATH, "portable": True, "timeout": INIT_TIMEOUT_MS}),
            ("credentials", {"path": MT5_PATH, "portable": True, "timeout": INIT_TIMEOUT_MS, **creds}),
        ]

        # `allow_launch` gates the one rung that can only ever launch, never attach.
        # portable=False resolves the data folder to %APPDATA%\MetaQuotes\Terminal, which is
        # never the folder the running /portable terminal owns, so the package cannot attach
        # and starts a SECOND instance against the same install — and MT5 permits one. Since
        # entrypoint.sh has already launched the terminal by the time this runs, that rung is
        # a coin flip over which instance survives, so it is held back until every attach has
        # failed and the terminal is presumed gone. That is the "last resort" the ladder
        # comment above describes; `allow_launch` was accepted and never read before.
        #
        # Do not read more into this than that. The broker session also dies on its own here
        # roughly 2-4 minutes after the terminal starts, with this rung removed entirely, so
        # the session loss is NOT attributable to the ladder.
        if allow_launch:
            ladder.insert(
                2, ("non-portable", {"path": MT5_PATH, "portable": False, "timeout": INIT_TIMEOUT_MS})
            )

        if require_credentials:
            # Switching accounts must NOT be allowed to succeed by attaching to the session
            # that is already open: the credential-less rungs would connect to the OLD login
            # and report success while nothing changed. Only the rung that actually carries
            # the new credentials counts here.
            ladder = [rung for rung in ladder if rung[0] == "credentials"]

        if os.environ.get("SP_INIT_CREDENTIALS_FIRST") == "1" and not require_credentials:
            ladder.sort(key=lambda item: 0 if item[0] == "credentials" else 1)

        connected = False
        rung_errors: list[str] = []
        for rung, kwargs in ladder:
            if mt5.initialize(**kwargs):
                log.info("initialize() connected on the %r rung", rung)
                connected = True
                break
            # Read the error BEFORE shutdown(): shutdown resets it to (1, 'Success'), which
            # made every failure report itself as a success and hid what actually went wrong.
            code, desc = _last_error()
            rung_errors.append(f"{rung}: {desc} ({code})")
            log.warning("initialize() rung %r failed: %s (last_error %d)", rung, desc, code)
            try:
                mt5.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass

        if not connected:
            raise RuntimeError(
                "mt5.initialize() failed on every rung of the ladder ["
                + "; ".join(rung_errors)
                + f"]; login={MT5_LOGIN or 'from ini'} server={MT5_SERVER or 'from ini'}"
            )

        info = mt5.terminal_info()
        if info is None:
            code, desc = _last_error()
            raise RuntimeError(f"terminal_info() returned None right after initialize(): {desc} (last_error {code})")

        # common.ini must be UTF-16LE or [Experts] is ignored. The entrypoint already
        # writes it that way; this is a belt-and-suspenders log if a later write overwrote it.
        _assert_common_ini_utf16le()

        # Exness (and other broker-branded) builds: AllowLiveTrading=1 in common.ini is
        # not enough. order_send returns 10027 "AutoTrading disabled by client" until the
        # toolbar Algo Trading toggle is on. terminal_info().trade_allowed tracks that
        # toggle. One Ctrl+E turns it on; a second press turns it off, so we send
        # once and then poll trade_allowed for several seconds. order_check is not
        # the effectiveness probe (Exness returns 10013 while live order_send is 10027).
        _ensure_algo_trading("initialize")

        info = mt5.terminal_info()
        if info is None:
            code, desc = _last_error()
            raise RuntimeError(f"terminal_info() returned None after Algo Trading enable: {desc} (last_error {code})")
        account = mt5.account_info()
        if not _trade_ready_from(info, account):
            raise TerminalTradeDisabled(
                "Algorithmic trading is blocked — every order_send would return "
                "TRADE_RETCODE_CLIENT_DISABLES_AT (10027) and nothing in this code would be wrong. "
                f"trade_allowed={bool(info.trade_allowed)} tradeapi_disabled={bool(info.tradeapi_disabled)}. "
                "The sidecar already tried Ctrl+E to enable the AutoTrading toolbar. "
                "If it is still off, use VNC (docker compose --profile debug up) and press Ctrl+E "
                "or click Algo Trading; also check Tools > Options > Expert Advisors and that "
                "common.ini was UTF-16LE with Account=0 / Profile=0."
            )


def _terminal_info_locked(timeout: float = 5.0) -> tuple[bool, Any]:
    """Probe the terminal without ever blocking on an in-flight RPC call.

    Returns (probed, info). ``probed`` is False when the lock was busy, which means a call
    is running, which means the channel is alive — the tick then leaves the state alone.
    """
    if not MT5_LOCK.acquire(timeout=timeout):
        return False, None
    try:
        try:
            return True, mt5.terminal_info()
        except Exception as exc:
            log.warning("terminal_info() raised: %s", exc)
            return True, None
    finally:
        MT5_LOCK.release()


def _launch_terminal() -> str:
    """Relaunch terminal64.exe from the Windows side.

    This process is a Windows process under Wine: it has no access to the Linux process
    table, so BUILD_PROMPT §1's ``pgrep -f terminal64.exe`` is replaced by the Windows-side
    equivalent — terminal_info() returning None — and the relaunch goes through the launcher
    script entrypoint.sh wrote into the prefix (cmd's ``start`` detaches it), falling back to
    starting the terminal directly with the same switches.
    """
    launcher = MT5_LAUNCHER
    if launcher and os.path.exists(launcher):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        command = [comspec, "/c", launcher.replace("/", "\\")]
    else:
        command = [MT5_PATH, "/portable", "/config:" + MT5_CONFIG]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _LAUNCHED.append(proc)
    del _LAUNCHED[:-4]
    return " ".join(command)


def _restart_terminal() -> None:
    """Relaunch + re-initialize, inside the restart budget."""
    if STATE.restart_budget_left() <= 0:
        STATE.set(
            unhealthy=True,
            initialized=False,
            trade_ready=False,
            reason=(
                f"terminal restarted {WATCHDOG_MAX_RESTARTS} times in "
                f"{int(WATCHDOG_RESTART_WINDOW)}s and never stayed up — refusing to thrash; "
                "look at the terminal over VNC (docker compose --profile debug up)"
            ),
        )
        log.error(
            "watchdog: restart budget exhausted (%d in %ds) — reporting permanently unhealthy",
            WATCHDOG_MAX_RESTARTS,
            int(WATCHDOG_RESTART_WINDOW),
        )
        return

    used = STATE.record_restart()
    _reset_algo_state()
    log.warning("watchdog: terminal is gone — relaunching (restart %d of %d in the window)", used, WATCHDOG_MAX_RESTARTS)
    try:
        command = _launch_terminal()
    except Exception as exc:
        STATE.set(initialized=False, terminal=False, trade_ready=False, reason=f"relaunch failed: {exc}")
        log.error("watchdog: relaunch failed: %s", exc)
        return
    log.info("watchdog: launched %s", command)

    deadline = time.time() + RELAUNCH_WAIT_SECONDS
    while time.time() < deadline and not STOP.is_set():
        STOP.wait(3.0)
        try:
            _initialize()
        except TerminalTradeDisabled as exc:
            STATE.set(initialized=True, terminal=True, trade_ready=False, reason=str(exc))
            STATE.ever_initialized = True
            log.error("watchdog: %s", exc)
            return
        except RuntimeError as exc:
            STATE.set(initialized=False, reason=str(exc))
            continue
        STATE.set(initialized=True, terminal=True, trade_ready=True, init_failures=0, reason="ok")
        STATE.ever_initialized = True
        log.info("watchdog: terminal is back and initialize() succeeded")
        return

    STATE.set(initialized=False, terminal=False, trade_ready=False, reason="relaunched terminal never answered")
    log.error("watchdog: relaunched terminal never answered within %.0fs", RELAUNCH_WAIT_SECONDS)


def _watchdog_tick() -> None:
    """One supervision cycle. Kept small and side-effect-explicit so it reads as a whole."""
    snapshot_now = STATE.snapshot()
    if snapshot_now["unhealthy"]:
        return

    # Never relaunch a terminal we have not yet reached even once. The watchdog's job is to
    # recover a channel that WAS working; during the initial connect the terminal is up but
    # not yet servable, and relaunching then starts a competing instance on the same data
    # folder -- which is the loop this sidecar spent its first live run stuck in.
    if not snapshot_now["initialized"] and not STATE.ever_initialized:
        return

    probed, info = _terminal_info_locked()
    if not probed:
        # A request is holding the IPC lock, so the channel is demonstrably alive.
        return

    if info is None:
        code, desc = _last_error()
        log.warning("watchdog: terminal_info() is None (last_error %d: %s)", code, desc)
        STATE.set(terminal=False, initialized=False, trade_ready=False, reason=f"terminal_info() is None: {desc}")
        _restart_terminal()
        return

    # Exness builds return 10027 while the AutoTrading toolbar is off. One-shot
    # Ctrl+E lives in _ensure_algo_trading on initialize. The watchdog must not
    # re-toggle while the flag is still catching up (a second Ctrl+E turns it off).
    need_restart = False
    trade_ready = False
    if not MT5_LOCK.acquire(timeout=2.0):
        return
    try:
        info = mt5.terminal_info()
        if info is None:
            code, desc = _last_error()
            STATE.set(
                terminal=False,
                initialized=False,
                trade_ready=False,
                reason=f"terminal_info() is None: {desc}",
            )
            need_restart = True
            trade_ready = False
        else:
            need_restart = False
            if _toolbar_off(info):
                _ensure_algo_trading("watchdog")
                info = mt5.terminal_info()
            _account = mt5.account_info()
            trade_ready = _trade_ready_from(info, _account)
    finally:
        MT5_LOCK.release()

    if need_restart:
        _restart_terminal()
        return

    snapshot = STATE.snapshot()

    if not snapshot["initialized"]:
        # The terminal answers but our channel is not established (or was lost). Re-initialize
        # without spending a restart; only a run of failures justifies relaunching the terminal.
        try:
            _initialize()
        except TerminalTradeDisabled as exc:
            STATE.set(initialized=True, terminal=True, trade_ready=False, init_failures=0, reason=str(exc))
            STATE.ever_initialized = True
            log.error("watchdog: %s", exc)
            return
        except RuntimeError as exc:
            failures = int(snapshot["init_failures"]) + 1
            STATE.set(initialized=False, terminal=True, trade_ready=False, init_failures=failures, reason=str(exc))
            log.error("watchdog: initialize() failed (%d in a row): %s", failures, exc)
            if failures >= max(1, WATCHDOG_MAX_RESTARTS):
                STATE.set(init_failures=0)
                _restart_terminal()
            return
        STATE.set(initialized=True, terminal=True, trade_ready=True, init_failures=0, reason="ok")
        STATE.ever_initialized = True
        log.info("watchdog: re-initialized the MT5 channel")
        return

    if trade_ready != snapshot["trade_ready"]:
        if trade_ready:
            log.info("watchdog: algo trading is enabled again")
            STATE.set(terminal=True, trade_ready=True, reason="ok")
        else:
            log.error(
                "watchdog: algorithmic trading is blocked — order_send will return 10027 "
                "(trade_allowed=%s tradeapi_disabled=%s)",
                bool(getattr(info, "trade_allowed", False)) if info is not None else None,
                bool(getattr(info, "tradeapi_disabled", False)) if info is not None else None,
            )
            STATE.set(terminal=True, trade_ready=False, reason="algorithmic trading is blocked (10027)")
        return

    STATE.set(terminal=True)


def _authorised_after(blob: str, since: float) -> bool:
    """Report an "authorized on" line written AFTER ``since`` (unix seconds).

    The terminal keeps one log per DAY, so a bare substring search matches the previous
    run's authorisation and returns instantly -- which defeats the whole wait. Each line
    starts with a tab-separated ``HH:MM:SS.mmm``, so the timestamp is what decides.
    """
    local = time.localtime(since)
    floor = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec - 5
    for line in blob.splitlines():
        if "authorized on" not in line:
            continue
        for field in line.split("\t"):
            field = field.strip()
            if len(field) >= 8 and field[2] == ":" and field[5] == ":":
                try:
                    hh, mm, ss = int(field[0:2]), int(field[3:5]), int(field[6:8])
                except ValueError:
                    break
                if hh * 3600 + mm * 60 + ss >= floor:
                    return True
                break
    return False


def _wait_for_terminal_login(timeout: float) -> bool:
    """Block until the terminal reports an authorised account in its own log.

    ``mt5.initialize(path=...)`` LAUNCHES a terminal when it cannot attach to one, and
    entrypoint.sh has already launched ours. Calling it too early therefore starts a SECOND
    terminal on the same data folder; MT5 permits one, so a terminal exits with code 0, the
    watchdog sees it gone and relaunches, and the pair never settles. Measured on a live
    Exness demo: initialize() at +12s produced exactly that loop, while the same call after
    the terminal had authorised attached first time.

    Watching the terminal's own log is what makes the wait no longer than it has to be: a
    fast login is picked up in a second or two instead of paying a fixed grace period.
    """
    deadline = time.time() + timeout
    log_dir = os.path.join(MT5_DIR_WIN, "logs")
    started = time.time()
    log.info("waiting up to %.0fs for the terminal to authorise before initialize()", timeout)
    while time.time() < deadline and not STOP.is_set():
        try:
            names = sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else []
            for name in reversed(names[-2:]):
                path = os.path.join(log_dir, name)
                if os.path.getmtime(path) < started - 300:
                    continue
                with open(path, "rb") as handle:
                    try:
                        handle.seek(-60000, os.SEEK_END)
                    except OSError:
                        handle.seek(0)
                    blob = handle.read().decode("utf-16-le", errors="ignore")
                if _authorised_after(blob, started):
                    log.info("terminal authorised; attaching")
                    # It writes the line a beat before the IPC channel is servable.
                    STOP.wait(5.0)
                    return True
        except Exception:  # pragma: no cover - the wait must never be fatal
            pass
        STOP.wait(1.0)
    log.warning("terminal did not report an authorisation within %.0fs; trying initialize() anyway", timeout)
    return False


def _boot() -> None:
    """Bounded initialize-with-backoff at startup, then hand over to the watchdog."""
    # No wait here: entrypoint.sh already gives the terminal its settle period before this
    # process is started, and the attach-first ladder below retries on its own backoff.
    delay = INIT_BACKOFF_START
    for attempt in range(1, INIT_MAX_ATTEMPTS + 1):
        if STOP.is_set():
            return
        # entrypoint.sh DOES launch terminal64.exe before this process starts — see the
        # comment above its launch_terminal call. The first attempts must therefore attach to
        # that terminal, not offer a rung that can only start a competing one; only once
        # attaching has failed repeatedly is the terminal presumed gone and a launch allowed.
        allow_launch = attempt > 2
        try:
            _initialize(allow_launch=allow_launch)
        except TerminalTradeDisabled as exc:
            # The channel is open and reads work; Ctrl+E did not turn the toolbar on.
            STATE.set(initialized=True, terminal=True, trade_ready=False, reason=str(exc))
            STATE.ever_initialized = True
            log.error("%s", exc)
            return
        except RuntimeError as exc:
            STATE.set(initialized=False, reason=str(exc))
            log.warning("initialize() attempt %d/%d failed: %s", attempt, INIT_MAX_ATTEMPTS, exc)
            if attempt < INIT_MAX_ATTEMPTS:
                STOP.wait(delay)
                delay = min(delay * 2.0, INIT_BACKOFF_MAX)
            continue
        except Exception as exc:  # pragma: no cover - defensive
            STATE.set(initialized=False, reason=f"initialize() raised {type(exc).__name__}: {exc}")
            log.warning("initialize() attempt %d/%d raised: %s", attempt, INIT_MAX_ATTEMPTS, exc)
            if attempt < INIT_MAX_ATTEMPTS:
                STOP.wait(delay)
                delay = min(delay * 2.0, INIT_BACKOFF_MAX)
            continue

        STATE.set(initialized=True, terminal=True, trade_ready=True, init_failures=0, reason="ok")
        STATE.ever_initialized = True
        try:
            account = mt5.account_info()
        except Exception:  # pragma: no cover - defensive
            account = None
        if account is None:
            log.info("MT5 initialized (account_info() not available yet)")
        else:
            # Login and server are not secrets; the password is never touched here.
            log.info(
                "MT5 initialized: login=%s server=%s currency=%s margin_mode=%s",
                getattr(account, "login", "?"),
                getattr(account, "server", "?"),
                getattr(account, "currency", "?"),
                getattr(account, "margin_mode", "?"),
            )
        return

    log.error("initialize() failed %d times at startup — the watchdog will keep trying", INIT_MAX_ATTEMPTS)


def _supervisor() -> None:
    """Boot, then run one watchdog tick every WATCHDOG_SECONDS until shutdown."""
    _boot()
    while not STOP.is_set():
        STATE.wake.wait(WATCHDOG_SECONDS)
        STATE.wake.clear()
        if STOP.is_set():
            return
        try:
            _watchdog_tick()
        except Exception as exc:  # pragma: no cover - the watchdog must never die
            log.error("watchdog tick raised: %s", exc, exc_info=True)


# ======================================================================================
# The method whitelist.
#
# Dispatch goes through this explicit dict. Never getattr(mt5, name): that turns one
# malformed request into an arbitrary call on the broker API.
# ======================================================================================

_INT_REQUEST_KEYS = frozenset(
    {"action", "type", "type_time", "type_filling", "magic", "position", "position_by", "order", "deviation", "expiration"}
)
_FLOAT_REQUEST_KEYS = frozenset({"volume", "price", "sl", "tp", "stoplimit"})


def _normalize_request(request: Any) -> dict[str, Any]:
    """Coerce a JSON order request into the exact types the package expects.

    JSON cannot distinguish 1 from 1.0, and the fields MT5 reads as integers (action, type,
    type_filling, magic, position…) must not arrive as floats. Keys whose value is None are
    dropped: the backend omits what it does not set, and a None field is rejected outright.
    """
    if not isinstance(request, dict):
        raise TypeError("'request' must be an object")
    out: dict[str, Any] = {}
    for key, value in request.items():
        name = str(key)
        if value is None:
            continue
        if name in _INT_REQUEST_KEYS:
            out[name] = int(value)
        elif name in _FLOAT_REQUEST_KEYS:
            out[name] = float(value)
        elif name in ("symbol", "comment"):
            out[name] = str(value)
        else:
            out[name] = value
    return out


def _m_account_info() -> Any:
    return mt5.account_info()


def _m_terminal_info() -> Any:
    return mt5.terminal_info()


def _m_symbols_get(group: str | None = None) -> Any:
    if group:
        return mt5.symbols_get(group=str(group))
    return mt5.symbols_get()


def _m_symbol_info(symbol: str) -> Any:
    return mt5.symbol_info(str(symbol))


def _m_symbol_select(symbol: str, enable: bool = True) -> Any:
    return mt5.symbol_select(str(symbol), bool(enable))


def _m_symbol_info_tick(symbol: str) -> Any:
    return mt5.symbol_info_tick(str(symbol))


def _m_copy_rates_from_pos(symbol: str, timeframe: int, start: int = 0, count: int = 100) -> Any:
    # timeframe is the bit-packed MT5 integer the backend sends (H1 == 16385), never minutes.
    return mt5.copy_rates_from_pos(str(symbol), int(timeframe), int(start), int(count))


def _m_positions_get(symbol: str | None = None, ticket: int | None = None, group: str | None = None) -> Any:
    # The package takes exactly one filter, or none at all; passing symbol=None is not the
    # same as passing nothing.
    if ticket is not None:
        return mt5.positions_get(ticket=int(ticket))
    if symbol:
        return mt5.positions_get(symbol=str(symbol))
    if group:
        return mt5.positions_get(group=str(group))
    return mt5.positions_get()


def _m_orders_get(symbol: str | None = None, ticket: int | None = None, group: str | None = None) -> Any:
    if ticket is not None:
        return mt5.orders_get(ticket=int(ticket))
    if symbol:
        return mt5.orders_get(symbol=str(symbol))
    if group:
        return mt5.orders_get(group=str(group))
    return mt5.orders_get()


def _m_history_deals_get(
    date_from: float,
    date_to: float,
    group: str | None = None,
    ticket: int | None = None,
    position: int | None = None,
) -> Any:
    """Deals in a time range, with date_to padded by a day.

    date_from/date_to arrive as unix floats and become naive datetimes through
    utcfromtimestamp, which is how the package interprets them. History times are
    broker-server time (usually UTC+2/+3), so the range is padded one day into the future
    or the deals just made fall outside it (BUILD_PROMPT §2).
    """
    if ticket is not None:
        return mt5.history_deals_get(ticket=int(ticket))
    if position is not None:
        return mt5.history_deals_get(position=int(position))
    dt_from = datetime.utcfromtimestamp(float(date_from))
    dt_to = datetime.utcfromtimestamp(float(date_to) + HISTORY_PAD_SECONDS)
    if group:
        return mt5.history_deals_get(dt_from, dt_to, group=str(group))
    return mt5.history_deals_get(dt_from, dt_to)


def _m_order_check(request: dict[str, Any]) -> Any:
    # order_check succeeds at retcode 0, not 10009 — the backend reads that; here it is just
    # a result object, and None is a legitimate answer that must reach the caller intact.
    return mt5.order_check(_normalize_request(request))


def _m_order_send(request: dict[str, Any]) -> Any:
    req = _normalize_request(request)
    result = mt5.order_send(req)
    if not _is_client_disables_at(result):
        return result
    # 10027 means the request was not accepted. Poll a recent Ctrl+E; send another
    # only if the cooldown / observed-on-then-off rule allows it. Effectiveness is
    # trade_allowed or a real order_send probe that places (never order_check).
    global _ALGO_PROBE_OK
    _ALGO_PROBE_OK = False
    log.warning(
        "order_send returned 10027 (AutoTrading disabled by client); "
        "attempting Algo Trading enable (one Ctrl+E at most) and one retry"
    )
    if not _ensure_algo_trading("order_send_10027"):
        log.error(
            "Algo Trading still blocked after enable attempt; not retrying order_send "
            "(the 10027 request was not accepted, so nothing was duplicated)"
        )
        return result
    retried = mt5.order_send(req)
    log.info(
        "order_send retry after Algo Trading enable retcode=%s",
        getattr(retried, "retcode", None),
    )
    return retried


def _m_order_calc_margin(action: int, symbol: str, volume: float, price: float) -> Any:
    return mt5.order_calc_margin(int(action), str(symbol), float(volume), float(price))


def _m_ping() -> dict[str, Any]:
    snapshot = STATE.snapshot()
    return {
        "ok": True,
        "ts": time.time(),
        "initialized": snapshot["initialized"],
        "terminal": snapshot["terminal"],
        "trade_ready": snapshot["trade_ready"],
    }


def _m_relogin(login: int, password: str, server: str) -> dict[str, Any]:
    """Re-attach the terminal to a different account, without restarting the container.

    The BRIEF calls the seeded account one "the operator changes in the UI later", but the
    credentials reach this process as environment variables read once at boot. Without this
    method, saving a new account in Settings stores it in the database and changes nothing at
    the broker until someone restarts the sidecar — the worst kind of bug, because the UI
    reports success.

    The new values replace the module globals so the watchdog's own re-initialise uses them
    too, and the password is registered with the log filter before it is used anywhere.
    """
    global MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

    login_i = int(login)
    password_s = str(password)
    server_s = str(server)
    if login_i <= 0 or not password_s or not server_s:
        raise ValueError("relogin needs a positive login, a password and a server name")

    _register_secret(password_s)
    previous = (MT5_LOGIN, MT5_PASSWORD, MT5_SERVER)
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER = login_i, password_s, server_s
    try:
        _initialize(allow_launch=True, require_credentials=True)
    except Exception:
        # Put the working account back so a bad password does not leave the process
        # unable to reach the broker at all.
        MT5_LOGIN, MT5_PASSWORD, MT5_SERVER = previous
        try:
            _initialize(allow_launch=True, require_credentials=True)
        except Exception:  # pragma: no cover - the original failure is the one that matters
            log.exception("relogin rollback failed; the terminal is detached")
        raise

    info = mt5.account_info()
    log.info("relogin ok login=%s server=%s", login_i, server_s)
    return {
        "ok": True,
        "login": int(getattr(info, "login", login_i)),
        "server": str(getattr(info, "server", server_s)),
        "name": str(getattr(info, "name", "")),
        "currency": str(getattr(info, "currency", "")),
        "balance": float(getattr(info, "balance", 0.0)),
    }


METHODS: dict[str, Callable[..., Any]] = {
    "account_info": _m_account_info,
    "relogin": _m_relogin,
    "terminal_info": _m_terminal_info,
    "symbols_get": _m_symbols_get,
    "symbol_info": _m_symbol_info,
    "symbol_select": _m_symbol_select,
    "symbol_info_tick": _m_symbol_info_tick,
    "copy_rates_from_pos": _m_copy_rates_from_pos,
    "positions_get": _m_positions_get,
    "orders_get": _m_orders_get,
    "history_deals_get": _m_history_deals_get,
    "order_check": _m_order_check,
    "order_send": _m_order_send,
    "order_calc_margin": _m_order_calc_margin,
    "ping": _m_ping,
}


def _error(code: int, message: str, retcode: int | None = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": int(code), "message": str(message), "retcode": retcode}}


def dispatch(method: Any, params: Any) -> dict[str, Any]:
    """Run one whitelisted method and return the reply envelope."""
    if not isinstance(method, str):
        return _error(-5, "'method' must be a string")
    handler = METHODS.get(method)
    if handler is None:
        return _error(-4, f"unknown method {method!r}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(-5, "'params' must be an object")

    ready, why = STATE.readiness()
    if not ready:
        return _error(-6, why)

    try:
        bound = inspect.signature(handler).bind(**params)
    except TypeError as exc:
        return _error(-5, f"bad params for {method}: {exc}")

    started = time.time()
    with MT5_LOCK:
        try:
            result = handler(*bound.args, **bound.kwargs)
        except TypeError as exc:
            return _error(-5, f"bad params for {method}: {exc}")
        except Exception as exc:
            log.error("%s raised: %s", method, exc, exc_info=True)
            return _error(-7, f"{method} raised {type(exc).__name__}: {exc}")

        if result is None and method not in NULLABLE_RESULT_METHODS:
            code, desc = _last_error()
            if code in IPC_ERROR_CODES:
                STATE.note_ipc_failure()
            return _error(-3, f"{method}() returned None — MT5 last_error {code}: {desc}")

        # order_send / order_check only: a None result is the documented answer, not an
        # error. It is handed back as ok:true with result null so executor.py sees exactly
        # the None the MetaTrader5 package would have given it. Read that twice before
        # "fixing" it into an error — the null-check in the money path depends on it.
        payload = _to_jsonable(result)

    elapsed_ms = (time.time() - started) * 1000.0
    if elapsed_ms > 2000.0:
        log.info("%s took %.0f ms", method, elapsed_ms)
    return {"ok": True, "result": payload}


# ======================================================================================
# HTTP surface.
# ======================================================================================


class RpcHandler(BaseHTTPRequestHandler):
    # A stalled header/body must not monopolize the single IPC-serving HTTP thread.
    timeout = 10.0

    server_version = "lv-copytrader-sidecar/1.0"
    sys_version = ""
    # HTTP/1.0: one request per connection. With a single-threaded server, a keep-alive
    # connection held open by one client would block every other caller — including the
    # compose healthcheck — until it closed.
    protocol_version = "HTTP/1.0"
    # A wedged or vanished client must not hold the only request thread forever.
    timeout = 60

    # -------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
        log.warning("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=repr, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client vanished
            log.debug("client disconnected before the reply was written")

    def _authorized(self) -> bool:
        if not TOKEN or TOKEN.lower() in INSECURE_TOKENS:
            return False
        provided = self.headers.get("X-SP-Token") or ""
        return hmac.compare_digest(provided.encode("utf-8", "ignore"), TOKEN.encode("utf-8"))

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json(400, _error(-5, "Content-Length is required"))
            return None
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._send_json(400, _error(-5, "Content-Length is not a number"))
            return None
        if length < 0:
            self._send_json(400, _error(-5, "Content-Length is negative"))
            return None
        if length > MAX_BODY_BYTES:
            self._send_json(413, _error(-5, f"body larger than {MAX_BODY_BYTES} bytes"))
            return None
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != length:
            self._send_json(400, _error(-5, "body shorter than Content-Length"))
            return None
        return body

    @staticmethod
    def _path(raw: str) -> str:
        return raw.split("?", 1)[0].rstrip("/") or "/"

    # -------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._path(self.path) == "/health":
            # No token, no secrets, no MT5 call: the watchdog's cached flags only, so the
            # healthcheck stays answerable and can never disturb the IPC channel.
            snapshot = STATE.snapshot()
            payload = {
                "ok": snapshot["ok"],
                "initialized": snapshot["initialized"],
                "terminal": snapshot["terminal"],
            }
            self._send_json(200 if snapshot["ok"] else 503, payload)
            return
        self._send_json(404, _error(-4, "not found"))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._path(self.path) != "/rpc":
            self._send_json(404, _error(-4, "not found"))
            return
        if not self._authorized():
            # The sidecar port is never published to the host, but this header is the only
            # thing between the compose network and order_send.
            log.warning("rejected an /rpc call from %s: missing or wrong X-SP-Token", self.address_string())
            self._send_json(401, _error(-2, "missing or wrong X-SP-Token"))
            return

        body = self._read_body()
        if body is None:
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._send_json(400, _error(-5, f"body is not valid JSON: {exc}"))
            return
        if not isinstance(request, dict):
            self._send_json(400, _error(-5, "body must be a JSON object"))
            return

        # Everything from here is a well-formed call, so failures come back as 200 + ok:false
        # and the backend can turn them into an RpcError with the code intact.
        self._send_json(200, dispatch(request.get("method"), request.get("params")))


class RpcServer(HTTPServer):
    """Single-threaded on purpose — see the IPC lock comment above."""

    allow_reuse_address = True


# ======================================================================================
# Entry point.
# ======================================================================================


def _install_signal_handlers(server: HTTPServer) -> None:
    def _handle(signum: int, _frame: Any) -> None:
        log.info("signal %s received — shutting down", signum)
        STOP.set()
        STATE.wake.set()
        threading.Thread(target=server.shutdown, name="sp-shutdown", daemon=True).start()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover - not every signal exists under Wine
            log.debug("could not install a handler for %s", name)


def main() -> int:
    if not TOKEN or TOKEN.lower() in INSECURE_TOKENS:
        log.critical(
            "SIDECAR_TOKEN is empty or a shipped placeholder — refusing to start. An unauthenticated /rpc endpoint is "
            "order_send on an open port; set SIDECAR_TOKEN in .env and restart."
        )
        return 2

    server = RpcServer((HOST, PORT), RpcHandler)
    log.info("listening on %s:%d (POST /rpc, GET /health)", HOST, PORT)

    supervisor = threading.Thread(target=_supervisor, name="sp-watchdog", daemon=True)
    supervisor.start()

    _install_signal_handlers(server)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        log.info("interrupted")
    finally:
        STOP.set()
        STATE.wake.set()
        try:
            server.server_close()
        except Exception:  # pragma: no cover - defensive
            pass
        supervisor.join(timeout=5.0)
        with MT5_LOCK:
            try:
                mt5.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
