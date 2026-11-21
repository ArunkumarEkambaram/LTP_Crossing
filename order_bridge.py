"""
order_bridge.py - turns an order signal (market entry, limit entry or cancel) into the AlgoDelta JSON Bridge payload
and POSTs it.

Safety: nothing is sent live unless ALL of these hold - Dry run unticked, algo not paused, the JSON template is confirmed and is a real
template (not a stub), a webhook URL is set for that index, and (entries only) Auto order is ticked and today's live-order
limit is not reached. In every other case the payload is only logged (dry run). Cancel orders are never blocked by
the Auto order checkbox or the daily limit (cancelling is protective). A failed live send is NOT retried.
"""
import copy
import json
import re
from datetime import datetime

import requests

import config

_today = {"date": None, "count": 0}

_PLACEHOLDER = re.compile(r"(?<!\{)\{(\w+)\}(?!\})")
_NUMERIC = ("qty", "strike", "tgt", "sl", "limit_price")


def _fill(value, ctx):
    """Replaces {name} placeholders that exist in ctx. Unknown names and double braces such as
    {{strategy.position_size}} are left exactly as written."""
    if isinstance(value, dict):
        return {k: _fill(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ctx) for v in value]
    if isinstance(value, str):
        m = _PLACEHOLDER.fullmatch(value)
        if m and m.group(1) in _NUMERIC and m.group(1) in ctx:
            return ctx[m.group(1)]                   # numbers stay numbers
        return _PLACEHOLDER.sub(lambda mm: str(ctx[mm.group(1)]) if mm.group(1) in ctx else mm.group(0), value)
    return value


def _expiry_code(expiry) -> str:
    """'2026-11-17' -> '17NOV26' (the format inside AlgoDelta option symbols)."""
    try:
        return datetime.strptime(str(expiry), "%Y-%m-%d").strftime("%d%b%y").upper()
    except ValueError:
        return ""


def _num(value, default):
    """Number from a form value: 30.0 -> 30, 12.5 stays 12.5, empty -> default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return int(f) if f == int(f) else f


def build_payload(sig: dict, template: dict = None, params: dict = None) -> dict:
    """Fills the placeholders of the template that matches sig["order_type"] (strategy_order / limit_order /
    cancel_order). `template` / `params` ({"tgt", "sl", "qty"}) override the saved ones (used by Preview JSON)."""
    cfg = config.AUTO_ORDER[sig["index"]]
    order_type = sig.get("order_type", "strategy_order")
    if template is None:
        template = cfg["templates"].get(order_type) or {"type": order_type}
    p = {"tgt": cfg["tgt"], "sl": cfg["sl"], "qty": cfg["qty"]}
    p.update(params or {})
    strike, opt, index = int(sig["strike"]), sig["opt"], sig["index"]
    expiry = _expiry_code(sig.get("expiry"))
    ctx = {
        "index": index, "opt": opt, "strike": strike, "expiry": expiry,
        "symbol": f"{index}{expiry}{strike}{opt}",
        "signal": "UP" if opt == "CE" else "DOWN",
        "bep": sig["bep"], "ltp": sig["ltp"],
        "qty": max(1, int(_num(p["qty"], 1))),
        "tgt": _num(p["tgt"], 0), "sl": _num(p["sl"], 0),
    }
    if sig.get("limit_price") is not None:
        ctx["limit_price"] = _num(round(float(sig["limit_price"]), 2), 0)
    return _fill(copy.deepcopy(template), ctx)


def _describe(sig: dict) -> str:
    kind = sig.get("kind")
    head = f"{sig['index']} {sig['opt']} {sig['strike']}"
    if kind == "cancel":
        return f"{head} [CANCEL limit order: {sig.get('reason', '')}]"
    if kind == "limit":
        return f"{head} [limit buy @ {float(sig['limit_price']):.2f}] (BEP {sig['bep']:.2f}, LTP {sig['ltp']:.2f})"
    label = "opening candle" if kind == "opening" else "market entry"
    return f"{head} [{label}] (BEP {sig['bep']:.2f}, LTP {sig['ltp']:.2f})"


def _res(status, message, payload, detail):
    return {"status": status, "message": message, "payload": payload, "detail": detail}


def send_order(sig: dict) -> dict:
    """Returns {"status": 'sent' | 'dry_run' | 'blocked' | 'failed', "message", "payload", "detail"}."""
    order_type = sig.get("order_type", "strategy_order")
    is_cancel = order_type == "cancel_order"
    cfg = config.AUTO_ORDER[sig["index"]]
    template = cfg["templates"].get(order_type) or {"type": order_type}
    payload = build_payload(sig)
    text = json.dumps(payload)
    head = _describe(sig)

    if not is_cancel and not config.AUTO_ORDER_ENABLED:
        return _res("blocked", f"Auto order is off; NOT sent: {head}", payload, "Auto order is off")
    if not is_cancel and config.AUTO_ORDER_PAUSED:
        return _res("blocked", f"Algo trading is PAUSED; NOT sent: {head}", payload, "Algo trading paused")

    today = datetime.now().strftime("%Y-%m-%d")
    if _today["date"] != today:
        _today.update(date=today, count=0)

    why = None
    if config.AUTO_ORDER_DRY_RUN:
        why = "dry run"
    elif len(template) <= 1:
        why = f"no JSON template for {order_type} yet"
    elif not cfg["confirmed"]:
        why = "JSON template not confirmed"
    elif not cfg["webhook_url"]:
        why = "no webhook URL set"
    if why:
        return _res("dry_run", f"DRY RUN ({why}) {head}: {text}", payload, why)

    if not is_cancel:
        if _today["count"] >= config.AUTO_ORDER_DAILY_LIMIT:
            return _res("blocked", f"Daily order limit ({config.AUTO_ORDER_DAILY_LIMIT}) reached; NOT sent: {head}", payload,
                        f"Daily limit ({config.AUTO_ORDER_DAILY_LIMIT}) reached")
        _today["count"] += 1
    try:
        resp = requests.post(cfg["webhook_url"], json=payload, timeout=5)
        body = (resp.text or "")[:200]
        if 200 <= resp.status_code < 300:
            return _res("sent", f"ORDER SENT {head} -> HTTP {resp.status_code} {body}", payload, f"HTTP {resp.status_code} {body}")
        return _res("failed", f"ORDER FAILED {head} -> HTTP {resp.status_code} {body} (not retried)", payload,
                    f"HTTP {resp.status_code} {body} (not retried)")
    except Exception as ex:
        return _res("failed", f"ORDER FAILED {head} -> {ex} (not retried)", payload, f"{ex} (not retried)")
