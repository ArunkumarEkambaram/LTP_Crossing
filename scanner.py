"""
scanner.py — one scan cycle for one index (NIFTY or SENSEX): get the index LTP, get CE/PE LTPs for the tracked
strike window, check for cross-strike CE == PE matches (any CE value
matching any PE value within the window — see match_logic.py).
"""
from datetime import datetime

import config
import market_utils
import match_logic
from models import MatchEvent


def run_scan_cycle(client, state_mgr, log_fn=print, detect: bool = True,
                   index: str = "NIFTY", index_ltp=None, now=None) -> dict:
    """
    Runs a single scan cycle for ONE index (`index` = "NIFTY" or "SENSEX"; `index_ltp` = its spot LTP
    if the caller already fetched it, otherwise it is fetched here). With detect=False (market closed) it only fetches the
    Nifty LTP and returns {"market_closed": True, ...}: no window work, no option-chain
    call, no matches and therefore no alerts.
    Returns a summary dict:
    {nifty_ltp, window, quotes, matches, frozen, cooldown_remaining, errors}
    "matches" is a list of MatchEvent (empty list if none found this cycle).
    `now` defaults to the current time; replay.py passes the simulated time of a past day.
    """
    summary = {"nifty_ltp": None, "index_ltp": None, "window": None, "near_strike": None, "quotes": {}, "matches": [], "errors": []}
    cfg = config.INDEXES[index]
    now = now or datetime.now()

    state_mgr.maybe_unfreeze(now)

    # 1. index spot LTP
    nifty_ltp = index_ltp
    if nifty_ltp is None:
        ltp_map, err = client.fetch_ltp([cfg["index_key"]])
        if ltp_map is None:
            summary["errors"].append(f"{index} LTP fetch failed: {err}")
            return summary
        nifty_ltp = ltp_map.get(cfg["index_key"])
        if nifty_ltp is None:
            summary["errors"].append(f"{index} LTP not found in response.")
            return summary
    summary["nifty_ltp"] = summary["index_ltp"] = nifty_ltp

    if not detect:
        summary["market_closed"] = True
        return summary

    # 2. Determine / recalc strike window (skipped while frozen)
    if state_mgr.should_recalculate(now):
        expiry = market_utils.current_weekly_expiry(client, cfg["underlying_key"])
        if expiry is None:
            summary["errors"].append(f"{index}: could not determine weekly expiry.")
            return summary
        window = state_mgr.recalculate_window(nifty_ltp, expiry, now)
    else:
        window = state_mgr.state.window

    if window is None:
        summary["errors"].append("No strike window available yet.")
        return summary
    summary["window"] = window.strikes

    # If frozen, report status only — do not fetch/alert on new matches
    if state_mgr.is_frozen:
        summary["frozen"] = True
        summary["cooldown_remaining"] = state_mgr.cooldown_remaining_seconds
        return summary

    # 3. Only while the index is near a strike: fetch that strike's CE / PE LTP
    near = match_logic.near_strike(nifty_ltp, cfg["strike_interval"], cfg.get("near_band", 10))
    summary["near_strike"] = near
    if near is None:
        return summary
    quotes, err = client.fetch_ce_pe_ltp_for_strikes(
        cfg["underlying_key"], window.expiry_date, [near]
    )
    if quotes is None:
        summary["errors"].append(f"Option chain fetch failed: {err}")
        return summary
    summary["quotes"] = quotes

    # 4. CE = PE at the near strike
    raw_matches = match_logic.find_matches(quotes)
    if raw_matches:
        events = [
            MatchEvent(ce_strike=m["ce_strike"], pe_strike=m["pe_strike"], price=m["price"],
                       triggered_at=now, ce_ltp=m["ce_ltp"], pe_ltp=m["pe_ltp"], index=index)
            for m in raw_matches
        ]
        state_mgr.register_matches(events, now)
        summary["matches"] = events
        for e in events:
            log_fn(f"[{now.strftime('%H:%M:%S')}] {index} MATCH: CE@{e.ce_strike} ({e.ce_ltp}) = "
                   f"PE@{e.pe_strike} ({e.pe_ltp}) -> {e.price}")

    return summary
