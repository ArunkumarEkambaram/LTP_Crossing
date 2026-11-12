"""
backtest_support.py - replays one past (or today's) trading day of 5-minute candles through the
SAME BEP Support rules the live monitor uses (support_logic.py) and returns the CE and PE tables.

  - Window: each candle's ATM window = Nifty's close in that candle rounded to the nearest 50,
    +/- config.STRIKES_EACH_SIDE strikes. New support rows are only created for strikes in that
    window. Rows already created keep being checked for failure on every later candle, even
    after the strike leaves the window.
  - BEP: from an uploaded CSV (used only for this run, the live BEP store is not touched), else
    (CE close + PE close) / 2 from the previous trading day's closes. A strike with no BEP is
    skipped and listed.
  - Only CLOSED candles are used (matters when the date is today). Time = candle CLOSE time.
"""
import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import config
import market_utils
import support_logic

CANDLE = timedelta(minutes=5)
WORKERS = 8


def parse_bep_csv(data: bytes):
    """Returns ({strike: BEP}, message). Accepts the same columns as the live upload."""
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        return {}, "The file needs a header row and at least one data row."

    def norm(h):
        return "".join(ch for ch in (h or "").lower() if ch.isalnum())

    def num(v):
        try:
            return float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    header = [norm(h) for h in rows[0]]
    idx = {n: header.index(n) for n in ("strike", "cepdc", "pepdc", "bep") if n in header}
    if "strike" not in idx or ("bep" not in idx and not ("cepdc" in idx and "pepdc" in idx)):
        return {}, "Need a 'Strike' column and a 'BEP' column (or 'CE PDC' and 'PE PDC')."
    out = {}
    for r in rows[1:]:
        def cell(n):
            return r[idx[n]] if n in idx and idx[n] < len(r) else None
        strike = num(cell("strike"))
        bep = num(cell("bep"))
        if bep is None:
            ce, pe = num(cell("cepdc")), num(cell("pepdc"))
            bep = (ce + pe) / 2 if ce is not None and pe is not None else None
        if strike is not None and bep is not None:
            out[int(round(strike))] = round(bep, 2)
    return out, f"{len(out)} strikes read from the uploaded file"


