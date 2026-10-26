"""The Wine sidecar's RPC layer, tested on Linux with the broker API stubbed out.

``mt5-sidecar/rpc_server.py`` runs under *Windows* Python inside Wine and is the only file
in the project allowed to import MetaTrader5, so it can never be exercised by the rest of
the suite. That is exactly why it is worth pinning here: its serialisation and auth are pure
logic, they sit directly in front of ``order_send``, and a mistake in them is invisible until
a real trade goes wrong.

A stub ``MetaTrader5`` module is installed in ``sys.modules`` before import, so the file is
loaded as written -- no edits, no shims inside the production source.

What this locks down:
  * the token check is constant-time and a wrong/missing token is a 401
  * dispatch goes through an explicit whitelist, never getattr(mt5, name)
  * namedtuples serialise via _asdict(), tuples of them become lists of dicts
  * numpy-style rate arrays survive .astype(object).tolist()
  * **order_send / order_check returning None comes back as ok:true, result:null** -- every
    other method's None is an error. That asymmetry is the documented contract and the whole
    reason the backend can null-check before touching .retcode.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import namedtuple
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SIDECAR = Path(__file__).resolve().parents[2] / "mt5-sidecar" / "rpc_server.py"

AccountInfo = namedtuple("AccountInfo", "login balance equity currency margin_free margin_mode")
SymbolInfo = namedtuple("SymbolInfo", "name digits point volume_min filling_mode trade_exemode")
Position = namedtuple("Position", "ticket symbol type volume price_open sl tp magic comment")


class _FakeRates(list):
    """Stands in for the numpy recarray copy_rates_* returns."""

    def astype(self, _dtype: Any) -> "_FakeRates":
        return self

    def tolist(self) -> list:
        return list(self)


def _install_stub() -> ModuleType:
    """Put a fake MetaTrader5 in sys.modules so the sidecar imports unmodified."""
    mt5 = ModuleType("MetaTrader5")
    mt5.calls = []                                          # type: ignore[attr-defined]
    mt5.last_error = lambda: (-10005, "IPC timeout")         # type: ignore[attr-defined]
    mt5.initialize = lambda **kw: True                       # type: ignore[attr-defined]
    mt5.shutdown = lambda: None                              # type: ignore[attr-defined]
    mt5.account_info = lambda: AccountInfo(474436674, 3000.0, 3000.0, "USD", 2900.0, 2)
    mt5.terminal_info = lambda: SimpleNamespace(
        trade_allowed=True, tradeapi_disabled=False, connected=True, build=4620
    )
    mt5.symbols_get = lambda *a, **k: (
        SymbolInfo("XAUUSDm", 2, 0.01, 0.01, 3, 2),
        SymbolInfo("EURUSDm", 5, 0.00001, 0.01, 3, 2),
    )
    mt5.symbol_info = lambda name: SymbolInfo(name, 2, 0.01, 0.01, 3, 2)
    mt5.symbol_select = lambda name, enable=True: True
    mt5.symbol_info_tick = lambda name: SimpleNamespace(bid=2380.5, ask=2380.75, time=1700000000)
    mt5.copy_rates_from_pos = lambda *a: _FakeRates([[1700000000, 1.0, 2.0, 0.5, 1.5, 10, 2, 10]])
    mt5.positions_get = lambda **k: (Position(500001, "XAUUSDm", 0, 0.1, 2380.0, 2370.0, 0.0, 770077, "SP:abc12345"),)
    mt5.orders_get = lambda **k: ()
    mt5.history_deals_get = lambda *a, **k: ()
    mt5.order_calc_margin = lambda *a: 120.5
    # The two methods whose real None must reach the backend as a null result.
    mt5.order_send = lambda req: None                        # type: ignore[attr-defined]
    mt5.order_check = lambda req: None                       # type: ignore[attr-defined]
    sys.modules["MetaTrader5"] = mt5
    return mt5


@pytest.fixture(scope="module")
def sidecar(monkeypatch_module: Any = None):
    """Import rpc_server.py once with the stub and a known token in place."""
    import os

    os.environ["SIDECAR_TOKEN"] = "test-token-abc"
    os.environ["MT5_LOGIN"] = "474436674"
    os.environ["MT5_PASSWORD"] = "irrelevant"
    os.environ["MT5_SERVER"] = "Exness-MT5Trial15"
    _install_stub()
    spec = importlib.util.spec_from_file_location("sp_rpc_server", SIDECAR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sp_rpc_server"] = module
    spec.loader.exec_module(module)
    return module


def test_dispatch_is_an_explicit_whitelist(sidecar) -> None:
    """Never getattr(mt5, name): that turns one bad request into arbitrary code."""
    assert set(sidecar.METHODS) == {
        "account_info", "terminal_info", "symbols_get", "symbol_info", "symbol_select",
        "symbol_info_tick", "copy_rates_from_pos", "positions_get", "orders_get",
        "history_deals_get", "order_check", "order_send", "order_calc_margin", "ping",
        # `relogin` re-attaches the terminal to a different account without a container
        # restart. Without it, changing the MT5 account in Settings stored the new
        # credentials and changed nothing at the broker -- a UI that reports success and
        # does nothing. It is on the whitelist deliberately, and it is the only method that
        # takes a password.
        "relogin",
    }
    # Only CODE counts: the file carries a comment saying "never getattr(mt5, name)",
    # and a naive substring search would flag the warning as the offence.
    code = [ln for ln in SIDECAR.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("getattr(mt5" in ln for ln in code), (
        "dispatch must never reach into the broker module by name"
    )


def test_single_threaded_server(sidecar) -> None:
    """The one request thread IS the serialisation of the non-thread-safe MT5 IPC channel."""
    from http.server import HTTPServer

    assert issubclass(sidecar.RpcServer, HTTPServer)
    assert "ThreadingHTTPServer" not in SIDECAR.read_text().split("# ")[0]


def test_namedtuple_results_serialise_as_dicts(sidecar) -> None:
    """Namedtuples convert with _asdict(); a tuple of them becomes a list of dicts."""
    import json

    account = sidecar.METHODS["account_info"]()
    encoded = json.loads(json.dumps(sidecar._to_jsonable(account)))
    assert encoded["login"] == 474436674
    assert encoded["margin_mode"] == 2, "the backend reads margin_mode to decide multi-TP splitting"


def test_symbols_get_becomes_a_list_of_dicts(sidecar) -> None:
    import json

    raw = sidecar.METHODS["symbols_get"]()
    encoded = json.loads(json.dumps(sidecar._to_jsonable(raw)))
    assert isinstance(encoded, list) and len(encoded) == 2
    assert encoded[0]["name"] == "XAUUSDm", "a tuple of namedtuples becomes a list of dicts"


def test_rates_survive_serialisation(sidecar) -> None:
    """copy_rates_* returns a numpy array; .astype(object).tolist() is the conversion."""
    import json

    raw = sidecar.METHODS["copy_rates_from_pos"]("XAUUSDm", 16385, 0, 1)
    encoded = json.loads(json.dumps(sidecar._to_jsonable(raw)))
    assert encoded[0][0] == 1700000000


def test_order_send_none_is_a_null_result_not_an_error(sidecar) -> None:
    """The documented asymmetry: the backend must SEE the None to null-check it."""
    assert "order_send" in sidecar.NULLABLE_RESULT_METHODS
    assert "order_check" in sidecar.NULLABLE_RESULT_METHODS
    assert "account_info" not in sidecar.NULLABLE_RESULT_METHODS


def test_token_comparison_is_constant_time(sidecar) -> None:
    """A timing-safe compare on the one header standing in front of order_send."""
    source = SIDECAR.read_text()
    assert "hmac.compare_digest" in source
    assert "X-SP-Token" in source


def test_relogin_restores_the_previous_account_when_the_new_one_fails(sidecar) -> None:
    """A wrong password must not leave the process unable to reach the broker at all."""
    import MetaTrader5 as stub

    good = (sidecar.MT5_LOGIN, sidecar.MT5_PASSWORD, sidecar.MT5_SERVER)
    attempts: list[dict] = []

    def failing_initialize(**kw):
        attempts.append(kw)
        # Fail only for the new login; succeed for the rollback to the original.
        return kw.get("login") != 999999

    original = stub.initialize
    stub.initialize = failing_initialize
    try:
        with pytest.raises(Exception):
            sidecar.METHODS["relogin"](login=999999, password="wrong-password", server="Some-Server")
    finally:
        stub.initialize = original

    assert (sidecar.MT5_LOGIN, sidecar.MT5_PASSWORD, sidecar.MT5_SERVER) == good, (
        "a failed relogin must roll the working credentials back"
    )
    assert len(attempts) == 2, "it tried the new account, then restored the old one"


def test_relogin_masks_the_new_password_in_logs(sidecar) -> None:
    """The password arrives after boot, so the boot-time filter would not know it."""
    secret = "a-brand-new-password-9f3a"
    sidecar._register_secret(secret)
    record = __import__("logging").LogRecord(
        "rpc", 20, __file__, 1, "connecting with %s", (secret,), None
    )
    assert sidecar._SECRET_FILTER is not None
    sidecar._SECRET_FILTER.filter(record)
    assert secret not in record.getMessage()
    assert "***" in record.getMessage()


@pytest.mark.parametrize("credentials_first", [False, True])
def test_initialization_can_prioritize_credentials_without_changing_default(sidecar, monkeypatch, credentials_first):
    """ARM attaches with known credentials; other hosts retain the original first rung."""
    import MetaTrader5 as stub

    attempts = []
    monkeypatch.setenv("SP_INIT_CREDENTIALS_FIRST", "1" if credentials_first else "0")
    monkeypatch.setattr(stub, "initialize", lambda **kw: attempts.append(kw) or True)
    monkeypatch.setattr(sidecar, "_ensure_algo_trading", lambda reason: True)
    sidecar._initialize()
    assert len(attempts) == 1
    assert (attempts[0].get("login") == int(sidecar.MT5_LOGIN)) is credentials_first


def test_credentials_first_does_not_attach_to_old_account_during_relogin(sidecar, monkeypatch):
    """A rejected new login must not fall back to an existing credential-less session."""
    import MetaTrader5 as stub

    attempts = []
    monkeypatch.setenv("SP_INIT_CREDENTIALS_FIRST", "1")
    monkeypatch.setattr(stub, "initialize", lambda **kw: attempts.append(kw) or False)
    with pytest.raises(RuntimeError, match="every rung"):
        sidecar._initialize(require_credentials=True)
    assert len(attempts) == 1
    assert attempts[0]["login"] == int(sidecar.MT5_LOGIN)


TradeResult = namedtuple("TradeResult", "retcode comment")


def _load_finder():
    path = SIDECAR.parent / "find_algo_button.py"
    spec = importlib.util.spec_from_file_location("sp_find_algo_button", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocked(_req: Any = None) -> Any:
    return TradeResult(10027, "AutoTrading disabled by client")


def _placed(ticket: int = 4242) -> Any:
    return SimpleNamespace(retcode=10008, comment="Placed", order=ticket)


def _is_probe_send(req: Any) -> bool:
    if not isinstance(req, dict):
        return False
    return int(req.get("magic") or 0) == 770070 or req.get("comment") == "algo-probe"


def _is_probe_cancel(req: Any) -> bool:
    return isinstance(req, dict) and int(req.get("action") or 0) == 8


@pytest.fixture
def algo(sidecar):
    """Reset toggle bookkeeping so module-scoped sidecar state cannot leak between tests."""
    import MetaTrader5 as stub

    sidecar._reset_algo_state()
    saved = {
        "settle": sidecar.ALGO_SETTLE_SECONDS,
        "poll": sidecar.ALGO_POLL_INTERVAL,
        "cool": sidecar.ALGO_TOGGLE_COOLDOWN,
        "info": stub.terminal_info,
        "send": stub.order_send,
        "check": stub.order_check,
        "linux": sidecar._request_linux_ctrl_e,
        "user32": sidecar._user32_ctrl_e,
        "sp_dir": sidecar._sp_dir,
        "click": sidecar._SEND_TOOLBAR_CLICK,
        "user32_hook": sidecar._SEND_USER32_CTRL_E,
    }
    sidecar.ALGO_SETTLE_SECONDS = 0.0
    sidecar.ALGO_POLL_INTERVAL = 0.0
    sidecar.ALGO_TOGGLE_COOLDOWN = 120.0
    sidecar._SEND_CTRL_E = None
    sidecar._SEND_TOOLBAR_CLICK = lambda: False
    sidecar._SEND_USER32_CTRL_E = lambda: False
    yield sidecar
    stub.terminal_info = saved["info"]
    stub.order_send = saved["send"]
    stub.order_check = saved["check"]
    sidecar._request_linux_ctrl_e = saved["linux"]
    sidecar._user32_ctrl_e = saved["user32"]
    sidecar._sp_dir = saved["sp_dir"]
    sidecar.ALGO_SETTLE_SECONDS = saved["settle"]
    sidecar.ALGO_POLL_INTERVAL = saved["poll"]
    sidecar.ALGO_TOGGLE_COOLDOWN = saved["cool"]
    sidecar._reset_algo_state()
    sidecar._SEND_CTRL_E = None
    sidecar._SEND_TOOLBAR_CLICK = saved["click"]
    sidecar._SEND_USER32_CTRL_E = saved["user32_hook"]


def test_sidecar_image_ships_xdotool_and_utf16le_guard() -> None:
    """Fresh VPS images must include the Ctrl+E helper and refuse a UTF-8 common.ini."""
    dockerfile = (SIDECAR.parent / "Dockerfile").read_text()
    assert "xdotool" in dockerfile
    assert "scrot" in dockerfile
    assert "python3-pil" in dockerfile
    entry = (SIDECAR.parent / "entrypoint.sh").read_text()
    assert "assert_common_ini_utf16le" in entry
    assert "iconv -f UTF-8 -t UTF-16LE" in entry
    helper = (SIDECAR.parent / "enable_algo_trading.sh").read_text()
    assert "ctrl+e" in helper
    assert "--watch" in helper
    assert "click_algo_button" in helper
    assert "mousemove" in helper
    assert "find_algo_button.py" in helper
    assert "xdotool windowactivate" not in helper
    assert (SIDECAR.parent / "find_algo_button.py").is_file()


def test_ensure_algo_trading_is_noop_when_already_on(algo) -> None:
    """A second Ctrl+E would turn AutoTrading off — never send one when it is already on."""
    toggles: list[int] = []
    algo._SEND_CTRL_E = lambda: toggles.append(1) or True
    assert algo._ensure_algo_trading("initialize") is True
    assert toggles == []


def test_ensure_sends_one_ctrl_e_then_polls_until_flag_is_on(algo) -> None:
    """The toolbar flag lags the keypress — poll, do not send a second Ctrl+E."""
    import MetaTrader5 as stub

    reads = {"n": 0}
    toggles = {"n": 0}

    def fake_info():
        reads["n"] += 1
        # First reads (before/just after send) stay false; a later poll sees true.
        return SimpleNamespace(
            trade_allowed=(toggles["n"] == 1 and reads["n"] >= 3),
            tradeapi_disabled=False,
            connected=True,
            build=1,
        )

    def fake_toggle() -> bool:
        toggles["n"] += 1
        return True

    stub.terminal_info = fake_info
    algo.ALGO_SETTLE_SECONDS = 5.0
    algo.ALGO_POLL_INTERVAL = 0.0
    algo._SEND_CTRL_E = fake_toggle
    assert algo._ensure_algo_trading("initialize") is True
    assert toggles["n"] == 1, "a second Ctrl+E would flip the toolbar back off"
    assert reads["n"] >= 3, "must poll trade_allowed after the keypress instead of failing immediately"


def test_second_ctrl_e_is_blocked_while_flag_stays_false(algo) -> None:
    """'Still false after the first send' is lag, not a reason to toggle again."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}

    stub.terminal_info = lambda: info
    algo.ALGO_TOGGLE_COOLDOWN = 999.0
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1
    assert algo._ensure_algo_trading("watchdog") is False
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1


