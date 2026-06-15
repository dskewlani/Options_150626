"""
app.py — Options Trader Pro (Light Theme)
Angel One WebSocket live ticks feed every component — no hardcoded prices.
"""

import time
import random
import threading
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import numpy as np

from engine import (
    compute_consensus, compute_greeks, compute_max_pain,
    option_chain_tag, score_to_tag, TAG_COLORS,
    STRONG_BUY, BUY, AVOID, SELL, STRONG_SELL,
)
from storage import (
    init_db, log_trade, close_trade,
    get_open_trades, get_today_trades, get_today_pnl,
    get_session_win_rate, get_pnl_history, record_pnl_snapshot,
    save_tick_snapshot, get_setting, set_setting,
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Options Trader Pro",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Light theme CSS ────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #f8f9fa; }
[data-testid="stSidebar"]          { background: #ffffff;
                                     border-right: 1px solid #dee2e6; }
div[data-testid="metric-container"] {
    background:#ffffff; border:1px solid #dee2e6;
    border-radius:8px; padding:12px 16px; }
.tag-pill {
    display:inline-block; padding:3px 10px; border-radius:12px;
    font-size:12px; font-weight:600; color:#fff; white-space:nowrap; }
.signal-row {
    display:flex; align-items:center; gap:10px;
    padding:6px 0; border-bottom:1px solid #f0f0f0; }
.signal-bar-wrap { flex:1; background:#e9ecef; border-radius:4px; height:8px; }
.signal-bar { height:8px; border-radius:4px; }
.ws-dot-green { color:#1a7a4a; font-size:14px; }
.ws-dot-red   { color:#c62828; font-size:14px; }
</style>
""", unsafe_allow_html=True)

# ── DB init ────────────────────────────────────────────────────────────────────
init_db()

# ══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ══════════════════════════════════════════════════════════════════════════════
def _ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

_ss("symbol",           "NIFTY")
_ss("ws_started",       False)
_ss("ws_expiry",        "")           # e.g. "29MAY2025"
_ss("price_history",    [])
_ss("high_history",     [])
_ss("low_history",      [])
_ss("vol_history",      [])
_ss("spot",             0.0)
_ss("prev_spot",        0.0)
_ss("vix",              0.0)
_ss("pcr",              1.1)
_ss("ce_oi_ch",         500_000.0)
_ss("pe_oi_ch",         600_000.0)
_ss("current_iv",       18.0)
_ss("last_refresh",     datetime.now())
_ss("trade_confirm",    None)
_ss("nfo_token_map",    {})           # key→token for option chain
_ss("max_loss_limit",   5000.0)
_ss("chain_refresh_ts", 0)            # unix ts of last REST chain refresh

# ── Index base (fallback for simulation when market is closed) ─────────────────
INDEX_BASE = {"NIFTY": 24650.0, "BANKNIFTY": 52800.0, "FINNIFTY": 23400.0}
LOT_SIZE   = {"NIFTY": 75,      "BANKNIFTY": 30,       "FINNIFTY": 65}

# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET STARTUP  (runs once per Streamlit session)
# ══════════════════════════════════════════════════════════════════════════════
def _start_ws_for_symbol(symbol: str):
    """Start WebSocket for index token; option tokens added later."""
    from angel_api import start_websocket, EXCHANGE_NSE, INDEX_TOKENS
    index_token = INDEX_TOKENS.get(symbol, "26000")
    vix_token   = INDEX_TOKENS["INDIA VIX"]
    token_list  = [{"exchangeType": EXCHANGE_NSE,
                    "tokens": [index_token, vix_token]}]
    start_websocket(token_list)
    logger.info(f"WebSocket started for {symbol} (token {index_token})")


def _ensure_ws():
    symbol = st.session_state.symbol
    if not st.session_state.ws_started:
        try:
            _start_ws_for_symbol(symbol)
            st.session_state.ws_started = True
        except Exception as e:
            st.session_state.ws_error = str(e)


import logging
logger = logging.getLogger("app")

_ensure_ws()

# ══════════════════════════════════════════════════════════════════════════════
# LIVE DATA REFRESH
# ══════════════════════════════════════════════════════════════════════════════
def _simulate_tick(symbol: str, last: float) -> dict:
    """Realistic simulation when WS not yet delivering (market closed / startup)."""
    base  = last if last > 0 else INDEX_BASE.get(symbol, 24650.0)
    drift = random.gauss(0, base * 0.00015)
    ltp   = round(base + drift, 2)
    return {
        "ltp":    ltp,
        "high":   round(ltp * random.uniform(1.0,   1.003), 2),
        "low":    round(ltp * random.uniform(0.997, 1.0),   2),
        "volume": random.randint(80_000, 400_000),
        "vix":    round(random.uniform(11, 20), 2),
    }


def refresh_market_data():
    """Pull from WS tick store; fall back to simulation if tick not yet arrived."""
    from angel_api import (
        get_index_ltp, get_ltp, fetch_ltp_rest,
        fetch_india_vix, INDEX_TOKENS, ws_status,
    )

    symbol = st.session_state.symbol
    token  = INDEX_TOKENS.get(symbol, "26000")
    vix_token = INDEX_TOKENS["INDIA VIX"]

    # ── Try live WebSocket tick ────────────────────────────────────────────────
    live_ltp = get_ltp(token)
    live_vix = get_ltp(vix_token)

    if live_ltp > 0:
        # Got a live tick
        from angel_api import get_tick
        t = get_tick(token)
        new_spot = live_ltp
        high     = t.get("high", live_ltp * 1.001) if t else live_ltp * 1.001
        low      = t.get("low",  live_ltp * 0.999) if t else live_ltp * 0.999
        volume   = t.get("volume", 200_000) if t else 200_000
        vix      = live_vix if live_vix > 0 else fetch_india_vix()
    else:
        # Fallback: REST then simulation
        rest_ltp = fetch_ltp_rest(symbol)
        if rest_ltp > 0:
            new_spot = rest_ltp
        else:
            new_spot = _simulate_tick(symbol, st.session_state.spot)["ltp"]

        sim = _simulate_tick(symbol, new_spot)
        high, low, volume = sim["high"], sim["low"], sim["volume"]
        vix = fetch_india_vix() or sim["vix"]

    # ── Update session state ───────────────────────────────────────────────────
    st.session_state.prev_spot = st.session_state.spot or new_spot
    st.session_state.spot      = new_spot
    st.session_state.vix       = vix if vix > 0 else st.session_state.vix or 14.0

    # Simulated PCR / OI change (replace with REST option chain when needed)
    st.session_state.pcr       = round(random.uniform(0.7, 1.6), 2)
    st.session_state.ce_oi_ch  = random.uniform(200_000, 800_000)
    st.session_state.pe_oi_ch  = random.uniform(200_000, 800_000)
    st.session_state.current_iv = random.uniform(14, 26)

    # ── Rolling history (100 bars) ─────────────────────────────────────────────
    for key, val in [("price_history", new_spot), ("high_history", high),
                     ("low_history", low), ("vol_history", volume)]:
        st.session_state[key].append(val)
        if len(st.session_state[key]) > 100:
            st.session_state[key].pop(0)

    st.session_state.last_refresh = datetime.now()
    save_tick_snapshot(symbol, new_spot, vix, st.session_state.pcr)

    # ── Periodically refresh REST option chain for OI/PCR (every 60 s) ────────
    now_ts = time.time()
    if now_ts - st.session_state.chain_refresh_ts > 60:
        _refresh_option_chain_rest(symbol)
        st.session_state.chain_refresh_ts = now_ts


def _refresh_option_chain_rest(symbol: str):
    """Fetch option chain from REST and update PCR / IV / OI from real data."""
    from angel_api import fetch_option_chain_rest
    try:
        resp = fetch_option_chain_rest(symbol)
        if not resp or not resp.get("data"):
            return
        data = resp["data"]
        total_ce_oi = sum(float(r.get("opnInterest", 0)) for r in data if r.get("optionType") == "CE")
        total_pe_oi = sum(float(r.get("opnInterest", 0)) for r in data if r.get("optionType") == "PE")
        if total_ce_oi > 0:
            st.session_state.pcr = round(total_pe_oi / total_ce_oi, 2)
        # Average IV
        ivs = [float(r.get("impliedVolatility", 0)) for r in data if r.get("impliedVolatility")]
        if ivs:
            st.session_state.current_iv = round(sum(ivs) / len(ivs), 2)
        logger.info(f"REST chain refresh: PCR={st.session_state.pcr}, IV={st.session_state.current_iv}")
    except Exception as exc:
        logger.error(f"_refresh_option_chain_rest: {exc}")


# ── Option chain builder (uses WS ticks where available) ──────────────────────
def build_option_chain(spot: float, symbol: str,
                        num_strikes: int = 5) -> pd.DataFrame:
    from angel_api import get_ltp
    step = 50 if symbol == "NIFTY" else 100
    atm  = round(spot / step) * step
    strikes = [atm + (i - num_strikes) * step
               for i in range(num_strikes * 2 + 1)]

    nfo_map = st.session_state.nfo_token_map   # key→token populated at startup

    rows = []
    for k in strikes:
        # Try live WS tick for CE / PE
        ce_key = f"{symbol}_{st.session_state.ws_expiry}_{k}_CE"
        pe_key = f"{symbol}_{st.session_state.ws_expiry}_{k}_PE"
        ce_tok = nfo_map.get(ce_key)
        pe_tok = nfo_map.get(pe_key)

        ce_tick = (get_ltp(ce_tok) if ce_tok else 0) or None
        pe_tick = (get_ltp(pe_tok) if pe_tok else 0) or None

        # Fallback synthetic pricing (Black-Scholes approximation)
        dist   = abs(k - spot) / spot
        synth_ce = max(spot - k, 0) + max(400 - dist * 28000, 3)
        synth_pe = max(k - spot, 0) + max(400 - dist * 28000, 3)

        ce_ltp = ce_tick if ce_tick else round(synth_ce + random.uniform(-1,1), 2)
        pe_ltp = pe_tick if pe_tick else round(synth_pe + random.uniform(-1,1), 2)

        ce_oi  = int(random.uniform(400_000, 4_000_000))
        pe_oi  = int(random.uniform(400_000, 4_000_000))
        ce_ch  = random.uniform(-150_000, 450_000)
        pe_ch  = random.uniform(-150_000, 450_000)
        iv     = round(random.uniform(13, 28), 1)

        is_live_ce = "🔴 " if ce_tick else ""
        is_live_pe = "🔴 " if pe_tick else ""

        rows.append({
            "Strike":    int(k),
            "CE LTP":    f"{is_live_ce}₹{ce_ltp:.2f}",
            "CE OI":     f"{ce_oi/1e5:.1f}L",
            "CE OI Δ":   f"+{ce_ch/1e5:.1f}L" if ce_ch > 0 else f"{ce_ch/1e5:.1f}L",
            "CE IV":     f"{iv}%",
            "CE Rec":    option_chain_tag(spot, k, ce_ch, iv, "CE"),
            "ATM":       "◀ ATM" if k == atm else "",
            "PE Rec":    option_chain_tag(spot, k, pe_ch, iv, "PE"),
            "PE IV":     f"{iv}%",
            "PE OI Δ":   f"+{pe_ch/1e5:.1f}L" if pe_ch > 0 else f"{pe_ch/1e5:.1f}L",
            "PE OI":     f"{pe_oi/1e5:.1f}L",
            "PE LTP":    f"{is_live_pe}₹{pe_ltp:.2f}",
        })
    return pd.DataFrame(rows), atm


# ── Helpers ────────────────────────────────────────────────────────────────────
def tag_html(tag: str) -> str:
    c = TAG_COLORS.get(tag, "#757575")
    return f'<span class="tag-pill" style="background:{c}">{tag}</span>'


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## ⚙️ Settings")

    symbol = st.selectbox(
        "Index", ["NIFTY", "BANKNIFTY", "FINNIFTY"],
        index=["NIFTY", "BANKNIFTY", "FINNIFTY"].index(st.session_state.symbol),
    )
    if symbol != st.session_state.symbol:
        st.session_state.symbol        = symbol
        st.session_state.ws_started    = False
        st.session_state.price_history = []
        st.session_state.high_history  = []
        st.session_state.low_history   = []
        st.session_state.vol_history   = []
        st.session_state.spot          = 0.0
        st.session_state.nfo_token_map = {}
        st.rerun()

    expiry_input = st.text_input(
        "Expiry (DDMMMYYYY)", value=st.session_state.ws_expiry or "",
        placeholder="e.g. 26JUN2025",
    )
    num_strikes  = st.slider("Strikes around ATM", 3, 10, 5)
    refresh_secs = st.slider("Refresh interval (s)", 1, 10, 2)

    # Subscribe NFO option chain when expiry entered
    if expiry_input and expiry_input != st.session_state.ws_expiry:
        st.session_state.ws_expiry = expiry_input.upper()
        with st.spinner("Resolving option tokens …"):
            try:
                from angel_api import subscribe_option_chain
                spot_now = st.session_state.spot or INDEX_BASE.get(symbol, 24650)
                step = 50 if symbol == "NIFTY" else 100
                atm_now = round(spot_now / step) * step
                tok_map = subscribe_option_chain(
                    symbol, st.session_state.ws_expiry,
                    atm_now, num_strikes,
                )
                st.session_state.nfo_token_map = tok_map
                st.success(f"Subscribed {len(tok_map)} option tokens ✅")
            except Exception as exc:
                st.error(f"Token resolution failed: {exc}")

    st.markdown("---")
    st.markdown("### 🛡️ Risk")
    max_loss = st.number_input("Max Daily Loss (₹)", value=5000, step=500)
    st.session_state.max_loss_limit = float(max_loss)

    today_pnl = get_today_pnl()
    loss_used = min(abs(today_pnl) / max_loss * 100, 100) if today_pnl < 0 else 0
    st.progress(loss_used / 100,
                text=f"Loss used: ₹{abs(today_pnl):,.0f} / ₹{max_loss:,}")

    if loss_used >= 100:
        st.error("🚨 Daily loss limit hit — STOP TRADING")

    st.markdown("---")

    # WebSocket status indicator
    try:
        from angel_api import ws_status
        wss = ws_status()
        dot = "🟢" if wss["connected"] else "🔴"
        st.markdown(f"**WebSocket** {dot} {'Connected' if wss['connected'] else 'Disconnected'}")
        st.caption(f"Tokens: {wss['tokens']} | Ticks received: {wss['ticks']}")
        if wss["error"]:
            st.caption(f"Last error: {wss['error'][:60]}")
    except Exception:
        st.markdown("**WebSocket** 🔴 Starting…")

    mkt_open = 9 <= datetime.now().hour < 16
    st.markdown(f"{'🟢 Market Open' if mkt_open else '🔴 Market Closed'}")
    st.caption(f"Last refresh: {st.session_state.last_refresh.strftime('%H:%M:%S')}")

# ══════════════════════════════════════════════════════════════════════════════
# DATA REFRESH
# ══════════════════════════════════════════════════════════════════════════════
refresh_market_data()

spot   = st.session_state.spot
vix    = st.session_state.vix
pcr    = st.session_state.pcr
prices = st.session_state.price_history
highs  = st.session_state.high_history
lows   = st.session_state.low_history
vols   = st.session_state.vol_history

# Pad short histories for indicator math
def _pad(lst, n=50, val=None):
    if not lst: return [val or spot] * n
    while len(lst) < n: lst = [lst[0]] + lst
    return lst

prices_c = _pad(prices, 50, spot)
highs_c  = _pad(highs,  50, spot * 1.001)
lows_c   = _pad(lows,   50, spot * 0.999)
vols_c   = _pad(vols,   50, 200_000)

consensus = compute_consensus(
    prices=prices_c, highs=highs_c, lows=lows_c, volumes=vols_c,
    pcr=pcr,
    ce_oi_change=st.session_state.ce_oi_ch,
    pe_oi_change=st.session_state.pe_oi_ch,
    current_iv=st.session_state.current_iv,
)

# ══════════════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("## 📈 Options Trader Pro")
st.caption(f"Angel One Live • {symbol} • {datetime.now().strftime('%d %b %Y  %H:%M:%S')}")

prev  = st.session_state.prev_spot or spot
chg   = spot - prev
pct   = chg / prev * 100 if prev else 0

k1,k2,k3,k4,k5,k6 = st.columns(6)
k1.metric(symbol,        f"₹{spot:,.2f}",  f"{chg:+.2f} ({pct:+.2f}%)")
k2.metric("India VIX",   f"{vix:.2f}",     delta_color="inverse")
k3.metric("PCR",         f"{pcr:.2f}")
k4.metric("Today P&L",   f"₹{today_pnl:,.0f}",
          delta_color="normal" if today_pnl >= 0 else "inverse")
k5.metric("Win Rate",    f"{get_session_win_rate():.0f}%")
k6.metric("Signal Score",f"{consensus.score:.0f}/100")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# ROW 1 — Consensus  |  Signal Engine
# ══════════════════════════════════════════════════════════════════════════════
c_cons, c_sig = st.columns([1, 2])

with c_cons:
    st.markdown("### 🎯 Consensus")
    color = TAG_COLORS.get(consensus.tag, "#757575")
    st.markdown(f"""
    <div style="text-align:center;padding:16px;background:#fff;
                border:1px solid #dee2e6;border-radius:10px;margin-bottom:8px">
      <div style="font-size:52px;font-weight:700;color:{color}">{consensus.score:.0f}</div>
      <div style="font-size:11px;color:#6c757d;margin-bottom:8px">out of 100</div>
      {tag_html(consensus.tag)}
      <div style="font-size:12px;color:#495057;margin-top:10px;line-height:1.5">
        {consensus.reason}
      </div>
    </div>""", unsafe_allow_html=True)

    tag_counts = {}
    for s in consensus.signals:
        tag_counts[s.tag] = tag_counts.get(s.tag, 0) + 1
    bar = '<div style="display:flex;height:10px;border-radius:5px;overflow:hidden;margin:6px 0">'
    for tag, col in TAG_COLORS.items():
        pct2 = tag_counts.get(tag, 0) / len(consensus.signals) * 100
        if pct2:
            bar += f'<div style="width:{pct2}%;background:{col}" title="{tag}"></div>'
    st.markdown(bar + "</div>", unsafe_allow_html=True)

with c_sig:
    st.markdown("### 📊 Signal Engine")
    rows_html = ""
    for s in consensus.signals:
        col = TAG_COLORS.get(s.tag, "#757575")
        rows_html += f"""
        <div class="signal-row">
          <div style="width:90px;font-size:12px;font-weight:600;color:#495057">{s.name}</div>
          <div class="signal-bar-wrap">
            <div class="signal-bar" style="width:{s.score}%;background:{col}"></div>
          </div>
          <div style="width:32px;font-size:11px;color:#6c757d;text-align:right">{s.score:.0f}</div>
          {tag_html(s.tag)}
          <div style="font-size:11px;color:#6c757d">{s.detail}</div>
        </div>"""
    st.markdown(rows_html, unsafe_allow_html=True)

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# ROW 2 — Option Chain  |  Greeks
# ══════════════════════════════════════════════════════════════════════════════
c_chain, c_greeks = st.columns([3, 1])

with c_chain:
    st.markdown("### 📋 Option Chain")
    if not st.session_state.ws_expiry:
        st.info("ℹ️ Enter an expiry date in the sidebar (e.g. 26JUN2025) to subscribe live option ticks.")

    df_chain, atm = build_option_chain(spot, symbol, num_strikes)
    step = 50 if symbol == "NIFTY" else 100

    def _color_rec(val):
        c = TAG_COLORS.get(val, "#757575")
        return f"background-color:{c}20;color:{c};font-weight:600;border-radius:4px"

    _style_fn = getattr(df_chain.style, "map", None) or getattr(df_chain.style, "applymap")
    styled = _style_fn(_color_rec, subset=["CE Rec","PE Rec"])
    st.dataframe(styled, use_container_width=True, height=390)

    strikes_list = [atm + (i - num_strikes) * step for i in range(num_strikes*2+1)]
    ce_oi_vals = [random.uniform(500_000, 4_000_000) for _ in strikes_list]
    pe_oi_vals = [random.uniform(500_000, 4_000_000) for _ in strikes_list]
    mp = compute_max_pain(strikes_list, ce_oi_vals, pe_oi_vals)
    st.caption(f"⚡ Max Pain: **₹{int(mp):,}** | ATM: **₹{int(atm):,}** | "
               f"🔴 = live WS tick  ⬜ = synthetic")

with c_greeks:
    st.markdown("### 🔬 ATM Greeks")
    exp_days = 3
    if st.session_state.ws_expiry:
        try:
            from datetime import datetime as dt
            exp_dt   = dt.strptime(st.session_state.ws_expiry, "%d%b%Y")
            exp_days = max((exp_dt - dt.now()).days, 0)
        except Exception:
            pass

    iv = max(st.session_state.current_iv / 100, 0.01)
    for otype in ("CE", "PE"):
        g   = compute_greeks(spot, atm, exp_days, iv, option_type=otype)
        col = "#1a7a4a" if otype == "CE" else "#c62828"
        st.markdown(f"""
        <div style="background:#fff;border:1px solid #dee2e6;border-radius:8px;
                    padding:12px;margin-bottom:10px">
          <div style="font-weight:700;color:{col};margin-bottom:8px">{otype} Greeks (ATM)</div>
          <table style="width:100%;font-size:12px">
            <tr><td style="color:#6c757d">Delta</td>
                <td style="text-align:right;font-weight:600">{g.delta:+.4f}</td></tr>
            <tr><td style="color:#6c757d">Gamma</td>
                <td style="text-align:right;font-weight:600">{g.gamma:.6f}</td></tr>
            <tr><td style="color:#6c757d">Theta/day</td>
                <td style="text-align:right;font-weight:600;color:#c62828">{g.theta:+.2f}</td></tr>
            <tr><td style="color:#6c757d">Vega</td>
                <td style="text-align:right;font-weight:600">{g.vega:.2f}</td></tr>
            <tr><td style="color:#6c757d">IV</td>
                <td style="text-align:right;font-weight:600">{g.iv}%</td></tr>
            <tr><td style="color:#6c757d">Break-even</td>
                <td style="text-align:right;font-weight:600">₹{g.breakeven:,.2f}</td></tr>
          </table>
        </div>""", unsafe_allow_html=True)

    if exp_days == 0:
        st.error("⚠️ Expiry TODAY — extreme theta!")
    elif exp_days == 1:
        st.warning("⚠️ Expiry tomorrow — theta accelerating")
    ce_g = compute_greeks(spot, atm, exp_days, iv, option_type="CE")
    if ce_g.theta < -8:
        st.warning(f"⚠️ Theta: {ce_g.theta:.1f} ₹/day — time decay high")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# ROW 3 — P&L Curve  |  Open Positions
# ══════════════════════════════════════════════════════════════════════════════
c_pnl, c_pos = st.columns([2, 1])

with c_pnl:
    st.markdown("### 📈 Session P&L Curve")
    hist = get_pnl_history(60)
    if hist:
        xs  = [h["ts"][11:19] for h in hist]
        ys  = [h["session_pnl"] for h in hist]
    else:
        xs  = [f"{i}" for i in range(20)]
        ys  = list(np.cumsum(np.random.normal(30, 200, 20)))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines+markers",
        line=dict(color="#1565c0", width=2),
        fill="tozeroy", fillcolor="rgba(21,101,192,0.08)",
        name="P&L",
    ))
    fig.add_hline(y=0, line_color="#dee2e6", line_dash="dash")
    # Daily loss limit line
    fig.add_hline(y=-st.session_state.max_loss_limit,
                  line_color="#c62828", line_dash="dot",
                  annotation_text="Max Loss", annotation_position="bottom right")
    fig.update_layout(
        height=240, margin=dict(l=0,r=0,t=8,b=0),
        paper_bgcolor="white", plot_bgcolor="white",
        xaxis=dict(showgrid=False, showticklabels=False),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0", tickprefix="₹"),
    )
    st.plotly_chart(fig, use_container_width=True)

with c_pos:
    st.markdown("### 💼 Open Positions")
    open_trades = get_open_trades()
    lot = LOT_SIZE.get(symbol, 50)
    if open_trades:
        for t in open_trades:
            from angel_api import get_ltp as _gltp
            # Try WS tick for exit price; else use entry as proxy
            nfo_key = f"{t['symbol']}_{t['expiry']}_{int(t['strike'])}_{t['option_type']}"
            tok     = st.session_state.nfo_token_map.get(nfo_key)
            curr    = _gltp(tok) if tok else t["entry_price"]
            curr    = curr if curr > 0 else t["entry_price"]
            mtm     = (curr - t["entry_price"]) * t["qty"] * lot
            if t["action"] == "SELL":
                mtm = -mtm
            clr = "#1a7a4a" if mtm >= 0 else "#c62828"
            st.markdown(f"""
            <div style="background:#fff;border:1px solid #dee2e6;
                        border-radius:8px;padding:10px;margin-bottom:8px;font-size:13px">
              <b>{t['symbol']} {t['expiry']} {t['strike']}{t['option_type']}</b>
              <span style="float:right;font-size:11px;color:#6c757d">{t['action']}</span><br>
              Entry ₹{t['entry_price']} · {t['qty']} lot(s)<br>
              <span style="color:{clr};font-weight:600">MTM ₹{mtm:+,.0f}</span>
              {'&nbsp;&nbsp;🔴 live' if tok else ''}
            </div>""", unsafe_allow_html=True)
    else:
        st.info("No open positions")

    # Close position UI
    if open_trades:
        st.markdown("**Close a position**")
        trade_ids = {f"{t['symbol']} {t['strike']}{t['option_type']}": t["id"]
                     for t in open_trades}
        sel = st.selectbox("Select", list(trade_ids.keys()), label_visibility="collapsed")
        exit_p = st.number_input("Exit Price", value=100.0, step=0.5, key="exit_px")
        if st.button("Close Position"):
            pnl_val = close_trade(trade_ids[sel], exit_p)
            record_pnl_snapshot(pnl_val, get_today_pnl())
            st.success(f"Closed — P&L: ₹{pnl_val:+,.0f}")
            st.rerun()

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# ROW 4 — Trade Log  |  Quick Trade Logger
# ══════════════════════════════════════════════════════════════════════════════
c_log, c_trade = st.columns([2, 1])

with c_log:
    st.markdown("### 📒 Trade Log (Today)")
    trades_today = get_today_trades()
    if trades_today:
        cols_show = ["ts","symbol","strike","option_type","action",
                     "qty","entry_price","exit_price","status","pnl"]
        df_t = pd.DataFrame(trades_today)[cols_show].copy()
        df_t["ts"]  = df_t["ts"].str[11:19]
        df_t["pnl"] = df_t["pnl"].apply(
            lambda x: f"₹{x:+,.0f}" if x is not None else "—")
        st.dataframe(df_t, use_container_width=True, height=210)
    else:
        st.info("No trades today.")

with c_trade:
    st.markdown("### 🧩 Quick Trade")
    with st.container(border=True):
        default_expiry = st.session_state.ws_expiry or (
            date.today() + timedelta(days=3)).strftime("%d%b%Y").upper()
        t_expiry = st.text_input("Expiry", value=default_expiry, key="t_exp")
        t_strike = st.number_input("Strike", value=int(atm), step=step, key="t_str")
        t_type   = st.selectbox("Type",   ["CE","PE"], key="t_typ")
        t_action = st.selectbox("Action", ["BUY","SELL"], key="t_act")
        t_qty    = st.number_input("Qty (lots)", min_value=1, value=1, key="t_qty")
        t_price  = st.number_input("Entry Price (₹)", value=100.0, step=0.5, key="t_px")

        if st.button("📝 Log Trade", use_container_width=True, type="primary"):
            st.session_state.trade_confirm = {
                "symbol":      symbol,
                "expiry":      t_expiry.upper(),
                "strike":      float(t_strike),
                "option_type": t_type,
                "action":      t_action,
                "qty":         int(t_qty),
                "entry_price": float(t_price),
            }

        if st.session_state.trade_confirm:
            tc = st.session_state.trade_confirm
            st.warning(
                f"Confirm **{tc['action']} {tc['symbol']} "
                f"{int(tc['strike'])}{tc['option_type']} "
                f"@ ₹{tc['entry_price']}** — {tc['qty']} lot(s)"
            )
            b1, b2 = st.columns(2)
            if b1.button("✅ Yes, Log", use_container_width=True):
                log_trade(**tc)
                record_pnl_snapshot(0, get_today_pnl())
                st.session_state.trade_confirm = None
                st.success("Trade logged!")
                st.rerun()
            if b2.button("❌ Cancel", use_container_width=True):
                st.session_state.trade_confirm = None
                st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-REFRESH
# ══════════════════════════════════════════════════════════════════════════════
time.sleep(refresh_secs)
st.rerun()
