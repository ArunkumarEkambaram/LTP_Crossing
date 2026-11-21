"""
match_logic.py — shared CE/PE match rule used by both scanner.py (live) and
backtest.py, so live and backtest never drift apart on what counts as a match.

Match rule (near strike, same strike, rounded):
  1. Only while the index is NEAR a strike: strike - band < index LTP <= strike + band
     (near_strike(); NIFTY band 10: 25091-25110 -> 25100, 25111-25140 -> nothing checked).
  2. Only that strike's own CE against its own PE (same strike price).
  3. Both prices are rounded to the nearest whole number (half rounds UP: 130.25 -> 130,
     130.50 -> 131, 130.72 -> 131); equal rounded prices = a match.
"""
import math


def round_half_up(value: float) -> int:
    """Round to nearest integer, with .5 always rounding up (not Python's
    banker's rounding). The tiny epsilon guards against float noise such as
    130.49999999999997 coming back from candle data."""
    return int(math.floor(value + 0.5 + 1e-9))


def near_strike(index_ltp: float, interval: int, band: float):
    """The strike the index is near (strike - band < LTP <= strike + band), or None between bands."""
    if index_ltp is None:
        return None
    strike = int(round(index_ltp / interval) * interval)
    for s in (strike - interval, strike, strike + interval):       # guards rounding at the band edges
        if s - band < index_ltp <= s + band:
            return s
    return None


def find_matches(quotes: dict) -> list:
    """
    quotes: {strike: {"CE": ltp_or_None, "PE": ltp_or_None}}

    Only a strike's own CE against its own PE (pass just the near strike's quotes).
    Returns one dict per matching strike:
    {"ce_strike", "pe_strike", "price" (the shared rounded value),
     "ce_ltp", "pe_ltp" (the actual unrounded prices)}
    """
    matches = []
    for strike, p in quotes.items():
        ce_val, pe_val = p.get("CE"), p.get("PE")
        if ce_val is None or pe_val is None:
            continue
        if round_half_up(ce_val) == round_half_up(pe_val):
            matches.append({
                "ce_strike": strike,
                "pe_strike": strike,
                "price": round_half_up(ce_val),
                "ce_ltp": ce_val,
                "pe_ltp": pe_val,
            })
    return matches
