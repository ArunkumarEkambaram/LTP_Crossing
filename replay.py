"""
replay.py - "Replay a past day" for the CE = PE (LTP Crossing) scanner.

Runs the LIVE scanner code (scanner.run_scan_cycle + StateManager + tracker) over one past trading day, minute by
minute, using Upstox historical candles instead of live quotes:
  - every 1-minute candle is one scan: the index close of that minute is the index LTP and re-centres the window
    (nearest strike +/- STRIKES_EACH_SIDE); each window strike's CE / PE "LTP" is its 1-minute close (the last
    traded close is carried forward if a leg did not trade in that minute);
  - the 5-minute cooldown after a match hides further matches, exactly like live;
  - every match is then watched for 6 five-minute candles by tracker.py (breakout then hold: Valid / Failed /
    Invalid (red) / None). On today's date, matches whose 6 candles haven't all closed stay "WATCHING n/6".
No toasts, no browser notifications. The live scanner is untouched and keeps running.

Limits:
  - Only days of the current weekly expiry cycle: older days' option contracts have expired and Upstox no longer
    lists their instrument keys.
  - Live scans every SCAN_INTERVAL_SECONDS (10 s); the replay sees one price per minute, so a crossing that
    lasted only a few seconds inside a minute is not seen.
"""
from collections import Counter
from datetime import datetime, timedelta

import config
import market_utils
import scanner
import tracker
from state_manager import StateManager

IST = market_utils.IST


class ReplayClient:
    """Wraps the real Upstox client for ONE past day. Option-chain LTPs become the 1-minute closes at the
    replay clock (set_minute), and 'today's intraday candles' become that day's historical candles.
    Everything else goes to the real client."""

    def __init__(self, client, date_str: str, expiry: str, underlying_key: str):
        self._client = client
        self.date_str = date_str
        self.expiry = expiry
        self.underlying_key = underlying_key
        self.minute = None                 # "YYYY-MM-DDTHH:MM" of the 1-minute candle being scanned
        self._minute_closes = {}           # instrument_key -> [(minute_label, close), ...] oldest first
        self._candles = {}                 # (instrument_key, interval) -> candles
        self._expiries = None
        self.errors = []

    def __getattr__(self, item):
        return getattr(self._client, item)

    def set_minute(self, label: str):
        self.minute = label

    def fetch_option_expiries(self, underlying_key: str):
        if self._expiries is None:                 # one call for the whole replay, not one per minute
            self._expiries = self._client.fetch_option_expiries(underlying_key)
        return self._expiries

    def _close_at(self, ik: str):
        if ik not in self._minute_closes:
            candles, err = self._client.fetch_minute_candles(ik, self.date_str)
            if candles is None:
                self.errors.append(f"{ik}: 1-minute candles unavailable ({err})")
            self._minute_closes[ik] = [(c[0][:16], float(c[4])) for c in (candles or [])]
        last = None
        for label, close in self._minute_closes[ik]:
            if label > self.minute:
                break
            last = close
        return last

    def fetch_ce_pe_ltp_for_strikes(self, underlying_key: str, expiry_date: str, strikes: list):
        mapping, err = market_utils.option_instrument_keys(self._client, expiry_date, underlying_key)
        if mapping is None:
            return None, err
        quotes = {}
        for strike in strikes:
            legs = mapping.get(strike, {})
            quotes[strike] = {opt: (self._close_at(legs[opt]) if legs.get(opt) else None) for opt in ("CE", "PE")}
        return quotes, None

    def fetch_intraday_candles(self, instrument_key: str, interval_minutes: int = 5):
        key = (instrument_key, interval_minutes)
        if key not in self._candles:
            candles, err = self._client.fetch_candles(instrument_key, self.date_str, interval_minutes)
            if candles is None:
                return None, err
            self._candles[key] = candles
        return self._candles[key], None

    def fetch_ltp(self, instrument_keys):
        return {}, None


