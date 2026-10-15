"""
match_logic.py — shared CE/PE match rule used by both scanner.py (live) and
backtest.py, so live and backtest never drift apart on what counts as a match.

Match rule (cross-strike, rounded): every CE and PE price in the tracked
window is rounded to the nearest whole number (half rounds UP: 130.25 -> 130,
130.50 -> 131, 130.72 -> 131). ANY rounded CE equal to ANY rounded PE is a
match — they do not need to be at the same strike. A same-strike match is
just one case of this general rule.
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

    Returns one dict per matching (CE, PE) pair:
    {"ce_strike", "pe_strike", "price" (the shared rounded value),
     "ce_ltp", "pe_ltp" (the actual unrounded prices)}
    """
    ce_list = [(s, p.get("CE")) for s, p in quotes.items() if p.get("CE") is not None]
    pe_list = [(s, p.get("PE")) for s, p in quotes.items() if p.get("PE") is not None]

    matches = []
    for ce_strike, ce_val in ce_list:
        ce_rounded = round_half_up(ce_val)
        for pe_strike, pe_val in pe_list:
            if ce_rounded == round_half_up(pe_val):
                matches.append({
                    "ce_strike": ce_strike,
                    "pe_strike": pe_strike,
                    "price": ce_rounded,
                    "ce_ltp": ce_val,
                    "pe_ltp": pe_val,
                })
    return matches
