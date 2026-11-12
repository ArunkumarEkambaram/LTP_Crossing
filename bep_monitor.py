"""
bep_monitor.py — watches the strikes near ATM against each strike's BEP.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, time as dtime, datetime

import config
import market_utils
import support_logic
import bep

CANDLE_MINUTES = 5
CANDLE = timedelta(minutes=CANDLE_MINUTES)
SESSION_FIRST_CANDLE = dtime(9, 15)
SESSION_LAST_CANDLE = dtime(15, 25)
MAX_CATCHUP_CANDLES = 12
MAX_BACKFILL_CANDLES = 80
FETCH_WORKERS = 8
NOTIFY_MAX_AGE = timedelta(minutes=6)   # only a candle that JUST closed may raise a support alert

def classify(candle, bep_level: float):
    high, low, close = float(candle[2]), float(candle[3]), float(candle[4])
    if close > bep_level:
        return "Closed Above"
    if close < bep_level:
        return "Closed Below"
    return None

class BepMonitor:
    def __init__(self):
        self.strikes = None
        self.last_recalc = None
        self.expiry = None
        self.buckets = {}
        self.last_bucket = None
        
        self.support = {"CE": [], "PE": []}
        self._support_seq = 0
        self.quotes = {}
        self.leg_states = {}
        
        self.first_start = None
        self.first_found = {"CE": None, "PE": None}
        self.first_view = {}
        
        self.latest_view = {}
        self._latest_sig = None

        self.candle_windows = {}     # candle start -> strikes in THAT candle's ATM window (Nifty close +/- N)
        self.new_support = []        # fresh support alerts waiting for the app to dispatch
        self._notified = set()       # (date, strike, opt, time) already alerted; survives Rescan / CSV upload

    def _reset_leg_state(self, strike, opt):
        self.leg_states[(strike, opt)] = {
            "state": "idle", "flush_low": None, "active_row_id": None
        }

    def reset_backfill(self):
        self.buckets.clear()
        self.last_bucket = None
        self._reset_support()

    def _reset_support(self):
        self.support = {"CE": [], "PE": []}
        self.leg_states = {}
        self.latest_view = {}
        self._latest_sig = None
        self.candle_windows = {}
        
        self.first_start = None
        self.first_found = {"CE": None, "PE": None}
        self.first_view = {}

    def force_recalc(self):
        """Forces the strike window to rebuild on the next step."""
        self.strikes = None
        self.last_recalc = None

    def step(self, client, nifty_ltp: float, expiry_hint, now) -> dict:
        logs = []
        log_fn = logs.append
        result = {"board_changed": False, "recalculated": False, "first_updated": False,
                  "latest_updated": False, "logs": logs}
        if expiry_hint:
            self.expiry = expiry_hint
        if not self.expiry:
            self.expiry = market_utils.current_weekly_expiry(client)
        if not self.expiry:
            log_fn("BEP monitor: could not determine weekly expiry.")
            return result

        if self.strikes is None or (now - self.last_recalc).total_seconds() >= config.RECALC_INTERVAL_SECONDS:
            self.strikes = market_utils.build_strike_window(nifty_ltp)
            self.last_recalc = now
            self._refresh_quotes(client, log_fn)
            result["recalculated"] = True

        self._schedule(client, now, log_fn)
        if self._process(client, now, log_fn):
            result["board_changed"] = True
            
        try:
            result["first_updated"] = self._update_first(client, result["recalculated"] or result["board_changed"], log_fn)
        except Exception as ex:
            log_fn(f"BEP first cards error: {ex}")
            
        try:
            result["latest_updated"] = self._update_latest(
                client, result["recalculated"] or result["board_changed"], log_fn)
        except Exception as ex:
            log_fn(f"BEP latest-support cards error: {ex}")
        return result

    def _refresh_quotes(self, client, log_fn):
        strikes = sorted(set(self.strikes or []) | self._card_strikes())
        quotes, err = client.fetch_ce_pe_ltp_for_strikes(
            config.NIFTY_OPTION_UNDERLYING_KEY, self.expiry, strikes
        )
        if quotes is None:
            log_fn(f"BEP monitor: option chain fetch failed: {err}")
            return
        self.quotes = quotes

    def _nifty_closes(self, client, log_fn) -> dict:
        candles, err = client.fetch_intraday_candles(config.NIFTY_INDEX_KEY, CANDLE_MINUTES)
        if not candles:
            log_fn(f"BEP monitor: Nifty 5-min candles unavailable ({err}); using live window.")
            return {}
        return {c[0][:16]: float(c[4]) for c in candles}

    def _latest_closed(self, now):
        t = now - timedelta(seconds=config.TRACK_FINALIZE_DELAY_SECONDS)
        cur_start = t.replace(minute=t.minute - t.minute % CANDLE_MINUTES, second=0, microsecond=0)
        latest = cur_start - CANDLE
        if latest.date() != now.date():
            return None
        latest = min(latest, latest.replace(hour=SESSION_LAST_CANDLE.hour, minute=SESSION_LAST_CANDLE.minute))
        first = latest.replace(hour=SESSION_FIRST_CANDLE.hour, minute=SESSION_FIRST_CANDLE.minute)
        return latest if latest >= first else None

    def _schedule(self, client, now, log_fn):
        if self.last_bucket is not None and self.last_bucket.date() != now.date():
            self.last_bucket = None
            self._reset_support()
            
        latest = self._latest_closed(now)
        if latest is None:
            return
        first_of_day = latest.replace(hour=SESSION_FIRST_CANDLE.hour, minute=SESSION_FIRST_CANDLE.minute)

        backfill = self.last_bucket is None
        starts, s = [], (first_of_day if backfill else self.last_bucket + CANDLE)
        limit = MAX_BACKFILL_CANDLES if backfill else MAX_CATCHUP_CANDLES
        while s <= latest and len(starts) < limit:
            starts.append(s)
            s += CANDLE
        if not starts:
            return

        closes = self._nifty_closes(client, log_fn)
        
        for s in starts:
            nifty_close = closes.get(s.strftime("%Y-%m-%dT%H:%M"))
            
            # 1. Normal ATM window for the Support Board
            window_strikes = market_utils.build_strike_window(nifty_close) if nifty_close else list(self.strikes)
            self.candle_windows[s] = set(window_strikes)
            
            # 2. First CE / First PE ignore the ATM window: while either card is still unfound,
            #    every candle is also scanned for ALL CSV strikes (the board stays on the window).
            if s == first_of_day:
                self.first_start = s
            strikes_to_scan = window_strikes
            if config.SUPPORT_SCAN_ALL_CSV_STRIKES or self.first_found["CE"] is None or self.first_found["PE"] is None:
                csv_strikes = set(getattr(bep, "_csv_bep", {}) or {})
                if csv_strikes:
                    strikes_to_scan = sorted(set(window_strikes) | csv_strikes)

            self.buckets[s] = {"strikes": strikes_to_scan, "expiry": self.expiry, "done": set()}
            self.last_bucket = s

    def _prefetch(self, client, cache):
        iks = set()
        for b in self.buckets.values():
            mapping, _ = market_utils.option_instrument_keys(client, b["expiry"])
            mapping = mapping or {}
            for strike in b["strikes"]:
                for opt in ("CE", "PE"):
                    ik = mapping.get(strike, {}).get(opt)
                    if ik and (strike, opt) not in b["done"]:
                        iks.add(ik)
        if not iks:
            return

        def fetch(ik):
            candles, _ = client.fetch_intraday_candles(ik, CANDLE_MINUTES)
            return ik, candles or []

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            for ik, candles in ex.map(fetch, sorted(iks)):
                cache[ik] = candles

    def _process(self, client, now, log_fn) -> bool:
        changed = False
        cache = {}
        give_up = timedelta(seconds=config.TRACK_GIVE_UP_SECONDS)
        self._prefetch(client, cache)
        
        for start in sorted(self.buckets):
            b = self.buckets[start]
            end = start + CANDLE
            label = start.strftime("%H:%M")
            mapping, _ = market_utils.option_instrument_keys(client, b["expiry"])
            mapping = mapping or {}
            
            for strike in b["strikes"]:
                for opt in ("CE", "PE"):
                    leg = (strike, opt)
                    if leg in b["done"]:
                        continue
                    ik = mapping.get(strike, {}).get(opt)
                    if not ik:
                        b["done"].add(leg)
                        continue
                    
                    res = self._evaluate_leg(client, strike, opt, ik, start, label, cache, now)
                    if res:
                        b["done"].add(leg)
                        if res == "changed":
                            changed = True
            
            if len(b["done"]) >= 2 * len(b["strikes"]) or now >= end + give_up:
                if start in self.buckets:
                    del self.buckets[start]
        return changed

    def _evaluate_leg(self, client, strike, opt, ik, start, label, cache, now):
        start_key = start.strftime("%Y-%m-%dT%H:%M")
        candles = cache.get(ik, [])
        candle = next((c for c in candles if c[0][:16] == start_key), None)

        if candle is None:
            if any(c[0][:16] > start_key for c in candles):
                return True
            return False

        level, _src = bep.get_bep(client, self.expiry, strike)
        if level is None:
            return True

        o, h, l, c = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])

        # --- 1. FIRST CARD LOGIC (ignores the ATM window) ---
        if self.first_found[opt] is None:
            if l <= level + 0.05 and c > level and c > o:
                self.first_found[opt] = strike
                self.first_view = {}  # force UI redraw

        # --- 2. FAILURES of rows this strike already has (shared rules: support_logic.py) ---
        changed = support_logic.check_failures(self.support[opt], strike, start, c, label)

        # --- 3. NEW support rows: only strikes in that candle's ATM window ---
        window = self.candle_windows.get(start)
        if window is None:
            window = set(self.strikes or [])
        if not config.SUPPORT_SCAN_ALL_CSV_STRIKES and window and strike not in window:
            return "changed" if changed else True

        if (strike, opt) not in self.leg_states:
            self._reset_leg_state(strike, opt)
        st = self.leg_states[(strike, opt)]

        if support_logic.process_candle(
                st, self.support[opt], strike, opt, level, o, l, c, start, label, self.first_start,
                lambda s, p, a, lb, lv, cl, fl, rule: self._add_support(s, p, a, lb, lv, cl, fl, rule, now)):
            changed = True
        return "changed" if changed else True

    def _add_support(self, strike, opt, start, label, level, close, fail_low, rule, now):
        self._support_seq += 1
        is_first = not any(r["strike"] == strike for r in self.support[opt])   # first support of the day for this strike + leg
        row = {
            "id": self._support_seq, "strike": strike, "opt": opt, "bep": level,
            "time": label, "start": start, "fail_time": None, "state": "green", "ltp": close,
            "resolved": False, "active_flush_low": fail_low, "rule": rule,
        }
        self.support[opt].append(row)
        if is_first:                      # later supports of the same strike stay silent (no alert)
            self._queue_support_alert(row, start, now)

    def _queue_support_alert(self, row, start, now):
        """Called only for a strike's FIRST support of the day (see _add_support). Queues an alert only
        for a candle that has JUST closed during market hours. Rows built
        while replaying the day (Rescan, CSV upload, late start) are old, so they stay silent.
        Each (date, strike, leg, candle) alerts once, even across a Rescan."""
        try:
            key = (start.strftime("%Y-%m-%d"), row["strike"], row["opt"], row["time"])
            if key in self._notified:
                return
            age = now - (start + CANDLE)
            if not (timedelta(seconds=-30) <= age <= NOTIFY_MAX_AGE):
                return
            if not market_utils.is_market_open(now):
                return
            self._notified.add(key)
            self.new_support.append({
                "strike": row["strike"], "opt": row["opt"], "bep": row["bep"], "close": row["ltp"],
                "time": row["time"], "rule": row["rule"],
            })
        except Exception:
            pass

    def pop_support_alerts(self) -> list:
        """Returns (and clears) the support alerts raised since the last call."""
        out, self.new_support = self.new_support, []
        return out

    def board_view(self) -> dict:
        out = {}
        for opt in ("CE", "PE"):
            evs = self.support[opt]
            evs = sorted(
                evs, 
                key=lambda e: (
                    -datetime.strptime(e["time"], "%H:%M").time().hour * 60 - datetime.strptime(e["time"], "%H:%M").time().minute,
                    e["strike"]
                ) if e.get("time") else 0
            )
            first_id = {}                      # strike -> id of its earliest support row of the day
            for e in self.support[opt]:
                cur = first_id.get(e["strike"])
                if cur is None or (e["start"], e["id"]) < cur[0]:
                    first_id[e["strike"]] = ((e["start"], e["id"]), e["id"])
            out[opt] = [{
                "id": str(e["id"]), "strike": str(e["strike"]),
                "first": first_id[e["strike"]][1] == e["id"], "ltp": f"{e['ltp']:.2f}" if e.get("ltp") else "--",
                "bep": f"{e['bep']:.2f}",
                "time": f"{e['time']} → {e['fail_time']}" if e['state'] == 'red' and e.get('fail_time') else e['time'],
                "kind": e["state"],
            } for e in evs]
        return out

    # ------------------------------------------------------------------ First CE / PE Cards
    def _card_strikes(self) -> set:
        s = set()
        for found in self.first_found.values():
            if found:
                s.add(found)               # first_found[opt] is the strike (int)
        s |= {v["strike"] for v in self.latest_view.values() if v}
        return s

    def _update_first(self, client, refresh, log_fn) -> bool:
        if not refresh and self.first_view:
            return False
            
        mapping, _ = market_utils.option_instrument_keys(client, self.expiry)
        mapping = mapping or {}
        view = {}
        for opt in ("CE", "PE"):
            found = self.first_found[opt]
            
            if not found:
                view[opt] = None
                continue

            strike = found
            level, _ = bep.get_bep(client, self.expiry, strike)
            ltp = (self.quotes.get(strike) or {}).get(opt)
            if ltp is None:
                ik = mapping.get(strike, {}).get(opt)
                if ik:
                    ltp_map, _ = client.fetch_ltp([ik])
                    ltp = (ltp_map or {}).get(ik)
            view[opt] = {"strike": strike, "bep": level, "ltp": ltp}
        
        self.first_view = view
        return True

    # ------------------------------------------------------------------ Latest Support CE / PE cards
    def _latest_event(self, opt):
        evs = self.support[opt]
        if not evs:
            return None
        newest_time_str = max(e["time"] for e in evs)
        newest_events = [e for e in evs if e["time"] == newest_time_str]
        return min(newest_events, key=lambda e: e["strike"])

    def _update_latest(self, client, refresh, log_fn) -> bool:
        events = {opt: self._latest_event(opt) for opt in ("CE", "PE")}
        sig = {opt: (e["id"], e["state"]) if e else None for opt, e in events.items()}
        if sig == self._latest_sig and not refresh and self.latest_view:
            return False
        mapping, _ = market_utils.option_instrument_keys(client, self.expiry)
        mapping = mapping or {}
        view = {}
        for opt, e in events.items():
            if e is None:
                view[opt] = None
                continue
            ltp = (self.quotes.get(e["strike"]) or {}).get(opt)
            if ltp is None:
                ik = mapping.get(e["strike"], {}).get(opt)
                if ik:
                    ltp_map, _ = client.fetch_ltp([ik])
                    ltp = (ltp_map or {}).get(ik)
            view[opt] = {"strike": e["strike"], "bep": e["bep"], "ltp": ltp, "state": e["state"]}
        self.latest_view = view
        self._latest_sig = sig
        return True