"""
소액 자본 단위 정책(기본 정책) — 자본이 적으면 종목 수를 줄여 한 종목에 적당한 금액을 싣고,
정수 주식으로 적당한 단위를 살 수 있는 종목만 산다. portfolio.lot_plan 참고.
실행: python3 -m pytest test_lot_sizing.py -q
"""
import pytest

from scorer import StockScore


@pytest.fixture
def pf(tmp_path, monkeypatch):
    import safe_store, portfolio, paper_account
    monkeypatch.setattr(safe_store, "_TRADE_LOCK_FILE", tmp_path / ".trade.lock")
    monkeypatch.setattr(portfolio, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(paper_account, "PAPER_FILE", tmp_path / "paper_account.json")
    monkeypatch.setattr(portfolio, "_sector_of", lambda t: "")
    return portfolio


def _sc(scores):
    return [StockScore(t, s, 0, 0, 0, 0)
            for t, s in sorted(scores.items(), key=lambda x: -x[1])]


def _dyn(pf, scores, prices, cash):
    pm = pf.PortfolioManager(paper=True)
    return pm.generate_orders(_sc(scores), prices, dynamic=True, available_override=cash)


def _val(o):
    return {b["ticker"]: b["shares"] * b["est_price"] for b in o["buy"]}


@pytest.mark.parametrize("equity,slots,cap,min_lot", [
    (10_000, 5, 0.25, 250),     # 자본 충분 → 설정 그대로
    (2_500, 5, 0.25, 250),
    (1_500, 3, 1 / 3, 250),
    (1_000, 2, 0.5, 250),
    (800, 1, 1.0, 250),
    (400, 1, 1.0, 200),         # 최소 단위보다 작은 자본 → 자본의 절반 이상으로 한 종목
])
def test_lot_plan_table(pf, equity, slots, cap, min_lot):
    n, c, m = pf.lot_plan(equity)
    assert (n, round(c, 4), m) == (slots, round(cap, 4), min_lot)


def test_large_capital_keeps_five_positions(pf):
    names = {"A": 90, "B": 88, "C": 86, "D": 84, "E": 82}
    o = _dyn(pf, names, {t: 100.0 for t in names}, 10_000)
    assert len(o["buy"]) == 5


def test_small_capital_buys_fewer_bigger_positions(pf):
    names = {"A": 90, "B": 88, "C": 86, "D": 84, "E": 82}
    o = _dyn(pf, names, {t: 50.0 for t in names}, 1_200)
    v = _val(o)
    assert set(v) == {"A", "B"}                          # $1,200 → 2종목만
    assert all(x >= pf.MIN_POSITION_VALUE for x in v.values())
    assert sum(v.values()) <= 1_200


def test_stock_too_pricey_for_unit_gives_slot_to_cheaper(pf):
    # $1,500 → 3종목 · 종목당 최대 $500. 1주 $600 은 상한 안에서 못 사므로 건너뛰고
    # 1주 $100 종목이 그 자리를 가져간다
    o = _dyn(pf, {"PRICY": 95, "MID": 90}, {"PRICY": 600.0, "MID": 100.0}, 1_500)
    assert [(b["ticker"], b["shares"]) for b in o["buy"]] == [("MID", 5)]


def test_thin_share_is_dropped_and_redistributed(pf):
    # $2,500 → 5슬롯이지만 61점 후보의 점수 비례 몫은 ~$130 → 자투리라 빼고 4종목에 나눈다
    names = {"A": 100, "B": 100, "C": 100, "D": 100, "WEAK": 61}
    o = _dyn(pf, names, {t: 50.0 for t in names}, 2_500)
    v = _val(o)
    assert "WEAK" not in v and set(v) == {"A", "B", "C", "D"}
    assert all(x >= pf.MIN_POSITION_VALUE for x in v.values())


def test_no_stub_entry_when_cash_is_short(pf):
    pm = pf.PortfolioManager(paper=True)
    pm.record_buy("A", 13, 50.0, 95)
    pm.record_buy("B", 13, 50.0, 95)
    scores, prices = {"A": 95, "B": 95, "C": 80}, {"A": 50.0, "B": 50.0, "C": 50.0}
    # 자본 $1,500(3슬롯)이지만 현금 $200 → C 는 $200 짜리 자투리로만 열 수 있음 → 안 산다
    assert not _dyn(pf, scores, prices, 200)["buy"]
    pm2 = pf.PortfolioManager(paper=True)
    pm2.record_sell("A", 50.0, "manual")
    pm2.record_buy("A", 12, 50.0, 95)
    pm2.record_sell("B", 50.0, "manual")
    pm2.record_buy("B", 12, 50.0, 95)
    # 현금 $300 이면 신규 최소 금액($250) 이상 → 산다
    assert [b["ticker"] for b in _dyn(pf, scores, prices, 300)["buy"]] == ["C"]


def test_crossing_slot_threshold_does_not_half_sell(pf):
    # $9xx 에서 1종목에 전액 → 자본이 $1,050 으로 늘어 2슬롯이 돼도
    # 두 번째 후보가 없으면 반을 팔아 현금으로 놀리지 않는다(문턱 왕복 방지)
    pf.PortfolioManager(paper=True).record_buy("A", 21, 50.0, 90)
    o = _dyn(pf, {"A": 90}, {"A": 50.0}, 0)
    assert o["sell"] == [] and o["buy"] == []


def test_fixed_mode_small_capital_uses_fewer_slots(pf):
    names = {"A": 90, "B": 88, "C": 86}
    pm = pf.PortfolioManager(paper=True)
    o = pm.generate_orders(_sc(names), {t: 100.0 for t in names}, available_override=1_000)
    v = _val(o)
    assert set(v) == {"A", "B"} and all(x == 500 for x in v.values())


def test_live_cycle_plans_with_real_account_cash(pf, monkeypatch, tmp_path):
    """실거래: CAPITAL_TOTAL($10,000)보다 계좌 현금($800)이 작으면 실제 현금 기준으로 계획."""
    import broker, realtime_feed, risk_guard, telegram_bot, watchlist
    from core import cycle, execution, ledger_guard, scoring
    captured = {}
    # 사이클이 cfg 로 덮어쓰는 portfolio 전역 — 테스트 후 원래 값으로 복구
    for k in ("STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "TRAIL_GIVEBACK_PCT", "MIN_SCORE_TO_BUY",
              "SELL_SCORE_THRESHOLD", "HOLD_DAYS_STRONG", "HOLD_DAYS_MEDIUM",
              "BUY_PRICE_MIN", "BUY_PRICE_MAX"):
        monkeypatch.setattr(pf, k, getattr(pf, k))
    monkeypatch.setitem(cycle._LOT_NOTE, "last", None)

    class FakeBroker:
        paper = False
        def __init__(self, *a, **k): pass
        def get_account(self): return {"equity": 800.0, "cash": 800.0,
                                        "buying_power": 800.0, "last_equity": 800.0}

    monkeypatch.setattr(broker, "Broker", FakeBroker)
    monkeypatch.setattr(execution, "reconcile_live", lambda *a, **k: {"halted": False})
    monkeypatch.setattr(ledger_guard, "check", lambda *a, **k: {})
    monkeypatch.setattr(watchlist, "load", lambda: ["A", "B", "C"])
    monkeypatch.setattr(watchlist, "sync_holdings", lambda *a, **k: None)
    monkeypatch.setattr(scoring, "score", lambda *a, **k: (
        [{"ticker": "A", "score": 90}, {"ticker": "B", "score": 85}, {"ticker": "C", "score": 80}],
        {"A": 100.0, "B": 100.0, "C": 100.0}, "bull"))
    monkeypatch.setattr(realtime_feed, "subscribe", lambda *a, **k: None)
    monkeypatch.setattr(realtime_feed, "get_price", lambda *a, **k: None)
    monkeypatch.setattr(risk_guard, "check", lambda *a, **k: {})
    monkeypatch.setattr(telegram_bot, "push", lambda *a, **k: True)
    monkeypatch.setattr(telegram_bot, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "execute_orders",
                        lambda orders, *a, **k: captured.update(orders) or {"bought": [], "sold": []})
    logs = []
    cycle._run_cycle({"paper": False, "strategy": "composite", "ev_enabled": False,
                      "decision": "every_cycle"}, log=logs.append)   # 판단 사이클 — 시각과 무관하게
    assert [(b["ticker"], b["shares"]) for b in captured["buy"]] == [("A", 8)]
    assert any("소액 자본 정책" in m and "최대 1종목" in m for m in logs)
