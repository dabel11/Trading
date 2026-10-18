"""
펀더멘털 모멘텀: 실적 개선 + 밸류에이션 매력 종목.

지표:
  - EPS 성장률 (분기 YoY)
  - 매출 성장률
  - ROE
  - 애널리스트 목표주가 상승 여력
  - PEG (P/E to Growth)
"""
import yfinance as yf
from .base import BaseStrategy, StrategyInfo


class FundamentalStrategy(BaseStrategy):
    info = StrategyInfo(
        name="fundamental",
        display_name="📊 펀더멘털",
        description="실적 성장 + 밸류에이션 매력을 동시에 보유한 종목.",
        best_for="중장기 가치투자",
        params={"min_revenue_growth": 0.10, "min_roe": 0.10},
    )

    def score(self, ticker: str) -> float:
        try:
            info = yf.Ticker(ticker).info or {}
            pts  = 0.0

            # 매출 성장 (0~30)
            rg = info.get("revenueGrowth")
            if rg is not None:
                pts += min(30, max(0, rg * 120))

            # EPS 분기 성장 (0~25)
            eg = info.get("earningsQuarterlyGrowth")
            if eg is not None:
                pts += min(25, max(0, eg * 80))

            # ROE (0~20)
            roe = info.get("returnOnEquity")
            if roe is not None:
                pts += min(20, max(0, roe * 80))

            # 목표주가 상승여력 (0~15)
            target  = info.get("targetMeanPrice")
            current = info.get("currentPrice") or info.get("regularMarketPrice")
            if target and current and current > 0:
                upside = (target - current) / current
                pts += min(15, max(0, upside * 60))

            # PEG: 낮을수록 좋음 (0~10)
            peg = info.get("pegRatio")
            if peg and peg > 0:
                pts += min(10, max(0, (2 - peg) * 5))

            return round(min(100, max(0, pts)), 1)
        except Exception:
            return 50.0
