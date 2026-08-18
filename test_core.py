"""
핵심 로직 테스트 — 돈이 걸린 부분 회귀 방지.

실행:
  python -m pytest test_core.py -v
  또는  python test_core.py   (pytest 없이도 동작)

네트워크·브로커 API는 호출하지 않는다 (순수 로직만).
"""

import os
import json
import time
import tempfile
from pathlib import Path
from datetime import date


# ─────────────────────────────────────────────────────────────────────────────
# safe_store: 원자적 쓰기 + 손상 복구
# ─────────────────────────────────────────────────────────────────────────────

def test_atomic_write_roundtrip():
    from safe_store import atomic_write_json, safe_read_json
    p = Path(tempfile.mktemp(suffix=".json"))
    atomic_write_json(p, {"positions": {"AAPL": 3}})
    assert safe_read_json(p) == {"positions": {"AAPL": 3}}
    p.unlink()


def test_corrupt_file_recovers_with_default():
    from safe_store import safe_read_json
    p = Path(tempfile.mktemp(suffix=".json"))
    p.write_text("{ this is not valid json")
    out = safe_read_json(p, default={"ok": True})
    assert out == {"ok": True}
    # 손상 파일은 .corrupt 로 백업됨
    assert p.with_suffix(p.suffix + ".corrupt").exists()
    p.with_suffix(p.suffix + ".corrupt").unlink()


def test_missing_file_returns_default():
    from safe_store import safe_read_json
    assert safe_read_json("/nonexistent/xyz.json", default=[]) == []


# ─────────────────────────────────────────────────────────────────────────────
# broker: client_order_id 멱등성 (네트워크 없이 ID 생성 규칙만)
# ─────────────────────────────────────────────────────────────────────────────

def test_client_order_id_deterministic_within_minute():
    # Broker.__init__ 은 Alpaca 클라이언트를 만들지만,
    # _coid 는 self 상태를 쓰지 않으므로 더미 객체로 호출 가능.
    from broker import Broker
    coid = Broker.__new__(Broker)._coid  # bound method, no __init__
    a = coid("AAPL", "buy", 10)
    b = coid("AAPL", "buy", 10)
    assert a == b, "같은 종목·방향·수량·분이면 동일 ID여야 중복 차단됨"
    assert a.startswith("ait-buy-AAPL-10-")


def test_client_order_id_differs_by_params():
    from broker import Broker
    coid = Broker.__new__(Broker)._coid
    assert coid("AAPL", "buy", 10) != coid("AAPL", "buy", 11)   # 수량 다름
    assert coid("AAPL", "buy", 10) != coid("AAPL", "sell", 10)  # 방향 다름
    assert coid("AAPL", "buy", 10) != coid("MSFT", "buy", 10)   # 종목 다름


# ─────────────────────────────────────────────────────────────────────────────
# broker: validate_fill — 미확정 주문 보류·부분 체결·슬리피지 가드
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_fill_confirmed_full_fill_ok():
    from broker import validate_fill
    res = {"status": "filled", "fill_price": 100.0, "filled_qty": 10}
    v = validate_fill(res, est_price=100.0, req_shares=10)
    assert v["ok"] and v["fill_price"] == 100.0 and v["filled_qty"] == 10
    assert v["warning"] == ""


def test_validate_fill_unconfirmed_status_blocks_recording():
    from broker import validate_fill
    # 시장가 주문이 즉시 체결되지 않고 'accepted'에 머물면 장부에 기록하면 안 됨
    res = {"status": "accepted", "fill_price": 0.0, "filled_qty": 0}
    v = validate_fill(res, est_price=100.0, req_shares=10)
    assert not v["ok"]
    assert v["fill_price"] == 0.0 and v["filled_qty"] == 0
    assert "체결 미확인" in v["warning"]


def test_validate_fill_zero_price_blocks_even_if_status_filled():
    from broker import validate_fill
    # 방어적: status는 filled인데 가격/수량이 비정상이면 신뢰하지 않음
    res = {"status": "filled", "fill_price": 0.0, "filled_qty": 0}
    v = validate_fill(res, est_price=100.0, req_shares=10)
    assert not v["ok"]


def test_validate_fill_partial_returns_actual_qty_with_warning():
    from broker import validate_fill
    res = {"status": "partially_filled", "fill_price": 100.0, "filled_qty": 4}
    v = validate_fill(res, est_price=100.0, req_shares=10)
    assert v["ok"]
    assert v["filled_qty"] == 4          # 요청 수량이 아닌 실제 체결 수량
    assert "부분 체결" in v["warning"]
    assert "4/10" in v["warning"]


def test_validate_fill_large_slippage_warns_but_still_ok():
    from broker import validate_fill
    # 예상가 대비 +15% 괴리 → 기록은 하되 경고를 띄움
    res = {"status": "filled", "fill_price": 115.0, "filled_qty": 10}
    v = validate_fill(res, est_price=100.0, req_shares=10)
    assert v["ok"] and v["fill_price"] == 115.0
    assert "슬리피지" in v["warning"]


