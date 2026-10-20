"""
라이브 동점 처리 — 같은 점수면 최근 평균 거래대금 큰 순으로 산다(예전: 워치리스트 순서).
core.scoring.rank 참고. 백테스트 tie_order("dollar_volume") 와 같은 기준.
실행: python3 -m pytest test_live_tie_break.py -q
"""
import pandas as pd
import pytest

from core import scoring


def _df(close: float, volume: float, n: int = 80) -> pd.DataFrame:
    return pd.DataFrame({"Close": [close] * n, "Volume": [volume] * n})


def test_rank_breaks_ties_by_dollar_volume_then_ticker():
    rows = [{"ticker": "AAA", "score": 70.0}, {"ticker": "ZZZ", "score": 70.0},
            {"ticker": "MMM", "score": 90.0}, {"ticker": "BBB", "score": 70.0}]
    scoring.rank(rows, {"ZZZ": 5e9, "AAA": 1e9})
    # 점수 우선, 동점은 거래대금 큰 순, 거래대금 모르면(0) 티커순
    assert [r["ticker"] for r in rows] == ["MMM", "ZZZ", "AAA", "BBB"]


def test_strategy_path_ties_follow_bundle_dollar_volume(monkeypatch):
    import backtester as bt
    sd = {"AAA": _df(10.0, 1_000),        # 거래대금 $10k
          "BBB": _df(100.0, 1_000_000),   # $100M
          "CCC": _df(50.0, 100_000)}      # $5M
    monkeypatch.setattr(scoring, "_BUNDLE_CACHE", {})
    monkeypatch.setattr(bt, "load_market_data", lambda *a, **k: {
        "stock_data": sd, "etf_data": {}, "spy_close": None, "spy_ma200": None})
    monkeypatch.setattr(bt, "_strategy_score_bt", lambda *a, **k: 70.0)   # 전부 동점
    scores, prices, _ = scoring.score("ichimoku", ["AAA", "BBB", "CCC"])
    assert [r["ticker"] for r in scores] == ["BBB", "CCC", "AAA"]


@pytest.fixture
def ai_path(monkeypatch):
    import realtime_feed
    import stock_browser as sb
    from influence import engine
    calls = []

    def fake_dv(t, days):
        calls.append(t)
        return {"AAA": 1e6, "BBB": 9e9}.get(t, 0.0)

    monkeypatch.setattr(scoring, "_DV_CACHE", {})
    monkeypatch.setattr(realtime_feed, "get_price", lambda t: None)
    monkeypatch.setattr(sb, "quote_prices", lambda ts: {t: 100.0 for t in ts})
    monkeypatch.setattr(sb, "market_regime", lambda: "neutral")
    monkeypatch.setattr(sb, "nasdaq_dollar_volume", fake_dv)
    monkeypatch.setattr(engine, "score_universe", lambda valid, **k: (
        {"AAA": 72.0, "BBB": 72.0, "CCC": 88.0}, {}))
    return calls


def test_ai_path_fetches_only_tied_and_caches(ai_path):
    scores, _, _ = scoring.score("ai_influencer", ["AAA", "BBB", "CCC"])
    assert [r["ticker"] for r in scores] == ["CCC", "BBB", "AAA"]
    assert sorted(ai_path) == ["AAA", "BBB"]          # 동점 아닌 CCC 는 조회 안 함
    scoring.score("ai_influencer", ["AAA", "BBB", "CCC"])
    assert sorted(ai_path) == ["AAA", "BBB"]          # 두 번째는 캐시


def test_ai_path_dollar_volume_failure_never_breaks_scoring(ai_path, monkeypatch):
    import stock_browser as sb

    def boom(t, days):
        raise RuntimeError("nasdaq down")
    monkeypatch.setattr(sb, "nasdaq_dollar_volume", boom)
    logs = []
    scores, _, _ = scoring.score("ai_influencer", ["BBB", "AAA", "CCC"], log=logs.append)
    assert [r["ticker"] for r in scores] == ["CCC", "AAA", "BBB"]   # 티커순 폴백
    assert any("동점 거래대금 조회 실패" in m for m in logs)
