"""
시장 상황 분석: VIX·SPY·이동평균으로 현재 시장 국면 판단 후 전략 자동 추천.
"""
import yfinance as yf
import numpy as np
import pandas as pd


def analyze() -> dict:
    """
    Returns:
        trend:    'bull' | 'bear' | 'neutral' | 'volatile'
        vix:      float
        spy_1m:   float (%)
        spy_vs_ma200: float (%)
        breadth:  float (상승 종목 비율 %)
        recommended_strategy: str
        reason:   str
    """
    result = {
        "trend": "neutral", "vix": 0.0,
        "spy_1m": 0.0, "spy_vs_ma200": 0.0,
        "breadth": 50.0, "recommended_strategy": "composite",
        "reason": "분석 데이터 없음",
    }
    try:
        # SPY 추세
        spy = yf.download("SPY", period="1y", interval="1d",
                          auto_adjust=True, progress=False)
        if not spy.empty:
            close = spy["Close"].squeeze()
            ma200 = close.rolling(200).mean().iloc[-1]
            ma50  = close.rolling(50).mean().iloc[-1]
            cur   = close.iloc[-1]
            result["spy_1m"]       = round((cur/close.iloc[-21]-1)*100, 2)
            result["spy_vs_ma200"] = round((cur/ma200-1)*100, 2)

        # VIX
        vix_df = yf.download("^VIX", period="5d", interval="1d",
                              auto_adjust=True, progress=False)
        if not vix_df.empty:
            result["vix"] = round(float(vix_df["Close"].squeeze().iloc[-1]), 1)

        # 시장 국면 판단
        vix   = result["vix"]
        spy1m = result["spy_1m"]
        vs200 = result["spy_vs_ma200"]

        if vix > 30:
            trend = "volatile"
            reason = f"VIX {vix:.0f} — 공포 구간. 섹터 로테이션으로 방어적 접근 권장."
            strat  = "sector_rotation"
        elif spy1m > 3 and vs200 > 2:
            trend = "bull"
            reason = f"SPY 1개월 +{spy1m:.1f}%, 200MA 위 +{vs200:.1f}% — 강세장. 모멘텀 전략 유리."
            strat  = "momentum"
        elif spy1m < -3 and vs200 < -2:
            trend = "bear"
            reason = f"SPY 1개월 {spy1m:.1f}%, 200MA 아래 {vs200:.1f}% — 약세장. 역추세·저가매수 전략."
            strat  = "mean_reversion"
        else:
            trend = "neutral"
            reason = f"SPY 횡보 중 ({spy1m:+.1f}%). 복합 전략으로 균형 잡힌 접근."
            strat  = "composite"

        result["trend"]                = trend
        result["recommended_strategy"] = strat
        result["reason"]               = reason

        # 시장 breadth (간단히 SPY 구성 상위 10종목 기준)
        sample = ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","JPM","UNH","LLY"]
        up = 0
        for t in sample:
            try:
                d = yf.download(t, period="1mo", interval="1d",
                                auto_adjust=True, progress=False)
                if not d.empty:
                    c = d["Close"].squeeze()
                    if c.iloc[-1] > c.iloc[0]:
                        up += 1
            except Exception:
                pass
        result["breadth"] = round(up / len(sample) * 100, 0)

    except Exception:
        pass
    return result
