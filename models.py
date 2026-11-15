"""
models.py — data structures shared across the CE/PE Match Alert app.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


@dataclass
class StrikeWindow:
    """The set of strikes currently being watched."""
    center_strike: int
    strikes: List[int]
    calculated_at: datetime
    expiry_date: Optional[str] = None


@dataclass
class MatchEvent:
    """A single CE/PE match. ce_strike and pe_strike may differ (cross-strike match)."""
    ce_strike: int
    pe_strike: int
    price: float              # the shared ROUNDED price
    triggered_at: datetime
    ce_ltp: Optional[float] = None   # actual unrounded CE price
    pe_ltp: Optional[float] = None   # actual unrounded PE price
    index: str = "NIFTY"             # which index the match is from (NIFTY / SENSEX)


@dataclass
class ScanState:
    """Tracks whether the scanner is frozen (in cooldown) and owns the
    currently active strike window."""
    window: Optional[StrikeWindow] = None
    frozen: bool = False
    frozen_until: Optional[datetime] = None
    last_recalc_at: Optional[datetime] = None
    last_match: Optional[MatchEvent] = None
    recent_matches: List[MatchEvent] = field(default_factory=list)
