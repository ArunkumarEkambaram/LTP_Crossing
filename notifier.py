"""
notifier.py — dispatches CE = PE alerts:
NiceGUI in-app toast and the browser Notification API.

Several matches can arrive in one scan cycle (and from both NIFTY and SENSEX): each gets its own
toast and browser notification.
"""
from nicegui import ui

import config


def _label(event) -> str:
    idx = getattr(event, "index", "NIFTY")
    ce_txt = f"{event.ce_ltp:.2f}" if event.ce_ltp is not None else "-"
    pe_txt = f"{event.pe_ltp:.2f}" if event.pe_ltp is not None else "-"
    if event.ce_strike == event.pe_strike:
        return f"{idx} strike {event.ce_strike}: CE {ce_txt} / PE {pe_txt} -> {event.price:.0f}"
    return f"{idx} CE {event.ce_strike} ({ce_txt}) / PE {event.pe_strike} ({pe_txt}) -> {event.price:.0f}"


def fire_toast(event):
    ui.notify(_label(event), type="warning", position="top", timeout=10000)


def fire_browser_notification(event):
    """
    Uses the browser Notification API. Requires permission to have been
    requested once on page load (see app.py).
    """
    title = f"{getattr(event, 'index', 'NIFTY')} CE = PE Match"
    body = _label(event)
    ui.run_javascript(f"""
        if (Notification.permission === "granted") {{
            new Notification({title!r}, {{ body: {body!r} }});
        }}
    """)


def dispatch_match_alerts(events: list, log_fn=print):
    """Fires all channels for every CE = PE match found in a scan cycle (unless alerts are off or paused)."""
    if not config.MATCH_ALERTS_ENABLED:
        return
    if config.ALERTS_PAUSED:
        log_fn(f"Alerts paused: {len(events)} match alert(s) not sent (toast, browser).")
        return
    for e in events:
        fire_toast(e)
        fire_browser_notification(e)