def test_validate_fill_small_slippage_no_warning():
    from broker import validate_fill
    # 기본 임계치(8%) 이내면 경고 없음
    res = {"status": "filled", "fill_price": 102.0, "filled_qty": 10}
    v = validate_fill(res, est_price=100.0, req_shares=10)
    assert v["ok"] and v["warning"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# risk_guard: 일일 손실 kill switch
# ─────────────────────────────────────────────────────────────────────────────

def _isolate_risk_guard():
    """day_state.json 을 임시 경로로 격리."""
    import risk_guard
    risk_guard.DAY_FILE = Path(tempfile.mktemp(suffix=".json"))
    return risk_guard


def test_killswitch_not_triggered_within_limit():
    rg = _isolate_risk_guard()
    rg.start_of_day(10000)
    assert rg.check(9800, loss_limit=0.05)["halted"] is False  # -2%
    rg.DAY_FILE.exists() and rg.DAY_FILE.unlink()


def test_killswitch_triggers_and_persists():
    rg = _isolate_risk_guard()
    rg.start_of_day(10000)
    assert rg.check(9400, loss_limit=0.05)["halted"] is True    # -6%
    assert rg.is_halted() is True                                # 한번 켜지면 유지
    rg.reset()
    assert rg.is_halted() is False
    rg.DAY_FILE.exists() and rg.DAY_FILE.unlink()


def test_killswitch_daily_pnl_calc():
    rg = _isolate_risk_guard()
    rg.start_of_day(10000)
    st = rg.check(10500, loss_limit=0.05)
    assert abs(st["daily_pnl_pct"] - 0.05) < 1e-9               # +5%
    rg.DAY_FILE.exists() and rg.DAY_FILE.unlink()


def test_killswitch_anchor_uses_start_equity():
    """start_equity(전일 종가 자산)를 앵커로 쓰면, 장중 늦게 켜도 손실률이 정확."""
    rg = _isolate_risk_guard()
    # 현재 9000이지만 전일 종가는 10000 → 당일 -10%로 평가돼야 함
    st = rg.check(9000, loss_limit=0.05, start_equity=10000)
    assert abs(st["start_equity"] - 10000) < 1e-9
    assert abs(st["daily_pnl_pct"] - (-0.10)) < 1e-9
    assert st["halted"] is True
    rg.DAY_FILE.exists() and rg.DAY_FILE.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# price_alerts: 조건부 가격 알림
# ─────────────────────────────────────────────────────────────────────────────

def _isolate_alerts():
    import price_alerts
    price_alerts.ALERTS_FILE = Path(tempfile.mktemp(suffix=".json"))
    return price_alerts


def test_alert_above_triggers():
    pa = _isolate_alerts()
    pa.add("AAPL", "above", 300, "목표")
    fired = pa.check({"AAPL": 305})
    assert len(fired) == 1 and fired[0]["ticker"] == "AAPL"
    # 한번 발동되면 재발동 안 함
    assert pa.check({"AAPL": 310}) == []
    pa.ALERTS_FILE.exists() and pa.ALERTS_FILE.unlink()


def test_alert_below_triggers():
    pa = _isolate_alerts()
    pa.add("TSLA", "below", 200)
    assert pa.check({"TSLA": 210}) == []     # 아직 위
    assert len(pa.check({"TSLA": 195})) == 1 # 이탈
    pa.ALERTS_FILE.exists() and pa.ALERTS_FILE.unlink()


def test_alert_dedup_on_add():
    pa = _isolate_alerts()
    pa.add("NVDA", "above", 500)
    pa.add("NVDA", "above", 500)   # 같은 조건 재추가
    assert len(pa.load()) == 1
    pa.ALERTS_FILE.exists() and pa.ALERTS_FILE.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# market_hours: 시장 시간 판별
# ─────────────────────────────────────────────────────────────────────────────

def test_market_closed_on_weekend():
    import market_hours as mh
    from datetime import datetime
    import pytz
    et = pytz.timezone("America/New_York")
    sat = et.localize(datetime(2026, 6, 6, 12, 0))   # 토요일 정오
    assert mh.is_market_open(sat) is False


def test_market_open_weekday_noon():
    import market_hours as mh
    from datetime import datetime
    import pytz
    et = pytz.timezone("America/New_York")
    wed = et.localize(datetime(2026, 6, 3, 12, 0))   # 수요일 정오
    assert mh.is_market_open(wed) is True


def test_market_closed_before_open():
    import market_hours as mh
    from datetime import datetime
    import pytz
    et = pytz.timezone("America/New_York")
    early = et.localize(datetime(2026, 6, 3, 8, 0))  # 수요일 08:00 (개장 전)
    assert mh.is_market_open(early) is False


# ─────────────────────────────────────────────────────────────────────────────
# portfolio: 매수/매도 장부 정합성
# ─────────────────────────────────────────────────────────────────────────────

def _isolate_portfolio():
    """state.json / trades.json 을 고유 임시 디렉터리로 격리."""
    import portfolio
    d = Path(tempfile.mkdtemp())
    portfolio.STATE_FILE = d / "state.json"
    return portfolio


def _score(ticker, total):
    from scorer import StockScore
    return StockScore(ticker, total, 0, 0, 0, 0)


def test_portfolio_record_and_pnl():
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=70)
    assert "AAPL" in pm.positions
    assert pm.positions["AAPL"].shares == 10
    assert pm.positions["AAPL"].entry_price == 100.0
    # 재로드해도 유지 (원자적 저장 확인)
    pm2 = portfolio.PortfolioManager()
    assert pm2.positions["AAPL"].shares == 10
    pm2.record_sell("AAPL", exit_price=120.0, reason="manual")
    assert "AAPL" not in pm2.positions


def test_paper_and_live_books_are_isolated():
    """모의(paper)와 실거래(live) 장부가 절대 섞이지 않아야 한다."""
    portfolio = _isolate_portfolio()
    live  = portfolio.PortfolioManager(paper=False)
    paper = portfolio.PortfolioManager(paper=True)
    live.record_buy("AAPL", shares=10, price=100.0, score=70)
    paper.record_buy("TSLA", shares=5, price=200.0, score=60)
    # 서로 다른 파일을 써야 함
    assert live.state_file != paper.state_file
    assert live.trade_file != paper.trade_file
    # 재로드 시 각자 자기 종목만 보유
    assert set(portfolio.PortfolioManager(paper=False).positions) == {"AAPL"}
    assert set(portfolio.PortfolioManager(paper=True).positions) == {"TSLA"}


def test_paper_trades_logged_separately():
    """페이퍼 매도 내역은 trades_paper.json 에만 기록된다."""
    portfolio = _isolate_portfolio()
    paper = portfolio.PortfolioManager(paper=True)
    paper.record_buy("NVDA", shares=4, price=50.0, score=70)
    paper.record_sell("NVDA", exit_price=60.0, reason="take_profit")
    live_trades  = safe_read_json_local(portfolio.STATE_FILE.parent / "trades.json")
    paper_trades = safe_read_json_local(portfolio.STATE_FILE.parent / "trades_paper.json")
    assert len(paper_trades.get("trades", [])) == 1
    assert live_trades.get("trades", []) == []   # 실거래 장부는 비어 있어야 함


