"""
Earnings & fundamental momentum signal. Score 0-100.

Metrics (via yfinance):
  - Revenue YoY growth
  - Earnings quarterly growth
  - Analyst price target upside
  - Return on equity (quality)

각 지표는 가용할 때만 합산하고, 마지막에 '실제 가용한 지표들의 최대 점수합'으로
0-100 정규화한다. (이전엔 항상 100 만점으로 나눠, 일부 지표만 제공되는 종목이
부당하게 낮은 점수를 받던 버그가 있었음.)
"""

import yfinance as yf


def score(ticker: str) -> float:
    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}

        points = 0.0
        available_max = 0.0   # 실제로 평가에 쓰인 지표들의 최대 점수합

        # 1. Revenue growth (0-30 pts)
        rev_growth = info.get("revenueGrowth")  # TTM YoY
        if rev_growth is not None:
            points += min(30, max(0, rev_growth * 100))   # +20% → 20pt, +30%+ → 30pt
            available_max += 30

        # 2. Earnings quarterly growth (0-25 pts)
        earn_growth = info.get("earningsQuarterlyGrowth")
        if earn_growth is not None:
            points += min(25, max(0, earn_growth * 50))
            available_max += 25

        # 3. Analyst target upside (0-25 pts)
        target = info.get("targetMeanPrice")
        current = info.get("currentPrice") or info.get("regularMarketPrice")
        if target and current and current > 0:
            upside = (target - current) / current
            points += min(25, max(0, upside * 100))
            available_max += 25

        # 4. Return on equity (0-20 pts): quality signal
        roe = info.get("returnOnEquity")
        if roe is not None:
            points += min(20, max(0, roe * 100))
            available_max += 20

        if available_max == 0:
            return 50.0   # 데이터 전무 → 중립

        # 가용 지표 기준으로 0-100 정규화
        return round(min(100.0, max(0.0, points / available_max * 100)), 1)

    except Exception:
        return 50.0
