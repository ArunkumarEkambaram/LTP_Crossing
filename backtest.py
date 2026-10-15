"""
backtest.py — replays a chosen day's 1-minute candles to find every raw
CE == PE crossing that would have happened, ignoring the live cooldown.

Design (per confirmed spec):
  - Strike window is FIXED for the whole day — the union of the
    nearest-strike +/-2 window computed from the day's OPEN price and the
    day's CLOSE price. Not recentered every minute like live mode.
  - Checked every 1-minute candle close (not tick-level).
  - Cooldown/freeze is ignored entirely — every raw match is reported.
  - Cross-strike, rounded matching: any rounded CE close in the window
    equal to any rounded PE close counts — same rule as the live scanner
    (match_logic.find_matches).
"""
import config
import market_utils
import match_logic


def run_backtest(client, date_str: str, expiry_date: str, log_fn=print) -> dict:
    """
    Returns a summary dict:
    {
      "date": ..., "expiry": ...,
      "day_open": float|None, "day_close": float|None,
      "strikes": [...],
      "events": [{"time": "HH:MM", "ce_strike": int, "pe_strike": int,
                  "price": int (rounded), "ce_ltp": float, "pe_ltp": float}, ...],
      "errors": [...],
    }
    """
    summary = {
        "date": date_str, "expiry": expiry_date,
        "day_open": None, "day_close": None,
        "strikes": [], "events": [], "errors": [],
    }

    # 1. Nifty 1-min candles for the day -> day open/close
    nifty_candles, err = client.fetch_minute_candles(config.NIFTY_INDEX_KEY, date_str)
    if nifty_candles is None:
        summary["errors"].append(f"Nifty candle fetch failed: {err}")
        return summary
    if not nifty_candles:
        summary["errors"].append(f"No Nifty candles returned for {date_str} — "
                                  f"check it's a trading day.")
        return summary

    day_open = nifty_candles[0][1]    # open of first (oldest) candle
    day_close = nifty_candles[-1][4]  # close of last (latest) candle
    summary["day_open"] = day_open
    summary["day_close"] = day_close

    # 2. Fixed strike window = union of open-based and close-based windows
    window_open = set(market_utils.build_strike_window(day_open))
    window_close = set(market_utils.build_strike_window(day_close))
    strikes = sorted(window_open | window_close)
    summary["strikes"] = strikes
    log_fn(f"Backtest {date_str}: open={day_open} close={day_close} strikes={strikes}")

    # 3. Resolve CE/PE instrument keys for each strike, for this expiry
    ik_map, err = client.fetch_option_instrument_keys(
        config.NIFTY_OPTION_UNDERLYING_KEY, expiry_date, strikes
    )
    if ik_map is None:
        summary["errors"].append(f"Option contract lookup failed: {err}")
        return summary

    # 4. Fetch 1-min candles for every CE/PE leg across all tracked strikes
    ce_series = {}  # strike -> {timestamp: close}
    pe_series = {}  # strike -> {timestamp: close}
    for strike in strikes:
        ce_key = ik_map.get(strike, {}).get("CE")
        pe_key = ik_map.get(strike, {}).get("PE")
        if not ce_key or not pe_key:
            summary["errors"].append(f"Strike {strike}: missing CE/PE instrument_key for this expiry.")
            continue

        ce_candles, err_ce = client.fetch_minute_candles(ce_key, date_str)
        if ce_candles is None:
            summary["errors"].append(f"Strike {strike} CE candle fetch failed: {err_ce}")
            continue

        pe_candles, err_pe = client.fetch_minute_candles(pe_key, date_str)
        if pe_candles is None:
            summary["errors"].append(f"Strike {strike} PE candle fetch failed: {err_pe}")
            continue

        ce_series[strike] = {c[0]: c[4] for c in ce_candles}
        pe_series[strike] = {c[0]: c[4] for c in pe_candles}

    if not ce_series or not pe_series:
        summary["errors"].append("No usable CE/PE candle data for any strike.")
        return summary

    # 5. Walk timestamps common to EVERY fetched leg, so each check point has
    #    a complete picture of all tracked strikes at that moment.
    all_time_sets = [set(d.keys()) for d in ce_series.values()] + [set(d.keys()) for d in pe_series.values()]
    common_times = sorted(set.intersection(*all_time_sets)) if all_time_sets else []

    for ts in common_times:
        quotes = {}
        for strike in strikes:
            quotes[strike] = {
                "CE": ce_series.get(strike, {}).get(ts),
                "PE": pe_series.get(strike, {}).get(ts),
            }

        raw_matches = match_logic.find_matches(quotes)
        if not raw_matches:
            continue

        time_label = ts[11:16] if len(ts) >= 16 else ts
        for m in raw_matches:
            summary["events"].append({
                "time": time_label,
                "ce_strike": m["ce_strike"],
                "pe_strike": m["pe_strike"],
                "price": m["price"],
                "ce_ltp": round(m["ce_ltp"], 2),
                "pe_ltp": round(m["pe_ltp"], 2),
            })

    summary["events"].sort(key=lambda e: e["time"])
    log_fn(f"Backtest {date_str}: found {len(summary['events'])} raw match(es).")
    return summary