def run(client, name: str, date_str: str, log_fn=print) -> dict:
    """Replays one day for index `name`. Returns
    {"rows": [...], "info": str, "error": str or None, "expiry": str, "strikes": [...]}."""
    out = {"rows": [], "info": "", "error": None, "expiry": None, "strikes": []}
    cfg = config.INDEXES[name]
    try:
        day = datetime.strptime(date_str or "", "%Y-%m-%d")
    except ValueError:
        out["error"] = "Pick a date first (click 'Load dates' if the list is empty)."
        return out
    if day.weekday() >= 5 or date_str in config.MARKET_HOLIDAYS:
        out["error"] = f"{day:%a %d %b %Y} is not a trading day."
        return out
    expiry = market_utils.current_weekly_expiry(client, cfg["underlying_key"])
    if not expiry:
        out["error"] = "Could not read the weekly expiry from Upstox."
        return out
    if expiry < date_str:
        out["error"] = f"{date_str} is after the current expiry {expiry}."
        return out
    out["expiry"] = expiry

    index_candles, err = client.fetch_minute_candles(cfg["index_key"], date_str)
    if not index_candles:
        out["error"] = f"No {name} 1-minute candles for {date_str} ({err or 'holiday?'})."
        return out
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if date_str == today and market_utils.is_market_open():
        index_candles = index_candles[:-1]          # drop the minute still forming

    rc = ReplayClient(client, date_str, expiry, cfg["underlying_key"])
    sm = StateManager(name)
    found, strikes_seen = [], set()
    for c in index_candles:
        label = c[0][:16]
        rc.set_minute(label)
        # the close of this minute, seen just before the minute ends (same 5-minute bucket as the candle)
        now = datetime.strptime(label, "%Y-%m-%dT%H:%M") + timedelta(seconds=59)
        summary = scanner.run_scan_cycle(rc, sm, log_fn=lambda m: None, detect=True, index=name,
                                         index_ltp=float(c[4]), now=now)
        for e in summary["errors"]:
            rc.errors.append(f"{label[11:]} {e}")
        strikes_seen.update(summary.get("window") or [])
        for m in summary["matches"]:
            found.append((label, m, float(c[4])))

    # watch every match for 6 five-minute candles (tracker.py, same rule as live)
    pending, rows = [], []
    for row_id, (label, m, index_ltp) in enumerate(found):
        start, end = tracker.bucket_bounds(IST.localize(datetime.strptime(label, "%Y-%m-%dT%H:%M")))
        rows.append({"id": row_id, "time": label[11:16], "ce_strike": m.ce_strike, "pe_strike": m.pe_strike,
                     "ce_ltp": round(m.ce_ltp, 2), "pe_ltp": round(m.pe_ltp, 2), "price": m.price,
                     "index_ltp": round(index_ltp, 2),
                     "check_till": tracker.watch_end(start).strftime("%H:%M"), "closed_above": "", "best": "",
                     "result": f"WATCHING 0/{tracker.watch_candles()}", "state": "watching"})
        pending.append(tracker.PendingTrack(row_id=row_id, ce_strike=m.ce_strike, pe_strike=m.pe_strike,
                                            price=m.price, expiry=expiry, bucket_start=start, bucket_end=end,
                                            underlying_key=cfg["underlying_key"]))
    end_of_day = IST.localize(day.replace(hour=23, minute=59))
    judge_at = min(end_of_day, datetime.now(IST))      # today: only candles that have closed
    for row_id, fields, _final in tracker.process_pending(rc, pending, judge_at):
        rows[row_id].update(fields)

    for e in rc.errors[:20]:
        log_fn(f"REPLAY {name} {date_str}: {e}")
    cnt = Counter(r["state"] for r in rows)
    out["rows"] = rows
    out["strikes"] = sorted(strikes_seen)
    out["info"] = (f"REPLAY {name} {day:%a %d %b %Y} · expiry {expiry} · {len(index_candles)} one-minute scans · "
                   f"strikes checked: nearest ± {config.STRIKES_EACH_SIDE} · "
                   f"{len(rows)} match(es): {cnt['valid']} valid, {cnt['failed']} failed, {cnt['invalid']} invalid / none"
                   + (f", {cnt['watching']} still watching" if cnt['watching'] else "")
                   + (f", {cnt['nodata']} no data" if cnt['nodata'] else "")
                   + (f" · {len(rc.errors)} data error(s), see log" if rc.errors else ""))
    return out
