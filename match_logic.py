"""
match_logic.py — shared CE/PE match rule used by both scanner.py (live) and
backtest.py, so live and backtest never drift apart on what counts as a match.

Match rule (same strike, rounded): for each strike in the window (nearest strike to the
index +/- STRIKES_EACH_SIDE, default 1 -> 3 strikes), that strike's own CE is compared with
its own PE. Both prices are rounded to the nearest whole number (half rounds UP: 130.25 -> 130,
130.50 -> 131, 130.72 -> 131); equal rounded prices = a match. A strike's CE only equals its
PE when the market is right at that strike, so this fires only near the 50s / 100s.
"""
import math


def round_half_up(value: float) -> int:
    """Round to nearest integer, with .5 always rounding up (not Python's
    banker's rounding). The tiny epsilon guards against float noise such as
    130.49999999999997 coming back from candle data."""
    return int(math.floor(value + 0.5 + 1e-9))


def find_matches(quotes: dict) -> list:
    """
    quotes: {strike: {"CE": ltp_or_None, "PE": ltp_or_None}}

    Only a strike's own CE against its own PE.
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