def run_support_backtest(client, date_str: str, expiry: str, csv_bytes: bytes = None,
                         log_fn=print, now=None) -> dict:
    """
    Returns {"date", "expiry", "strikes_each_side", "strikes", "bep_source", "skipped",
             "rows": {"CE": [...], "PE": [...]}, "errors": [...]}
    Each row: {"strike","opt","bep","time","start","ltp","state","fail_time","fail_close",
               "fail_level","rule"}  (state "green" | "red"), newest support first.
    """
    n_side = config.STRIKES_EACH_SIDE
    result = {"date": date_str, "expiry": expiry, "strikes_each_side": n_side, "strikes": [],
              "bep_source": {}, "skipped": [], "rows": {"CE": [], "PE": []}, "errors": []}
    ist = market_utils.IST
    now = now or datetime.now(ist)

    # 1. Nifty candles -> each candle's ATM window (closed candles only)
    nifty, err = client.fetch_candles(config.NIFTY_INDEX_KEY, date_str, 5)
    if nifty is None:
        result["errors"].append(f"Nifty candle fetch failed: {err}")
        return result

    def is_closed(c):
        start = ist.localize(datetime.strptime(c[0][:16].replace("T", " "), "%Y-%m-%d %H:%M"))
        return start + CANDLE <= now

    nifty = [c for c in nifty if is_closed(c)]
    if not nifty:
        result["errors"].append(f"No closed Nifty candles for {date_str} - check it is a trading day.")
        return result
    windows = {c[0][:16]: set(market_utils.build_strike_window(float(c[4]))) for c in nifty}
    strikes = sorted(set().union(*windows.values()))
    result["strikes"] = strikes
    log_fn(f"Support backtest {date_str}: {len(nifty)} candles, strikes {strikes[0]}-{strikes[-1]} "
           f"(center +/- {n_side})")

    # 2. Instrument keys
    mapping, err = market_utils.option_instrument_keys(client, expiry)
    if not mapping:
        result["errors"].append(f"Option contract lookup failed: {err}")
        return result

    # 3. BEP per strike: uploaded CSV first, else (CE close + PE close) / 2 the day before
    csv_bep = {}
    if csv_bytes:
        csv_bep, msg = parse_bep_csv(csv_bytes)
        log_fn(f"Support backtest: {msg}")
    bep_map = {}
    to_calc = []
    for s in strikes:
        if s in csv_bep:
            bep_map[s] = csv_bep[s]
            result["bep_source"][s] = "csv"
        else:
            to_calc.append(s)

    def calc(s):
        legs = mapping.get(s, {})
        if not legs.get("CE") or not legs.get("PE"):
            return s, None, "no CE/PE contract for this expiry"
        ce, e1 = client.fetch_close_before(legs["CE"], date_str)
        pe, e2 = client.fetch_close_before(legs["PE"], date_str)
        if ce is None or pe is None:
            return s, None, e1 or e2
        return s, round((ce + pe) / 2, 2), None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for s, value, why in ex.map(calc, to_calc):
            if value is None:
                result["skipped"].append(f"{s}: no BEP ({why})")
            else:
                bep_map[s] = value
                result["bep_source"][s] = "calc"

    # 4. Candles for every leg that has a BEP
    legs = [(s, opt) for s in strikes if s in bep_map for opt in ("CE", "PE")]

    def load(leg):
        s, opt = leg
        ik = mapping.get(s, {}).get(opt)
        if not ik:
            return leg, None, "no instrument key"
        candles, e = client.fetch_candles(ik, date_str, 5)
        return leg, candles, e

    series = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for leg, candles, e in ex.map(load, legs):
            if candles is None:
                result["errors"].append(f"{leg[0]}{leg[1]} candle fetch failed: {e}")
            else:
                series[leg] = {c[0][:16]: c for c in candles}

    # 5. Replay candle by candle, in time order, through the shared rules
    rows = {"CE": [], "PE": []}
    states = {}
    seq = [0]
    first_start = ist.localize(datetime.strptime(nifty[0][0][:16].replace("T", " "), "%Y-%m-%d %H:%M"))

    def make_adder(opt):
        def add_row(strike, _opt, start, label, level, close, fail_low, rule):
            seq[0] += 1
            rows[opt].append({
                "id": seq[0], "strike": strike, "opt": opt, "bep": level, "time": label, "start": start,
                "ltp": close, "state": "green", "fail_time": None, "fail_close": None,
                "resolved": False, "active_flush_low": fail_low, "rule": rule,
            })
        return add_row

    adders = {"CE": make_adder("CE"), "PE": make_adder("PE")}

    for c in nifty:
        key = c[0][:16]
        start = ist.localize(datetime.strptime(key.replace("T", " "), "%Y-%m-%d %H:%M"))
        label = (start + CANDLE).strftime("%H:%M")
        window = windows[key]
        for (s, opt), by_time in series.items():
            candle = by_time.get(key)
            if candle is None:
                continue
            level = bep_map[s]
            o, l, cl = float(candle[1]), float(candle[3]), float(candle[4])
            support_logic.check_failures(rows[opt], s, start, cl, label)     # every strike, window or not
            if s not in window:
                continue
            st = states.setdefault((s, opt), support_logic.new_leg_state())

            support_logic.process_candle(st, rows[opt], s, opt, level, o, l, cl, start, label,
                                         first_start, adders[opt])

    for opt in ("CE", "PE"):
        ordered = sorted(rows[opt], key=lambda r: (-r["start"].timestamp(), r["strike"]))
        result["rows"][opt] = [{
            "strike": r["strike"], "opt": opt, "bep": r["bep"], "time": r["time"], "start": r["start"],
            "ltp": r["ltp"], "state": r["state"], "fail_time": r["fail_time"],
            "fail_close": r["fail_close"], "fail_level": r["active_flush_low"], "rule": r["rule"],
        } for r in ordered]
    log_fn(f"Support backtest {date_str}: CE {len(result['rows']['CE'])} row(s), "
           f"PE {len(result['rows']['PE'])} row(s).")
    return result


# ------------------------------------------------------------------ helpers for the UI / downloads
EXPORT_COLUMNS = [
    ("opt", "Option"), ("strike", "Strike"), ("bep", "BEP"), ("ltp", "Support close"),
    ("time", "Time (candle close)"), ("result", "Result"), ("rule", "Rule"),
    ("fail_level", "Fail level"), ("fail_time", "Fail time"), ("fail_close", "Failing close"),
]
SUPPORT_GREEN = ("rgb(200,230,201)", "#1b3d1e")
SUPPORT_RED = ("rgb(255,205,210)", "#7f1d1d")


def board_rows(rows: list) -> list:
    """Rows for the on-screen tables (same look as the live boards)."""
    out = []
    for i, r in enumerate(rows):
        bg, fg = SUPPORT_GREEN if r["state"] == "green" else SUPPORT_RED
        out.append({
            "id": i, "strike": str(r["strike"]), "ltp": f"{r['ltp']:.2f}", "bep": f"{r['bep']:.2f}",
            "time": r["time"] if not r["fail_time"] else f"{r['time']} \u2192 {r['fail_time']}",
            "bg": bg, "fg": fg,
        })
    return out


def export_rows(result: dict) -> list:
    """CE rows then PE rows, ready for export_utils.build_csv / build_xlsx with EXPORT_COLUMNS."""
    out = []
    for opt in ("CE", "PE"):
        for r in result["rows"][opt]:
            out.append({
                "opt": opt, "strike": r["strike"], "bep": r["bep"], "ltp": r["ltp"], "time": r["time"],
                "result": "Green" if r["state"] == "green" else "Red",
                "rule": "Failed breakdown" if r["rule"] == 1 else "Touch and go",
                "fail_level": r["fail_level"], "fail_time": r["fail_time"] or "",
                "fail_close": r["fail_close"] if r["fail_close"] is not None else "",
            })
    return out
