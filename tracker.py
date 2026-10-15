"""
tracker.py — follow-up check after a confirmed CE/PE match (live scanner only).

Candles are 5-MINUTE chart candles on the 9:15 grid (9:15-9:20, 9:20-9:25 ...).
The match is judged on the candle containing the match time: a match at 09:31
uses the 09:30-09:35 candle; a match at exactly 09:35:00 uses 09:35-09:40.
No further candles are checked.

For each leg (CE, PE):   above = close > P      red = open > close
(a candle that is not red — green or flat — counts as green)

  - any leg closed above P in a RED candle        -> INVALID
  - else a leg closed above P in a GREEN candle   -> VALID
  - else (neither leg closed above P)             -> INVALID

Separately, it checks whether the CE (or PE) closed above the BEP of its own
strike (see bep.py). The first time that happens for a match, a BEP alert is
raised (once per match) and the row is flagged. This is independent of the
Valid/Invalid result.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import config
import market_utils

CANDLE_MINUTES = 5


@dataclass
class PendingTrack:
    row_id: int              # id of the table row to fill in
    ce_strike: int
    pe_strike: int
    price: int               # the rounded value P
    expiry: str
    bucket_start: datetime   # IST-aware start of the match candle
    bucket_end: datetime     # IST-aware end of the match candle (when we can read it)
    ce_bep: Optional[float] = None   # BEP of the CE's strike (None if unavailable)
    pe_bep: Optional[float] = None   # BEP of the PE's strike (None if unavailable)
    bep_alerted: bool = False        # the BEP alert fires at most once per match


def bucket_bounds(t: datetime):
    """5-minute candle containing t (t should be IST-aware). A time exactly on a
    boundary (e.g. 09:35:00) belongs to the candle that STARTS there."""
    start = t.replace(minute=t.minute - t.minute % CANDLE_MINUTES, second=0, microsecond=0)
    return start, start + timedelta(minutes=CANDLE_MINUTES)


def _candle_for_bucket(client, instrument_key: str, bucket_start: datetime, cache: dict):
    """Returns the [ts, o, h, l, c, ...] 5-min candle starting at bucket_start, or None."""
    if instrument_key not in cache:
        candles, _ = client.fetch_intraday_candles(instrument_key, CANDLE_MINUTES)
        cache[instrument_key] = candles or []
    wanted = bucket_start.strftime("%Y-%m-%dT%H:%M")
    for c in cache[instrument_key]:
        if c[0][:16] == wanted:
            return c
    return None


def evaluate(ce_candle, pe_candle, price: int) -> dict:
    """Judge the match candle. Returns the table fields (closed_above, touched,
    result, state)."""
    legs = {}
    for name, c in (("CE", ce_candle), ("PE", pe_candle)):
        o, h, l, cl = c[1], c[2], c[3], c[4]
        legs[name] = {"above": cl > price, "red": o > cl, "touched": l <= price <= h}

    above = [n for n in ("CE", "PE") if legs[n]["above"]]
    red_above = [n for n in above if legs[n]["red"]]
    green_above = [n for n in above if not legs[n]["red"]]
    closed_above = " + ".join(f"{n} ({'red' if legs[n]['red'] else 'green'})" for n in above) or "None"

    ce_t, pe_t = legs["CE"]["touched"], legs["PE"]["touched"]
    touched = "Both" if ce_t and pe_t else "CE only" if ce_t else "PE only" if pe_t else "Neither"

    valid = bool(green_above) and not red_above
    return {"closed_above": closed_above, "touched": touched,
            "result": "Valid" if valid else "Invalid",
            "state": "valid" if valid else "invalid"}


def check_bep(track, ce_candle, pe_candle):
    """Did the CE close above its strike's BEP, or the PE above its own?
    Returns {"hits": [...], "text": "..."} or None. A leg whose BEP is unknown
    is simply skipped."""
    hits = []
    if track.ce_bep is not None and ce_candle[4] > track.ce_bep:
        hits.append({"leg": "CE", "strike": track.ce_strike, "close": ce_candle[4], "bep": track.ce_bep})
    if track.pe_bep is not None and pe_candle[4] > track.pe_bep:
        hits.append({"leg": "PE", "strike": track.pe_strike, "close": pe_candle[4], "bep": track.pe_bep})
    if not hits:
        return None
    text = " + ".join(f"{h['leg']} {h['close']:.2f} > BEP {h['bep']:.2f}" for h in hits)
    return {"hits": hits, "text": text}


def process_pending(client, pending: list, now: datetime) -> list:
    """
    Judges every pending match whose candle has closed (plus a short delay so
    Upstox has the finished candle). Judged matches are REMOVED from `pending`.
    Returns [(row_id, fields_to_update, True), ...] — the third item is always
    True now (every match is settled by a single candle).
    If the candle is still missing after TRACK_GIVE_UP_SECONDS the row is marked
    "No data" (e.g. an option that didn't trade in that candle).
    """
    updates = []
    cache: dict = {}
    delay = timedelta(seconds=config.TRACK_FINALIZE_DELAY_SECONDS)
    give_up = timedelta(seconds=config.TRACK_GIVE_UP_SECONDS)

    for track in list(pending):
        if now < track.bucket_end + delay:
            continue

        mapping, _ = market_utils.option_instrument_keys(client, track.expiry)
        mapping = mapping or {}
        ce_ik = mapping.get(track.ce_strike, {}).get("CE")
        pe_ik = mapping.get(track.pe_strike, {}).get("PE")
        ce_c = _candle_for_bucket(client, ce_ik, track.bucket_start, cache) if ce_ik else None
        pe_c = _candle_for_bucket(client, pe_ik, track.bucket_start, cache) if pe_ik else None

        if not (ce_c and pe_c):
            if now >= track.bucket_end + give_up:
                pending.remove(track)
                updates.append((track.row_id, {"closed_above": "No data", "touched": "No data",
                                               "result": "No data", "state": "nodata"}, True))
            continue  # otherwise: candle not available yet, retry on the next scan

        fields = {"check_till": track.bucket_end.strftime("%H:%M")}
        fields.update(evaluate(ce_c, pe_c, track.price))

        if not track.bep_alerted:
            hit = check_bep(track, ce_c, pe_c)
            if hit:
                track.bep_alerted = True
                candle_end = track.bucket_end.strftime("%H:%M")
                fields.update({
                    "bep_hit": True,
                    "bep_alert": f"{hit['text']} (candle till {candle_end})",
                    "_bep_alert": {**hit, "candle_end": candle_end,   # consumed by app.py, not a table field
                                   "ce_strike": track.ce_strike, "pe_strike": track.pe_strike,
                                   "price": track.price},
                })

        pending.remove(track)
        updates.append((track.row_id, fields, True))
    return updates
