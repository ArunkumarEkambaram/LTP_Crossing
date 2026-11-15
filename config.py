"""
config.py — central configuration for the BEP Support monitor.
"""

# Nifty index instrument key (spot LTP)
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"

# Underlying key used for option-chain / option-expiry lookups
NIFTY_OPTION_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"

# Indices the BEP Support monitor can watch. "enabled" is the starting state of the NIFTY / SENSEX
# checkboxes in the top bar (changed at runtime from the UI). A disabled index is not polled at all.
INDEXES = {
    "NIFTY":  {"index_key": "NSE_INDEX|Nifty 50", "underlying_key": "NSE_INDEX|Nifty 50",
               "strike_interval": 50,  "enabled": True},
    "SENSEX": {"index_key": "BSE_INDEX|SENSEX",   "underlying_key": "BSE_INDEX|SENSEX",
               "strike_interval": 100, "enabled": False},
}

STRIKE_INTERVAL = 50        # Nifty strike spacing (used by the Nifty-only backtest)
STRIKES_EACH_SIDE = 2       # nearest strike +/- 2 -> 5 strikes total (same count for every index)

# Live scan interval (seconds). Editable at runtime from the Live Scanner tab.
SCAN_INTERVAL_SECONDS = 10

RECALC_INTERVAL_SECONDS = 60      # re-center the strike window every 1 min

# Candle timing used by the BEP monitor
TRACK_FINALIZE_DELAY_SECONDS = 10   # wait this long after a 5-min candle closes before reading it
TRACK_GIVE_UP_SECONDS = 180         # if the candle is still missing after this, stop waiting for it

# Telegram — fill in directly, or wire up a settings page later like Stock Price Alert
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_ENABLED = True

# Upstox
UPSTOX_ACCESS_TOKEN = ""   # paste a pre-generated access token

# Market hours (IST). BEP Support alerts are only raised between MARKET_OPEN and MARKET_CLOSE on
# weekdays. Outside these hours the app still shows the Nifty LTP and keeps the boards up to date
# from candles, but raises no alerts. Add exchange holidays as "YYYY-MM-DD" strings.
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:40"
MARKET_HOLIDAYS = []

# Alert pause switch (top-bar button in the UI). While True, BEP Support rows are still found and
# tabled, but NO toast, browser notification or Telegram message is sent. Rows found while paused
# are not sent after resuming. In-memory only (resets to False).
ALERTS_PAUSED = False

# BEP Support boards: True = check EVERY strike in the uploaded BEP CSV on every candle;
# False = only strikes inside that candle's ATM window (Nifty close +/- strike count).
SUPPORT_SCAN_ALL_CSV_STRIKES = True

# BEP Support failure window: a green support row can turn red only if a candle closes below its
# fail level within this many 5-minute candles AFTER the trigger candle (3 = 15 minutes).
# After that the row stays green whatever the price does later. 0 = no limit (old behaviour).
SUPPORT_FAIL_WINDOW_CANDLES = 3

# ---------------------------------------------------------------- CE = PE (LTP crossing)
# Master switch = the "CE = PE" checkbox in the top bar (default on). It applies to every enabled index.
CE_PE_ENABLED = True
COOLDOWN_SECONDS = 5 * 60         # freeze the window + suppress further matches for 5 min after a match
COOLDOWN_GRACE_SECONDS = 0        # optional early-unfreeze tolerance (not needed at this scan rate)
# CE = PE alerts (toast, browser, Telegram). False = matches still appear in the table and are followed
# up, but no notification is sent. BEP Support alerts are separate and unaffected.
MATCH_ALERTS_ENABLED = False
