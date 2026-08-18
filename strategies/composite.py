"""
복합 전략: 기관 매수세·뉴스·섹터·펀더멘털 4개 신호 가중 합산.
config.py의 SIGNAL_WEIGHTS를 따릅니다.
"""
from .base import BaseStrategy, StrategyInfo


class CompositeStrategy(BaseStrategy):
    info = StrategyInfo(
        name="composite",
        display_name="🔬 복합 (기본)",
        description="기관 매수세·뉴스 센티먼트·섹터 흐름·펀더멘털 4개 신호 가중 합산.",
        best_for="모든 시장 (범용)",
        params={},
    )

    def score(self, ticker: str) -> float:
        from signals import institutional, sentiment, sector, fundamental
        from config import SIGNAL_WEIGHTS as W
        try:
            inst = institutional.score(ticker)
            sent = sentiment.score(ticker)
            sect = sector.score(ticker)
            fund = fundamental.score(ticker)
            return round(
                W["institutional"]*inst + W["sentiment"]*sent +
                W["sector"]*sect + W["fundamental"]*fund, 1)
        except Exception:
            return 0.0
