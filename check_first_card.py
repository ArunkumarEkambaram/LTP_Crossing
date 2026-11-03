"""
check_first_card.py - standalone diagnostic (does NOT touch the app).

Replays today's 5-min candles for every CSV strike and shows, per leg, the first candle that
satisfies the First CE / First PE rule:  low <= BEP + 0.05  and  close > BEP  and  close > open.
It also shows what the APP would pick: the 09:15 candle looks at every CSV strike, later candles
only at that candle's ATM window (Nifty 5-min close, +/- N strikes).

Run from the project folder:
    python check_first_card.py <BEP csv path> [strikes_each_side, default 2] [access token]
(if no token is given, config.UPSTOX_ACCESS_TOKEN is used)
"""
import sys, time
import config, bep, market_utils
from upstox_client import UpstoxClient

csv_path = sys.argv[1]
n_side = int(sys.argv[2]) if len(sys.argv) > 2 else 2
token = sys.argv[3] if len(sys.argv) > 3 else config.UPSTOX_ACCESS_TOKEN
config.STRIKES_EACH_SIDE = n_side

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
first_key = nifty[0][0][:16]

def qualifies(c, level):
    o, h, l, cl = (float(x) for x in c[1:5])
    return l <= level + 0.05 and cl > level and cl > o

app_pick = {"CE": None, "PE": None}       # (time, strike) the app would lock
for opt in ("CE", "PE"):
    print(f"\n=== {opt} ===  strike | BEP | day low-high | first qualifying candle (any window) | in that candle's window?")
    hits = []
    for strike in bep.csv_strikes() if hasattr(bep, "csv_strikes") else sorted(bep._csv_bep):
        level, _ = bep.get_bep(client, expiry, strike)
        ik = mapping.get(strike, {}).get(opt)
        if not ik or level is None:
            print(f"{strike:>6} | no instrument key / BEP"); continue
        candles, err = client.fetch_intraday_candles(ik, 5)
        time.sleep(0.25)                                   # stay well under Upstox rate limits
        if candles is None:
            print(f"{strike:>6} | FETCH FAILED: {err}"); continue
        if not candles:
            print(f"{strike:>6} | BEP {level:.2f} | no candles"); continue
        lo, hi = min(float(c[3]) for c in candles), max(float(c[2]) for c in candles)
        q = next((c for c in candles if qualifies(c, level)), None)
        if q is None:
            print(f"{strike:>6} | {level:8.2f} | {lo:.2f}-{hi:.2f} | none"); continue
        key = q[0][:16]
        in_win = key == first_key or strike in windows.get(key, set())
        print(f"{strike:>6} | {level:8.2f} | {lo:.2f}-{hi:.2f} | {key[11:]} (o {q[1]} h {q[2]} l {q[3]} c {q[4]}) | {'YES' if in_win else 'NO - app skips it'}")
        if in_win:
            hits.append((key, strike))
    app_pick[opt] = min(hits) if hits else None
print("\nWhat the app should lock:")
for opt, v in app_pick.items():
    print(f"  First {opt}: " + (f"{v[1]}{opt} (candle {v[0][11:]})" if v else "NOTHING qualifies -> card stays blank (legit)"))
