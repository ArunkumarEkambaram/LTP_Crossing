"""
config.py — central configuration for the CE/PE Match Alert app.
"""

# Nifty index instrument key (spot LTP)
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"

# Underlying key used for option-chain / option-expiry lookups
NIFTY_OPTION_UNDERLYING_KEY = "NSE_INDEX|Nifty 50"

STRIKE_INTERVAL = 50        # Nifty strike spacing
STRIKES_EACH_SIDE = 2       # nearest strike +/- 2 -> 5 strikes total

# Live scan interval (seconds). Editable at runtime from the Live Scanner tab.
SCAN_INTERVAL_SECONDS = 10

RECALC_INTERVAL_SECONDS = 60      # re-center strike window every 1 min (only when not frozen)
COOLDOWN_SECONDS = 5 * 60         # freeze window + suppress alerts for 5 min after a match
COOLDOWN_GRACE_SECONDS = 0        # optional early-unfreeze tolerance (not needed at this scan rate)

# Follow-up tracking after a match (see tracker.py)
TRACK_FINALIZE_DELAY_SECONDS = 10   # wait this long after a 5-min candle closes before reading it
TRACK_GIVE_UP_SECONDS = 180         # if the candle is still missing after this, mark "No data"

# Telegram — fill in directly, or wire up a settings page later like Stock Price Alert
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_ENABLED = True

# Upstox
UPSTOX_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1RUJCRk0iLCJqdGkiOiI2YWVhOGUzMDkzMWQ2MjNhM2JjMjAzNjAiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzkzNzU2NzIwLCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3OTM4Mjk2MDB9.EsSv4C9c-qIIuAtxypNDu-82HS79WSl1Zrp5gQ2kuRQ"   # paste a pre-generated access token

# Market hours (IST). New CE = PE matches are only scanned for, and alerted on, between
# MARKET_OPEN and MARKET_CLOSE on weekdays. Outside these hours the app still shows the
# Nifty LTP and keeps the BEP boards / follow-ups up to date from candles, but it raises
# no match alerts (prices are stale). Add exchange holidays as "YYYY-MM-DD" strings.
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:40"
MARKET_HOLIDAYS = []

# Alert pause switch (top-bar button in the UI). While True, CE = PE matches are still found,
# tabled and followed up, but NO toast, browser notification or Telegram message is sent.
# Matches found while paused are not sent after resuming. In-memory only (resets to False).
ALERTS_PAUSED = False

# CE = PE match alerts (toast, browser, Telegram). False = matches still appear in the table and
# are followed up, but no notification is sent. BEP Support alerts are separate and unaffected.
MATCH_ALERTS_ENABLED = False
