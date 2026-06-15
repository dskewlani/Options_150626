"""
angel_api.py — Angel One SmartAPI REST + WebSocket handler
Full live WebSocket subscription with auto-reconnect and option chain token management.
"""

import os
import time
import threading
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pyotp
from dotenv import load_dotenv

load_dotenv()

# ── Credentials (prefer .env / Streamlit secrets over hardcoded) ───────────────
ANGEL_API_KEY     = os.getenv("ANGEL_API_KEY",     "WKZ1Ve6i")
ANGEL_CLIENT_ID   = os.getenv("ANGEL_CLIENT_ID",   "K258077")
ANGEL_PASSWORD    = os.getenv("ANGEL_PASSWORD",     "1811")"""
angel_api.py — Angel One SmartAPI REST + WebSocket handler
Full live WebSocket subscription with auto-reconnect and option chain token management.
"""

import os
import time
import threading
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pyotp
from dotenv import load_dotenv

load_dotenv()

# ── Credentials (prefer .env / Streamlit secrets over hardcoded) ───────────────
ANGEL_API_KEY     = os.getenv("ANGEL_API_KEY",     "WKZ1Ve6i")
ANGEL_CLIENT_ID   = os.getenv("ANGEL_CLIENT_ID",   "K258077")
ANGEL_PASSWORD    = os.getenv("ANGEL_PASSWORD",     "1811")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "HBIWFBUKBUJ4XXNY6MUTE65WIM")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("angel_api")

# ── Exchange codes ─────────────────────────────────────────────────────────────
EXCHANGE_NSE = 1   # NSE Cash (indices)
EXCHANGE_NFO = 2   # NSE F&O  (options / futures)

# ── Index token map ────────────────────────────────────────────────────────────
INDEX_TOKENS: Dict[str, str] = {
    "NIFTY":     "26000",
    "BANKNIFTY": "26009",
    "FINNIFTY":  "26037",
    "INDIA VIX": "26017",
}

# ── Shared live tick store ─────────────────────────────────────────────────────
# { token_str: { ltp, open, high, low, close, volume, oi, ts } }
_tick_store: Dict[str, dict] = {}
_tick_lock  = threading.Lock()

# ── Shared connection state ────────────────────────────────────────────────────
_ws_connected  = False
_ws_error      = None       # last error string
_ws_instance   = None
_ws_thread     = None
_reconnect_flag = threading.Event()

# ── REST client (singleton) ────────────────────────────────────────────────────
_smart_api      = None
_session_lock   = threading.Lock()
_feed_token     = None
_jwt_token      = None


def get_smart_api():
    """Return authenticated SmartConnect instance; thread-safe singleton."""
    global _smart_api, _feed_token, _jwt_token
    with _session_lock:
        if _smart_api is not None:
            return _smart_api
        try:
            from SmartApi import SmartConnect          # type: ignore
            obj  = SmartConnect(api_key=ANGEL_API_KEY)
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            resp = obj.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)
            if resp and resp.get("status"):
                _smart_api  = obj
                _feed_token = resp["data"].get("feedToken", "")
                _jwt_token  = resp["data"].get("jwtToken", "")
                logger.info("Angel One REST session established.")
            else:
                logger.error(f"Session failed: {resp}")
        except Exception as exc:
            logger.error(f"get_smart_api error: {exc}")
    return _smart_api


def refresh_session():
    """Force a new session (call if JWT expires)."""
    global _smart_api
    with _session_lock:
        _smart_api = None
    return get_smart_api()


# ── NFO token resolution ───────────────────────────────────────────────────────
_nfo_token_cache: Dict[str, str] = {}


def resolve_nfo_token(symbol: str, expiry: str,
                       strike: int, option_type: str) -> Optional[str]:
    """
    Resolve NFO instrument token for an option contract.
      symbol      : 'NIFTY' | 'BANKNIFTY' | 'FINNIFTY'
      expiry      : Angel One format e.g. '29MAY2025'
      strike      : integer e.g. 24700
      option_type : 'CE' | 'PE'
    Returns token string or None.
    """
    key = f"{symbol}_{expiry}_{strike}_{option_type}"
    if key in _nfo_token_cache:
        return _nfo_token_cache[key]
    try:
        api = get_smart_api()
        if api is None:
            return None
        # Angel One searchScrip for NFO
        scrip_name = f"{symbol}{expiry}{strike}{option_type}"
        resp = api.searchScrip("NFO", scrip_name)
        if resp and resp.get("data"):
            token = resp["data"][0]["symboltoken"]
            _nfo_token_cache[key] = str(token)
            logger.info(f"Resolved NFO token {token} for {key}")
            return str(token)
    except Exception as exc:
        logger.error(f"resolve_nfo_token error for {key}: {exc}")
    return None


