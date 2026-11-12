"""
support_logic.py - the BEP Support rules for ONE option leg, shared by the live monitor
(bep_monitor.py) and the backtest (backtest_support.py) so the two can never drift apart.

Per 5-minute candle of one leg (one strike, CE or PE):

  TRIGGER  green candle (close > open), low <= BEP + 0.05, close > BEP. The open can be anywhere.
           Every qualifying candle becomes its own green row.
  FAIL     a LATER candle whose close is below the row's fail level turns the row red, but only
           within config.SUPPORT_FAIL_WINDOW_CANDLES candles after the trigger candle (default 3 =
           15 minutes). Once that window has passed the row stays green for good:
             rule 1 (a failed-breakdown setup stood behind the trigger):
                 fail level = the low of the flush candle
             rule 2 (no setup):
                 fail level = the low of the trigger candle itself
  SETUP    flush: a candle closes below BEP.
           validate: the next candle opens at or below BEP and its low does not break the flush
           candle's low. The trigger candle may itself be the validate candle.
"""
from datetime import timedelta

import config

TOL = 0.05
CANDLE_MINUTES = 5


def _within_fail_window(row_start, start) -> bool:
    """True if candle `start` is inside the failure window of a row triggered at `row_start`."""
    n = getattr(config, "SUPPORT_FAIL_WINDOW_CANDLES", 0)
    if not n:                       # 0 / None = no limit (old behaviour)
        return True
    try:
        return start <= row_start + timedelta(minutes=CANDLE_MINUTES * n)
    except TypeError:               # start times are not datetimes: cannot limit, keep old behaviour
        return True


def new_leg_state() -> dict:
    return {"state": "idle", "flush_low": None, "active_row_id": None}


def check_failures(rows: list, strike, start, close, label) -> bool:
    """Turns every green, unresolved row of this strike red if a LATER candle, inside the failure
    window, closes below its fail level. Returns True if any row changed."""
    changed = False
    for row in rows:
        if row["strike"] == strike and row["state"] == "green" and not row["resolved"] \
                and row["start"] < start and _within_fail_window(row["start"], start) \
                and close < row["active_flush_low"]:
            row["state"] = "red"
            row["fail_time"] = label
            row["fail_close"] = close
            row["resolved"] = True
            changed = True
    return changed


def process_candle(st, rows, strike, opt, level, o, l, c, start, label, first_start, add_row) -> bool:
    """Trigger check plus the flush/validate state machine for one candle of one leg.
    `st` is the leg's state dict (mutated in place). `add_row(strike, opt, start, label, level,
    close, fail_low, rule)` creates and stores a new green row. Returns True if a row was added."""
    # Is a valid setup standing behind this candle? (flush done and validated)
    setup_low = None
    if st["state"] == "waiting":
        setup_low = st["flush_low"]
    elif st["state"] == "flushing" and o <= level + TOL and \
            (l >= st["flush_low"] or start == first_start):
        setup_low = st["flush_low"]              # this candle is also the validate candle

    if c > o and c > level and l <= level + TOL:
        added = False
        if not any(r["strike"] == strike and r["time"] == label for r in rows):
            add_row(strike, opt, start, label, level, c,
                    setup_low if setup_low is not None else l,
                    1 if setup_low is not None else 2)
            added = True
        st.update(new_leg_state())
        return added

    if st["state"] == "flushing":
        if o <= level + TOL:
            if l < st["flush_low"] and start != first_start:
                st.update(new_leg_state())
            else:
                st["state"] = "waiting"
        else:
            st.update(new_leg_state())
    elif st["state"] == "idle":
        if c < level:
            st["state"] = "flushing"
            st["flush_low"] = l
    return False
