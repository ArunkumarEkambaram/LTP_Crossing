"""
bep_monitor.py — watches the strikes near ATM against each strike's BEP, for ONE index (NIFTY or SENSEX).
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
TICK = 0.05
ORDER_MAX_AGE = timedelta(seconds=90)   # ...and only a star from the last 90 s may place an order (the price must still be current)

def classify(candle, bep_level: float):
    high, low, close = float(candle[2]), float(candle[3]), float(candle[4])
    if close > bep_level:
        return "Closed Above"
    if close < bep_level:
        return "Closed Below"
    return None

class BepMonitor:
    def __init__(self, name: str = "NIFTY"):
        self.name = name
        self.cfg = config.INDEXES[name]      # index_key, underlying_key, strike_interval
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

        self.candle_windows = {}     # candle start -> strikes in THAT candle's ATM window (index close +/- N)
        self.new_support = []        # fresh support alerts waiting for the app to dispatch
        self._notified = set()       # (date, strike, opt, time) already alerted; survives Rescan / CSV upload
        self.ordered = set()         # (date, opt, strike) already ordered - one order per star, survives Rescan / CSV upload
        self._open_low = {}          # (date, strike, opt) -> lowest live LTP seen in the opening candle
        self.new_orders = []         # auto-order signals waiting for the app to send
        self.entries = []            # stars waiting for their entry / open limit orders (see _process_entries)

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

    def step(self, client, index_ltp: float, expiry_hint, now) -> dict:
        logs = []
        log_fn = logs.append
        result = {"board_changed": False, "recalculated": False, "first_updated": False,
                  "latest_updated": False, "logs": logs}
        if expiry_hint:
            self.expiry = expiry_hint
        if not self.expiry:
            self.expiry = market_utils.current_weekly_expiry(client, self.cfg["underlying_key"])
        if not self.expiry:
            log_fn(f"BEP monitor {self.name}: could not determine weekly expiry.")
            return result

        if self.strikes is None or (now - self.last_recalc).total_seconds() >= config.RECALC_INTERVAL_SECONDS:
            self.strikes = market_utils.build_strike_window(index_ltp, self.cfg["strike_interval"])
            self.last_recalc = now
            self._refresh_quotes(client, log_fn)
            result["recalculated"] = True

        try:
            self._check_open_candle(client, index_ltp, now, log_fn)
        except Exception as ex:
            log_fn(f"BEP {self.name} opening-candle order check error: {ex}")

        self._schedule(client, now, log_fn)
        if self._process(client, now, log_fn):
            result["board_changed"] = True

        try:
            self._process_entries(client, now, log_fn)
        except Exception as ex:
            log_fn(f"BEP {self.name} entry/limit-order check error: {ex}")
            
        try:
            result["first_updated"] = self._update_first(client, result["recalculated"] or result["board_changed"], log_fn)
        except Exception as ex:
            log_fn(f"BEP {self.name} first cards error: {ex}")
            
        try:
            result["latest_updated"] = self._update_latest(
                client, result["recalculated"] or result["board_changed"], log_fn)
        except Exception as ex:
            log_fn(f"BEP {self.name} latest-support cards error: {ex}")
        return result

    @staticmethod
    def _orders_on() -> bool:
        """Entry orders are only queued while Auto order is ticked AND the algo is not paused."""
        return config.AUTO_ORDER_ENABLED and not config.AUTO_ORDER_PAUSED

    def pop_order_signals(self) -> list:
        """Returns (and clears) the auto-order signals raised since the last call."""
        out, self.new_orders = self.new_orders, []
        return out

    def _queue_order(self, opt, strike, level, ltp, now, kind, order_type="strategy_order", limit_price=None):
        """Queues ONE entry order signal per (day, leg, strike). Returns True if it was queued."""
        if not self._orders_on():
            return False
        okey = (now.strftime("%Y-%m-%d"), opt, strike)
        if okey in self.ordered:
            return False
        self.ordered.add(okey)
        sig = {"index": self.name, "opt": opt, "strike": strike, "bep": level, "ltp": ltp, "expiry": self.expiry,
               "kind": kind, "order_type": order_type, "time": now.strftime("%H:%M:%S")}
        if limit_price is not None:
            sig["limit_price"] = limit_price
        self.new_orders.append(sig)
        return True

    def _queue_cancel(self, e, ltp, now, reason):
        self.new_orders.append({"index": self.name, "opt": e["opt"], "strike": e["strike"], "bep": e["bep"],
                                "ltp": ltp if ltp is not None else e["close"], "expiry": self.expiry,
                                "kind": "cancel", "order_type": "cancel_order", "reason": reason,
                                "limit_price": e["limit_price"], "time": now.strftime("%H:%M:%S")})

    def pending_summary(self) -> list:
        """Open limit orders, for the label next to the index name."""
        return [f"{e['strike']} {e['opt']} @ {e['limit_price']:.2f}" for e in self.entries if e["stage"] == "limit_open"]

    def _open_entry(self, row, start, now):
        """A fresh star (first support of the day for its strike) waits here for its entry, from the NEXT candle on:
        market entry if (support close - BEP) <= max_dist, otherwise a limit buy at BEP + limit_offset."""
        if not self._orders_on():
            return
        if (now.strftime("%Y-%m-%d"), row["opt"], row["strike"]) in self.ordered:
            return                                  # the opening-candle rule already ordered this strike
        if any(e["opt"] == row["opt"] and e["strike"] == row["strike"] for e in self.entries):
            return
        self.entries.append({"opt": row["opt"], "strike": row["strike"], "bep": row["bep"], "close": row["close"],
                             "fail_low": row["active_flush_low"], "start": start, "stage": "new",
                             "limit_price": None, "placed_at": None, "min_seen": None, "expires": None, "ik": None})

    def _process_entries(self, client, now, log_fn):
        """Runs every scan. stage 'new': place the market or limit entry. stage 'limit_open': watch the limit order -
        assume it filled once the price traded below the limit, cancel it at once when the support turns red, or when
        the support's failure window ends without a fill."""
        if not self.entries or not self.expiry:
            return
        cfg = config.AUTO_ORDER[self.name]
        mapping, _ = market_utils.option_instrument_keys(client, self.expiry, self.cfg["underlying_key"])
        mapping = mapping or {}
        for e in self.entries:
            e["ik"] = mapping.get(e["strike"], {}).get(e["opt"])
        iks = [e["ik"] for e in self.entries if e["ik"]]
        ltps, _err = client.fetch_ltp(iks) if iks else ({}, None)
        ltps = ltps or {}
        market_open = market_utils.is_market_open(now)

        keep = []
        for e in self.entries:
            tag = f"BEP {self.name} {e['opt']} {e['strike']}"
            ltp = ltps.get(e["ik"])
            row = next((r for r in self.support[e["opt"]] if r["strike"] == e["strike"] and r["start"] == e["start"]), None)
            red = bool(row and row["state"] == "red")

            if e["stage"] == "new":
                if now - (e["start"] + CANDLE) > ORDER_MAX_AGE:
                    log_fn(f"{tag}: star is too old for an entry - no order.")
                    continue
                if red:
                    log_fn(f"{tag}: support already failed - no order.")
                    continue
                if ltp is None:
                    keep.append(e)                       # price not available yet - retry on the next scan
                    continue
                if ltp < e["fail_low"]:
                    log_fn(f"{tag}: LTP {ltp:.2f} is already below the fail level {e['fail_low']:.2f} - no order.")
                    continue
                distance = e["close"] - e["bep"]
                if distance <= cfg["max_dist"]:
                    if self._queue_order(e["opt"], e["strike"], e["bep"], ltp, now, "star"):
                        log_fn(f"{tag}: close is {distance:.2f} above BEP (max {cfg['max_dist']}) - market entry queued at LTP {ltp:.2f}")
                    continue
                limit_price = round(round((e["bep"] + cfg["limit_offset"]) / TICK) * TICK, 2)
                if self._queue_order(e["opt"], e["strike"], e["bep"], ltp, now, "limit", "limit_order", limit_price):
                    log_fn(f"{tag}: close is {distance:.2f} above BEP (max {cfg['max_dist']}) - limit buy queued at {limit_price:.2f}")
                    e.update(stage="limit_open", limit_price=limit_price, placed_at=now, min_seen=ltp,
                             expires=e["start"] + CANDLE * (1 + config.SUPPORT_FAIL_WINDOW_CANDLES))
                    keep.append(e)
                continue

            # ---- stage 'limit_open'
            if not market_open:
                continue                                 # intraday limit orders lapse at the close
            pa = e["placed_at"]
            placed_label = pa.replace(minute=pa.minute - pa.minute % CANDLE_MINUTES, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
            seen = [e["min_seen"]] + ([ltp] if ltp is not None else [])
            if e["ik"]:
                candles, _e = client.fetch_intraday_candles(e["ik"], CANDLE_MINUTES)
                seen += [float(c[3]) for c in (candles or []) if c[0][:16] >= placed_label]
            e["min_seen"] = min(seen)
            if e["min_seen"] <= e["limit_price"] - TICK + 1e-9:
                log_fn(f"{tag}: price traded down to {e['min_seen']:.2f} (limit {e['limit_price']:.2f}) - assumed filled, no cancel.")
                continue
            if red:
                self._queue_cancel(e, ltp, now, "support turned red, limit not filled")
                log_fn(f"{tag}: support turned RED before the limit filled - cancel queued.")
                continue
            if now >= e["expires"]:
                self._queue_cancel(e, ltp, now, "failure window ended, limit not filled")
                log_fn(f"{tag}: failure window ended and the limit did not fill - cancel queued.")
                continue
            keep.append(e)
        self.entries = keep

    def _check_open_candle(self, client, index_ltp, now, log_fn):
        """Opening candle (09:15-09:20): as soon as a strike's still-forming candle has touched its BEP (low <= BEP)
        and its live LTP is above BEP in a green candle (LTP > open), that strike's star is ordered immediately,
        without waiting for the candle to close. Every qualifying strike of the window gets its own order."""
        if not self._orders_on() or not self.strikes or not self.expiry:
            return
        if not (SESSION_FIRST_CANDLE <= now.time() < dtime(9, 15 + CANDLE_MINUTES)) or not market_utils.is_market_open(now):
            return
        day = now.strftime("%Y-%m-%d")
        quotes = None
        mapping = None
        start_label = day + "T" + SESSION_FIRST_CANDLE.strftime("%H:%M")
        for strike in self.strikes:
            for opt in ("CE", "PE"):
                if (day, opt, strike) in self.ordered:
                    continue
                if quotes is None:                     # one option-chain call per scan, only if something is still open
                    quotes, err = client.fetch_ce_pe_ltp_for_strikes(self.cfg["underlying_key"], self.expiry, list(self.strikes))
                    if quotes is None:
                        log_fn(f"BEP {self.name} opening check: option chain fetch failed: {err}")
                        return
                    mapping, _ = market_utils.option_instrument_keys(client, self.expiry, self.cfg["underlying_key"])
                    mapping = mapping or {}
                ltp = (quotes.get(strike) or {}).get(opt)
                if ltp is None:
                    continue
                key = (day, strike, opt)
                seen_low = min(self._open_low.get(key, ltp), ltp)       # lowest LTP this app has seen
                self._open_low[key] = seen_low
                level, _src = bep.get_bep(client, self.expiry, strike, self.name)
                if level is None or ltp <= level:
                    continue
                ik = mapping.get(strike, {}).get(opt)
                if not ik:
                    continue
                candles, _e = client.fetch_intraday_candles(ik, CANDLE_MINUTES)
                c = next((x for x in (candles or []) if x[0][:16] == start_label), None)
                if not c:
                    continue
                o, low = float(c[1]), min(float(c[3]), seen_low)
                if ltp > o and low <= level + support_logic.TOL:
                    if self._queue_order(opt, strike, level, ltp, now, "opening"):
                        log_fn(f"BEP {self.name} opening candle: {opt} {strike} crossed above BEP {level:.2f} (LTP {ltp:.2f}) - order signal queued")

    def _refresh_quotes(self, client, log_fn):
        strikes = sorted(set(self.strikes or []) | self._card_strikes())
        quotes, err = client.fetch_ce_pe_ltp_for_strikes(
            self.cfg["underlying_key"], self.expiry, strikes
        )
        if quotes is None:
            log_fn(f"BEP monitor {self.name}: option chain fetch failed: {err}")
            return
        self.quotes = quotes

    def _nifty_closes(self, client, log_fn) -> dict:
        candles, err = client.fetch_intraday_candles(self.cfg["index_key"], CANDLE_MINUTES)
        if not candles:
            log_fn(f"BEP monitor {self.name}: {self.name} 5-min candles unavailable ({err}); using live window.")
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
            self.expiry = None            # re-read the expiry on the next step (it may have rolled over)
            self.entries = []
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
            window_strikes = market_utils.build_strike_window(nifty_close, self.cfg["strike_interval"]) if nifty_close else list(self.strikes)
            self.candle_windows[s] = set(window_strikes)
            
            # 2. First CE / First PE ignore the ATM window: while either card is still unfound,
            #    every candle is also scanned for ALL CSV strikes (the board stays on the window).
            if s == first_of_day:
                self.first_start = s
            strikes_to_scan = window_strikes
            if config.SUPPORT_SCAN_ALL_CSV_STRIKES or self.first_found["CE"] is None or self.first_found["PE"] is None:
                csv_strikes = bep.csv_strikes(self.name)
                if csv_strikes:
                    strikes_to_scan = sorted(set(window_strikes) | csv_strikes)

            self.buckets[s] = {"strikes": strikes_to_scan, "expiry": self.expiry, "done": set()}
            self.last_bucket = s

    def _prefetch(self, client, cache):
        iks = set()
        for b in self.buckets.values():
            mapping, _ = market_utils.option_instrument_keys(client, b["expiry"], self.cfg["underlying_key"])
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
            mapping, _ = market_utils.option_instrument_keys(client, b["expiry"], self.cfg["underlying_key"])
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

        level, _src = bep.get_bep(client, self.expiry, strike, self.name)
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
            "resolved": False, "active_flush_low": fail_low, "rule": rule, "close": close,
        }
        self.support[opt].append(row)
        if is_first:                      # later supports of the same strike stay silent (no alert)
            self._queue_support_alert(row, start, now)

    def _queue_support_alert(self, row, start, now):
        """Called only for a strike's FIRST support of the day (see _add_support). Queues an alert only
        for a candle that has JUST closed during market hours. Rows built
        while replaying the day (Rescan, CSV upload, late start) are old, so they stay silent.
        Each (date, strike, leg, candle) alerts once, even across a Rescan. The same fresh star also queues its
        AlgoDelta entry (once per day, leg and strike) when Auto order is on."""
        try:
            key = (start.strftime("%Y-%m-%d"), row["strike"], row["opt"], row["time"])
            age = now - (start + CANDLE)
            if not (timedelta(seconds=-30) <= age <= NOTIFY_MAX_AGE):
                return
            if not market_utils.is_market_open(now):
                return
            if key not in self._notified:
                self._notified.add(key)
                self.new_support.append({
                    "index": self.name, "strike": row["strike"], "opt": row["opt"], "bep": row["bep"], "close": row["ltp"],
                    "time": row["time"], "rule": row["rule"],
                })
            # auto order: every star support waits for its entry from the next candle (see _process_entries)
            if age <= ORDER_MAX_AGE:
                self._open_entry(row, start, now)
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

    def export_rows(self) -> list:
        """Every BEP Support row of this index, CE and PE together, oldest first (for the XLSX download)."""
        first = {}                                        # (opt, strike) -> earliest (start, id)
        for opt in ("CE", "PE"):
            for e in self.support[opt]:
                k = (opt, e["strike"])
                if k not in first or (e["start"], e["id"]) < first[k]:
                    first[k] = (e["start"], e["id"])
        rows = []
        for opt in ("CE", "PE"):
            for e in self.support[opt]:
                failed = e["state"] == "red"
                rows.append({
                    "_sort": (e["start"], 0 if opt == "CE" else 1, e["strike"]),
                    "index": self.name, "opt": opt, "strike": e["strike"],
                    "first": "Yes" if first[(opt, e["strike"])] == (e["start"], e["id"]) else "",
                    "bep": round(e["bep"], 2), "time": e["time"],
                    "setup": "Failed breakdown" if e.get("rule") == 1 else "Touch and go",
                    "fail_level": round(e["active_flush_low"], 2) if e.get("active_flush_low") is not None else "",
                    "status": "Failed" if failed else "Held",
                    "fail_time": e.get("fail_time") or "" if failed else "",
                    "fail_close": round(e["fail_close"], 2) if failed and e.get("fail_close") is not None else "",
                    "ltp": round(e["ltp"], 2) if e.get("ltp") else "",
                })
        rows.sort(key=lambda r: r["_sort"])
        for r in rows:
            del r["_sort"]
        return rows

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
            
        mapping, _ = market_utils.option_instrument_keys(client, self.expiry, self.cfg["underlying_key"])
        mapping = mapping or {}
        view = {}
        for opt in ("CE", "PE"):
            found = self.first_found[opt]
            
            if not found:
                view[opt] = None
                continue

            strike = found
            level, _ = bep.get_bep(client, self.expiry, strike, self.name)
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
        mapping, _ = market_utils.option_instrument_keys(client, self.expiry, self.cfg["underlying_key"])
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