from .base import BaseStrategy
from .momentum import MomentumStrategy
from .mean_reversion import MeanReversionStrategy
from .sector_rotation import SectorRotationStrategy
from .fundamental import FundamentalStrategy
from .composite import CompositeStrategy
from .technical import (
    GoldenCrossStrategy, BreakoutStrategy, MACDStrategy, BollingerSqueezeStrategy,
)

STRATEGIES: dict[str, type[BaseStrategy]] = {
    # ── 펀더멘털/플로우 계열 ──
    "composite":       CompositeStrategy,
    "momentum":        MomentumStrategy,
    "mean_reversion":  MeanReversionStrategy,
    "sector_rotation": SectorRotationStrategy,
    "fundamental":     FundamentalStrategy,
    # ── 차트(기술적) 계열 ──
    "golden_cross":    GoldenCrossStrategy,
    "breakout":        BreakoutStrategy,
    "macd":            MACDStrategy,
    "bollinger":       BollingerSqueezeStrategy,
}

# 분류 (UI 그룹핑용)
CATEGORIES = {
    "시장 흐름": ["composite", "momentum", "mean_reversion", "sector_rotation", "fundamental"],
    "차트 기법": ["golden_cross", "breakout", "macd", "bollinger"],
}


def get(name: str) -> BaseStrategy:
    cls = STRATEGIES.get(name)
    if cls is not None:
        return cls()
    # 전용 클래스가 없는 카탈로그 전략 → 범용 라이브 스코어러
    from .generic import GenericCatalogStrategy
    try:
        return GenericCatalogStrategy(name)
    except Exception:
        return CompositeStrategy()


def auto_recommend(market_trend: str = "neutral") -> str:
    mapping = {
        "bull":     "breakout",        # 강세장 → 돌파 추격
        "bear":     "mean_reversion",  # 약세장 → 역추세
        "neutral":  "composite",       # 중립   → 복합
        "volatile": "bollinger",       # 변동성  → 스퀴즈 돌파
    }
    return mapping.get(market_trend, "composite")
