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


def test_rotation_pairs_one_slot_at_a_time(pf):
    """10/20 장 시작 5종목 전부 교체 재현 — 신규 1등(88)만 압도적이고 나머지는 보유보다 조금 높다.
    예전: 1등 88 이 모든 보유(66~75)보다 10점 이상 높아 5개 전부 회전.
    지금: 약한 보유↔강한 신규를 한 쌍씩 — 66↔88(+22), 68↔79(+11) 만 교체, 70↔78(+8)에서 멈춘다."""
    held = {"H1": 66, "H2": 68, "H3": 70, "H4": 72, "H5": 75}
    for t, sc in held.items():
        pf.PortfolioManager(paper=True).record_buy(t, 20, 100.0, sc)
    pf.set_clock(D1)
    scores = {**held, "N1": 88, "N2": 79, "N3": 78, "N4": 77, "N5": 76}
    pm = pf.PortfolioManager(paper=True)
    sc = [StockScore(t, s, 0, 0, 0, 0) for t, s in sorted(scores.items(), key=lambda x: -x[1])]
    o = pm.generate_orders(sc, {t: 100.0 for t in scores}, dynamic=True, available_override=0.0)
    assert sorted(s["ticker"] for s in o["sell"] if s["reason"].startswith("rotate")) == ["H1", "H2"]
    assert sorted(b["ticker"] for b in o["buy"]) == ["N1", "N2"]       # 슬롯 5 유지
