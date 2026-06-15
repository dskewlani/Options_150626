"""
engine.py — Signal Engine
Computes 8 technical indicators + Greeks + consensus recommendation score.
All inputs are plain Python floats/lists — no Streamlit dependency here.
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ── Recommendation tags ────────────────────────────────────────────────────────
STRONG_BUY  = "Strong Buy"
BUY         = "Buy"
AVOID       = "Avoid"
SELL        = "Sell"
STRONG_SELL = "Strong Sell"

TAG_COLORS = {
    STRONG_BUY:  "#1a7a4a",   # dark green
    BUY:         "#1565c0",   # blue
    AVOID:       "#757575",   # gray
    SELL:        "#e65100",   # amber
    STRONG_SELL: "#c62828",   # red
}

def score_to_tag(score: float) -> str:
    if score >= 78: return STRONG_BUY
    if score >= 62: return BUY
    if score >= 45: return AVOID
    if score >= 30: return SELL
    return STRONG_SELL


# ── Dataclasses ────────────────────────────────────────────────────────────────
@dataclass
class SignalResult:
    name:   str
    value:  float
    score:  float          # 0–100
    tag:    str
    detail: str = ""


@dataclass
class GreeksResult:
    delta:     float = 0.0
    gamma:     float = 0.0
    theta:     float = 0.0
    vega:      float = 0.0
    iv:        float = 0.0
    breakeven: float = 0.0


@dataclass
class ConsensusResult:
    score:   float
    tag:     str
    signals: List[SignalResult] = field(default_factory=list)
    reason:  str = ""


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _ema(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return [prices[-1]] * len(prices)
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return [ema[0]] * (period - 1) + ema


def compute_rsi(prices: List[float], period: int = 14) -> SignalResult:
    if len(prices) < period + 1:
        return SignalResult("RSI", 50, 50, AVOID, "Insufficient data")
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    # Score: oversold = bullish (high score), overbought = bearish (low score)
    if rsi <= 30:   score, tag = 90, STRONG_BUY
    elif rsi <= 45: score, tag = 70, BUY
    elif rsi <= 55: score, tag = 50, AVOID
    elif rsi <= 70: score, tag = 30, SELL
    else:           score, tag = 10, STRONG_SELL

    return SignalResult("RSI", round(rsi, 1), score, tag, f"RSI={rsi:.1f}")


def compute_macd(prices: List[float]) -> SignalResult:
    if len(prices) < 26:
        return SignalResult("MACD", 0, 50, AVOID, "Insufficient data")
    ema12 = _ema(prices, 12)
    ema26 = _ema(prices, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = _ema(macd_line, 9)
    hist = macd_line[-1] - signal_line[-1]

    prev_hist = macd_line[-2] - signal_line[-2]
    if hist > 0 and prev_hist <= 0:   score, tag = 90, STRONG_BUY
    elif hist > 0 and hist > prev_hist: score, tag = 70, BUY
    elif hist < 0 and prev_hist >= 0: score, tag = 10, STRONG_SELL
    elif hist < 0 and hist < prev_hist: score, tag = 30, SELL
    else:                              score, tag = 50, AVOID

    return SignalResult("MACD", round(hist, 2), score, tag,
                        f"MACD hist={hist:.2f}")


def compute_supertrend(prices: List[float], highs: List[float],
                        lows: List[float], atr_period: int = 10,
                        multiplier: float = 3.0) -> SignalResult:
    if len(prices) < atr_period + 1:
        return SignalResult("Supertrend", 0, 50, AVOID, "Insufficient data")

    # ATR
    tr_list = []
    for i in range(1, len(prices)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - prices[i-1]),
                 abs(lows[i]  - prices[i-1]))
        tr_list.append(tr)
    atr = sum(tr_list[-atr_period:]) / atr_period

    close = prices[-1]
    hl2   = (highs[-1] + lows[-1]) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    # Simplified: price vs bands
    if close > upper:   score, tag, detail = 85, STRONG_BUY,  "Above upper band"
    elif close > lower: score, tag, detail = 65, BUY,         "Inside band — bullish"
    elif close < lower: score, tag, detail = 15, STRONG_SELL, "Below lower band"
    else:               score, tag, detail = 35, SELL,        "Inside band — bearish"

    return SignalResult("Supertrend", round(close - lower, 2), score, tag, detail)


def compute_vwap(prices: List[float], volumes: List[float]) -> SignalResult:
    if not volumes or sum(volumes) == 0:
        return SignalResult("VWAP", 0, 50, AVOID, "No volume data")
    vwap = sum(p * v for p, v in zip(prices, volumes)) / sum(volumes)
    close = prices[-1]
    pct   = (close - vwap) / vwap * 100

    if pct > 0.5:    score, tag = 75, BUY
    elif pct > 0.1:  score, tag = 60, BUY
    elif pct < -0.5: score, tag = 25, SELL
    elif pct < -0.1: score, tag = 40, SELL
    else:            score, tag = 50, AVOID

    return SignalResult("VWAP", round(vwap, 2), score, tag,
                        f"Price {'+' if pct>0 else ''}{pct:.2f}% vs VWAP")


def compute_pcr(pcr: float) -> SignalResult:
    """Put-Call Ratio signal. PCR > 1 = bearish sentiment = contrarian bullish."""
    if pcr > 1.5:    score, tag, detail = 85, STRONG_BUY,  "Extreme fear → contrarian BUY"
    elif pcr > 1.2:  score, tag, detail = 70, BUY,         "Elevated PCR → bullish bias"
    elif pcr > 0.8:  score, tag, detail = 50, AVOID,       "Neutral PCR"
    elif pcr > 0.5:  score, tag, detail = 30, SELL,        "Low PCR → bearish bias"
    else:            score, tag, detail = 15, STRONG_SELL,  "Extreme greed → contrarian SELL"
    return SignalResult("PCR", round(pcr, 2), score, tag, detail)


def compute_oi_change(ce_oi_change: float, pe_oi_change: float) -> SignalResult:
    """
    OI buildup analysis.
    CE OI rising = resistance building (bearish).
    PE OI rising = support building (bullish).
    """
    ratio = pe_oi_change / (ce_oi_change + 1e-6)
    if ratio > 2.0:    score, tag, detail = 80, STRONG_BUY,  "Strong PE OI buildup"
    elif ratio > 1.2:  score, tag, detail = 65, BUY,         "PE OI rising > CE OI"
    elif ratio > 0.8:  score, tag, detail = 50, AVOID,       "Balanced OI"
    elif ratio > 0.4:  score, tag, detail = 35, SELL,        "CE OI rising > PE OI"
    else:              score, tag, detail = 15, STRONG_SELL,  "Strong CE OI buildup"
    return SignalResult("OI Change", round(ratio, 2), score, tag, detail)


def compute_iv_rank(current_iv: float, iv_52w_low: float,
                     iv_52w_high: float) -> SignalResult:
    """
    IV Rank 0–100. High IVR → sell premium. Low IVR → buy premium.
    """
    if iv_52w_high == iv_52w_low:
        ivr = 50.0
    else:
        ivr = (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100

    # High IVR is good for selling, Low IVR for buying
    if ivr >= 70:    score, tag, detail = 80, STRONG_BUY,  "High IVR → sell premium"
    elif ivr >= 50:  score, tag, detail = 65, BUY,         "Elevated IV → sell bias"
    elif ivr >= 30:  score, tag, detail = 50, AVOID,       "Average IV"
    elif ivr >= 15:  score, tag, detail = 35, SELL,        "Low IV → buy debit spreads"
    else:            score, tag, detail = 20, AVOID,       "Very low IV — wait"
    return SignalResult("IV Rank", round(ivr, 1), score, tag, detail)


def compute_bollinger(prices: List[float], period: int = 20,
                       std_mult: float = 2.0) -> SignalResult:
    if len(prices) < period:
        return SignalResult("Bollinger", 0, 50, AVOID, "Insufficient data")
    window = prices[-period:]
    mid    = sum(window) / period
    std    = (sum((p - mid)**2 for p in window) / period) ** 0.5
    upper  = mid + std_mult * std
    lower  = mid - std_mult * std
    close  = prices[-1]
    bw     = (upper - lower) / mid * 100   # bandwidth %

    if close <= lower:       score, tag, detail = 85, STRONG_BUY,  f"Price at lower band"
    elif close <= mid:       score, tag, detail = 60, BUY,         f"Below midline"
    elif close >= upper:     score, tag, detail = 15, STRONG_SELL, f"Price at upper band"
    else:                    score, tag, detail = 40, SELL,        f"Above midline"

    return SignalResult("Bollinger", round(close, 2), score, tag,
                        f"{detail} | BW={bw:.1f}%")


# ── Greeks (Black-Scholes) ─────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return (1 + math.erf(x / math.sqrt(2))) / 2


def compute_greeks(spot: float, strike: float, expiry_days: int,
                    iv: float, r: float = 0.065,
                    option_type: str = "CE") -> GreeksResult:
    """Black-Scholes Greeks."""
    try:
        T = max(expiry_days / 365, 1e-6)
        if iv <= 0 or spot <= 0 or strike <= 0:
            return GreeksResult()
        d1 = (math.log(spot / strike) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        nd1  = _norm_cdf(d1)
        nd2  = _norm_cdf(d2)
        nd1_ = _norm_cdf(-d1)
        nd2_ = _norm_cdf(-d2)

        pdf_d1 = math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)

        if option_type == "CE":
            delta = nd1
            price = spot * nd1 - strike * math.exp(-r * T) * nd2
            beven = strike + price
        else:
            delta = nd1 - 1
            price = strike * math.exp(-r * T) * nd2_ - spot * nd1_
            beven = strike - price

        gamma = pdf_d1 / (spot * iv * math.sqrt(T))
        vega  = spot * pdf_d1 * math.sqrt(T) / 100
        theta = (-(spot * pdf_d1 * iv) / (2 * math.sqrt(T))
                 - r * strike * math.exp(-r * T) * (nd2 if option_type == "CE" else nd2_)) / 365

        return GreeksResult(
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta=round(theta, 2),
            vega=round(vega, 2),
            iv=round(iv * 100, 2),
            breakeven=round(beven, 2),
        )
    except Exception:
        return GreeksResult()


# ── Consensus ──────────────────────────────────────────────────────────────────

def compute_consensus(
    prices: List[float],
    highs: List[float],
    lows: List[float],
    volumes: List[float],
    pcr: float,
    ce_oi_change: float,
    pe_oi_change: float,
    current_iv: float,
    iv_52w_low: float = 10.0,
    iv_52w_high: float = 40.0,
) -> ConsensusResult:
    signals = [
        compute_rsi(prices),
        compute_macd(prices),
        compute_supertrend(prices, highs, lows),
        compute_vwap(prices, volumes),
        compute_pcr(pcr),
        compute_oi_change(ce_oi_change, pe_oi_change),
        compute_iv_rank(current_iv, iv_52w_low, iv_52w_high),
        compute_bollinger(prices),
    ]

    avg_score = sum(s.score for s in signals) / len(signals)
    tag       = score_to_tag(avg_score)

    # Human-readable reason
    top = max(signals, key=lambda s: abs(s.score - 50))
    reason_map = {
        STRONG_BUY:  f"Dominant bullish signal from {top.name} ({top.detail})",
        BUY:         f"Moderate bullish bias; {top.name} leading.",
        AVOID:       f"Mixed signals — no clear edge. Wait for confirmation.",
        SELL:        f"Moderate bearish bias; {top.name} leading.",
        STRONG_SELL: f"Strong bearish signal from {top.name} ({top.detail})",
    }

    return ConsensusResult(
        score=round(avg_score, 1),
        tag=tag,
        signals=signals,
        reason=reason_map.get(tag, ""),
    )


# ── Max Pain ───────────────────────────────────────────────────────────────────

def compute_max_pain(strikes: List[float], ce_oi: List[float],
                      pe_oi: List[float]) -> float:
    """Return the strike with minimum total pain for option sellers."""
    min_pain  = float("inf")
    max_pain_strike = strikes[0]
    for expiry_strike in strikes:
        pain = sum(max(expiry_strike - s, 0) * oi
                   for s, oi in zip(strikes, ce_oi))
        pain += sum(max(s - expiry_strike, 0) * oi
                    for s, oi in zip(strikes, pe_oi))
        if pain < min_pain:
            min_pain        = pain
            max_pain_strike = expiry_strike
    return max_pain_strike


# ── Option chain recommendation per strike ─────────────────────────────────────

def option_chain_tag(spot: float, strike: float,
                      oi_change: float, iv: float,
                      option_type: str = "CE") -> str:
    """
    Quick tag for each row in the option chain table.
    Combines moneyness + OI change + IV.
    """
    itm = (strike < spot) if option_type == "CE" else (strike > spot)
    deep_itm = abs(spot - strike) / spot > 0.015

    score = 50.0
    if itm:      score += 15
    if deep_itm: score += 10
    if oi_change > 0:
        score += min(oi_change / 1e6, 15)   # OI adds up to 15 pts
    if iv > 20:  score -= 5                  # High IV penalises buyers slightly

    if option_type == "PE":
        score = 100 - score   # Flip for PE perspective

    return score_to_tag(score)
