"""
섹터 로테이션 전략: 강세 섹터로 자금이 이동하는 흐름 추적.

지표:
  - 섹터 ETF 상대 모멘텀 (1·3개월)
  - 섹터 내 종목 상대강도
  - 자금 흐름 (거래량 가중)
"""
import numpy as np
import yfinance as yf
from .base import BaseStrategy, StrategyInfo

SECTOR_ETFS = ["XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLB","XLU","XLRE"]
TICKER_SECTOR = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK","AVGO":"XLK",
    "CRM":"XLK","ORCL":"XLK","ADBE":"XLK","GOOGL":"XLK","META":"XLK",
    "NFLX":"XLK","SHOP":"XLK","INTC":"XLK","QCOM":"XLK",
    "JPM":"XLF","GS":"XLF","MS":"XLF","BAC":"XLF","WFC":"XLF",
    "XOM":"XLE","CVX":"XLE","COP":"XLE",
    "LLY":"XLV","UNH":"XLV","JNJ":"XLV","PFE":"XLV","ABBV":"XLV",
    "TSLA":"XLY","AMZN":"XLY","HD":"XLY","NKE":"XLY",
    "CAT":"XLI","DE":"XLI","BA":"XLI","GE":"XLI",
    "PG":"XLP","KO":"XLP","PEP":"XLP","WMT":"XLP",
}

_sector_cache: dict | None = None
_sector_cache_ts: float = 0


def _get_sector_ranks() -> dict[str, float]:
    import time
    global _sector_cache, _sector_cache_ts
    now = time.time()
    if _sector_cache and (now - _sector_cache_ts) < 3600:
        return _sector_cache
    try:
        data = yf.download(SECTOR_ETFS, period="3mo", interval="1d",
                           auto_adjust=True, progress=False)
        close = data["Close"] if isinstance(data.columns, yf.download.__class__) else data["Close"]
        returns = {}
        for etf in SECTOR_ETFS:
            try:
                c = data["Close"][etf].dropna() if hasattr(data["Close"], "__getitem__") else data["Close"].dropna()
                ret1m = (c.iloc[-1]/c.iloc[max(-21,-len(c))] - 1)
                ret3m = (c.iloc[-1]/c.iloc[0] - 1)
                returns[etf] = ret1m*0.6 + ret3m*0.4
            except Exception:
                returns[etf] = 0.0
        ranked = sorted(returns, key=returns.get)
        n = len(ranked)
        _sector_cache = {etf: round(i/(n-1)*100, 1) for i, etf in enumerate(ranked)}
        _sector_cache_ts = now
    except Exception:
        _sector_cache = {e: 50.0 for e in SECTOR_ETFS}
    return _sector_cache


class SectorRotationStrategy(BaseStrategy):
    info = StrategyInfo(
        name="sector_rotation",
        display_name="🔀 섹터 로테이션",
        description="강세 섹터로 이동하는 자금 흐름을 따라가는 전략.",
        best_for="섹터 순환장",
        params={"lookback_1m": 21, "lookback_3m": 63},
    )

    def score(self, ticker: str) -> float:
        try:
            ranks = _get_sector_ranks()
            etf   = TICKER_SECTOR.get(ticker, "XLK")
            sect_score = ranks.get(etf, 50.0)  # 0~100

            # 종목 자체 상대강도 (섹터 내에서)
            df = yf.download(ticker, period="3mo", interval="1d",
                             auto_adjust=True, progress=False)
            if df is None or len(df) < 20:
                return sect_score
            close  = df["Close"].squeeze()
            volume = df["Volume"].squeeze()
            ret1m  = (close.iloc[-1]/close.iloc[max(-21,-len(close))] - 1) * 100
            rs_score = min(30, max(0, 15 + ret1m))

            # 거래량 가중치
            avg_vol = volume.iloc[-20:].mean()
            rec_vol = volume.iloc[-5:].mean()
            vf = min(1.5, rec_vol/avg_vol) if avg_vol > 0 else 1.0

            raw = sect_score * 0.6 + rs_score * 0.4
            return round(min(100, raw * (0.8 + vf*0.2)), 1)
        except Exception:
            return 50.0
