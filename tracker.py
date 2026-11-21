"""
tracker.py — follow-up check after a CE = PE match (live scanner and replay).

Candles are 5-MINUTE chart candles on the 9:15 grid (9:15-9:20, 9:20-9:25 ...). The match is watched for
config.TRACK_WATCH_CANDLES = 6 candles: the candle containing the match time plus the next 5 (a match at 09:26
watches 09:25, 09:30 ... 09:50; the result is known after 09:55). Meanwhile the row shows "WATCHING n/6".

For each leg (CE, PE):   above = close > P      red = open > close (a flat candle counts as green)

  1. Breakout: the first candle in which a leg closes above P.
       - closed above P in a RED candle (either leg)  -> INVALID (red breakout), done
       - closed above P in a GREEN candle             -> that leg is the BREAKOUT leg
         (both legs green above P: the one further above P)
  2. Hold: from the next candle on, the breakout leg closing below P -> FAILED · hh:mm, done
  3. After the 6th candle: breakout held -> VALID;  no breakout at all -> NONE.
Best move = the breakout leg's highest high minus P over the watched candles.
A candle still missing TRACK_GIVE_UP_SECONDS after it closed is skipped (still counted); if every candle
was missing the row reads "No data".
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import market_utils

CANDLE_MINUTES = 5


def watch_candles() -> int:
    return int(getattr(config, "TRACK_WATCH_CANDLES", 6))


@dataclass
class PendingTrack:
    row_id: int              # id of the table row to fill in
    ce_strike: int
    pe_strike: int
    price: int               # the rounded value P
    expiry: str
    bucket_start: datetime   # IST-aware start of the match candle
    bucket_end: datetime     # IST-aware end of the match candle
    underlying_key: str = None   # option underlying of the index (None = Nifty)
    n: int = 0               # watched candles processed so far
    seen: int = 0            # of those, how many had data
    leg: str = None          # breakout leg "CE" / "PE" once it happened
    leg_time: str = ""       # hh:mm of the breakout candle
    best: float = None       # breakout leg: highest high - P


def bucket_bounds(t: datetime):
    """5-minute candle containing t (t should be IST-aware). A time exactly on a
    boundary (e.g. 09:35:00) belongs to the candle that STARTS there."""
    start = t.replace(minute=t.minute - t.minute % CANDLE_MINUTES, second=0, microsecond=0)
    return start, start + timedelta(minutes=CANDLE_MINUTES)


def watch_end(bucket_start: datetime) -> datetime:
    """When the last watched candle closes."""
    return bucket_start + timedelta(minutes=CANDLE_MINUTES * watch_candles())


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


def _fields(t: PendingTrack, result: str, state: str) -> dict:
    if t.leg:
        above = f"{t.leg} (green) {t.leg_time}"
    elif t.leg_time:                       # red breakout: leg_time holds "CE (red) hh:mm"
        above = t.leg_time
    else:
        above = "None yet" if state == "watching" else "None"
    return {"closed_above": above, "result": result, "state": state,
            "best": f"{t.best:+.2f}" if t.best is not None else ""}


def step(t: PendingTrack, ce_c, pe_c, label: str):
    """Applies one watched candle (either may be None = no data). Returns (result, state) when the
    track is decided by this candle, else None."""
    p = t.price
    if ce_c or pe_c:
        t.seen += 1
    if t.leg is None:
        legs = {}
        for name, c in (("CE", ce_c), ("PE", pe_c)):
            if c:
                o, h, cl = float(c[1]), float(c[2]), float(c[4])
                legs[name] = {"above": cl > p, "red": o > cl, "dist": cl - p, "high": h}
        above = [n for n in legs if legs[n]["above"]]
        if any(legs[n]["red"] for n in above):
            reds = " + ".join(n for n in above if legs[n]["red"])
            t.leg_time = f"{reds} (red) {label}"
            return "Invalid (red)", "invalid"
        if above:
            t.leg = max(above, key=lambda n: legs[n]["dist"])
            t.leg_time = label
            t.best = round(legs[t.leg]["high"] - p, 2)
        return None
    c = ce_c if t.leg == "CE" else pe_c
    if c:
        t.best = max(t.best, round(float(c[2]) - p, 2))
        if float(c[4]) < p:
            return f"Failed · {label}", "failed"
    return None


def process_pending(client, pending: list, now: datetime) -> list:
    """
    Applies every watched candle that has closed (plus TRACK_FINALIZE_DELAY_SECONDS so Upstox has it).
    Returns [(row_id, fields_to_update, final), ...]; final tracks are REMOVED from `pending`.
    While watching, fields["result"] = "WATCHING n/6".
    """
    updates = []
    cache: dict = {}
    delay = timedelta(seconds=config.TRACK_FINALIZE_DELAY_SECONDS)
    give_up = timedelta(seconds=config.TRACK_GIVE_UP_SECONDS)
    n_max = watch_candles()
    step_td = timedelta(minutes=CANDLE_MINUTES)

    for t in list(pending):
        changed, final = False, None
        while t.n < n_max and final is None:
            c_start = t.bucket_start + step_td * t.n
            c_end = c_start + step_td
            if now < c_end + delay:
                break                                  # this candle hasn't closed yet
            mapping, _ = market_utils.option_instrument_keys(client, t.expiry, t.underlying_key)
            mapping = mapping or {}
            ce_ik = mapping.get(t.ce_strike, {}).get("CE")
            pe_ik = mapping.get(t.pe_strike, {}).get("PE")
            ce_c = _candle_for_bucket(client, ce_ik, c_start, cache) if ce_ik else None
            pe_c = _candle_for_bucket(client, pe_ik, c_start, cache) if pe_ik else None
            need = (ce_c and pe_c) if t.leg is None else (ce_c if t.leg == "CE" else pe_c)
            if not need and now < c_end + give_up:
                break                                  # not available yet: retry on the next scan
            final = step(t, ce_c, pe_c, c_start.strftime("%H:%M"))
            t.n += 1
            changed = True
        if final is None and t.n >= n_max:
            if t.seen == 0:
                final = ("No data", "nodata")
            elif t.leg:
                final = ("Valid", "valid")
            else:
                final = ("None", "invalid")
        if final:
            pending.remove(t)
            updates.append((t.row_id, _fields(t, *final), True))
        elif changed:
            updates.append((t.row_id, _fields(t, f"WATCHING {t.n}/{n_max}", "watching"), False))
    return updates