def resolve_nfo_tokens_bulk(contracts: List[dict]) -> Dict[str, str]:
    """
    Resolve multiple NFO tokens in one go.
    contracts = [{"symbol":"NIFTY","expiry":"29MAY2025",
                  "strike":24700,"option_type":"CE"}, ...]
    Returns { key: token }
    """
    result = {}
    for c in contracts:
        token = resolve_nfo_token(
            c["symbol"], c["expiry"], c["strike"], c["option_type"])
        if token:
            key = f"{c['symbol']}_{c['expiry']}_{c['strike']}_{c['option_type']}"
            result[key] = token
    return result


# ── WebSocket callbacks ────────────────────────────────────────────────────────

def _on_data(wsapp, message):
    """Fired on every tick — stores into _tick_store."""
    global _ws_connected
    try:
        _ws_connected = True
        token = str(message.get("token", ""))
        if not token:
            return
        with _tick_lock:
            _tick_store[token] = {
                "ltp":    message.get("last_traded_price",   0) / 100,
                "open":   message.get("open_price_of_the_day", 0) / 100,
                "high":   message.get("high_price_of_the_day", 0) / 100,
                "low":    message.get("low_price_of_the_day",  0) / 100,
                "close":  message.get("closed_price",          0) / 100,
                "volume": message.get("volume_trade_for_the_day", 0),
                "oi":     message.get("open_interest",         0),
                "bid":    message.get("best_5_buy_data",  [{}])[0].get("price", 0) / 100,
                "ask":    message.get("best_5_sell_data", [{}])[0].get("price", 0) / 100,
                "ts":     datetime.now(),
            }
    except Exception as exc:
        logger.error(f"_on_data error: {exc}")


def _on_open(wsapp):
    global _ws_connected, _ws_error, _ws_instance
    _ws_connected = True
    _ws_error     = None
    logger.info("Angel One WebSocket CONNECTED.")
    # Subscribe AFTER connection is open — subscribing before connect() causes
    # 'NoneType has no attribute send' because the underlying socket isn't ready
    if _current_token_list and _ws_instance is not None:
        try:
            total = sum(len(g["tokens"]) for g in _current_token_list)
            logger.info(f"Subscribing {total} tokens on open …")
            _ws_instance.subscribe("session_ws", 3, _current_token_list)
        except Exception as exc:
            logger.error(f"subscribe in on_open failed: {exc}")


def _on_error(wsapp, error):
    global _ws_connected, _ws_error
    _ws_connected = False
    _ws_error     = str(error)
    logger.error(f"WebSocket error: {error}")
    _reconnect_flag.set()


def _on_close(wsapp):
    global _ws_connected
    _ws_connected = False
    logger.warning("WebSocket CLOSED.")
    _reconnect_flag.set()


# ── WebSocket manager ──────────────────────────────────────────────────────────

# Current token subscription list — kept globally so reconnect can re-subscribe
_current_token_list: List[dict] = []


def _build_ws_instance():
    """Create a fresh SmartWebSocketV2 instance."""
    global _feed_token
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore

    api = get_smart_api()
    if api is None:
        raise RuntimeError("REST session unavailable — cannot create WebSocket.")

    # Refresh feed token on every new WS instance
    _feed_token = api.getfeedToken()

    # Refresh jwt_token from the API instance if available
    global _jwt_token
    if hasattr(api, "access_token") and api.access_token:
        _jwt_token = api.access_token

    ws = SmartWebSocketV2(
        auth_token=_jwt_token,      # ← JWT Bearer token, NOT feed token
        api_key=ANGEL_API_KEY,
        client_code=ANGEL_CLIENT_ID,
        feed_token=_feed_token,
    )
    ws.on_open  = _on_open
    ws.on_data  = _on_data
    ws.on_error = _on_error
    ws.on_close = _on_close
    return ws