def test_generate_orders_respects_available_override():
    """모의 가용 현금(available_override)이 매수 규모를 제한해야 한다."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager(paper=True)
    scores = [_score("AAPL", 90)]
    prices = {"AAPL": 100.0}
    # 가용 현금을 $150 로 제한 → 최대 1주(=$100)만 매수 가능
    orders = pm.generate_orders(scores, prices, available_override=150.0)
    bought = sum(o["shares"] for o in orders["buy"])
    assert bought <= 1


def test_record_sell_partial_keeps_remainder():
    """분할 매도: shares 지정 시 그만큼만 차감하고 포지션은 유지 (장부 desync 방지)."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=70)
    pm.record_sell("AAPL", exit_price=110.0, reason="score_drop", shares=4)
    assert "AAPL" in pm.positions                      # 아직 보유 중
    assert pm.positions["AAPL"].shares == 6             # 6주 남음
    assert pm.positions["AAPL"].entry_price == 100.0    # 평단가 유지
    # 재로드해도 6주 유지
    assert portfolio.PortfolioManager().positions["AAPL"].shares == 6
    # trades.json 에는 실제 매도 수량(4)이 기록됨
    trades = safe_read_json_local(portfolio.STATE_FILE.parent / "trades.json")
    assert trades["trades"][-1]["shares"] == 4


def test_record_sell_full_removes_position():
    """shares=None (기본) 이면 전량 매도 → 포지션 삭제."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=70)
    pm.record_sell("AAPL", exit_price=120.0, reason="take_profit")
    assert "AAPL" not in pm.positions


def test_record_buy_averages_in():
    """추가 매수(물타기) → 가중평균 평단가로 누적."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=70)
    pm.record_buy("AAPL", shares=10, price=120.0, score=80)
    assert pm.positions["AAPL"].shares == 20
    assert abs(pm.positions["AAPL"].entry_price - 110.0) < 1e-9   # (1000+1200)/20


def test_generate_orders_split_sell_pct():
    """분할 매도: sell_pct(1~100%)만큼만 매도, 손절 아니면 슬롯 반환 안 함."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=70)
    _backdate(pm, "AAPL")
    scores = [_score("AAPL", 20.0)]   # 점수 급락 → score_drop (손절 아님)
    # 30% 매도 → 3주
    o30 = pm.generate_orders(scores, {"AAPL": 100.0}, sell_mode="분할", sell_pct=0.30)
    assert o30["sell"][0]["shares"] == 3
    # 50% 매도 → 5주
    o50 = pm.generate_orders(scores, {"AAPL": 100.0}, sell_mode="분할", sell_pct=0.50)
    assert o50["sell"][0]["shares"] == 5
    # 100% 매도 → 10주
    o100 = pm.generate_orders(scores, {"AAPL": 100.0}, sell_mode="분할", sell_pct=1.0)
    assert o100["sell"][0]["shares"] == 10


def test_generate_orders_stop_loss_always_full():
    """손절은 분할 비율과 무관하게 항상 전량 매도."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=70)
    # -10% → stop_loss, 점수는 높게 줘서 score_drop 배제
    orders = pm.generate_orders([_score("AAPL", 90.0)], {"AAPL": 90.0},
                                sell_mode="분할", sell_pct=0.30)
    assert orders["sell"][0]["shares"] == 10           # 전량


def test_generate_orders_split_buy_tops_up_existing():
    """분할 매수: 기존 포지션이 목표 비중 미달이면 추가 매수 주문 생성."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=90)  # cost_basis 1000 < target 2500
    orders = pm.generate_orders([_score("AAPL", 90.0)], {"AAPL": 100.0},
                                buy_mode="분할", buy_pct=0.50)
    aapl_buys = [b for b in orders["buy"] if b["ticker"] == "AAPL"]
    assert len(aapl_buys) == 1                          # 물타기 주문 생성됨
    assert aapl_buys[0]["shares"] >= 1


def test_trailing_stop_lets_winner_run_then_exits():
    """트레일링 스탑: 목표 도달 후 고점 근처면 보유 유지, 고점 대비 되돌리면 전량 청산."""
    portfolio = _isolate_portfolio()
    portfolio.TAKE_PROFIT_PCT = 0.20
    portfolio.STOP_LOSS_PCT = 0.07
    portfolio.TRAIL_GIVEBACK_PCT = 0.08
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=90)

    # 1) +30% 까지 상승 → 목표(+20%) 넘었지만 고점 근처 → 청산 안 함(승자 유지)
    o = pm.generate_orders([_score("AAPL", 90.0)], {"AAPL": 130.0})
    assert o["sell"] == [], "고점 근처에선 트레일링 청산하지 않아야 함"
    assert pm.positions["AAPL"].peak_price == 130.0       # 고점 갱신됨

    # 2) 고점(130) 대비 10% 하락(=117) → 8% 되돌림 초과 → 전량 청산
    o2 = pm.generate_orders([_score("AAPL", 90.0)], {"AAPL": 117.0}, sell_mode="분할", sell_pct=0.3)
    assert len(o2["sell"]) == 1
    assert o2["sell"][0]["shares"] == 10                  # 트레일링은 전량
    assert o2["sell"][0]["reason"].startswith("trailing_stop")


def test_trailing_stop_not_active_below_target():
    """목표 미달 구간에선 트레일링이 작동하지 않고 기존 로직(점수/기간) 따른다."""
    portfolio = _isolate_portfolio()
    portfolio.TAKE_PROFIT_PCT = 0.20
    portfolio.STOP_LOSS_PCT = 0.07
    portfolio.SELL_SCORE_THRESHOLD = 35
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=90)
    # +10% (목표 미달) & 점수 양호 → 청산 없음
    o = pm.generate_orders([_score("AAPL", 90.0)], {"AAPL": 110.0})
    assert o["sell"] == []


def test_peak_price_persists_across_reload():
    """고점은 디스크에 저장돼 재로드 후에도 트레일링 기준으로 유지된다."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=5, price=100.0, score=80)
    pm.generate_orders([_score("AAPL", 80.0)], {"AAPL": 140.0})   # 고점 140 기록
    reloaded = portfolio.PortfolioManager()
    assert reloaded.positions["AAPL"].peak_price == 140.0


def test_fundamental_normalizes_partial_data():
    """부분 지표만 있어도 가용 지표 기준으로 정규화 (이전 캡 버그 회귀 방지)."""
    import signals.fundamental as fnd

    class _Stub:
        def __init__(self, info): self.info = info
    # 매출 +30% '단일' 지표만 제공 → 완벽하므로 100 이어야 함 (이전엔 30)
    import yfinance as _yf
    _orig = _yf.Ticker
    _yf.Ticker = lambda t: _Stub({"revenueGrowth": 0.30})
    try:
        assert fnd.score("X") == 100.0
        _yf.Ticker = lambda t: _Stub({"revenueGrowth": 0.10})
        assert abs(fnd.score("X") - 33.3) < 0.5
        _yf.Ticker = lambda t: _Stub({})       # 데이터 전무 → 중립
        assert fnd.score("X") == 50.0
    finally:
        _yf.Ticker = _orig


