"""
Composite scorer: combines all 4 signals into a single 0-100 score per ticker.
"""

import concurrent.futures
from dataclasses import dataclass

from config import SIGNAL_WEIGHTS, TRADEABLE_UNIVERSE
from signals import institutional, sentiment, sector, fundamental


@dataclass
class StockScore:
    ticker: str
    total: float
    institutional: float
    sentiment: float
    sector: float
    fundamental: float

    def __repr__(self):
        return (
            f"{self.ticker:6s} | total={self.total:5.1f} | "
            f"inst={self.institutional:5.1f} sent={self.sentiment:5.1f} "
            f"sect={self.sector:5.1f} fund={self.fundamental:5.1f}"
        )


def _score_one(ticker: str) -> StockScore:
    inst = institutional.score(ticker)
    sent = sentiment.score(ticker)
    sect = sector.score(ticker)
    fund = fundamental.score(ticker)

    w = SIGNAL_WEIGHTS
    total = (
        w["institutional"] * inst
        + w["sentiment"] * sent
        + w["sector"] * sect
        + w["fundamental"] * fund
    )

    return StockScore(
        ticker=ticker,
        total=round(total, 1),
        institutional=inst,
        sentiment=sent,
        sector=sect,
        fundamental=fund,
    )


def score_universe(universe: list[str] = None, max_workers: int = 4) -> list[StockScore]:
    """Score all tickers in universe, return sorted best-first."""
    if universe is None:
        universe = TRADEABLE_UNIVERSE

    # Prefetch sector ranks once before parallel scoring
    sector.refresh_ranks()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_score_one, t): t for t in universe}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                print(f"  [scorer] error on {futures[future]}: {e}")

    results.sort(key=lambda s: s.total, reverse=True)
    return results
