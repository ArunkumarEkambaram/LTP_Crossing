"""
state_manager.py — owns the freeze/cooldown/recalculation decisions.

Rules (per finalized design):
- Every RECALC_INTERVAL_SECONDS, recalc the strike window from live Nifty LTP,
  but ONLY if not currently frozen.
- The moment any match(es) are found in a scan cycle: freeze the window
  (stop recalculating) and suppress further alerts, for COOLDOWN_SECONDS.
  All matches found in that same cycle are recorded (not just the first).
- When the freeze expires: recalc the window from the *current* Nifty LTP
  and resume normal scanning.
"""
from datetime import datetime, timedelta

import config
import market_utils
from models import ScanState, StrikeWindow, MatchEvent


class StateManager:
    def __init__(self):
        self.state = ScanState()

    def should_recalculate(self, now: datetime) -> bool:
        if self.state.frozen:
            return False
        if self.state.window is None or self.state.last_recalc_at is None:
            return True
        elapsed = (now - self.state.last_recalc_at).total_seconds()
        return elapsed >= config.RECALC_INTERVAL_SECONDS

    def recalculate_window(self, nifty_ltp: float, expiry_date: str, now: datetime) -> StrikeWindow:
        strikes = market_utils.build_strike_window(nifty_ltp)
        self.state.window = StrikeWindow(
            center_strike=market_utils.nearest_strike(nifty_ltp),
            strikes=strikes,
            calculated_at=now,
            expiry_date=expiry_date,
        )
        self.state.last_recalc_at = now
        return self.state.window

    def maybe_unfreeze(self, now: datetime):
        if self.state.frozen and self.state.frozen_until and \
                now >= self.state.frozen_until - timedelta(seconds=config.COOLDOWN_GRACE_SECONDS):
            self.state.frozen = False
            self.state.frozen_until = None

    def register_matches(self, events: list, now: datetime):
        """Records all match events found in one scan cycle and freezes once."""
        if not events:
            return
        self.state.recent_matches.extend(events)
        self.state.last_match = events[-1]
        self.state.frozen = True
        self.state.frozen_until = now + timedelta(seconds=config.COOLDOWN_SECONDS)

    @property
    def is_frozen(self) -> bool:
        return self.state.frozen

    @property
    def cooldown_remaining_seconds(self) -> int:
        if not self.state.frozen_until:
            return 0
        remaining = (self.state.frozen_until - datetime.now()).total_seconds()
        return max(0, int(remaining))