def test_sentiment_lexicon_directional():
    """뉴스 어휘 채점이 방향성을 반영 (긍정>50, 부정<50)."""
    from signals.sentiment import _lexicon_score
    pos = _lexicon_score(["earnings beat, shares surge and upgrade"] * 8)
    neg = _lexicon_score(["revenue miss, shares plunge after downgrade"] * 8)
    assert pos is not None and pos > 55
    assert neg is not None and neg < 45
    assert _lexicon_score(["company holds routine meeting"]) is None


def test_generate_orders_full_buy_skips_held():
    """전량 매수 모드: 기존 보유 종목은 추가 매수하지 않음."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAPL", shares=10, price=100.0, score=90)
    orders = pm.generate_orders([_score("AAPL", 90.0)], {"AAPL": 100.0},
                                buy_mode="전량")
    assert all(b["ticker"] != "AAPL" for b in orders["buy"])


def safe_read_json_local(path):
    from safe_store import safe_read_json
    return safe_read_json(path, default={"trades": []})


# ─────────────────────────────────────────────────────────────────────────────
# 멀티브로커 기반: 토스 어댑터 골격 + 팩토리
# ─────────────────────────────────────────────────────────────────────────────

def test_toss_not_configured_by_default():
    """플레이스홀더 키 상태에서는 configured() False (실수로 미연동 동작 방지)."""
    from toss_broker import TossBroker
    assert TossBroker.configured() is False
    # 미연동이면 조회류는 빈 값으로 graceful
    tb = TossBroker(paper=True)
    assert tb.get_prices(["AAPL"]) == {}
    assert tb.get_positions() == {}
    assert tb.get_account()["equity"] == 0.0


def test_toss_domestic_routing():
    """국내/해외 종목 라우팅: 6자리 숫자·.KS/.KQ → 국내, 그 외 → 해외."""
    from toss_broker import TossBroker
    assert TossBroker._is_domestic("005930") is True      # 삼성전자
    assert TossBroker._is_domestic("034220.KS") is True
    assert TossBroker._is_domestic("035720.KQ") is True
    assert TossBroker._is_domestic("AAPL") is False
    assert TossBroker._is_domestic("TSLA") is False


def test_toss_order_blocked_until_launch():
    """정식 출시 전 주문류는 명확히 예외 (가짜 체결 방지)."""
    from toss_broker import TossBroker
    tb = TossBroker(paper=True)
    raised = False
    try:
        tb.place_buy("005930", 1)
    except Exception:
        raised = True
    assert raised is True


def test_paper_account_cash_flow():
    """모의 가상 현금: 초기화·매수 차감·매도 증가."""
    import paper_account as P
    P.PAPER_FILE = Path(tempfile.mktemp(suffix=".json"))
    P.reset(10000)
    assert P.cash() == 10000.0
    assert P.adjust(-100 * 50.0) == 5000.0     # 매수 100주 @ $50
    assert P.adjust(40 * 55.0) == 7200.0       # 매도 40주 @ $55
    P.reset()
    assert P.cash() > 0                          # 기본 자본으로 복구


def test_make_broker_selects_toss(monkeypatch=None):
    """BROKER=toss 면 팩토리가 TossBroker 를 반환 (Alpaca 키 없이 검증)."""
    import config, broker
    _orig = config.BROKER
    config.BROKER = "toss"
    try:
        b = broker.make_broker(paper=True)
        assert type(b).__name__ == "TossBroker"
    finally:
        config.BROKER = _orig


# ─────────────────────────────────────────────────────────────────────────────
# 퀀트 기법: 신규 팩터 전략 + 변동성 타겟 사이징 (네트워크 없이 합성 데이터)
# ─────────────────────────────────────────────────────────────────────────────

def _synth(seed, drift, noise, n=320):
    import pandas as pd, numpy as np
    idx = pd.date_range("2022-01-01", periods=n, freq="B")
    p = np.linspace(100, 100 * (1 + drift), n) + np.random.RandomState(seed).normal(0, noise, n)
    p = np.clip(p, 1, None)
    return pd.DataFrame({"Close": p, "High": p * 1.01, "Low": p * 0.99,
                         "Volume": np.full(n, 1e6)}, index=idx)


def test_quant_strategies_registered():
    """신규 퀀트 전략이 카탈로그·청산 프로파일에 등록돼 있어야 한다."""
    import strategy_catalog as scat
    for k in ("risk_adj_momentum", "quant_multifactor"):
        assert k in scat.CATALOG
        assert scat.exit_profile(k) is not None
        assert k in scat.RULES


def test_risk_adj_momentum_prefers_smooth_uptrend():
    """샤프 모멘텀: 같은 상승폭이면 덜 흔들린 종목에 더 높은 점수."""
    import backtester as bt
    smooth = _synth(1, 0.6, 1.5)    # 저변동 우상향
    choppy = _synth(2, 0.6, 12.0)   # 고변동 우상향
    sd = {"SMOOTH": smooth, "CHOPPY": choppy}
    s = bt._strategy_score_bt("risk_adj_momentum", "SMOOTH", sd, {}, 300)
    c = bt._strategy_score_bt("risk_adj_momentum", "CHOPPY", sd, {}, 300)
    assert s > c


def test_quant_multifactor_prefers_low_vol_quality():
    """멀티팩터: 저변동·저낙폭 우상향이 고변동보다 높은 점수."""
    import backtester as bt
    quality = _synth(3, 0.5, 1.0)
    noisy   = _synth(4, 0.5, 14.0)
    sd = {"Q": quality, "N": noisy}
    assert bt._strategy_score_bt("quant_multifactor", "Q", sd, {}, 300) > \
           bt._strategy_score_bt("quant_multifactor", "N", sd, {}, 300)


def test_risk_sizing_shrinks_high_vol_position():
    """변동성 타겟 사이징: 고변동 종목은 동일비중 대비 더 적은 수량으로 담는다."""
    import backtester as bt
    import pandas as pd
    lo = _synth(1, 0.5, 1.0); hi = _synth(2, 0.5, 9.0)
    pre = {"stock_data": {"LO": lo, "HI": hi}, "etf_data": {},
           "date_index": lo.index, "spy_close": None, "spy_ma200": None, "spy_ma50": None}
    common = dict(start="2022-01-01", end="2023-03-01", capital=10000,
                  universe=["LO", "HI"], strategy="momentum", max_positions=2,
                  min_score=30, use_trend_filter=False, adaptive_regime=False, prefetched=pre)
    rp = bt.run(risk_sizing=True, vol_target=0.25, **common)

    def first_shares(res, tk):
        for t in res.trades:
            if t.ticker == tk:
                return t.shares
        return None
    hi_sh = first_shares(rp, "HI"); lo_sh = first_shares(rp, "LO")
    assert hi_sh is not None and lo_sh is not None
    assert hi_sh < lo_sh, "고변동 종목이 저변동보다 적게 담겨야 함(리스크 패리티)"


# ─────────────────────────────────────────────────────────────────────────────
# 시장 자동 발굴(스크리너) + 워치리스트 출처 관리
# ─────────────────────────────────────────────────────────────────────────────

def _isolate_watchlist():
    """워치리스트 파일을 임시경로로 격리. (테스트 후 자동 정리 X — mktemp)"""
    import watchlist as wl
    wl.WATCHLIST_FILE = Path(tempfile.mktemp(suffix=".json"))
    return wl


def test_watchlist_legacy_migrates_to_manual():
    """레거시({stocks})는 전부 manual 로 승격돼 자동 정리에서 보호된다."""
    import json
    wl = _isolate_watchlist()
    wl.WATCHLIST_FILE.write_text(json.dumps({"stocks": ["AAPL", "MSFT", "TSLA"]}))
    full = wl._load_full()
    assert full["manual"] == ["AAPL", "MSFT", "TSLA"]
    assert full["auto"] == [] and full["held"] == []


def test_watchlist_sync_holdings_keeps_held_in_universe():
    """보유종목은 워치리스트에 없어도 동기화로 항상 유니버스에 포함된다."""
    wl = _isolate_watchlist()
    wl.save(["AAPL"])              # manual=AAPL
    wl.sync_holdings(["AAPL", "NVDA"])
    assert "NVDA" in wl.load(), "보유종목이 유니버스에 편입돼야 함(강제매도 방지)"


def test_watchlist_held_cannot_be_removed():
    """보유 중인 종목은 remove() 로 지울 수 없다(강제 매도 방지)."""
    wl = _isolate_watchlist()
    wl.sync_holdings(["NVDA"])
    assert wl.remove("NVDA") is False
    assert "NVDA" in wl.load()


def test_apply_screen_preserves_manual_and_held():
    """자동 편입은 manual/held 를 건드리지 않고 auto 슬롯만 채운다."""
    wl = _isolate_watchlist()
    wl.save(["AAPL", "MSFT"])           # manual
    wl.sync_holdings(["NVDA"])          # held
    summ = wl.apply_screen(
        [{"ticker": "GOOGL", "score": 80}, {"ticker": "AMD", "score": 75},
         {"ticker": "AAPL", "score": 99},   # manual 중복 → 무시
         {"ticker": "NVDA", "score": 90}],  # held 중복 → 무시
        cap=50)
    full = wl._load_full()
    assert "GOOGL" in full["auto"] and "AMD" in full["auto"]
    assert "AAPL" not in full["auto"] and "NVDA" not in full["auto"]
    assert set(full["manual"]) == {"AAPL", "MSFT"}
    assert full["held"] == ["NVDA"]
    assert summ["auto_count"] == 2


def test_apply_screen_respects_cap():
    """워치리스트 상한(cap)은 보유·수동 포함 전체에 적용된다."""
    wl = _isolate_watchlist()
    wl.save(["AAPL", "MSFT"])           # manual 2
    wl.sync_holdings(["NVDA"])          # held 1 → 보호 3
    disc = [{"ticker": f"T{i}", "score": 50 + i} for i in range(40)]
    wl.apply_screen(disc, cap=10)
    assert len(wl.load()) <= 10


def test_apply_screen_promotes_higher_scores_first():
    """발굴 후보 중 점수 높은 순으로 자동 슬롯을 채운다."""
    wl = _isolate_watchlist()
    wl.save([])  # manual 없음
    disc = [{"ticker": "LOW", "score": 60}, {"ticker": "HIGH", "score": 95},
            {"ticker": "MID", "score": 75}]
    wl.apply_screen(disc, cap=2)
    auto = wl._load_full()["auto"]
    assert auto[:2] == ["HIGH", "MID"], "고점수 우선 편입"


def test_screener_prefilter_prefers_smooth_uptrend():
    """프리필터: 같은 추세면 위험조정 모멘텀이 높은(덜 흔들린) 종목 우선."""
    import screener as scr
    import pandas as pd, numpy as np

    def make(drift, noise, base=100.0, n=300):
        rng = np.random.default_rng(7)
        rets = drift + noise * rng.standard_normal(n)
        close = base * np.exp(np.cumsum(rets))
        return pd.DataFrame({"Close": close, "Volume": np.full(n, 2e6)})

    smooth = scr._prefilter_score(make(0.0008, 0.008))
    choppy = scr._prefilter_score(make(0.0008, 0.030))
    down   = scr._prefilter_score(make(-0.0008, 0.012))
    assert smooth["score"] > choppy["score"]
    assert smooth["score"] > down["score"]
    assert down["score"] < 50


def test_screener_excludes_penny_and_short_history():
    """동전주(<$5)·짧은 히스토리(<60봉)는 부적격으로 배제."""
    import screener as scr
    import pandas as pd, numpy as np
    penny = pd.DataFrame({"Close": np.full(200, 2.0), "Volume": np.full(200, 1e6)})
    short = pd.DataFrame({"Close": np.full(40, 100.0), "Volume": np.full(40, 1e6)})
    assert scr._prefilter_score(penny) is None
    assert scr._prefilter_score(short) is None


def test_screener_should_run_time_gate():
    """should_run: 스캔 이력 없으면 True, 방금 스캔했으면 False."""
    import screener as scr
    wl = _isolate_watchlist()
    wl.save(["AAPL"])
    assert scr.should_run(3600) is True            # 이력 없음 → 실행
    wl.apply_screen([{"ticker": "AMD", "score": 70}], cap=50)  # auto_ts 갱신
    assert scr.should_run(3600) is False           # 방금 스캔 → 대기


# ─────────────────────────────────────────────────────────────────────────────
# 유동형(동적 배분) 주문
# ─────────────────────────────────────────────────────────────────────────────

def test_dynamic_alloc_weights_by_conviction():
    """빈 포트폴리오: 신호가 강할수록 큰 비중, 종목당 상한(25%) 준수."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    scores = [_score("AAA", 90), _score("BBB", 75), _score("CCC", 62),
              _score("DDD", 40)]   # DDD는 매수문턱(60) 미달
    prices = {"AAA": 100, "BBB": 50, "CCC": 25, "DDD": 10}
    o = pm.generate_orders(scores, prices, dynamic=True, available_override=10000)
    val = {b["ticker"]: b["shares"] * b["est_price"] for b in o["buy"]}
    assert "DDD" not in val, "문턱 미달은 매수 안 함"
    assert val["AAA"] >= val["BBB"] >= val["CCC"], "점수 높을수록 큰 비중"
    cap = portfolio.CAPITAL_TOTAL * portfolio.MAX_POSITION_PCT
    for t, v in val.items():
        assert v <= cap + 100, f"{t} 종목 상한 초과: {v} > {cap}"


