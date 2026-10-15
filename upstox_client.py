"""
upstox_client.py — Upstox v2/v3 API wrapper for the CE/PE Match Alert app.

Auth model matches your other projects: paste in a pre-generated
access_token; no OAuth redirect flow implemented here.
"""
import requests
from urllib.parse import quote
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

BASE_V2 = "https://api.upstox.com/v2"
BASE_V3 = "https://api.upstox.com/v3"


class UpstoxClient:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    # ---------------------------------------------------------- LTP
    def fetch_ltp(self, instrument_keys: list):
        """Returns ({instrument_key: ltp}, None) or (None, error_str)."""
        if not instrument_keys:
            return {}, None
        joined = ",".join(instrument_keys)
        url = f"{BASE_V2}/market-quote/ltp"
        try:
            resp = requests.get(url, headers=self.headers,
                                 params={"instrument_key": joined}, timeout=10)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            data = resp.json().get("data", {})
            result = {}
            for v in data.values():
                ik = v.get("instrument_token") or v.get("instrument_key")
                ltp = v.get("last_price")
                if ik is not None and ltp is not None:
                    result[ik] = ltp
            return result, None
        except Exception as e:
            return None, str(e)

    # ---------------------------------------------------------- expiries
    def fetch_option_expiries(self, underlying_key: str):
        """Returns (list_of_expiry_date_strings_ascending, None) or (None, error)."""
        url = f"{BASE_V2}/option/contract"
        try:
            resp = requests.get(url, headers=self.headers,
                                 params={"instrument_key": underlying_key}, timeout=10)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            contracts = resp.json().get("data", [])
            expiries = sorted({c["expiry"] for c in contracts if c.get("expiry")})
            return expiries, None
        except Exception as e:
            return None, str(e)

    # ---------------------------------------------------------- option chain
    def fetch_option_chain(self, underlying_key: str, expiry_date: str):
        """
        Returns (chain, None) or (None, error). chain is a list of dicts:
        {strike_price, call_options: {market_data: {ltp,...}}, put_options: {...}}
        """
        url = f"{BASE_V2}/option/chain"
        try:
            resp = requests.get(
                url, headers=self.headers,
                params={"instrument_key": underlying_key, "expiry_date": expiry_date},
                timeout=10,
            )
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return resp.json().get("data", []), None
        except Exception as e:
            return None, str(e)

    def fetch_ce_pe_ltp_for_strikes(self, underlying_key: str, expiry_date: str, strikes: list):
        """
        Filters the full option chain down to just the strikes we care about.
        Returns ({strike: {"CE": ltp_or_None, "PE": ltp_or_None}}, None) or (None, error).
        """
        chain, err = self.fetch_option_chain(underlying_key, expiry_date)
        if chain is None:
            return None, err

        wanted = set(strikes)
        result = {s: {"CE": None, "PE": None} for s in strikes}
        for row in chain:
            strike = row.get("strike_price")
            if strike is None or int(strike) not in wanted:
                continue
            strike = int(strike)
            ce = (row.get("call_options") or {}).get("market_data", {}) or {}
            pe = (row.get("put_options") or {}).get("market_data", {}) or {}
            result[strike]["CE"] = ce.get("ltp")
            result[strike]["PE"] = pe.get("ltp")
        return result, None

    # ---------------------------------------------------------- option contracts (for backtest)
    def fetch_option_contracts(self, underlying_key: str, expiry_date: str = None):
        """
        Returns (contracts, None) or (None, error). Each contract dict includes
        instrument_key, strike_price, expiry, instrument_type ('CE'/'PE').
        """
        url = f"{BASE_V2}/option/contract"
        params = {"instrument_key": underlying_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return resp.json().get("data", []), None
        except Exception as e:
            return None, str(e)

    def fetch_option_instrument_keys(self, underlying_key: str, expiry_date: str, strikes: list):
        """
        Resolves each tracked strike to its CE/PE instrument_key for the given expiry.
        Returns ({strike: {"CE": instrument_key_or_None, "PE": instrument_key_or_None}}, None)
        or (None, error). Needed because historical candles are fetched per
        instrument_key, unlike the live option chain which returns LTP directly.
        """
        contracts, err = self.fetch_option_contracts(underlying_key, expiry_date)
        if contracts is None:
            return None, err

        wanted = set(strikes)
        result = {s: {"CE": None, "PE": None} for s in strikes}
        for c in contracts:
            strike = c.get("strike_price")
            if strike is None or int(strike) not in wanted:
                continue
            strike = int(strike)
            opt_type = c.get("instrument_type")  # 'CE' or 'PE'
            ik = c.get("instrument_key")
            if opt_type in ("CE", "PE") and ik:
                result[strike][opt_type] = ik
        return result, None

    # ---------------------------------------------------------- minute candles (for backtest)
    def fetch_minute_candles(self, instrument_key: str, date_str: str):
        """
        Returns (candles, None) or (None, error). candles is a list of
        [timestamp, open, high, low, close, volume, oi], oldest-first.

        Uses the historical endpoint for past dates (Upstox has no historical
        data for a day still in progress) and the intraday endpoint when
        date_str is today.
        """
        encoded_key = quote(instrument_key, safe="")
        today_str = datetime.now(IST).strftime("%Y-%m-%d")

        if date_str == today_str:
            url = f"{BASE_V3}/historical-candle/intraday/{encoded_key}/minutes/1"
        else:
            url = f"{BASE_V3}/historical-candle/{encoded_key}/minutes/1/{date_str}/{date_str}"

        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            candles = resp.json().get("data", {}).get("candles", [])
            candles = sorted(candles, key=lambda c: c[0])  # oldest-first
            return candles, None
        except Exception as e:
            return None, str(e)


    def fetch_intraday_candles(self, instrument_key: str, interval_minutes: int = 5):
        """
        Today's N-minute candles (v3 intraday endpoint), including the one still
        forming. Returns (candles_oldest_first, None) or (None, error).
        Each candle: [timestamp, open, high, low, close, volume, oi].
        """
        encoded_key = quote(instrument_key, safe="")
        url = f"{BASE_V3}/historical-candle/intraday/{encoded_key}/minutes/{interval_minutes}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            candles = resp.json().get("data", {}).get("candles", [])
            return sorted(candles, key=lambda c: c[0]), None
        except Exception as e:
            return None, str(e)

    def fetch_previous_day_close(self, instrument_key: str):
        """
        Close of the most recent COMPLETED daily candle, i.e. dated strictly before
        today. (Upstox can include today's still-forming candle as the newest row
        during market hours, so we skip it.) Looks back 7 days to cover weekends
        and holidays. Returns (close, None) or (None, error).
        """
        encoded_key = quote(instrument_key, safe="")
        now = datetime.now(IST)
        to_date = now.strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        url = f"{BASE_V3}/historical-candle/{encoded_key}/days/1/{to_date}/{from_date}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
            candles = resp.json().get("data", {}).get("candles", [])
            for c in sorted(candles, key=lambda c: c[0], reverse=True):  # newest first
                if c[0][:10] < to_date:
                    return c[4], None  # index 4 = close
            return None, "No completed daily candle found in the last 7 days"
        except Exception as e:
            return None, str(e)


def test_connection(access_token: str):
    """Quick sanity check the token works — hits the user profile endpoint."""
    url = f"{BASE_V2}/user/profile"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            return True, data.get("user_name", "Connected")
        return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
    except Exception as e:
        return False, str(e)
