"""
notifier.py — dispatches BEP Support and CE = PE alerts across all channels:
NiceGUI in-app toast, browser Notification API, and Telegram.

Several supports can arrive in one scan cycle (and from both NIFTY and SENSEX): each gets its own
toast and browser notification, while Telegram sends one combined message listing all of them.
"""
from nicegui import ui

import config
import telegram_bot


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


def fire_telegram(events: list):
    """
    Sends to every configured chat ID (comma-separated). Returns (all_ok, err_summary).
    A partial failure (some recipients succeeded, others didn't) still
    returns False so it gets logged, but doesn't stop the successful sends
    from having gone out - send_message_to_all already tried every recipient.
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
    """Fires all channels for every CE = PE match found in a scan cycle (unless alerts are off or paused)."""
    if not config.MATCH_ALERTS_ENABLED:
        return
    if config.ALERTS_PAUSED:
        log_fn(f"Alerts paused: {len(events)} match alert(s) not sent (toast, browser, Telegram).")
        return
    for e in events:
        fire_toast(e)
        fire_browser_notification(e)
    ok, err = fire_telegram(events)
    if not ok:
        log_fn(f"Telegram send failed: {err}")


# ------------------------------------------------------------------ BEP Support alerts
def _support_label(e: dict) -> str:
    how = "failed breakdown" if e.get("rule") == 1 else "touch and go"
    return (f"{e.get('index', 'NIFTY')} BEP Support {e['opt']}: {e['strike']} | BEP {e['bep']:.2f} | "
            f"close {e['close']:.2f} | {e['time']} ({how})")


def dispatch_support_alerts(events: list, log_fn=print):
    """Toast + browser notification per new BEP support row, one combined Telegram message."""
    if not events:
        return
    # if config.ALERTS_PAUSED:
    #     log_fn(f"Alerts paused: {len(events)} BEP support alert(s) not sent (toast, browser, Telegram).")
    #     return
    for e in events:
        label = _support_label(e)
        ui.notify(label, type="positive", position="top", timeout=10000)
        ui.run_javascript(f"""
            if (Notification.permission === "granted") {{
                new Notification({(e.get('index', 'NIFTY') + " BEP Support " + e['opt'])!r}, {{ body: {label!r} }});
            }}
        """)
    if not config.TELEGRAM_ENABLED:
        return
    all_ok, results = telegram_bot.send_message_to_all(telegram_bot.build_support_message(events))
    if not all_ok:
        failed = [f"{cid}: {err}" for cid, ok, err in results if not ok]
        log_fn(f"Telegram send failed: {'; '.join(failed)}")
