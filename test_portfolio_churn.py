"""
회전율 안전장치(감사 #18) — 당일 매수 종목 회전 금지, 같은 발행사 중복 진입 방지.

2026-10-19 장중: 22:56 매수한 AAPL·GOOGL 을 5분 뒤 '회전'으로 팔고 GOOG 등을 샀다
(전체시장 스캔 진행으로 순위가 바뀜). 실거래였다면 수수료·슬리피지만 날린 거래.
실행: python3 -m pytest test_portfolio_churn.py -q
"""
from datetime import date

import pytest

from scorer import StockScore

D0, D1 = date(2026, 10, 19), date(2026, 10, 20)


@pytest.fixture
def pf(tmp_path, monkeypatch):
    import safe_store, portfolio, paper_account
    monkeypatch.setattr(safe_store, "_TRADE_LOCK_FILE", tmp_path / ".trade.lock")
    monkeypatch.setattr(portfolio, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(paper_account, "PAPER_FILE", tmp_path / "paper_account.json")
    monkeypatch.setattr(portfolio, "_sector_of", lambda t: "")
    portfolio.set_clock(D0)
    yield portfolio
    portfolio.set_clock(None)


def _orders(pf, scores):
    pm = pf.PortfolioManager(paper=True)
    sc = [StockScore(t, s, 0, 0, 0, 0) for t, s in sorted(scores.items(), key=lambda x: -x[1])]
    return pm.generate_orders(sc, {t: 100.0 for t in scores}, dynamic=True,
                              available_override=10000.0)


def test_same_day_purchase_is_not_rotated(pf):
    pf.PortfolioManager(paper=True).record_buy("AAPL", 7, 100.0, 71)
    scores = {"AAPL": 71, "N1": 95, "N2": 94, "N3": 93, "N4": 92, "N5": 91}
    o = _orders(pf, scores)
    assert not [s for s in o["sell"] if s["ticker"] == "AAPL"]
    assert len(o["buy"]) <= pf.MAX_POSITIONS - 1                 # 슬롯 하드캡 유지
    pf.set_clock(D1)                                             # 다음 거래일엔 회전 허용
    o = _orders(pf, scores)
    assert [s["reason"] for s in o["sell"] if s["ticker"] == "AAPL"] == ["rotate (목표 이탈)"]


def test_risk_exits_still_fire_same_day(pf):
    pf.PortfolioManager(paper=True).record_buy("AAPL", 7, 100.0, 71)
    pm = pf.PortfolioManager(paper=True)
    o = pm.generate_orders([StockScore("AAPL", 71, 0, 0, 0, 0)], {"AAPL": 90.0},   # -10% → 손절
                           dynamic=True, available_override=10000.0)
    assert [s["reason"][:9] for s in o["sell"]] == ["stop_loss"]


def test_no_second_share_class_of_held_issuer(pf):
    pf.PortfolioManager(paper=True).record_buy("GOOGL", 7, 100.0, 75)
    o = _orders(pf, {"GOOGL": 75, "GOOG": 90, "MSFT": 80})
    bought = {b["ticker"] for b in o["buy"]}
    assert "GOOG" not in bought and "MSFT" in bought


def test_only_one_share_class_when_flat(pf):
    o = _orders(pf, {"GOOG": 90, "GOOGL": 88, "MSFT": 70})
    bought = {b["ticker"] for b in o["buy"]}
    assert "GOOG" in bought and "GOOGL" not in bought
