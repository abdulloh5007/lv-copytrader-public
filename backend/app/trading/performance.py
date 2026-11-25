"""Desk calendar, rebuilt from the persisted position journal."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.db import get_setting, session_scope
from app.models import Trade
from app.util.logging import get_logger

TZ = ZoneInfo("Asia/Tashkent")
log = get_logger(__name__)


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    return month_start(month_start(day) + timedelta(days=32))


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def utc_start(day: date) -> datetime:
    return datetime.combine(day, time.min, TZ).astimezone(UTC).replace(tzinfo=None)


async def calendar_data(config, start: date, end: date, *, today: date) -> dict:
    coverage = config.PERFORMANCE_START_DATE
    start_at, end_at = utc_start(max(start, coverage or start)), utc_start(end)
    async with session_scope() as session:
        rows = (await session.execute(select(
            Trade.opened_at, Trade.closed_at, Trade.profit, Trade.close_cause, Trade.status,
        ).where(
            Trade.kind == "position", Trade.external.is_(False),
            Trade.magic == config.MAGIC_NUMBER, Trade.signal_ref.is_not(None),
            or_((Trade.opened_at >= start_at) & (Trade.opened_at < end_at),
                (Trade.closed_at >= start_at) & (Trade.closed_at < end_at)),
        ))).all()
    grouped = {}
    opened = {}
    for opened_at, closed_at, profit, cause, status in rows:
        if opened_at is not None and start_at <= opened_at < end_at:
            day = opened_at.replace(tzinfo=UTC).astimezone(TZ).date()
            opened[day] = opened.get(day, 0) + 1
        if status == "closed" and closed_at is not None and start_at <= closed_at < end_at:
            day = closed_at.replace(tzinfo=UTC).astimezone(TZ).date()
            grouped.setdefault(day, []).append((profit, cause))
    days = []
    for offset in range((end - start).days):
        day = start + timedelta(days=offset)
        positions = grouped.get(day, [])
        amounts = [Decimal(str(profit)) for profit, _ in positions
                   if profit is not None and Decimal(str(profit)).is_finite()]
        net = sum(amounts, Decimal(0))
        missing = len(positions) - len(amounts)
        state = ("future" if day > today else "untracked" if coverage and day < coverage else
                 "unknown" if missing else "win" if net > 0 else "loss" if net < 0 else
                 "flat" if positions else "open" if day in opened else
                 "weekend" if day.weekday() >= 5 else "idle")
        days.append({
            "date": day.isoformat(), "net": float(net), "positions": len(positions), "opened": opened.get(day, 0),
            "wins": sum(amount > 0 for amount in amounts), "losses": sum(amount < 0 for amount in amounts),
            "stops": sum(cause == "sl" for _, cause in positions), "unknown": missing,
            "weekend": day.weekday() >= 5, "state": state,
        })
    for state in ("win", "loss"):
        matching = [day for day in days if day["state"] == state]
        maximum = max((abs(day["net"]) for day in matching), default=1)
        for day in matching:
            size = abs(day["net"])
            day["level"] = 1 + int(size > maximum / 3) + int(size > 2 * maximum / 3)
    return {
        "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat(), "days": days,
        "net": float(sum((Decimal(str(day["net"])) for day in days), Decimal(0))),
        "active_days": sum(bool(day["positions"] or day["opened"]) for day in days),
        "win_days": sum(day["state"] == "win" for day in days),
        "loss_days": sum(day["state"] == "loss" for day in days),
        "unknown": sum(day["unknown"] for day in days),
    }


COLORS = {"win": ("#dbf4e2", "#abdfb7", "#67c886"), "loss": ("#ffe0e1", "#f7b6bb", "#f28b93"),
          "weekend": ("#e8d5be",), "idle": ("#fff0b3",), "flat": ("#e4e8ed",),
          "unknown": ("#ece3f7",), "untracked": ("#f5f5f5",), "open": ("#dcecf7",)}


def report_html(period: str, data: dict, currency: str, simulation: list[dict] | None = None) -> bytes:
    title = "Trading week" if period == "week" else "Trading month"
    cells = ["<td></td>"] * date.fromisoformat(data["start"]).weekday()
    for day in data["days"]:
        color = COLORS.get(day["state"], ("#fff",))[day.get("level", 1) - 1]
        amount = f'{day["net"]:+.2f}' if day["positions"] else "—"
        if day["unknown"]:
            amount += f' ({day["unknown"]} unknown)'
        cells.append(f'<td style="background:{color}"><b>{int(day["date"][-2:])}</b><br>{amount}</td>')
    cells.extend(["<td></td>"] * (-len(cells) % 7))
    grid = "".join("<tr>" + "".join(cells[i:i+7]) + "</tr>" for i in range(0, len(cells), 7))
    weekdays = "".join(f"<th>{name}</th>" for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    detail = "".join(
        f'<tr><td>{d["date"]}</td><td>{d["opened"]}</td><td>{d["positions"]}</td><td>{d["wins"]}</td>'
        f'<td>{d["losses"]}</td><td>{d["stops"]}</td><td>{d["net"]:+.2f}</td></tr>'
        for d in data["days"] if d["positions"] or d["opened"])
    paper = "".join(
        f'<tr><td>{escape(d["date"])}</td><td>{d["gross"]:+.2f} {escape(d["currency"])}</td>'
        f'<td>{d["wins"]}</td><td>{d["losses"]}</td><td>{d["open"]}</td></tr>' for d in simulation or ()
    )
    paper_section = (('<h2>Simulation after the daily goal</h2><p>Estimated gross P&amp;L before costs.</p>'
                      '<table><thead><tr><th>Date</th><th>P&amp;L</th><th>Wins</th><th>Losses</th><th>Open</th>'
                      f'</tr></thead><tbody>{paper}</tbody></table>') if paper else '')
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>LV Copytrader · {title}</title><style>body{{font:16px system-ui;margin:24px;max-width:880px}}'
            'table{width:100%;border-spacing:4px}td,th{padding:6px;text-align:center;border-radius:8px}'
            '@media print{td{print-color-adjust:exact}}</style>'
            f'<h1>LV Copytrader · {title}</h1><p>{data["start"]} – {data["end"]} · Asia/Tashkent</p>'
            f'<h2>{data["net"]:+.2f} {escape(currency)}</h2>'
            f'<p>Trading days {data["active_days"]} · Positive {data["win_days"]} · Negative {data["loss_days"]}</p>'
            f'<table><thead><tr>{weekdays}</tr></thead><tbody>{grid}</tbody></table>'
            '<p>Green: profit · Red: loss · Brown: weekend · Yellow: no trades · Blue: entries without a close · Grey: flat or outside coverage</p>'
            '<table><thead><tr><th>Date</th><th>Opened</th><th>Closed</th><th>Wins</th><th>Losses</th><th>SL</th><th>Known net</th></tr></thead>'
            f'<tbody>{detail}</tbody></table><p>Unknown results: {data["unknown"]}. Amounts are recorded at position closure.</p>'
            f'{paper_section}</html>').encode()


async def run_performance(runtime, app_state) -> None:
    config = runtime.settings
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date()
            week, month = week_start(today), month_start(today)
            if config.PERFORMANCE_PUBLIC:
                app_state.performance_snapshot = {
                    "today": today.isoformat(), "currency": config.PERFORMANCE_CURRENCY, "timezone": TZ.key,
                    "week": await calendar_data(config, week, week + timedelta(days=7), today=today),
                    "month": await calendar_data(config, month, next_month(month), today=today),
                }
            if config.PERFORMANCE_TELEGRAM and now.hour >= 8:
                for name, start, end in (("week", week - timedelta(days=7), week),
                                         ("month", month_start(month - timedelta(days=1)), month)):
                    if end <= config.PERFORMANCE_START_DATE:
                        continue
                    key = f"performance.report.{name}.{end}"
                    async with session_scope() as session:
                        prior = await get_setting(session, key)
                    if not prior:
                        from app.trading.daily_simulation import period_totals

                        data = await calendar_data(config, start, end, today=today)
                        simulation = await period_totals(start.isoformat(), end.isoformat())
                        await runtime.notifier.send_performance_report(
                            key, name, data, report_html(name, data, config.PERFORMANCE_CURRENCY, simulation))
        except Exception:
            log.exception("performance_refresh_failed")
        await asyncio.sleep(60)
