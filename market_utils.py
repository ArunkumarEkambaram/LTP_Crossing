"""
market_utils.py — index LTP -> nearest strike -> strike window helpers (Nifty and Sensex).
"""
from datetime import datetime, timedelta
import pytz

import config

IST = pytz.timezone("Asia/Kolkata")


def _parse_hhmm(text: str):
    hh, mm = text.split(":")
    return int(hh), int(mm)


def is_market_open(now=None) -> bool:
    """True on a weekday (not in config.MARKET_HOLIDAYS) between config.MARKET_OPEN
    (inclusive) and config.MARKET_CLOSE (exclusive), IST. `now` defaults to the current
    IST time; a naive datetime is treated as IST."""
    now = now or datetime.now(IST)
    if now.tzinfo is not None:
        now = now.astimezone(IST)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in config.MARKET_HOLIDAYS:
        return False
    minutes = now.hour * 60 + now.minute
    open_h, open_m = _parse_hhmm(config.MARKET_OPEN)
    close_h, close_m = _parse_hhmm(config.MARKET_CLOSE)
    return open_h * 60 + open_m <= minutes < close_h * 60 + close_m


def nearest_strike(ltp: float, interval: int = config.STRIKE_INTERVAL) -> int:
    """Rounds Nifty LTP to the nearest tradable strike."""
    return int(round(ltp / interval) * interval)


def build_strike_window(ltp: float, interval: int = None) -> list:
    """
    Returns the list of strikes to watch: nearest strike +/- N
    (N = STRIKES_EACH_SIDE). `interval` is the index's strike spacing (default: Nifty's).
    e.g. ltp=24408 -> nearest=24400 -> [24300, 24350, 24400, 24450, 24500]
    """
    interval = interval or config.STRIKE_INTERVAL
    center = nearest_strike(ltp, interval)
    n = config.STRIKES_EACH_SIDE
    return [center + (i * interval) for i in range(-n, n + 1)]


def current_weekly_expiry(client, underlying_key: str = None):
    """
    Fetches the nearest expiry date for the index's options (default: Nifty).
    Returns a date string (e.g. '2026-07-10') or None on failure.
    """
    expiries, err = client.fetch_option_expiries(underlying_key or config.NIFTY_OPTION_UNDERLYING_KEY)
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


# (underlying, expiry) -> {strike: {"CE": instrument_key, "PE": instrument_key}}
_option_key_cache: dict = {}


def option_instrument_keys(client, expiry: str, underlying_key: str = None):
    """
    CE/PE instrument keys for every strike of the given expiry, fetched once and
    cached per index. Returns ({strike: {"CE": key, "PE": key}}, None) or (None, error).
    `underlying_key` defaults to Nifty.
    """
    underlying_key = underlying_key or config.NIFTY_OPTION_UNDERLYING_KEY
    cache_key = (underlying_key, expiry)
    if cache_key in _option_key_cache:
        return _option_key_cache[cache_key], None
    contracts, err = client.fetch_option_contracts(underlying_key, expiry)
    if contracts is None:
        return None, err
    mapping: dict = {}
    for c in contracts:
        strike, opt, ik = c.get("strike_price"), c.get("instrument_type"), c.get("instrument_key")
        if strike is None or opt not in ("CE", "PE") or not ik:
            continue
        mapping.setdefault(int(strike), {})[opt] = ik
    _option_key_cache[cache_key] = mapping
    return mapping, None
