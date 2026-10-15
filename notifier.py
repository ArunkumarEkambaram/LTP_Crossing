"""
notifier.py — dispatches match alerts across all channels:
NiceGUI in-app toast, browser Notification API, and Telegram.

Multiple matches can occur in a single scan cycle: each gets its own toast
and browser notification, while Telegram sends one combined message listing
all of them (avoids spamming Telegram with several messages per second).
"""
from nicegui import ui

import config
import telegram_bot


def _label(event) -> str:
    ce_txt = f"{event.ce_ltp:.2f}" if event.ce_ltp is not None else "-"
    pe_txt = f"{event.pe_ltp:.2f}" if event.pe_ltp is not None else "-"
    if event.ce_strike == event.pe_strike:
        return f"Strike {event.ce_strike}: CE {ce_txt} / PE {pe_txt} -> {event.price:.0f}"
    return f"CE {event.ce_strike} ({ce_txt}) / PE {event.pe_strike} ({pe_txt}) -> {event.price:.0f}"


def fire_toast(event):
    ui.notify(_label(event), type="warning", position="top", timeout=10000)


def fire_browser_notification(event):
    """
    Uses the browser Notification API. Requires permission to have been
    requested once on page load (see app.py).
    """
    title = "CE = PE Match"
    body = _label(event)
    ui.run_javascript(f"""
        if (Notification.permission === "granted") {{
            new Notification({title!r}, {{ body: {body!r} }});
        }}
    """)


def fire_telegram(events: list):
    """
    Sends to every configured chat ID (comma-separated). Returns (all_ok, err_summary).
    A partial failure (some recipients succeeded, others didn't) still
    returns False so it gets logged, but doesn't stop the successful sends
    from having gone out — send_message_to_all already tried every recipient.
    """
    if not config.TELEGRAM_ENABLED:
        return True, "disabled"
    msg = telegram_bot.build_match_message(events)
    all_ok, results = telegram_bot.send_message_to_all(msg)
    if all_ok:
        return True, ""
    failed = [f"{cid}: {err}" for cid, ok, err in results if not ok]
    return False, "; ".join(failed)


def dispatch_match_alerts(events: list, log_fn=print):
    """Fires all channels for every match event found in a scan cycle."""
    for e in events:
        fire_toast(e)
        fire_browser_notification(e)
    ok, err = fire_telegram(events)
    if not ok:
        log_fn(f"Telegram send failed: {err}")


def _bep_label(alert: dict) -> str:
    return f"BEP: {alert['text']} (5-min candle till {alert['candle_end']})"


def dispatch_bep_alert(alert: dict, log_fn=print):
    """Separate alert (toast + browser notification + Telegram) raised when a CE or
    PE closes above its strike's BEP."""
    label = _bep_label(alert)
    ui.notify(label, type="info", position="top", timeout=10000)
    ui.run_javascript(f"""
        if (Notification.permission === "granted") {{
            new Notification("BEP Alert", {{ body: {label!r} }});
        }}
    """)
    if config.TELEGRAM_ENABLED:
        all_ok, results = telegram_bot.send_message_to_all(telegram_bot.build_bep_message(alert))
        if not all_ok:
            failed = "; ".join(f"{cid}: {err}" for cid, ok, err in results if not ok)
            log_fn(f"Telegram BEP alert failed: {failed}")