def _ws_runner(token_list: List[dict]):
    """Thread target — connects WS and handles auto-reconnect loop."""
    global _ws_instance, _ws_connected, _current_token_list
    _current_token_list = token_list
    MAX_RETRIES = 10
    retry = 0

    while retry < MAX_RETRIES:
        _reconnect_flag.clear()
        try:
            _ws_instance = _build_ws_instance()
            # NOTE: do NOT call subscribe() here — the socket isn't open yet.
            # Subscription happens inside _on_open, which fires once connected.
            logger.info("Connecting WebSocket …")
            _ws_instance.connect()          # blocks until close/error
        except Exception as exc:
            logger.error(f"WS runner error (attempt {retry+1}): {exc}")

        was_connected = _ws_connected
        _ws_connected = False
        # Reset retry counter if we had a successful connection before dropping,
        # so brief network blips don't burn through all retries permanently
        if was_connected:
            retry = 0
        else:
            retry += 1
        wait = min(2 ** retry, 60)
        logger.warning(f"Reconnecting in {wait}s … (attempt {retry}/{MAX_RETRIES})")
        _reconnect_flag.wait(timeout=wait)

    logger.error("WebSocket: max retries reached. Giving up.")


def start_websocket(token_list: List[dict]):
    """
    Launch WebSocket in a daemon thread. Safe to call once at app startup.

    token_list format:
      [
        {"exchangeType": 1, "tokens": ["26000", "26009"]},   # NSE indices
        {"exchangeType": 2, "tokens": ["token_nfo_1", …]},   # NFO options
      ]
    """
    global _ws_thread
    if _ws_thread and _ws_thread.is_alive():
        logger.info("WebSocket thread already running.")
        return

    _ws_thread = threading.Thread(
        target=_ws_runner, args=(token_list,), daemon=True, name="ws_thread")
    _ws_thread.start()
    logger.info("WebSocket thread launched.")


def subscribe_tokens(new_tokens: List[dict]):
    """
    Add more tokens to an already-running WebSocket session.
    new_tokens: same format as token_list in start_websocket.
    """
    global _ws_instance, _current_token_list
    if _ws_instance is None or not _ws_connected:
        logger.warning("WebSocket not connected — queuing tokens for next connect.")
        _current_token_list.extend(new_tokens)
        return
    try:
        _ws_instance.subscribe("session_ws", 3, new_tokens)
        _current_token_list.extend(new_tokens)
        logger.info(f"Subscribed {sum(len(g['tokens']) for g in new_tokens)} additional tokens.")
    except Exception as exc:
        logger.error(f"subscribe_tokens error: {exc}")


def ws_status() -> dict:
    """Return current WebSocket connection status for UI display."""
    return {
        "connected": _ws_connected,
        "error":     _ws_error,
        "tokens":    sum(len(g["tokens"]) for g in _current_token_list),
        "ticks":     len(_tick_store),
    }


# ── Tick accessors ─────────────────────────────────────────────────────────────

def get_tick(token: str) -> Optional[dict]:
    with _tick_lock:
        return dict(_tick_store[str(token)]) if str(token) in _tick_store else None


def get_ltp(token: str, fallback: float = 0.0) -> float:
    t = get_tick(token)
    return t["ltp"] if t else fallback


def get_index_ltp(symbol: str) -> float:
    token = INDEX_TOKENS.get(symbol)
    return get_ltp(token) if token else 0.0


def all_ticks() -> Dict[str, dict]:
    with _tick_lock:
        return dict(_tick_store)


# ── Option chain subscription helper ──────────────────────────────────────────

def subscribe_option_chain(symbol: str, expiry: str,
                            atm_strike: int, num_strikes: int = 5):
    """
    Resolve and subscribe option chain tokens around ATM.
    Builds CE + PE for ±num_strikes around atm_strike.
    """
    step = 50 if symbol == "NIFTY" else 100

    contracts = []
    for i in range(-num_strikes, num_strikes + 1):
        strike = atm_strike + i * step
        for otype in ("CE", "PE"):
            contracts.append({
                "symbol": symbol, "expiry": expiry,
                "strike": strike, "option_type": otype,
            })

    token_map = resolve_nfo_tokens_bulk(contracts)
    nfo_tokens = list(token_map.values())

    if nfo_tokens:
        subscribe_tokens([{"exchangeType": EXCHANGE_NFO, "tokens": nfo_tokens}])
        logger.info(f"Subscribed {len(nfo_tokens)} NFO option tokens for {symbol} {expiry}")
    else:
        logger.warning("No NFO tokens resolved — check symbol/expiry format.")

    return token_map


# ── REST fallbacks ─────────────────────────────────────────────────────────────

