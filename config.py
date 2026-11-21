"""
config.py — central configuration for the LTP Crossing (CE = PE) scanner.
"""

# Nifty index instrument key (spot LTP)
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"

# Underlying key used for option-chain / option-expiry lookups
NIFTY_OPTION_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"

# Indices the scanner can watch. "enabled" is the starting state of the NIFTY / SENSEX
# checkboxes in the top bar (changed at runtime from the UI). A disabled index is not polled at all.
INDEXES = {
    "NIFTY":  {"index_key": "NSE_INDEX|Nifty 50", "underlying_key": "NSE_INDEX|Nifty 50",
               "strike_interval": 50,  "enabled": True,  "ce_pe": True},
    "SENSEX": {"index_key": "BSE_INDEX|SENSEX",   "underlying_key": "BSE_INDEX|SENSEX",
               "strike_interval": 100, "enabled": False, "ce_pe": True},
}

STRIKE_INTERVAL = 50        # Nifty strike spacing (used by the Nifty-only backtest)
STRIKES_EACH_SIDE = 2       # nearest strike +/- 2 -> 5 strikes total (same count for every index)

# Live scan interval (seconds). Editable at runtime from the Live Scanner tab.
SCAN_INTERVAL_SECONDS = 10

RECALC_INTERVAL_SECONDS = 60      # re-center the strike window every 1 min

# Candle timing used by the CE = PE follow-up check (tracker.py)
TRACK_FINALIZE_DELAY_SECONDS = 10   # wait this long after a 5-min candle closes before reading it
TRACK_GIVE_UP_SECONDS = 180         # if the candle is still missing after this, stop waiting for it

# Telegram — fill in directly, or wire up a settings page later like Stock Price Alert
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_ENABLED = True

# Upstox
UPSTOX_ACCESS_TOKEN = ""   # paste a pre-generated access token

# Market hours (IST). CE = PE matches are only searched between MARKET_OPEN and MARKET_CLOSE on
# weekdays. Outside these hours the app still shows the index LTP but raises no alerts. Add exchange holidays as "YYYY-MM-DD" strings.
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:40"
MARKET_HOLIDAYS = []

# Alert pause switch (top-bar button in the UI). While True, CE = PE matches are still found and
# tabled, but NO toast, browser notification or Telegram message is sent. Matches found while paused
# are not sent after resuming. In-memory only (resets to False).
ALERTS_PAUSED = False

# ---------------------------------------------------------------- CE = PE (LTP crossing)
# CE = PE is switched on/off per index with the checkbox in that index's section (INDEXES[..]["ce_pe"]).
COOLDOWN_SECONDS = 5 * 60         # freeze the window + suppress further matches for 5 min after a match
COOLDOWN_GRACE_SECONDS = 0        # optional early-unfreeze tolerance (not needed at this scan rate)
# CE = PE alerts (toast, browser, Telegram). False = matches still appear in the table and are followed
# up, but no notification is sent.
MATCH_ALERTS_ENABLED = False
