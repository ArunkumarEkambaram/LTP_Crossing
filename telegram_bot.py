"""
telegram_bot.py — Telegram send wrapper, kept separate from notifier.py so
credentials/plumbing don't mix into the general alert-dispatch interface.

Supports sending the same message to MULTIPLE chat IDs (comma-separated
in config.TELEGRAM_CHAT_ID / the sidebar input), so one alert reaches
several Telegram accounts at once.
"""
import requests
import config


def _parse_chat_ids(chat_id_str: str) -> list:
    """Splits a comma-separated chat ID string into a clean list, e.g.
    '111111, 222222,333333' -> ['111111', '222222', '333333']."""
    return [cid.strip() for cid in (chat_id_str or "").split(",") if cid.strip()]


def send_message(message: str, bot_token: str = None, chat_id: str = None):
    """Sends to a SINGLE chat_id. Returns (ok: bool, error: str)."""
    bot_token = bot_token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        return False, "Telegram bot token or chat ID not configured"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id.strip(), "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True, ""
        err = resp.json().get("description", resp.text[:150])
        return False, err
    except Exception as e:
        return False, str(e)


def send_message_to_all(message: str, bot_token: str = None, chat_id: str = None):
    """
    Sends the same message to every chat ID in a comma-separated chat_id
    string (falls back to config.TELEGRAM_CHAT_ID if not passed).

    Returns (all_ok: bool, results: list[(chat_id, ok, error_str)]).
    all_ok is True only if every recipient succeeded — a partial failure
    (e.g. one account blocked the bot) still shows up per-recipient in results.
    """
    bot_token = bot_token or config.TELEGRAM_BOT_TOKEN
    chat_id_str = chat_id or config.TELEGRAM_CHAT_ID
    chat_ids = _parse_chat_ids(chat_id_str)

    if not bot_token or not chat_ids:
        return False, [("", False, "Bot token or chat ID(s) not configured")]

    results = []
    all_ok = True
    for cid in chat_ids:
        ok, err = send_message(message, bot_token, cid)
        results.append((cid, ok, err))
        if not ok:
            all_ok = False
    return all_ok, results


def build_match_message(events: list) -> str:
    """events: list of MatchEvent — builds one message covering all matches
    found in a single scan cycle."""
    lines = ["\U0001f3af *CE = PE Match*",
             "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"]
    for e in events:
        time_label = e.triggered_at.strftime("%H:%M:%S")
        ce_txt = f"{e.ce_ltp:.2f}" if e.ce_ltp is not None else "-"
        pe_txt = f"{e.pe_ltp:.2f}" if e.pe_ltp is not None else "-"
        if e.ce_strike == e.pe_strike:
            lines.append(f"[{time_label}] Strike *{e.ce_strike}*: CE {ce_txt} / PE {pe_txt} \u2192 *{e.price:.0f}*")
        else:
            lines.append(f"[{time_label}] CE *{e.ce_strike}* ({ce_txt}) / PE *{e.pe_strike}* ({pe_txt}) \u2192 *{e.price:.0f}*")
    lines.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")
    return "\n".join(lines)


def test_telegram(bot_token: str, chat_id: str):
    """Sends a test message to every chat ID. Returns (all_ok, results)
    — same shape as send_message_to_all."""
    msg = "\u2705 *CE/PE Match Alert* \u2014 Telegram connected successfully!"
    return send_message_to_all(msg, bot_token, chat_id)
