"""
config.py — central configuration for the LTP Crossing (CE = PE) scanner.
"""

# Nifty index instrument key (spot LTP)
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"

# Underlying key used for option-chain / option-expiry lookups
NIFTY_OPTION_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"

# Indices the scanner can watch. "enabled" is the starting state of the NIFTY / SENSEX
# checkboxes in the top bar (changed at runtime from the UI). A disabled index is not polled at all.
# "near_band": CE = PE is only checked while the index is within this many points of its nearest strike:
#   strike - near_band < index LTP <= strike + near_band   (NIFTY 10: 25091-25110 -> 25100, 25141-25160 -> 25150;
#   25111-25140 -> no check). Editable in the side panel.
INDEXES = {
    "NIFTY":  {"index_key": "NSE_INDEX|Nifty 50", "underlying_key": "NSE_INDEX|Nifty 50",
               "strike_interval": 50,  "enabled": True,  "ce_pe": True, "near_band": 10},
    "SENSEX": {"index_key": "BSE_INDEX|SENSEX",   "underlying_key": "BSE_INDEX|SENSEX",
               "strike_interval": 100, "enabled": False, "ce_pe": True, "near_band": 20},
}

STRIKE_INTERVAL = 50        # Nifty strike spacing (used by the Nifty-only backtest)
STRIKES_EACH_SIDE = 2       # internal strike window (bookkeeping only; matching uses the near strike, see "near_band")

# Live scan interval (seconds). Editable at runtime from the Live Scanner tab.
SCAN_INTERVAL_SECONDS = 10

RECALC_INTERVAL_SECONDS = 60      # re-center the strike window every 1 min

# Candle timing used by the CE = PE follow-up check (tracker.py)
TRACK_FINALIZE_DELAY_SECONDS = 10   # wait this long after a 5-min candle closes before reading it
TRACK_GIVE_UP_SECONDS = 180         # if the candle is still missing after this, stop waiting for it

# Upstox
UPSTOX_ACCESS_TOKEN = ""   # paste a pre-generated access token

# Market hours (IST). CE = PE matches are only searched between MARKET_OPEN and MARKET_CLOSE on
# weekdays. Outside these hours the app still shows the index LTP but raises no alerts. Add exchange holidays as "YYYY-MM-DD" strings.
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:40"
MARKET_HOLIDAYS = []

# Alert pause switch (top-bar button in the UI). While True, CE = PE matches are still found and
# tabled, but NO toast or browser notification is shown. Matches found while paused
# are not sent after resuming. In-memory only (resets to False).
ALERTS_PAUSED = False

# ---------------------------------------------------------------- CE = PE (LTP crossing)
# CE = PE is switched on/off per index with the checkbox in that index's section (INDEXES[..]["ce_pe"]).
COOLDOWN_SECONDS = 5 * 60         # freeze the window + suppress further matches for 5 min after a match
COOLDOWN_GRACE_SECONDS = 0        # optional early-unfreeze tolerance (not needed at this scan rate)
# CE = PE alerts (toast, browser). False = matches still appear in the table and are followed
# up, but no notification is sent.
MATCH_ALERTS_ENABLED = True
