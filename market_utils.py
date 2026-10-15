"""
market_utils.py — Nifty LTP -> nearest strike -> strike window helpers.
"""
from datetime import datetime, timedelta
import pytz

import config

IST = pytz.timezone("Asia/Kolkata")


def nearest_strike(ltp: float, interval: int = config.STRIKE_INTERVAL) -> int:
    """Rounds Nifty LTP to the nearest tradable strike."""
    return int(round(ltp / interval) * interval)


def build_strike_window(ltp: float) -> list:
    """
    Returns the list of strikes to watch: nearest strike +/- N
    (N = STRIKES_EACH_SIDE).
    e.g. ltp=24408 -> nearest=24400 -> [24300, 24350, 24400, 24450, 24500]
    """
    center = nearest_strike(ltp)
    n = config.STRIKES_EACH_SIDE
    return [center + (i * config.STRIKE_INTERVAL) for i in range(-n, n + 1)]


def current_weekly_expiry(client):
    """
    Fetches the nearest weekly expiry date for Nifty options.
    Returns a date string (e.g. '2026-07-10') or None on failure.
    """
    expiries, err = client.fetch_option_expiries(config.NIFTY_OPTION_UNDERLYING_KEY)
    if err or not expiries:
        return None
    return expiries[0]  # ascending order -> nearest expiry first


def current_weekly_expiry_dates(client):
    """
    Returns (expiry_date_str, [valid_backtest_dates]) for the current weekly
    expiry cycle. Nifty weekly options expire on Tuesdays, so the cycle runs
    from the day after the previous Tuesday through this Tuesday. The date
    list excludes weekends and any date later than today (can't backtest a
    day that hasn't happened). It does NOT filter out exchange holidays —
    if a listed date has no trading data, the backtest will simply report
    that when you try to run it.
    """
    expiry_str = current_weekly_expiry(client)
    if not expiry_str:
        return None, []

    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    cycle_start = expiry_date - timedelta(days=6)  # previous Wednesday
    today = datetime.now(IST).date()

    dates = []
    d = cycle_start
    while d <= expiry_date:
        if d.weekday() < 5 and d <= today:  # Mon-Fri only, not in the future
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return expiry_str, dates


# expiry -> {strike: {"CE": instrument_key, "PE": instrument_key}}
_option_key_cache: dict = {}


def option_instrument_keys(client, expiry: str):
    """
    CE/PE instrument keys for every strike of the given expiry, fetched once and
    cached. Returns ({strike: {"CE": key, "PE": key}}, None) or (None, error).
    """
    if expiry in _option_key_cache:
        return _option_key_cache[expiry], None
    contracts, err = client.fetch_option_contracts(config.NIFTY_OPTION_UNDERLYING_KEY, expiry)
    if contracts is None:
        return None, err
    mapping: dict = {}
    for c in contracts:
        strike, opt, ik = c.get("strike_price"), c.get("instrument_type"), c.get("instrument_key")
        if strike is None or opt not in ("CE", "PE") or not ik:
            continue
        mapping.setdefault(int(strike), {})[opt] = ik
    _option_key_cache[expiry] = mapping
    return mapping, None
