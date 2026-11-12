"""
config.py — central configuration for the BEP Support monitor.
"""

# Nifty index instrument key (spot LTP)
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"

# Underlying key used for option-chain / option-expiry lookups
NIFTY_OPTION_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"

STRIKE_INTERVAL = 50        # Nifty strike spacing
STRIKES_EACH_SIDE = 2       # nearest strike +/- 2 -> 5 strikes total

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