def test_dynamic_alloc_respects_seed_cap():
    """매수 총액이 시드(가용 현금)를 넘지 않는다."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    scores = [_score("AAA", 90), _score("BBB", 85), _score("CCC", 80),
              _score("DDD", 75), _score("EEE", 70)]
    prices = {t: 30 for t in ["AAA", "BBB", "CCC", "DDD", "EEE"]}
    seed = 5000
    o = pm.generate_orders(scores, prices, dynamic=True, available_override=seed)
    spent = sum(b["shares"] * b["est_price"] for b in o["buy"])
    assert spent <= seed + 1e-6, f"시드 초과 매수: {spent} > {seed}"


def _backdate(pm, ticker, days=1):
    """포지션 진입일을 과거로 — 최소 보유일(MIN_HOLD_DAYS) 가드 통과용."""
    from datetime import date as _d, timedelta as _td
    pm.positions[ticker].entry_date = (_d.today() - _td(days=days)).isoformat()
    pm._save_state()


def test_dynamic_alloc_trims_overweight():
    """보유가 목표 비중보다 크게 초과하면 그 차액만큼 트림(매도)."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("AAA", shares=40, price=100, score=90)   # $4,000 (목표 $2,500 초과)
    _backdate(pm, "AAA")
    o = pm.generate_orders([_score("AAA", 90), _score("BBB", 80)],
                           {"AAA": 100, "BBB": 50},
                           dynamic=True, available_override=2000)
    trims = [s for s in o["sell"] if "trim" in s["reason"] and s["ticker"] == "AAA"]
    assert trims, "과비중 AAA는 트림되어야 함"