def test_second_ctrl_e_allowed_only_after_observed_on_then_off(algo) -> None:
    """A real disable (True → False) may be toggled back on even inside the cooldown."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}

    stub.terminal_info = lambda: info
    algo.ALGO_TOGGLE_COOLDOWN = 999.0
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1
    info.trade_allowed = True
    assert algo._ensure_algo_trading("watchdog") is True
    assert toggles["n"] == 1
    info.trade_allowed = False
    assert algo._ensure_algo_trading("watchdog") is False
    assert toggles["n"] == 2


def test_probe_order_check_10013_is_not_treated_as_enabled(algo) -> None:
    """The production false positive: order_check 10013 while live order_send is 10027."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}
    stub.terminal_info = lambda: info
    stub.order_check = lambda _req: TradeResult(10013, "invalid")
    stub.order_send = _blocked
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1
    assert algo._ALGO_PROBE_OK is False
    assert algo._ensure_algo_trading("watchdog") is False
    assert toggles["n"] == 1


def test_probe_order_send_placed_then_cancelled_counts_as_enabled(algo) -> None:
    """A far pending that actually places (then cancelled) is the only probe success."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}
    cancelled: list[int] = []

    def fake_send(req: Any) -> Any:
        if _is_probe_cancel(req):
            cancelled.append(int(req["order"]))
            return TradeResult(10009, "Done")
        if _is_probe_send(req):
            return _placed(99)
        return _blocked(req)

    stub.terminal_info = lambda: info
    stub.order_check = lambda _req: TradeResult(10013, "invalid")
    stub.order_send = fake_send
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is True
    assert toggles["n"] == 1
    assert cancelled == [99]
    assert algo._ensure_algo_trading("watchdog") is True
    assert toggles["n"] == 1, "a successful probe must not be followed by another Ctrl+E"


def test_toolbar_click_runs_after_ctrl_e_when_send_probe_is_10027(algo) -> None:
    """Wine drops Ctrl+E; the red toolbar button is the alternative enable path."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    keys = {"n": 0}
    clicks = {"n": 0}

    def after_click() -> bool:
        clicks["n"] += 1
        info.trade_allowed = True
        return True

    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo._SEND_CTRL_E = lambda: keys.__setitem__("n", keys["n"] + 1) or True
    algo._SEND_TOOLBAR_CLICK = after_click
    assert algo._ensure_algo_trading("initialize") is True
    assert keys["n"] == 1
    assert clicks["n"] == 1
    assert algo._ensure_algo_trading("watchdog") is True
    assert keys["n"] == 1
    assert clicks["n"] == 1, "click is also a toggle — never send a second one"


