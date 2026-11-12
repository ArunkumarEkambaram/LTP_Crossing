"""
check_support_board.py - standalone diagnostic (does NOT touch the app).

For every strike in your BEP CSV it downloads today's 5-min candles and lists each candle that
meets the BEP Support trigger:  green (close > open), low <= BEP + 0.05, close > BEP.
It then marks whether the strike was inside THAT candle's ATM window (Nifty's 5-min close, rounded
to the nearest 50, +/- N strikes). Hits inside the window are the rows the board should show.

Run from the project folder:
    python check_support_board.py <BEP csv path> [strikes_each_side, default 2] [access token]
(if no token is given, config.UPSTOX_ACCESS_TOKEN is used)
"""
import sys, time
from datetime import datetime, timedelta
import pytz
import config, bep, market_utils
from upstox_client import UpstoxClient

csv_path = sys.argv[1]
n_side = int(sys.argv[2]) if len(sys.argv) > 2 else 2
token = sys.argv[3] if len(sys.argv) > 3 else config.UPSTOX_ACCESS_TOKEN
config.STRIKES_EACH_SIDE = n_side
IST = pytz.timezone("Asia/Kolkata")
NOW = datetime.now(IST)

def is_closed(candle):
    """Upstox returns the still-forming candle too; the app only reads CLOSED candles."""
    start = IST.localize(datetime.strptime(candle[0][:16].replace("T", " "), "%Y-%m-%d %H:%M"))
    return start + timedelta(minutes=5) <= NOW

client = UpstoxClient(token)
ok, msg, _ = bep.load_csv(open(csv_path, "rb").read(), csv_path)
print(msg)
if not ok:
    sys.exit(1)
expiry = market_utils.current_weekly_expiry(client)
mapping, err = market_utils.option_instrument_keys(client, expiry)
if not mapping:
    sys.exit(f"contract lookup failed: {err}")
print("expiry:", expiry, "| strikes each side:", n_side)

nifty, err = client.fetch_intraday_candles(config.NIFTY_INDEX_KEY, 5)
if not nifty:
    sys.exit(f"Nifty candles failed: {err}")
windows = {c[0][:16]: set(market_utils.build_strike_window(float(c[4]))) for c in nifty}
print("Nifty 5-min closes:", ", ".join(f"{c[0][11:16]}={float(c[4]):.0f}" for c in nifty[:6]), "...")

should_show, hidden, failed = [], [], []
for opt in ("CE", "PE"):
    for strike in sorted(bep._csv_bep):
        level, _src = bep.get_bep(client, expiry, strike)
        ik = mapping.get(strike, {}).get(opt)
        if not ik or level is None:
            continue
        candles, err = client.fetch_intraday_candles(ik, 5)
        time.sleep(0.25)                                   # stay well under Upstox rate limits
        if candles is None:
            failed.append(f"{strike}{opt}: {err}"); continue
        for c in candles:
            if not is_closed(c):
                continue
            o, h, l, cl = (float(x) for x in c[1:5])
            if cl > o and cl > level and l <= level + 0.05:
                key = c[0][:16]
                row = (key[11:], opt, strike, level, o, l, cl)
                (should_show if strike in windows.get(key, set()) else hidden).append(row)

def show(title, rows):
    print(f"\n{title}  ({len(rows)})")
    for t, opt, s, lv, o, l, cl in sorted(rows):
        print(f"  candle {t}  {opt} {s}  BEP {lv:.2f}  o {o}  low {l}  close {cl}")
show("SHOULD be on the board (strike inside that candle's window)", should_show)
show("Hit the rule but OUTSIDE that candle's window (the board correctly skips these)", hidden)
if failed:
    print("\nFETCH FAILED (these legs could not be checked):")
    for f in failed: print("  ", f)
if not should_show:
    print("\n=> No in-window strike met the rule, so an empty board is correct for this window size.")
