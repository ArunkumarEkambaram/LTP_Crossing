"""
test_option_chain.py — standalone sanity check.

Run this BEFORE trusting scanner.py / upstox_client.py against live data.
It fetches one real option chain response and prints:
  1. the raw shape of the first row (so you can confirm field names)
  2. the nearest-strike window computed from live Nifty LTP
  3. the CE/PE LTPs specifically for that window

Usage:
    python test_option_chain.py
(Make sure config.UPSTOX_ACCESS_TOKEN is filled in first.)
"""
import json

import config
import market_utils
from upstox_client import UpstoxClient


def main():
    if not config.UPSTOX_ACCESS_TOKEN:
        print("Set config.UPSTOX_ACCESS_TOKEN before running this test.")
        return

    client = UpstoxClient(config.UPSTOX_ACCESS_TOKEN)

    # 1. Nifty LTP
    ltp_map, err = client.fetch_ltp([config.NIFTY_INDEX_KEY])
    if ltp_map is None:
        print(f"Nifty LTP fetch failed: {err}")
        return
    nifty_ltp = ltp_map.get(config.NIFTY_INDEX_KEY)
    print(f"Nifty LTP: {nifty_ltp}")
    if nifty_ltp is None:
        print(f"Raw ltp_map: {ltp_map}")
        return

    # 2. Nearest expiry
    expiry = market_utils.current_weekly_expiry(client)
    print(f"Nearest expiry: {expiry}")
    if expiry is None:
        print("Could not fetch expiry — check fetch_option_expiries() against "
              "the actual /v2/option/contract response shape.")
        return

    # 3. Strike window
    window = market_utils.build_strike_window(nifty_ltp)
    print(f"Strike window: {window}")

    # 4. Raw chain — print ONE full row so we can eyeball field names
    chain, err = client.fetch_option_chain(config.NIFTY_OPTION_UNDERLYING_KEY, expiry)
    if chain is None:
        print(f"Option chain fetch failed: {err}")
        return
    if not chain:
        print("Option chain came back empty.")
        return

    print("\n--- First raw chain row (for field-name verification) ---")
    print(json.dumps(chain[0], indent=2))

    # 5. Filtered CE/PE for our window
    quotes, err = client.fetch_ce_pe_ltp_for_strikes(
        config.NIFTY_OPTION_UNDERLYING_KEY, expiry, window
    )
    print("\n--- CE/PE LTPs for tracked window ---")
    if quotes is None:
        print(f"Filter failed: {err}")
    else:
        for strike, pair in quotes.items():
            print(f"  {strike}: CE={pair['CE']}  PE={pair['PE']}")


if __name__ == "__main__":
    main()
