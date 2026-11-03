"""
bep.py — Break-Even Point (BEP) per strike.

Primary source: a CSV you upload (columns: Strike, CE PDC, PE PDC, BEP).
Fallback: for a strike that is NOT in the uploaded file, the app calculates
BEP = (CE_PDC + PE_PDC) / 2 from the previous day's closes (cached for the day).
"""
import csv
import io
import time
from datetime import datetime

import market_utils

CALC_RETRY_SECONDS = 300

# ---------------------------------------------------------------- uploaded CSV store
_csv_bep: dict = {}            
_csv_info: dict = {"loaded_at": None, "count": 0, "filename": ""}

# ---------------------------------------------------------------- calculated fallback store
_calc_cache: dict = {}         
_calc_fail: dict = {}          
_calc_strikes: set = set()     

def _norm(header: str) -> str:
    return "".join(ch for ch in (header or "").lower() if ch.isalnum())

def _num(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None

def load_csv(data: bytes, filename: str = ""):
    """Parses an uploaded BEP file and replaces the stored one."""
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return False, "The file is empty.", []

    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        reader = csv.reader(io.StringIO(text), dialect)
    except csv.Error:
        reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        return False, "The file needs a header row and at least one data row.", []

    header = [_norm(h) for h in rows[0]]
    idx = {name: header.index(name) for name in ("strike", "cepdc", "pepdc", "bep") if name in header}
    if "strike" not in idx:
        return False, "No 'Strike' column found.", []
    has_pdc = "cepdc" in idx and "pepdc" in idx
    if "bep" not in idx and not has_pdc:
        return False, "Need a 'BEP' column (or both 'CE PDC' and 'PE PDC').", []

    loaded, warnings, skipped = {}, [], 0
    for r in rows[1:]:
        def cell(name):
            return r[idx[name]] if name in idx and idx[name] < len(r) else None

        strike = _num(cell("strike"))
        if strike is None:
            skipped += 1
            continue
        strike = int(round(strike))
        bep = _num(cell("bep"))
        ce_pdc, pe_pdc = _num(cell("cepdc")), _num(cell("pepdc"))
        calc = (ce_pdc + pe_pdc) / 2 if ce_pdc is not None and pe_pdc is not None else None
        if bep is None:
            bep = calc
        if bep is None:
            skipped += 1
            continue
        loaded[strike] = round(bep, 2)

    if not loaded:
        return False, "No usable rows found.", warnings

    _csv_bep.clear()
    _csv_bep.update(loaded)
    _csv_info.update(loaded_at=datetime.now(market_utils.IST), count=len(loaded), filename=filename)
    msg = f"Loaded {len(loaded)} strikes ({min(loaded)} - {max(loaded)})"
    return True, msg, warnings

def status_text() -> str:
    if not _csv_info["loaded_at"]:
        text = "BEP file: not uploaded (all BEPs calculated)"
    else:
        loaded_at = _csv_info["loaded_at"]
        text = f"BEP file: {_csv_info['count']} strikes loaded at {loaded_at.strftime('%d-%b %H:%M')}"
        if loaded_at.date() != datetime.now(market_utils.IST).date():
            text += "  ⚠ NOT from today"
    if _calc_strikes:
        text += f" | calculated: {len(_calc_strikes)}"
    return text

def _calc_bep(client, expiry: str, strike: int):
    today = datetime.now(market_utils.IST).strftime("%Y-%m-%d")
    key = (expiry, strike, today)
    if key in _calc_cache:
        return _calc_cache[key]
    if time.monotonic() - _calc_fail.get(key, -1e9) < CALC_RETRY_SECONDS:
        return None

    mapping, _ = market_utils.option_instrument_keys(client, expiry)
    legs = (mapping or {}).get(strike, {})
    ce_ik, pe_ik = legs.get("CE"), legs.get("PE")
    ce_pdc = pe_pdc = None
    if ce_ik and pe_ik:
        ce_pdc, _ = client.fetch_previous_day_close(ce_ik)
        pe_pdc, _ = client.fetch_previous_day_close(pe_ik)
    if ce_pdc is None or pe_pdc is None:
        _calc_fail[key] = time.monotonic()
        return None

    bep = round((ce_pdc + pe_pdc) / 2, 2)
    _calc_cache[key] = bep
    return bep

def get_bep(client, expiry: str, strike: int):
    strike = int(strike)
    if strike in _csv_bep:
        return _csv_bep[strike], "csv"
    calc = _calc_bep(client, expiry, strike)
    if calc is not None:
        _calc_strikes.add(strike)
        return calc, "calc"
    return None, None