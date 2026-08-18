from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class StrategyInfo:
    name:        str
    display_name: str
    description: str
    best_for:    str          # 어떤 시장 상황에 적합한지
    params:      dict = field(default_factory=dict)


class BaseStrategy(ABC):
    info: StrategyInfo

    @abstractmethod
    def score(self, ticker: str) -> float:
        """주어진 티커에 대해 0~100점 반환."""
        ...

    def score_many(self, tickers: list[str], max_workers: int = 4) -> list[dict]:
        """여러 종목 병렬 스코어링. [{ticker, score}, ...]"""
        import concurrent.futures
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.score, t): t for t in tickers}
            for f in concurrent.futures.as_completed(futures):
                t = futures[f]
                try:
                    s = f.result()
                except Exception:
                    s = 0.0
                results.append({"ticker": t, "score": round(s, 1)})
        return sorted(results, key=lambda x: x["score"], reverse=True)
