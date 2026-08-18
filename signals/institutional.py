"""
Institutional buying signal.

Detects unusual volume and options-like accumulation patterns using
free data (yfinance). Score 0-100.

High score = big money quietly accumulating:
  - Volume spike vs 20-day average
  - Price closing near daily high (distribution vs accumulation)
  - Consistent up-volume vs down-volume ratio (OBV trend)
"""

import numpy as np
import yfinance as yf
from config import VOLUME_LOOKBACK_DAYS


def score(ticker: str) -> float:
    try:
        df = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if df is None or len(df) < VOLUME_LOOKBACK_DAYS + 5:
            return 0.0

        close = df["Close"].squeeze()
        high = df["High"].squeeze()
        low = df["Low"].squeeze()
        volume = df["Volume"].squeeze()

        # 1. Volume spike score (0-40 pts)
        avg_vol = volume.iloc[-VOLUME_LOOKBACK_DAYS - 1 : -1].mean()
        recent_vol = volume.iloc[-5:].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
        vol_score = min(40, (vol_ratio - 1.0) * 25)  # 1.6x avg vol → 15pts, 2.6x → 40pts
        vol_score = max(0, vol_score)

        # 2. Close-to-high ratio over last 5 days (0-30 pts): accumulation = closing near highs
        daily_range = high - low
        close_position = (close - low) / daily_range.where(daily_range > 0, other=np.nan)
        avg_close_pos = close_position.iloc[-5:].mean()
        cth_score = avg_close_pos * 30  # 1.0 (close at high) → 30pts

        # 3. OBV trend (0-30 pts): rising OBV = buying pressure
        obv = (np.sign(close.diff()) * volume).fillna(0).cumsum()
        obv_recent = obv.iloc[-5:].mean()
        obv_prev = obv.iloc[-20:-5].mean()
        obv_change = (obv_recent - obv_prev) / (abs(obv_prev) + 1)
        obv_score = min(30, max(0, obv_change * 60))

        total = vol_score + cth_score + obv_score
        return round(min(100.0, max(0.0, total)), 1)

    except Exception:
        return 0.0