def test_dynamic_alloc_rotates_out_of_target():
    """목표에서 빠진 약한 보유는 회전 청산(자본 회전)."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("OLD", shares=50, price=100, score=70)
    _backdate(pm, "OLD")
    # OLD 점수 40(문턱 미달이나 손절문턱 35 이상이라 should_sell엔 안 걸림) → 회전 대상
    o = pm.generate_orders([_score("NEW", 85), _score("OLD", 40)],
                           {"OLD": 100, "NEW": 50},
                           dynamic=True, available_override=0)
    assert any(s["ticker"] == "OLD" and "rotate" in s["reason"] for s in o["sell"]), \
        "목표 이탈 OLD는 회전 청산"


def test_reentry_blocked_until_price_moves():
    """판 가격 그대로면 재매수 금지, 1% 이상 움직이면 재진입 허용
    (주가 변화 없는 매도↔매수 반복 차단의 핵심)."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("HYS", shares=10, price=100, score=70)
    pm.record_sell("HYS", exit_price=100.0, reason="rotate (목표 이탈)")
    scores = [_score("HYS", 90)]
    # 청산가와 거의 같은 가격(+0.3%) → 양쪽 모드 모두 재매수 차단
    o_dyn = pm.generate_orders(scores, {"HYS": 100.3}, dynamic=True,
                               available_override=10000)
    assert not o_dyn["buy"], "가격 변화 미미 → 재매수 금지(유동형)"
    o_fix = pm.generate_orders(scores, {"HYS": 100.3}, available_override=10000)
    assert not o_fix["buy"], "가격 변화 미미 → 재매수 금지(고정형)"
    # 1% 이상 움직이면(신호가 유효해지면) 재진입 허용
    o_mv = pm.generate_orders(scores, {"HYS": 102.0}, dynamic=True,
                              available_override=10000)
    assert o_mv["buy"], "가격이 충분히 움직이면 재진입 가능"


