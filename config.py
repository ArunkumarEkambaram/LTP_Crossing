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
               "strike_interval": 50,  "enabled": True,  "ce_pe": True},
    "SENSEX": {"index_key": "BSE_INDEX|SENSEX",   "underlying_key": "BSE_INDEX|SENSEX",
               "strike_interval": 100, "enabled": False, "ce_pe": True},
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
# CE = PE is switched on/off per index with the checkbox under that index's BEP Support boards (INDEXES[..]["ce_pe"]).
COOLDOWN_SECONDS = 5 * 60         # freeze the window + suppress further matches for 5 min after a match
COOLDOWN_GRACE_SECONDS = 0        # optional early-unfreeze tolerance (not needed at this scan rate)
# CE = PE alerts (toast, browser, Telegram). False = matches still appear in the table and are followed
# up, but no notification is sent. BEP Support alerts are separate and unaffected.
MATCH_ALERTS_ENABLED = False

# ---------------------------------------------------------------- AUTO ORDER (AlgoDelta JSON Bridge)
# One order is sent for EVERY star support: the first support of the day for a strike on a leg (the star on the board).
#   * 09:15 candle: the moment a strike's still-forming candle has touched BEP (low <= BEP) and its live LTP is above
#     BEP while the candle is green (LTP > open) - immediately, without waiting for the candle to close.
#   * later candles: entered from the next candle (see max_dist / limit_offset below).
# Each (day, index, CE/PE, strike) is ordered at most once, also across a Rescan or a restart of the page.
# Replayed history (Rescan, CSV upload, late start) never orders.
AUTO_ORDER_ENABLED = False        # "Auto order" checkbox. Always off when the app starts.
AUTO_ORDER_DRY_RUN = True         # "Dry run" checkbox. True = log the JSON only, send nothing.
AUTO_ORDER_PAUSED = False         # "Pause algo trade" button: no NEW entry orders while True (open limit orders are still
                                  # cancelled on red / expiry). Stars seen while paused are skipped, never queued for later.
AUTO_ORDER_DAILY_LIMIT = 20       # "Max orders/day" box: hard cap on LIVE orders per day, all indices together

# Per-index AlgoDelta settings. Edit them under each index's BEP Support boards (webhook, JSON, Target, SL, Quantity,
# distance and limit offset) or set the starting values here. The webhook URL is a secret: keep it out of shared files.
#   webhook_url   AlgoDelta "Copy Webhook" URL         tgt / sl     Target / SL in points  ({tgt} {sl})
#   qty           quantity ({qty}), default 1           confirmed    the "I checked this JSON" box - live orders stay
#                                                                   blocked until it is ticked and saved
#   max_dist      support candle close may be at most this many points above BEP for a MARKET entry (default 8)
#   limit_offset  farther than that -> a LIMIT buy at BEP + this many points ({limit_price}), default 5
# Stars after 09:15 are entered from the next candle: market entry if (close - BEP) <= max_dist, otherwise a limit order
# at BEP + limit_offset. An unfilled limit order is cancelled at once when its support turns red, or when the 3-candle
# window (SUPPORT_FAIL_WINDOW_CANDLES) ends. The 09:15 candle keeps its immediate market entry.
# Three templates (AlgoDelta Syntax Generator JSON): strategy_order = market entry, limit_order, cancel_order.
# Placeholders: {symbol} e.g. NIFTY17NOV2625400CE (index + expiry DDMMMYY + strike + CE/PE), {opt}, {signal} (UP for CE,
# DOWN for PE), {qty} {tgt} {sl} {limit_price} {strike} {expiry} {index} {bep} {ltp}. A value that is exactly "{qty}",
# "{tgt}", "{sl}", "{limit_price}" or "{strike}" is sent as a number. Anything else in { } is left untouched
# (so "{{strategy.position_size}}" is sent exactly as written).
import copy as _copy

_STRATEGY_ORDER = {
    "type": "strategy_order",
    "exit_on_opposite": True,
    "is_tgt": True,
    "is_sl": True,
    "is_trail_set": False,
    "position_size": "{{strategy.position_size}}",
    "transaction_type": "BUY",
    "script_type": "{opt}",
    "is_rollover": False,
    "option_selection": "SYMBOL_BASED",
    "script": "{symbol}",
    "product": "INTRADAY",
    "quantity": "{qty}",
    "signal_type": "{signal}",
    "tgt_type": "POINT",
    "tgt_value": "{tgt}",
    "sl_type": "POINT",
    "sl_value": "{sl}",
}

_LIMIT_ORDER = {
    "type": "limit_order",
    "script_type": "{opt}",
    "script": "{symbol}",
    "product": "INTRADAY",
    "quantity": "{qty}",
    "price": "{limit_price}",
    "transaction_type": "BUY",
}

_CANCEL_ORDER = {
    "type": "cancel_order",
    "script_type": "{opt}",
    "script": "{symbol}",
    "product": "INTRADAY",
}


def _default_auto_order() -> dict:
    return {
        "webhook_url": "", "confirmed": False,
        "tgt": 30, "sl": 12, "qty": 1,
        "max_dist": 8, "limit_offset": 5,
        "templates": {"strategy_order": _copy.deepcopy(_STRATEGY_ORDER),
                      "limit_order": _copy.deepcopy(_LIMIT_ORDER),
                      "cancel_order": _copy.deepcopy(_CANCEL_ORDER)},
    }


AUTO_ORDER = {name: _default_auto_order() for name in INDEXES}
