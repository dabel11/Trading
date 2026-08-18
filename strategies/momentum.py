"""
모멘텀 전략: 강한 상승 추세 종목 매수.

지표:
  - 52주 고점 근접도 (신고가 돌파)
  - 1·3·6개월 상대 수익률
  - 거래량 트렌드
  - RSI (과매수 제외)
"""
import numpy as np
import yfinance as yf
from .base import BaseStrategy, StrategyInfo


class MomentumStrategy(BaseStrategy):
    info = StrategyInfo(
        name="momentum",
        display_name="📈 모멘텀",
        description="강한 상승 추세 종목을 추적. 신고가 돌파 + 거래량 증가 조합.",
        best_for="강세장 (Bull Market)",
        params={"lookback_days": 252, "rsi_max": 75},
    )

    def score(self, ticker: str) -> float:
        try:
            df = yf.download(ticker, period="1y", interval="1d",
                             auto_adjust=True, progress=False)
            if df is None or len(df) < 60:
                return 0.0
            close  = df["Close"].squeeze()
            volume = df["Volume"].squeeze()
            high52 = close.max()
            cur    = close.iloc[-1]

            # 1. 52주 고점 근접도 (0~35)
            nearness = cur / high52  # 1.0 = 신고가
            near_score = nearness * 35

            # 2. 모멘텀 수익률 (0~35): 1M×0.4 + 3M×0.4 + 6M×0.2
            def ret(n): return (cur / close.iloc[-min(n,len(close)-1)] - 1) * 100
            mom = ret(21)*0.4 + ret(63)*0.4 + ret(126)*0.2
            mom_score = min(35, max(0, 17.5 + mom * 0.7))

            # 3. 거래량 트렌드 (0~20)
            avg_vol = volume.iloc[-60:-5].mean()
            rec_vol = volume.iloc[-5:].mean()
            vol_score = min(20, max(0, (rec_vol/avg_vol - 1) * 20)) if avg_vol > 0 else 0

            # 4. RSI 필터 (0~10): 30~70 구간이 이상적
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.where(loss != 0, other=np.nan)
            rsi   = (100 - 100 / (1 + rs)).iloc[-1]
            rsi_score = 10 if 40 <= rsi <= 70 else (5 if 30 <= rsi < 40 else 0)

            return round(min(100, near_score + mom_score + vol_score + rsi_score), 1)
        except Exception:
            return 0.0
