"""
backtest.py — replays a chosen day's 1-minute candles to find every raw
CE == PE crossing that would have happened, ignoring the live cooldown.

Same rule as the live scanner (match_logic.py):
  - Each 1-minute candle: the window = nearest strike to the Nifty close +/- STRIKES_EACH_SIDE.
  - Each strike's own CE close vs its own PE close, both rounded half-up.
  - Cooldown/freeze is ignored entirely — every raw match is reported.
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

    # 2. Window of every minute (nearest strike +/- STRIKES_EACH_SIDE)
    cfg = config.INDEXES["NIFTY"]
    window_by_ts = {c[0]: market_utils.build_strike_window(float(c[4]), cfg["strike_interval"]) for c in nifty_candles}
    strikes = sorted({s for w in window_by_ts.values() for s in w})
    summary["strikes"] = strikes
    log_fn(f"Backtest {date_str}: open={day_open} close={day_close} strikes={strikes}")
    if not strikes:
        return summary

    # 3. Resolve CE/PE instrument keys for each strike, for this expiry
    ik_map, err = client.fetch_option_instrument_keys(
        config.NIFTY_OPTION_UNDERLYING_KEY, expiry_date, strikes
    )
    if ik_map is None:
        summary["errors"].append(f"Option contract lookup failed: {err}")
        return summary

    # 4. 1-min closes of both legs of every strike
    series = {}  # strike -> {"CE": {ts: close}, "PE": {ts: close}}
    for strike in strikes:
        legs = {}
        for opt in ("CE", "PE"):
            ik = ik_map.get(strike, {}).get(opt)
            if not ik:
                summary["errors"].append(f"Strike {strike}: missing {opt} instrument_key for this expiry.")
                break
            candles, e = client.fetch_minute_candles(ik, date_str)
            if candles is None:
                summary["errors"].append(f"Strike {strike} {opt} candle fetch failed: {e}")
                break
            legs[opt] = {c[0]: c[4] for c in candles}
        if len(legs) == 2:
            series[strike] = legs

    # 5. Each minute: CE = PE at each strike of that minute's window
    for c in nifty_candles:
        ts = c[0]
        quotes = {s: {"CE": series[s]["CE"].get(ts), "PE": series[s]["PE"].get(ts)}
                  for s in window_by_ts[ts] if s in series}
        for m in match_logic.find_matches(quotes):
            summary["events"].append({
                "time": ts[11:16] if len(ts) >= 16 else ts,
                "ce_strike": m["ce_strike"],
                "pe_strike": m["pe_strike"],
                "price": m["price"],
                "ce_ltp": round(m["ce_ltp"], 2),
                "pe_ltp": round(m["pe_ltp"], 2),
            })

    summary["events"].sort(key=lambda e: e["time"])
    log_fn(f"Backtest {date_str}: found {len(summary['events'])} raw match(es).")
    return summary