def test_rotate_requires_score_margin():
    """회전 청산은 교체 이득이 분명할 때만 — 신규 후보가 보유보다
    ROTATE_MARGIN 이상 높지 않으면 멀쩡한 보유를 팔지 않는다."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("OLD", shares=50, price=100, score=70)
    # OLD 55(문턱 60 미달 → 목표 이탈) vs NEW 62 → 차이 7 < 마진 10 → 회전 안 함
    o = pm.generate_orders([_score("NEW", 62), _score("OLD", 55)],
                           {"OLD": 100, "NEW": 50},
                           dynamic=True, available_override=0)
    assert not any(s["ticker"] == "OLD" for s in o["sell"]), \
        "점수 차이가 작으면 회전하지 않아야 함"
    # NEW 85 → 차이 30 ≥ 10 → 회전 실행
    o2 = pm.generate_orders([_score("NEW", 85), _score("OLD", 55)],
                            {"OLD": 100, "NEW": 50},
                            dynamic=True, available_override=0)
    assert any(s["ticker"] == "OLD" and "rotate" in s["reason"] for s in o2["sell"])


def test_dynamic_alloc_keeps_running_winner():
    """목표에서 빠져도, 목표수익 도달한 승자는 트레일링이 관리 → 회전 제외."""
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    pm.record_buy("WIN", shares=10, price=100, score=70)
    pm.positions["WIN"].peak_price = 140.0     # 고점 +40% (목표 도달)
    o = pm.generate_orders([_score("NEW", 85), _score("WIN", 40)],
                           {"WIN": 135, "NEW": 50},   # 현재 +35% (승자, 트레일링 미발동)
                           dynamic=True, available_override=0)
    assert not any(s["ticker"] == "WIN" for s in o["sell"]), \
        "달리는 승자는 회전하지 않아야 함(트레일링 스탑이 관리)"


# ─────────────────────────────────────────────────────────────────────────────
# 매수 가격대 제한 + 손절 후 재진입 쿨다운
# ─────────────────────────────────────────────────────────────────────────────

def test_price_band_filters_new_buys():
    """상·하한가 밖 종목은 신규 매수에서 제외 (보유 종목은 영향 없음)."""
    portfolio = _isolate_portfolio()
    portfolio.BUY_PRICE_MIN = 50.0
    portfolio.BUY_PRICE_MAX = 500.0
    try:
        pm = portfolio.PortfolioManager()
        scores = [_score("CHEAP", 90), _score("MID", 85), _score("PRICY", 80)]
        prices = {"CHEAP": 10.0, "MID": 100.0, "PRICY": 1200.0}
        o = pm.generate_orders(scores, prices, dynamic=True, available_override=10000)
        bought = {b["ticker"] for b in o["buy"]}
        assert "MID" in bought, "가격대 내 종목은 매수"
        assert "CHEAP" not in bought and "PRICY" not in bought, "가격대 밖 제외"
    finally:
        portfolio.BUY_PRICE_MIN = 0.0
        portfolio.BUY_PRICE_MAX = 0.0


def test_reentry_cooldown_blocks_rebuy_after_stop_loss():
    """손절 직후 같은 종목 재매수 금지 (휩쏘 방지) — 쿨다운 지나면 허용."""
    import json
    portfolio = _isolate_portfolio()
    pm = portfolio.PortfolioManager()
    # 오늘 손절 기록 주입
    pm.trade_file.write_text(json.dumps({"trades": [{
        "ticker": "WHIP", "entry_date": "2026-06-01",
        "exit_date": date.today().isoformat(),
        "entry_price": 100, "exit_price": 90, "shares": 5, "reason": "stop_loss"}]}))
    o = pm.generate_orders([_score("WHIP", 90), _score("OK", 85)],
                           {"WHIP": 95.0, "OK": 50.0},
                           dynamic=True, available_override=10000)
    bought = {b["ticker"] for b in o["buy"]}
    assert "WHIP" not in bought, "손절 직후 재매수 금지"
    assert "OK" in bought


def test_telegram_buy_sell_parse_and_guard():
    """봇 매매 명령: 금액→수량 환산, 미설정 채팅 차단, /confirm 없는 상태."""
    import telegram_bot as tg
    # 매매 가드: allowed 미설정이면 차단
    tg._allowed_chat = ""
    assert "⛔" in tg.handle("/buy AAPL 500", "123")
    # allowed 설정 + 시세 mock
    tg._allowed_chat = "123"
    _orig = tg._quote
    tg._quote = lambda tk: 100.0
    try:
        r = tg.cmd_buy(["AAPL", "550"], "123")
        assert "5주" in r and "/confirm" in r, r          # $550/$100 → 5주
        r2 = tg.cmd_buy(["AAPL", "3주"], "123")
        assert "3주" in r2
        assert "취소" in tg.cmd_cancel("123")
        assert "대기 중인 주문이 없" in tg.cmd_confirm("123")
    finally:
        tg._quote = _orig
        tg._allowed_chat = ""
        tg._pending.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 국면 적응형(regime-switching) 메타 전략 + 전략 상세 설명
# ─────────────────────────────────────────────────────────────────────────────

def test_adaptive_registered():
    """적응형 전략이 카탈로그·규칙·청산프로파일·카테고리에 등록돼 있어야 한다."""
    import strategy_catalog as scat
    assert "adaptive" in scat.CATALOG
    assert "adaptive" in scat.RULES
    assert scat.exit_profile("adaptive") is not None
    assert "적응형" in scat.CATEGORY_ORDER


def test_all_strategies_have_detail():
    """모든 전략이 상세 논리(detail) 문자열을 가져야 한다 — UI 노출용."""
    import strategy_catalog as scat
    for k in scat.CATALOG:
        d = scat.detail(k)
        assert isinstance(d, str) and len(d) >= 50, f"{k} detail 부족: {d!r}"


def test_adaptive_delegates_to_momentum_in_uptrend():
    """강세 추세(가격>50일>200일)에서는 샤프 모멘텀과 동일 점수로 위임."""
    import backtester as bt
    up = _synth(11, 0.6, 1.2)            # 저변동 우상향 → px>ma50>ma200
    sd = {"UP": up}; idx = len(up) - 1
    a   = bt._strategy_score_bt("adaptive", "UP", sd, {}, idx)
    ram = bt._strategy_score_bt("risk_adj_momentum", "UP", sd, {}, idx)
    assert abs(a - ram) < 0.05, f"강세장 위임 불일치: adaptive={a} ram={ram}"


def test_adaptive_guards_insufficient_history():
    """데이터가 252봉 미만이면 0점(과최적화·오작동 방지)."""
    import backtester as bt
    up = _synth(12, 0.5, 1.0)
    assert bt._strategy_score_bt("adaptive", "UP", {"UP": up}, {}, 100) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 매수/매도 알림 포맷터
# ─────────────────────────────────────────────────────────────────────────────

def test_notify_buy_auto_shows_score_and_source():
    import notifier
    m = notifier._format_buy("AAPL", 3, 205.10, score=72, cost=615, source="자동")
    assert m["title"] == "매수 · AAPL"
    assert "스코어 72" in m["body"] and "자동" in m["body"] and "3주" in m["body"]


def test_notify_buy_manual_omits_score():
    import notifier
    m = notifier._format_buy("TSLA", 1, 391.0, score=None, cost=391, source="수동")
    assert "스코어" not in m["body"]
    assert "수동" in m["body"]


def test_notify_sell_take_profit_shows_pnl_arrow():
    import notifier
    m = notifier._format_sell("NVDA", 2, 215.3, pnl_pct=0.052,
                              reason="take_profit", source="자동")
    assert "▲5.2%" in m["title"] and "익절" in m["body"]


def test_notify_sell_maps_reason_key_with_suffix():
    """reason에 부가설명이 붙어도 첫 토큰(키)만으로 한글 라벨 매핑."""
    import notifier
    m = notifier._format_sell("AMD", 5, 100.0, pnl_pct=-0.03,
                              reason="trailing_stop (고점 +20%→현재 +12%)")
    assert "▼3.0%" in m["title"] and "트레일링 청산" in m["body"]


def test_notify_sell_manual_omits_pnl():
    import notifier
    m = notifier._format_sell("MSFT", 1, 416.0, pnl_pct=None, reason="manual")
    assert m["title"] == "매도 · MSFT"          # 손익률 화살표 없음
    assert "수동 청산" in m["body"]


def test_notify_messages_have_no_emoji():
    """알림 본문·제목에 이모지(장식 아이콘) 없음 — 앱 톤과 일치."""
    import notifier
    msgs = [
        notifier._format_buy("AAPL", 3, 205.1, score=72, cost=615, source="자동"),
        notifier._format_sell("NVDA", 2, 215.3, pnl_pct=0.05,
                              reason="take_profit", source="자동"),
    ]
    for m in msgs:
        for ch in m["title"] + m["body"]:
            assert ord(ch) < 0x1F000, f"emoji found: {ch!r}"


def test_notify_dispatch_respects_disabled_channels():
    """모든 채널 비활성이면 발송 시도 자체가 없어야 한다(부작용 0)."""
    import notifier
    cfg = {"macos": {"enabled": False}, "telegram": {"enabled": False},
           "slack": {"enabled": False}, "kakao": {"enabled": False},
           "email": {"enabled": False}}
    res = notifier._dispatch({"title": "t", "body": "b", "color": "#000"}, cfg)
    assert res == {}


# ─────────────────────────────────────────────────────────────────────────────
# core.execution: 단일 주문 실행기
# ─────────────────────────────────────────────────────────────────────────────

def _isolate_execution():
    """장부·모의현금·주문로그를 전부 임시 경로로 격리."""
    import paper_account as P
    from core import execution as EX
    portfolio = _isolate_portfolio()
    P.PAPER_FILE = Path(tempfile.mktemp(suffix=".json"))
    EX.ORDERS_FILE = Path(tempfile.mktemp(suffix=".json"))
    P.reset(10000)
    return portfolio, P, EX


def test_execute_orders_paper_fills_and_logs():
    """모의 체결: 매수→현금 차감+장부 기록+주문로그, 매도→현금 증가+포지션 정리."""
    portfolio, P, EX = _isolate_execution()
    pm = portfolio.PortfolioManager(paper=True)
    res = EX.execute_orders(
        {"sell": [], "buy": [{"ticker": "AAPL", "shares": 10, "score": 80,
                              "est_price": 100.0, "est_cost": 1000.0}]},
        {"AAPL": 100.0}, paper=True, pm=pm)
    assert len(res["bought"]) == 1
    assert P.cash() == 9000.0
    assert pm.positions["AAPL"].shares == 10
    logged = safe_read_json_local(EX.ORDERS_FILE)["orders"]
    assert logged[-1]["ticker"] == "AAPL" and logged[-1]["side"] == "buy"
    res2 = EX.execute_orders(
        {"sell": [{"ticker": "AAPL", "shares": 10, "reason": "manual",
                   "est_price": 110.0}], "buy": []},
        {"AAPL": 110.0}, paper=True, pm=pm)
    assert len(res2["sold"]) == 1 and "AAPL" not in pm.positions
    assert P.cash() == 10100.0
    logged = safe_read_json_local(EX.ORDERS_FILE)["orders"]
    assert logged[-1]["side"] == "sell"


def test_execute_orders_blocks_overbuy():
    """가용 현금을 넘는 매수는 보류(skipped) — 과매수 차단."""
    portfolio, P, EX = _isolate_execution()
    P.reset(500)
    pm = portfolio.PortfolioManager(paper=True)
    res = EX.execute_orders(
        {"sell": [], "buy": [{"ticker": "NVDA", "shares": 10, "score": 90,
                              "est_price": 100.0, "est_cost": 1000.0}]},
        {"NVDA": 100.0}, paper=True, pm=pm)
    assert not res["bought"] and "NVDA" in res["skipped"]
    assert P.cash() == 500.0 and not pm.positions


def test_execute_manual_paper_roundtrip():
    """수동 주문 공용 함수: 매수→매도 왕복, 미보유 매도는 거부."""
    portfolio, P, EX = _isolate_execution()
    pm = portfolio.PortfolioManager(paper=True)
    r = EX.execute_manual("MSFT", 5, "buy", True, est_price=200.0, pm=pm)
    assert r["shares"] == 5 and P.cash() == 9000.0
    r2 = EX.execute_manual("MSFT", 5, "sell", True, est_price=220.0, pm=pm)
    assert r2["shares"] == 5 and abs(r2["pnl_pct"] - 0.10) < 1e-9
    assert P.cash() == 10100.0
    raised = False
    try:
        EX.execute_manual("TSLA", 1, "sell", True, est_price=100.0, pm=pm)
    except RuntimeError:
        raised = True
    assert raised


def test_tick_guard_fires_stop_loss_immediately():
    """틱 가드: 손절가 틱 수신 → 즉시 전량 매도. 정상가 틱·쿨다운은 무발동."""
    import market_hours as mh
    from core import control, guard
    portfolio, P, EX = _isolate_execution()
    control.CONFIG_FILE = Path(tempfile.mktemp(suffix=".json"))
    control.CONFIG_FILE.write_text(json.dumps(
        {"enabled": True, "paper": True, "stop_loss": 0.07,
         "take_profit": 0.15, "trail": 0.07}))
    guard._cfg_cache = (0.0, None)
    guard._last_fire.clear()
    _orig_open = mh.is_market_open
    mh.is_market_open = lambda: True
    try:
        pm = portfolio.PortfolioManager(paper=True)
        pm.record_buy("AAPL", shares=10, price=100.0, score=70)
        logs = []
        # 정상 가격 틱 → 아무 일도 없어야 함
        guard._evaluate("AAPL", 99.0, logs.append)
        assert "AAPL" in portfolio.PortfolioManager(paper=True).positions
        # 손절선(-7%) 하회 틱 → 즉시 전량 매도 + 현금 회수
        guard._evaluate("AAPL", 92.0, logs.append)
        assert "AAPL" not in portfolio.PortfolioManager(paper=True).positions
        assert any("틱 가드 발동" in m for m in logs)
        # record_buy 는 장부만 기록(현금 차감은 실행기 몫) → 매도 대금만 더해짐
        assert P.cash() == 10000.0 + 920.0
        # 쿨다운: 같은 종목 재발동 안 함 (이미 매도됐지만 가드 자체도 침묵)
        n_before = guard._stats["fires"]
        guard._evaluate("AAPL", 90.0, logs.append)
        assert guard._stats["fires"] == n_before
    finally:
        mh.is_market_open = _orig_open


# ─────────────────────────────────────────────────────────────────────────────
# 러너 (pytest 없이도 실행)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed / {len(fns)} total")
    raise SystemExit(1 if failed else 0)
