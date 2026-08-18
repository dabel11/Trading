"""
범용 라이브 전략 — 백테스트 스코어링 로직을 실시간 데이터에 적용.
카탈로그에만 있고 별도 클래스가 없는 전략(rsi2, turtle, ichimoku 등)을 위해.
"""
import yfinance as yf
import pandas as pd
from .base import BaseStrategy, StrategyInfo


class GenericCatalogStrategy(BaseStrategy):
    """strategy_catalog의 전략 이름으로 백테스트 스코어 함수를 호출."""
    def __init__(self, name: str):
        import strategy_catalog as scat
        m = scat.meta(name)
        self._name = name
        self.info = StrategyInfo(
            name=name, display_name=m["name"],
            description=m["desc"], best_for=m["horizon"],
        )

    def score(self, ticker: str) -> float:
        import backtester
        try:
            df = yf.download(ticker, period="2y", interval="1d",
                             auto_adjust=True, progress=False)
            if df is None or len(df) < 60:
                return 0.0
            # 단일 종목 MultiIndex 컬럼 평탄화 → ('Close','NVDA') → 'Close'
            if isinstance(df.columns, pd.MultiIndex):
                df = df.xs(ticker, axis=1, level=1)
            # 백테스트 스코어 함수에 단일 종목 데이터 전달, 마지막 인덱스 평가
            stock_data = {ticker: df}
            idx = len(df) - 1
            return backtester._strategy_score_bt(self._name, ticker, stock_data, {}, idx)
        except Exception:
            return 0.0
