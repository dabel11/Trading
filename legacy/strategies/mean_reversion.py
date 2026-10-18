"""
평균회귀 전략: 과매도 구간에서 반등 매수.

지표:
  - RSI (14) < 35 → 매수 신호
  - 볼린저 밴드 하단 이탈
  - 20일 이평 대비 괴리율
"""
import numpy as np
import yfinance as yf
from .base import BaseStrategy, StrategyInfo


class MeanReversionStrategy(BaseStrategy):
    info = StrategyInfo(
        name="mean_reversion",
        display_name="🔄 평균회귀",
        description="과매도 구간에서 반등을 노리는 역추세 전략.",
        best_for="횡보장·약세장",
        params={"rsi_buy": 35, "rsi_sell": 65, "bb_period": 20},
    )

    def score(self, ticker: str) -> float:
        try:
            df = yf.download(ticker, period="6mo", interval="1d",
                             auto_adjust=True, progress=False)
            if df is None or len(df) < 30:
                return 0.0
            close = df["Close"].squeeze()

            # RSI (0~40)
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.where(loss!=0, other=np.nan)
            rsi   = (100 - 100/(1+rs)).iloc[-1]
            if rsi <= 20:
                rsi_score = 40
            elif rsi <= 35:
                rsi_score = 30
            elif rsi <= 45:
                rsi_score = 15
            else:
                rsi_score = max(0, 15 - (rsi - 45) * 0.5)

            # 볼린저 밴드 이탈 (0~35)
            ma20  = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            lower = ma20 - 2 * std20
            upper = ma20 + 2 * std20
            cur   = close.iloc[-1]
            bb_pos = (cur - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1]) if (upper.iloc[-1] - lower.iloc[-1]) > 0 else 0.5
            # 0 = 하단 이탈(강한 매수), 1 = 상단(매도 구간)
            bb_score = (1 - bb_pos) * 35

            # 20일 이평 대비 괴리율 (0~25)
            dev = (cur - ma20.iloc[-1]) / ma20.iloc[-1]
            dev_score = min(25, max(0, (-dev) * 200))  # -5% 이탈 → 10pt, -12% → 25pt

            return round(min(100, rsi_score + bb_score + dev_score), 1)
        except Exception:
            return 0.0