def fetch_ltp_rest(symbol: str) -> float:
    """REST LTP — used as fallback when WS tick not yet arrived."""
    try:
        api = get_smart_api()
        if not api:
            return 0.0
        token = INDEX_TOKENS.get(symbol)
        if not token:
            return 0.0
        resp = api.ltpData("NSE", symbol, token)
        return float(resp.get("data", {}).get("ltp", 0.0))
    except Exception as exc:
        logger.error(f"fetch_ltp_rest({symbol}): {exc}")
        return 0.0


def fetch_india_vix() -> float:
    try:
        api = get_smart_api()
        if not api:
            return 0.0
        resp = api.ltpData("NSE", "INDIA VIX", "26017")
        return float(resp.get("data", {}).get("ltp", 0.0))
    except Exception as exc:
        logger.error(f"fetch_india_vix: {exc}")
        return 0.0


def fetch_option_chain_rest(symbol: str = "NIFTY") -> Optional[dict]:
    """Full option chain snapshot via REST (used for OI, IV enrichment)."""
    try:
        api = get_smart_api()
        if not api:
            return None
        return api.getOptionGreeks(name=symbol, expirytype="NEAR")
    except Exception as exc:
        logger.error(f"fetch_option_chain_rest: {exc}")
        return None
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "HBIWFBUKBUJ4XXNY6MUTE65WIM")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("angel_api")

# ── Exchange codes ─────────────────────────────────────────────────────────────
EXCHANGE_NSE = 1   # NSE Cash (indices)
EXCHANGE_NFO = 2   # NSE F&O  (options / futures)

# ── Index token map ────────────────────────────────────────────────────────────
INDEX_TOKENS: Dict[str, str] = {
    "NIFTY":     "26000",
    "BANKNIFTY": "26009",
    "FINNIFTY":  "26037",
    "INDIA VIX": "26017",
}

# ── Shared live tick store ─────────────────────────────────────────────────────
# { token_str: { ltp, open, high, low, close, volume, oi, ts } }
_tick_store: Dict[str, dict] = {}
_tick_lock  = threading.Lock()

# ── Shared connection state ────────────────────────────────────────────────────
_ws_connected  = False
_ws_error      = None       # last error string
_ws_instance   = None
_ws_thread     = None
_reconnect_flag = threading.Event()

# ── REST client (singleton) ────────────────────────────────────────────────────
_smart_api      = None
_session_lock   = threading.Lock()
_feed_token     = None
_jwt_token      = None


def get_smart_api():
    """Return authenticated SmartConnect instance; thread-safe singleton."""
    global _smart_api, _feed_token, _jwt_token
    with _session_lock:
        if _smart_api is not None:
            return _smart_api
        try:
            from SmartApi import SmartConnect          # type: ignore
            obj  = SmartConnect(api_key=ANGEL_API_KEY)
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            resp = obj.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)
            if resp and resp.get("status"):
                _smart_api  = obj
                _feed_token = resp["data"].get("feedToken", "")
                _jwt_token  = resp["data"].get("jwtToken", "")
                logger.info("Angel One REST session established.")
            else:
                logger.error(f"Session failed: {resp}")
        except Exception as exc:
            logger.error(f"get_smart_api error: {exc}")
    return _smart_api


def refresh_session():
    """Force a new session (call if JWT expires)."""
    global _smart_api
    with _session_lock:
        _smart_api = None
    return get_smart_api()


# ── NFO token resolution ───────────────────────────────────────────────────────
_nfo_token_cache: Dict[str, str] = {}


def resolve_nfo_token(symbol: str, expiry: str,
                       strike: int, option_type: str) -> Optional[str]:
    """
    Resolve NFO instrument token for an option contract.
      symbol      : 'NIFTY' | 'BANKNIFTY' | 'FINNIFTY'
      expiry      : Angel One format e.g. '29MAY2025'
      strike      : integer e.g. 24700
      option_type : 'CE' | 'PE'
    Returns token string or None.
    """
    key = f"{symbol}_{expiry}_{strike}_{option_type}"
    if key in _nfo_token_cache:
        return _nfo_token_cache[key]
    try:
        api = get_smart_api()
        if api is None:
            return None
        # Angel One searchScrip for NFO
        scrip_name = f"{symbol}{expiry}{strike}{option_type}"
        resp = api.searchScrip("NFO", scrip_name)
        if resp and resp.get("data"):
            token = resp["data"][0]["symboltoken"]
            _nfo_token_cache[key] = str(token)
            logger.info(f"Resolved NFO token {token} for {key}")
            return str(token)
    except Exception as exc:
        logger.error(f"resolve_nfo_token error for {key}: {exc}")
    return None