def test_toolbar_click_is_not_repeated_while_still_false(algo) -> None:
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    clicks = {"n": 0}
    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo._SEND_CTRL_E = lambda: True
    algo._SEND_TOOLBAR_CLICK = lambda: clicks.__setitem__("n", clicks["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert clicks["n"] == 1
    assert algo._ensure_algo_trading("initialize") is False
    assert clicks["n"] == 1


def test_watchdog_does_not_resend_after_cooldown_if_never_saw_true(algo) -> None:
    """Watchdog must not spam-toggle just because the settle window and cooldown elapsed."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}
    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo.ALGO_TOGGLE_COOLDOWN = 0.0
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1
    assert algo._ensure_algo_trading("watchdog") is False
    assert algo._ensure_algo_trading("watchdog") is False
    assert toggles["n"] == 1


def test_second_ctrl_e_after_cooldown_only_if_probe_still_10027(algo) -> None:
    """Cooldown expiry on the 10027 path may send once more, and only if still blocked."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}
    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo.ALGO_TOGGLE_COOLDOWN = 0.0
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1
    assert algo._ensure_algo_trading("order_send_10027") is False
    assert toggles["n"] == 2


def test_second_ctrl_e_after_cooldown_skipped_when_probe_placed(algo) -> None:
    """A stale false flag after cooldown must not toggle a working terminal off."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}
    sends = {"n": 0}

    def fake_send(req: Any) -> Any:
        if _is_probe_cancel(req):
            return TradeResult(10009, "Done")
        if _is_probe_send(req):
            sends["n"] += 1
            # Ctrl+E poll + user32 poll + click poll all fire a probe during the first ensure.
            if sends["n"] <= 3:
                return _blocked(req)
            return _placed(7)
        return _blocked(req)

    stub.terminal_info = lambda: info
    stub.order_send = fake_send
    algo.ALGO_TOGGLE_COOLDOWN = 0.0
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1
    assert algo._ensure_algo_trading("initialize") is True
    assert toggles["n"] == 1


def test_send_one_ctrl_e_does_not_fall_through_to_user32_when_helper_ready(algo, tmp_path) -> None:
    """A missed helper ack is not a reason to send a second Ctrl+E via user32 in the same step."""
    (tmp_path / "enable_algo.ready").write_text("1")
    calls = {"linux": 0, "user32": 0}
    algo._sp_dir = lambda: str(tmp_path)
    algo._request_linux_ctrl_e = lambda timeout=None: (calls.__setitem__("linux", calls["linux"] + 1) or False)
    algo._user32_ctrl_e = lambda: (calls.__setitem__("user32", calls["user32"] + 1) or True)
    algo._SEND_CTRL_E = None
    algo._SEND_USER32_CTRL_E = None
    assert algo._send_one_ctrl_e() is False
    assert calls == {"linux": 1, "user32": 0}


def test_user32_runs_once_when_helper_ack_is_not_effective(algo, tmp_path) -> None:
    """ack ≠ enable: after helper Ctrl+E, a 10027 probe must fall through to user32 once."""
    import MetaTrader5 as stub

    (tmp_path / "enable_algo.ready").write_text("1")
    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    calls = {"linux": 0, "user32": 0}
    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo._sp_dir = lambda: str(tmp_path)
    algo._SEND_CTRL_E = None
    algo._SEND_TOOLBAR_CLICK = lambda: False
    algo._request_linux_ctrl_e = lambda timeout=None: (calls.__setitem__("linux", calls["linux"] + 1) or True)

    def user32() -> bool:
        calls["user32"] += 1
        info.trade_allowed = True
        return True

    algo._SEND_USER32_CTRL_E = user32
    assert algo._ensure_algo_trading("initialize") is True
    assert calls == {"linux": 1, "user32": 1}
    assert algo._ensure_algo_trading("watchdog") is True
    assert calls == {"linux": 1, "user32": 1}, "user32 is also a toggle — never send a second one"


def test_user32_is_tried_before_toolbar_click(algo, tmp_path) -> None:
    """Prefer user32 earlier on Wine: helper ack + 10027 → user32, then click."""
    import MetaTrader5 as stub

    (tmp_path / "enable_algo.ready").write_text("1")
    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    order: list[str] = []
    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo._sp_dir = lambda: str(tmp_path)
    algo._SEND_CTRL_E = None
    algo._request_linux_ctrl_e = lambda timeout=None: (order.append("linux") or True)
    algo._SEND_USER32_CTRL_E = lambda: (order.append("user32") or False)
    algo._SEND_TOOLBAR_CLICK = lambda: (order.append("click") or False)
    assert algo._ensure_algo_trading("initialize") is False
    assert order == ["linux", "user32", "click"]


def test_user32_fallback_is_not_repeated_while_still_false(algo) -> None:
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    user32 = {"n": 0}
    stub.terminal_info = lambda: info
    stub.order_send = _blocked
    algo._SEND_CTRL_E = lambda: True
    algo._SEND_USER32_CTRL_E = lambda: user32.__setitem__("n", user32["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert user32["n"] == 1
    assert algo._ensure_algo_trading("initialize") is False
    assert user32["n"] == 1


def test_order_send_retries_once_after_10027(algo) -> None:
    """10027 means the request was not accepted, so one retry cannot duplicate it."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    calls = {"send": 0, "toggle": 0}

    def fake_send(_req: Any) -> Any:
        calls["send"] += 1
        if calls["send"] == 1:
            return TradeResult(10027, "AutoTrading disabled by client")
        return TradeResult(10009, "Done")

    def fake_toggle() -> bool:
        calls["toggle"] += 1
        info.trade_allowed = True
        return True

    stub.order_send = fake_send
    stub.terminal_info = lambda: info
    algo._SEND_CTRL_E = fake_toggle
    result = algo.METHODS["order_send"](
        {"action": 1, "symbol": "XAUUSDm", "volume": 0.01, "type": 0, "price": 1.0}
    )
    assert result.retcode == 10009
    assert calls["send"] == 2
    assert calls["toggle"] == 1


def test_order_send_10027_during_cooldown_does_not_send_another_ctrl_e(algo) -> None:
    """The lagging-flag case: first enable already fired; 10027 must not flip the toolbar off."""
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    toggles = {"n": 0}
    stub.terminal_info = lambda: info
    algo.ALGO_TOGGLE_COOLDOWN = 999.0
    algo._SEND_CTRL_E = lambda: toggles.__setitem__("n", toggles["n"] + 1) or True
    assert algo._ensure_algo_trading("initialize") is False
    assert toggles["n"] == 1

    stub.order_send = lambda _req: TradeResult(10027, "AutoTrading disabled by client")
    result = algo.METHODS["order_send"](
        {"action": 1, "symbol": "XAUUSDm", "volume": 0.01, "type": 0, "price": 1.0}
    )
    assert result.retcode == 10027
    assert toggles["n"] == 1


def test_order_send_does_not_retry_10027_when_enable_fails(algo) -> None:
    import MetaTrader5 as stub

    info = SimpleNamespace(trade_allowed=False, tradeapi_disabled=False, connected=True, build=1)
    calls = {"send": 0}

    def fake_send(_req: Any) -> Any:
        if int((_req or {}).get("action") or 0) == 1:
            calls["send"] += 1
        return TradeResult(10027, "AutoTrading disabled by client")

    stub.order_send = fake_send
    stub.terminal_info = lambda: info
    algo._SEND_CTRL_E = lambda: True
    result = algo.METHODS["order_send"](
        {"action": 1, "symbol": "XAUUSDm", "volume": 0.01, "type": 0, "price": 1.0}
    )
    assert result.retcode == 10027
    assert calls["send"] == 1, "a failed enable must not retry — still no duplicate, but pointless"


def test_order_send_success_does_not_toggle(algo) -> None:
    import MetaTrader5 as stub

    toggles: list[int] = []
    stub.order_send = lambda _req: TradeResult(10009, "Done")
    algo._SEND_CTRL_E = lambda: toggles.append(1) or True
    result = algo.METHODS["order_send"](
        {"action": 1, "symbol": "XAUUSDm", "volume": 0.01, "type": 0, "price": 1.0}
    )
    assert result.retcode == 10009
    assert toggles == []


def test_find_red_button_centroid_is_the_off_control_not_green() -> None:
    """The click target is the red OFF control; green ON pixels must not match."""
    finder = _load_finder()
    width, height = 200, 40
    pixels = bytearray(width * height * 3)
    # Green "already on" wash on the left — must be ignored.
    for y in range(height):
        for x in range(20, 50):
            i = (y * width + x) * 3
            pixels[i] = 20
            pixels[i + 1] = 180
            pixels[i + 2] = 80
    # Red Algo Trading OFF button.
    for y in range(8, 28):
        for x in range(90, 150):
            i = (y * width + x) * 3
            pixels[i] = 200
            pixels[i + 1] = 40
            pixels[i + 2] = 40
    found = finder.find_red_button(bytes(pixels), width, height, margin_left=10, margin_right=10, min_pixels=20)
    assert found is not None
    x, y = found
    assert 90 <= x <= 150
    assert 8 <= y <= 28
