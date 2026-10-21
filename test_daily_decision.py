"""
하루 1번 판단(decision="daily", 기본) — 매수·교체·비중조정은 ET 거래일마다 decision_time 이후 첫
사이클 1번만, 나머지 사이클은 손절·트레일링 감시만. core/cycle.py 참고.
실행: python3 -m pytest test_daily_decision.py -q
"""
from datetime import datetime

import pytest
import pytz

from core import control, cycle

ET = pytz.timezone("America/New_York")


def _et(y, m, d, hh, mm):
    return ET.localize(datetime(y, m, d, hh, mm))


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "CONFIG_FILE", tmp_path / "autotrader_config.json")
    return tmp_path / "decision_state.json"


def test_every_cycle_mode_always_decides(state):
    assert cycle.decision_due({"decision": "every_cycle"}, _et(2026, 10, 21, 9, 31))
    assert not state.exists()


def test_daily_decides_once_after_decision_time(state):
    cfg = {}                                                        # 기본 = daily, 09:35 ET
    assert not cycle.decision_due(cfg, _et(2026, 10, 21, 9, 32))    # 개장 직후 — 아직
    t = _et(2026, 10, 21, 9, 36)
    assert cycle.decision_due(cfg, t)
    cycle._mark_decided(t)
    assert not cycle.decision_due(cfg, _et(2026, 10, 21, 11, 0))    # 같은 날 — 감시 모드
    assert not cycle.decision_due(cfg, _et(2026, 10, 21, 15, 55))
    assert cycle.decision_due(cfg, _et(2026, 10, 22, 9, 40))        # 다음 거래일


def test_decision_day_is_et_not_kst(state):
    """KST 자정은 ET 장중 — 로컬 날짜로 세면 하루 2번 판단한다."""
    cfg = {}
    cycle._mark_decided(_et(2026, 10, 21, 9, 40))                   # = KST 10/21 22:40
    assert not cycle.decision_due(cfg, _et(2026, 10, 21, 11, 30))   # = KST 10/22 00:30


def test_custom_decision_time(state):
    cfg = {"decision_time": "15:45"}                                # 장 마감 직전 판단
    assert not cycle.decision_due(cfg, _et(2026, 10, 21, 15, 0))
    assert cycle.decision_due(cfg, _et(2026, 10, 21, 15, 46))


def test_monitor_only_keeps_risk_exits():
    orders = {"sell": [{"ticker": "A", "reason": "stop_loss (-7.2%)"},
                       {"ticker": "B", "reason": "trailing_stop (고점 +20%→현재 +11%)"},
                       {"ticker": "C", "reason": "rotate (목표 이탈)"},
                       {"ticker": "D", "reason": "trim (비중 축소)"},
                       {"ticker": "E", "reason": "score_drop (30)"}],
              "buy": [{"ticker": "F"}, {"ticker": "G"}]}
    kept, n = cycle.monitor_only(orders)
    assert [o["ticker"] for o in kept["sell"]] == ["A", "B"] and kept["buy"] == []
    assert n == 5


# ── 사이클 통합: 감시 모드면 손절·트레일링만 주문으로 나간다 ─────────────────────

@pytest.fixture
def live_cycle(monkeypatch, tmp_path):
    """test_lot_sizing 의 실거래 사이클 격리를 그대로 — 보유 A(손절 −10%)·B, 후보 C·D."""
    import broker, paper_account, portfolio as pf, realtime_feed, risk_guard, safe_store
    import telegram_bot, watchlist
    from core import execution, ledger_guard, scoring
    monkeypatch.setattr(safe_store, "_TRADE_LOCK_FILE", tmp_path / ".trade.lock")
    monkeypatch.setattr(pf, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(paper_account, "PAPER_FILE", tmp_path / "paper_account.json")
    monkeypatch.setattr(pf, "_sector_of", lambda t: "")
    monkeypatch.setattr(control, "CONFIG_FILE", tmp_path / "autotrader_config.json")
    for k in ("STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "TRAIL_GIVEBACK_PCT", "MIN_SCORE_TO_BUY",
              "SELL_SCORE_THRESHOLD", "HOLD_DAYS_STRONG", "HOLD_DAYS_MEDIUM",
              "BUY_PRICE_MIN", "BUY_PRICE_MAX"):
        monkeypatch.setattr(pf, k, getattr(pf, k))
    pm = pf.PortfolioManager(paper=False)
    pm.record_buy("A", 10, 100.0, 70)
    pm.record_buy("B", 10, 100.0, 70)

    class FakeBroker:
        paper = False
        def __init__(self, *a, **k): pass
        def get_account(self): return {"equity": 10_000.0, "cash": 8_000.0,
                                        "buying_power": 8_000.0, "last_equity": 10_000.0}

    monkeypatch.setattr(broker, "Broker", FakeBroker)
    monkeypatch.setattr(execution, "reconcile_live", lambda *a, **k: {"halted": False})
    monkeypatch.setattr(ledger_guard, "check", lambda *a, **k: {})
    monkeypatch.setattr(watchlist, "load", lambda: ["A", "B", "C", "D"])
    monkeypatch.setattr(watchlist, "sync_holdings", lambda *a, **k: None)
    monkeypatch.setattr(scoring, "score", lambda *a, **k: (
        [{"ticker": "C", "score": 95}, {"ticker": "D", "score": 90},
         {"ticker": "A", "score": 70}, {"ticker": "B", "score": 70}],
        {"A": 90.0, "B": 100.0, "C": 50.0, "D": 50.0}, "bull"))
    monkeypatch.setattr(realtime_feed, "subscribe", lambda *a, **k: None)
    monkeypatch.setattr(realtime_feed, "get_price", lambda *a, **k: None)
    monkeypatch.setattr(risk_guard, "check", lambda *a, **k: {})
    monkeypatch.setattr(telegram_bot, "push", lambda *a, **k: True)
    monkeypatch.setattr(telegram_bot, "write_status", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(cycle, "execute_orders",
                        lambda orders, *a, **k: captured.update(orders) or {"bought": [], "sold": []})
    return captured


def test_monitor_cycle_sends_only_risk_exits(live_cycle, monkeypatch):
    monkeypatch.setattr(cycle, "decision_due", lambda cfg, now_et=None: False)
    logs = []
    r = cycle._run_cycle({"paper": False, "strategy": "composite", "ev_enabled": False},
                         log=logs.append)
    assert [o["ticker"] for o in live_cycle["sell"]] == ["A"]          # A −10% 손절만
    assert live_cycle["sell"][0]["reason"].startswith("stop_loss")
    assert live_cycle["buy"] == []                                     # 신규 C·D 매수 보류
    assert any("감시 모드" in m for m in logs) and r["decided"] is False
    assert not (control.CONFIG_FILE.parent / "decision_state.json").exists()


def test_decision_cycle_trades_and_marks_the_day(live_cycle, monkeypatch):
    monkeypatch.setattr(cycle, "decision_due", lambda cfg, now_et=None: True)
    r = cycle._run_cycle({"paper": False, "strategy": "composite", "ev_enabled": False},
                         log=lambda m: None)
    assert {b["ticker"] for b in live_cycle["buy"]} >= {"C"} and r["decided"] is True
    assert (control.CONFIG_FILE.parent / "decision_state.json").exists()