def resolve_nfo_tokens_bulk(contracts: List[dict]) -> Dict[str, str]:
    """
    Resolve multiple NFO tokens in one go.
    contracts = [{"symbol":"NIFTY","expiry":"29MAY2025",
                  "strike":24700,"option_type":"CE"}, ...]
    Returns { key: token }
    """
    result = {}
    for c in contracts:
        token = resolve_nfo_token(
            c["symbol"], c["expiry"], c["strike"], c["option_type"])
        if token:
            key = f"{c['symbol']}_{c['expiry']}_{c['strike']}_{c['option_type']}"
            result[key] = token
    return result


# ── WebSocket callbacks ────────────────────────────────────────────────────────

def _on_data(wsapp, message):
    """Fired on every tick — stores into _tick_store."""
    global _ws_connected
    try:
        _ws_connected = True
        token = str(message.get("token", ""))
        if not token:
            return
        with _tick_lock:
            _tick_store[token] = {
                "ltp":    message.get("last_traded_price",   0) / 100,
                "open":   message.get("open_price_of_the_day", 0) / 100,
                "high":   message.get("high_price_of_the_day", 0) / 100,
                "low":    message.get("low_price_of_the_day",  0) / 100,
                "close":  message.get("closed_price",          0) / 100,
                "volume": message.get("volume_trade_for_the_day", 0),
                "oi":     message.get("open_interest",         0),
                "bid":    message.get("best_5_buy_data",  [{}])[0].get("price", 0) / 100,
                "ask":    message.get("best_5_sell_data", [{}])[0].get("price", 0) / 100,
                "ts":     datetime.now(),
            }
    except Exception as exc:
        logger.error(f"_on_data error: {exc}")


def _on_open(wsapp):
    global _ws_connected, _ws_error
    _ws_connected = True
    _ws_error     = None
    logger.info("Angel One WebSocket CONNECTED.")


def _on_error(wsapp, error):
    global _ws_connected, _ws_error
    _ws_connected = False
    _ws_error     = str(error)
    logger.error(f"WebSocket error: {error}")
    _reconnect_flag.set()


def _on_close(wsapp):
    global _ws_connected
    _ws_connected = False
    logger.warning("WebSocket CLOSED.")
    _reconnect_flag.set()


# ── WebSocket manager ──────────────────────────────────────────────────────────

# Current token subscription list — kept globally so reconnect can re-subscribe
_current_token_list: List[dict] = []


def _build_ws_instance():
    """Create a fresh SmartWebSocketV2 instance."""
    global _feed_token
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore

    api = get_smart_api()
    if api is None:
        raise RuntimeError("REST session unavailable — cannot create WebSocket.")

    # Refresh feed token on every new WS instance
    _feed_token = api.getfeedToken()

    ws = SmartWebSocketV2(
        auth_token=_feed_token,
        api_key=ANGEL_API_KEY,
        client_code=ANGEL_CLIENT_ID,
        feed_token=_feed_token,
    )
    ws.on_open  = _on_open
    ws.on_data  = _on_data
    ws.on_error = _on_error
    ws.on_close = _on_close
    return ws


def _ws_runner(token_list: List[dict]):
    """Thread target — connects WS and handles auto-reconnect loop."""
    global _ws_instance, _ws_connected, _current_token_list
    _current_token_list = token_list
    MAX_RETRIES = 10
    retry = 0

    while retry < MAX_RETRIES:
        _reconnect_flag.clear()
        try:
            _ws_instance = _build_ws_instance()
            logger.info(f"Subscribing {sum(len(g['tokens']) for g in token_list)} tokens …")
            # Mode 3 = SNAP_QUOTE (LTP + depth + OI + Greeks)
            _ws_instance.subscribe("session_ws", 3, token_list)
            _ws_instance.connect()          # blocks until close/error
        except Exception as exc:
            logger.error(f"WS runner error (attempt {retry+1}): {exc}")

        _ws_connected = False
        retry += 1
        wait = min(2 ** retry, 60)
        logger.warning(f"Reconnecting in {wait}s … (attempt {retry}/{MAX_RETRIES})")
        _reconnect_flag.wait(timeout=wait)

    logger.error("WebSocket: max retries reached. Giving up.")


