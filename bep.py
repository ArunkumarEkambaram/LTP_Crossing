"""
bep.py — Break-Even Point (BEP) per strike.

    BEP = (CE_PDC + PE_PDC) / 2

PDC = previous day's close. Both legs of a strike share the same BEP. The
previous-day close never changes during the trading day, so each strike's BEP
is fetched once per day and cached; only strikes that newly enter the watched
window cost any API calls.

For a cross-strike match (CE at strike X = PE at strike Y) the CE is compared
with the BEP of strike X and the PE with the BEP of strike Y.
"""
from datetime import datetime

import market_utils

# (expiry, strike, date) -> BEP. Only successful lookups are cached, so a
# strike that failed (network blip, no data) is retried next time.
_bep_cache: dict = {}


def get_bep(client, expiry: str, strike: int):
    """Returns the BEP for the strike (rounded to 2 decimals), or None if either
    leg's previous-day close isn't available."""
    today = datetime.now(market_utils.IST).strftime("%Y-%m-%d")
    key = (expiry, strike, today)
    if key in _bep_cache:
        return _bep_cache[key]

    mapping, _ = market_utils.option_instrument_keys(client, expiry)
    legs = (mapping or {}).get(strike, {})
    ce_ik, pe_ik = legs.get("CE"), legs.get("PE")
    if not ce_ik or not pe_ik:
        return None

    ce_pdc, _ = client.fetch_previous_day_close(ce_ik)
    pe_pdc, _ = client.fetch_previous_day_close(pe_ik)
    if ce_pdc is None or pe_pdc is None:
        return None

    bep = round((ce_pdc + pe_pdc) / 2, 2)
    _bep_cache[key] = bep
    return bep