def start_websocket(token_list: List[dict]):
    """
    Launch WebSocket in a daemon thread. Safe to call once at app startup.

    token_list format:
      [
        {"exchangeType": 1, "tokens": ["26000", "26009"]},   # NSE indices
        {"exchangeType": 2, "tokens": ["token_nfo_1", …]},   # NFO options
      ]
    """
    global _ws_thread
    if _ws_thread and _ws_thread.is_alive():
        logger.info("WebSocket thread already running.")
        return

    _ws_thread = threading.Thread(
        target=_ws_runner, args=(token_list,), daemon=True, name="ws_thread")
    _ws_thread.start()
    logger.info("WebSocket thread launched.")


def subscribe_tokens(new_tokens: List[dict]):
    """
    Add more tokens to an already-running WebSocket session.
    new_tokens: same format as token_list in start_websocket.
    """
    global _ws_instance, _current_token_list
    if _ws_instance is None or not _ws_connected:
        logger.warning("WebSocket not connected — queuing tokens for next connect.")
        _current_token_list.extend(new_tokens)
        return
    try:
        _ws_instance.subscribe("session_ws", 3, new_tokens)
        _current_token_list.extend(new_tokens)
        logger.info(f"Subscribed {sum(len(g['tokens']) for g in new_tokens)} additional tokens.")
    except Exception as exc:
        logger.error(f"subscribe_tokens error: {exc}")


def ws_status() -> dict:
    """Return current WebSocket connection status for UI display."""
    return {
        "connected": _ws_connected,
        "error":     _ws_error,
        "tokens":    sum(len(g["tokens"]) for g in _current_token_list),
        "ticks":     len(_tick_store),
    }


# ── Tick accessors ─────────────────────────────────────────────────────────────

def get_tick(token: str) -> Optional[dict]:
    with _tick_lock:
        return dict(_tick_store[str(token)]) if str(token) in _tick_store else None


def get_ltp(token: str, fallback: float = 0.0) -> float:
    t = get_tick(token)
    return t["ltp"] if t else fallback


def get_index_ltp(symbol: str) -> float:
    token = INDEX_TOKENS.get(symbol)
    return get_ltp(token) if token else 0.0


def all_ticks() -> Dict[str, dict]:
    with _tick_lock:
        return dict(_tick_store)


# ── Option chain subscription helper ──────────────────────────────────────────

def subscribe_option_chain(symbol: str, expiry: str,
                            atm_strike: int, num_strikes: int = 5):
    """
    Resolve and subscribe option chain tokens around ATM.
    Builds CE + PE for ±num_strikes around atm_strike.
    """
    step = 50 if symbol == "NIFTY" else 100

    contracts = []
    for i in range(-num_strikes, num_strikes + 1):
        strike = atm_strike + i * step
        for otype in ("CE", "PE"):
            contracts.append({
                "symbol": symbol, "expiry": expiry,
                "strike": strike, "option_type": otype,
            })

    token_map = resolve_nfo_tokens_bulk(contracts)
    nfo_tokens = list(token_map.values())

    if nfo_tokens:
        subscribe_tokens([{"exchangeType": EXCHANGE_NFO, "tokens": nfo_tokens}])
        logger.info(f"Subscribed {len(nfo_tokens)} NFO option tokens for {symbol} {expiry}")
    else:
        logger.warning("No NFO tokens resolved — check symbol/expiry format.")

    return token_map


# ── REST fallbacks ─────────────────────────────────────────────────────────────

def fetch_ltp_rest(symbol: str) -> float:
    """REST LTP — used as fallback when WS tick not yet arrived."""
    try:
        api = get_smart_api()
        if not api:
            return 0.0
        token = INDEX_TOKENS.get(symbol)
        if not token:
            return 0.0
        resp = api.ltpData("NSE", symbol, token)
        return float(resp.get("data", {}).get("ltp", 0.0))
    except Exception as exc:
        logger.error(f"fetch_ltp_rest({symbol}): {exc}")
        return 0.0


def fetch_india_vix() -> float:
    try:
        api = get_smart_api()
        if not api:
            return 0.0
        resp = api.ltpData("NSE", "INDIA VIX", "26017")
        return float(resp.get("data", {}).get("ltp", 0.0))
    except Exception as exc:
        logger.error(f"fetch_india_vix: {exc}")
        return 0.0


def fetch_option_chain_rest(symbol: str = "NIFTY") -> Optional[dict]:
    """Full option chain snapshot via REST (used for OI, IV enrichment)."""
    try:
        api = get_smart_api()
        if not api:
            return None
        return api.getOptionGreeks(name=symbol, expirytype="NEAR")
    except Exception as exc:
        logger.error(f"fetch_option_chain_rest: {exc}")
        return None
