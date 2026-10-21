"""
라이브 동점 처리 — 같은 점수면 그날(ET)의 무작위 순서로 가른다.
(예전: 워치리스트 순서 → 0473d9f: 거래대금 큰 순 → 지금: 백테스트 tie_order("random") 와 같은 의미)
core.scoring.rank / tie_key 참고.
실행: python3 -m pytest test_live_tie_break.py -q
"""
import os
import random
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from core import scoring

DAY = "2026-10-21"
NAMES = [f"T{i:03d}" for i in range(60)]     # 이름순 = 알파벳 위치


def _order(rows, day=DAY):
    rows = [dict(r) for r in rows]
    scoring.rank(rows, day=day)
    return [r["ticker"] for r in rows]


def _tied(tickers, score=70.0):
    return [{"ticker": t, "score": score} for t in tickers]


def _days(n, start=date(2026, 1, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# ── 키 성질 ──────────────────────────────────────────────────────────────────

def test_tie_key_survives_process_restart():
    """내장 hash() 는 PYTHONHASHSEED 로 프로세스마다 달라진다 — 재시작해도 같은 키여야 한다."""
    here = scoring.tie_key("AAPL", DAY)
    code = f"from core import scoring; print(scoring.tie_key('AAPL', '{DAY}'))"
    for hs in ("1", "4242"):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=Path(__file__).parent, check=True,
                             env=dict(os.environ, PYTHONHASHSEED=hs))
        assert int(out.stdout.strip()) == here


def test_tie_day_is_us_eastern_date(monkeypatch):
    """KST 자정(= ET 오전 10~11시, 장중)에 순서가 바뀌면 안 된다 → 기준일은 ET 날짜."""
    import market_hours as mh
    import pytz
    et = pytz.timezone("America/New_York")
    # KST 2026-10-22 00:30 = ET 2026-10-21 11:30 — 아직 ET 10/21 세션
    monkeypatch.setattr(mh, "now_et", lambda: et.localize(datetime(2026, 10, 21, 11, 30)))
    assert scoring._tie_day() == "2026-10-21"
    rows = _tied(NAMES[:20])
    a = [dict(r) for r in rows]
    scoring.rank(a)                                   # day 생략 → _tie_day()
    assert [r["ticker"] for r in a] == _order(rows, "2026-10-21")


# ── 정렬 성질 ────────────────────────────────────────────────────────────────

def test_non_tied_order_is_plain_score_descending():
    rnd = random.Random(1)
    distinct = [{"ticker": t, "score": float(s)} for t, s in
                zip(NAMES[:15], rnd.sample(range(10, 100), 15))]
    rows = distinct + _tied(NAMES[15:25], 55.0)       # 동점 묶음 하나 섞음
    rnd.shuffle(rows)
    got = [dict(r) for r in rows]
    scoring.rank(got, day=DAY)
    plain = sorted(rows, key=lambda r: -r["score"])   # 동점 처리 없는 점수 정렬
    assert [r["score"] for r in got] == [r["score"] for r in plain]
    for i, r in enumerate(plain):                     # 동점이 아닌 종목은 자리까지 같다
        if r["score"] != 55.0:
            assert got[i]["ticker"] == r["ticker"]


def test_same_day_order_is_stable_and_ignores_input_order():
    base = _order(_tied(NAMES))
    rnd = random.Random(7)
    for _ in range(50):                               # 300초마다 재채점 + 입력 순서 뒤섞임
        rows = _tied(NAMES)
        rnd.shuffle(rows)
        assert _order(rows) == base
    assert base != NAMES                              # 이름순(옛 안정 정렬)과는 다르다


def test_universe_change_keeps_relative_order():
    """장중에 워치리스트가 바뀌어도(추가·제거) 남은 종목끼리의 순서는 그대로."""
    base = _order(_tied(NAMES))
    changed = NAMES[::2] + ["ZNEW", "ANEW", "MNEW"]
    got = _order(_tied(changed))
    keep = set(NAMES[::2])
    assert [t for t in got if t in keep] == [t for t in base if t in keep]


def test_order_changes_across_days():
    orders = [_order(_tied(NAMES), d) for d in _days(30)]
    assert len({tuple(o) for o in orders}) == 30
    assert len({o[0] for o in orders}) >= 10          # 첫 매수 후보가 날마다 고루 바뀐다


def test_no_alphabetical_bias_in_first_pick():
    """옛 안정 정렬은 첫 매수가 항상 이름순 맨 앞(위치 0)이었다 — 이제 평균 위치 ≈ 0.5."""
    pos = {t: i / (len(NAMES) - 1) for i, t in enumerate(NAMES)}
    firsts = [_order(_tied(NAMES), d)[0] for d in _days(2000)]
    mean = sum(pos[t] for t in firsts) / len(firsts)
    assert abs(mean - 0.5) < 0.03
    first_decile = sum(pos[t] < 0.1 for t in firsts) / len(firsts)
    assert abs(first_decile - 0.1) < 0.03


def test_same_distribution_as_backtest_random_mode():
    """백테스트 tie_order("random")(결정일마다 이름순 전체 순열)와 라이브(티커별 해시)는
    실현값은 달라도 분포가 같다 — 첫 매수의 이름순 사분위가 둘 다 균등(각 ≈ 0.25)."""
    import backtester as bt
    n_days = 2000
    q = {t: min(3, i * 4 // len(NAMES)) for i, t in enumerate(NAMES)}
    bt_key = bt.tie_order("random", {t: None for t in NAMES}, seed=0)
    bt_first = [min(NAMES, key=bt_key(i).get) for i in range(n_days)]
    live_first = [_order(_tied(NAMES), d)[0] for d in _days(n_days)]
    for firsts in (bt_first, live_first):
        share = [sum(q[t] == k for t in firsts) / n_days for k in range(4)]
        assert all(abs(s - 0.25) < 0.03 for s in share), share


# ── 채점 경로 ────────────────────────────────────────────────────────────────

def _df(close: float, volume: float, n: int = 80) -> pd.DataFrame:
    return pd.DataFrame({"Close": [close] * n, "Volume": [volume] * n})


def test_strategy_path_ties_use_daily_key_not_dollar_volume(monkeypatch):
    import backtester as bt
    sd = {"AAA": _df(10.0, 1_000),        # 거래대금 $10k
          "BBB": _df(100.0, 1_000_000),   # $100M — 거래대금 순이면 항상 맨 앞이었다
          "CCC": _df(50.0, 100_000)}
    monkeypatch.setattr(scoring, "_BUNDLE_CACHE", {})
    monkeypatch.setattr(scoring, "_tie_day", lambda: DAY)
    monkeypatch.setattr(bt, "load_market_data", lambda *a, **k: {
        "stock_data": sd, "etf_data": {}, "spy_close": None, "spy_ma200": None})
    monkeypatch.setattr(bt, "_strategy_score_bt", lambda *a, **k: 70.0)   # 전부 동점
    want = sorted(sd, key=lambda t: scoring.tie_key(t, DAY))
    for tickers in (["AAA", "BBB", "CCC"], ["CCC", "BBB", "AAA"]):
        scores, _, _ = scoring.score("ichimoku", tickers)
        assert [r["ticker"] for r in scores] == want


@pytest.fixture
def ai_path(monkeypatch):
    """AI 경로 — 동점 처리에 네트워크가 필요 없어야 한다(예전: 동점 종목 Nasdaq 일봉 조회)."""
    import realtime_feed
    import stock_browser as sb
    from influence import engine
    net = []

    def no_connect(self, addr, *a, **k):
        net.append(addr)
        raise OSError("테스트: 네트워크 금지")

    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(scoring, "_tie_day", lambda: DAY)
    monkeypatch.setattr(realtime_feed, "get_price", lambda t: None)
    monkeypatch.setattr(sb, "quote_prices", lambda ts: {t: 100.0 for t in ts})
    monkeypatch.setattr(sb, "market_regime", lambda: "neutral")
    monkeypatch.setattr(engine, "score_universe", lambda valid, **k: (
        {"AAA": 72.0, "BBB": 72.0, "CCC": 88.0, "DDD": 72.0}, {}))
    return net


def test_ai_path_breaks_ties_without_network(ai_path):
    tied = sorted(["AAA", "BBB", "DDD"], key=lambda t: scoring.tie_key(t, DAY))
    for tickers in (["AAA", "BBB", "CCC", "DDD"], ["DDD", "CCC", "BBB", "AAA"]):
        scores, _, _ = scoring.score("ai_influencer", tickers)
        assert [r["ticker"] for r in scores] == ["CCC"] + tied
    assert ai_path == []                              # 소켓 연결 시도 0회


# ── 회전(churn) — 동점 순서가 날마다 바뀌어도 멀쩡한 보유를 팔지 않는가 ────────

@pytest.fixture
def full_book(monkeypatch, tmp_path):
    """$100 × 20주 보유 5개(점수 70, 3일 전 매수), 현금 0 → 자본 $10k = 슬롯 5개 꽉 참."""
    import portfolio
    monkeypatch.setenv("TRADE_NOTIFY_DISABLED", "1")
    for k, v in {"STATE_FILE": tmp_path / "state.json", "MAX_POSITIONS": 5,
                 "MAX_POSITION_PCT": 0.25, "MIN_SCORE_TO_BUY": 60, "SELL_SCORE_THRESHOLD": 35,
                 "STOP_LOSS_PCT": 0.07, "TAKE_PROFIT_PCT": 0.15, "HOLD_DAYS_STRONG": 60,
                 "HOLD_DAYS_MEDIUM": 30, "BUY_PRICE_MIN": 0.0, "BUY_PRICE_MAX": 0.0,
                 "ROTATE_MARGIN": 10.0, "MIN_ROTATE_DAYS": 1}.items():
        monkeypatch.setattr(portfolio, k, v)
    pm = portfolio.PortfolioManager()
    held = [f"HLD{i}" for i in range(5)]
    for t in held:
        pm.record_buy(t, shares=20, price=100.0, score=70)
    for t in held:      # record_buy 가 디스크 장부를 다시 읽으므로 매수를 다 끝낸 뒤에 바꾼다
        pm.positions[t].entry_date = (portfolio._today() - timedelta(days=3)).isoformat()
    return pm, held


def _plan(pm, rows, day):
    from scorer import StockScore
    rows = [dict(r) for r in rows]
    scoring.rank(rows, day=day)
    scores = [StockScore(r["ticker"], r["score"], 0, 0, 0, 0) for r in rows]
    prices = {r["ticker"]: 100.0 for r in rows}
    o = pm.generate_orders(scores, prices, dynamic=True, available_override=0.0)
    top5 = {r["ticker"] for r in rows[:5]}
    return o, top5


def test_tie_reshuffle_alone_never_rotates_held(full_book):
    """보유 5 + 신규 10 이 모두 70점 동점. 날마다 순서가 바뀌어 보유가 상위 5 밖으로 밀려도,
    신규 최고점 − 보유 점수 = 0 < ROTATE_MARGIN 이라 회전 매도가 없다(슬롯이 꽉 차 신규 매수도 없음)."""
    pm, held = full_book
    rows = _tied(held + [f"NEW{i}" for i in range(10)])
    pushed_out = 0
    for d in _days(60):
        o, top5 = _plan(pm, rows, d)
        pushed_out += not (set(held) <= top5)
        assert o["sell"] == [] and o["buy"] == [], d
    assert pushed_out >= 50                           # 대부분의 날 보유가 상위 5 밖 — 그래도 무매도


def _rotations_per_day(pm, held, n_days=60):
    rows = _tied(held + [f"NEW{i}" for i in range(10)]) + [{"ticker": "STRONG", "score": 85.0}]
    out = []
    for d in _days(n_days):
        o, _ = _plan(pm, rows, d)
        out.append(sum(s["reason"].startswith("rotate") for s in o["sell"]))
    return out


def test_strong_new_candidate_rotation_count_depends_on_tie_order(full_book):
    """알려진 한계(현재 동작을 고정): ROTATE_MARGIN 은 '신규 후보 최고점'과 비교한다.
    85점 신규가 하나 있으면 85 − 70 ≥ 10 이라, 그날 동점 순서로 목표(상위 5)에서 밀린 70점 보유는
    모두 회전된다 — 필요한 교체는 1건인데 날에 따라 1~5건(60일 평균 3.8건), 빈자리는 같은 70점
    신규가 채운다. 백테스트도 같은 generate_orders 라 이 효과는 판정에 이미 들어 있다(다만 백테스트는
    7거래일마다, 라이브는 매 거래일 섞여 기회가 더 많다). 고치면 이 테스트를 '하루 1건'으로 바꾼다."""
    pm, held = full_book
    per_day = _rotations_per_day(pm, held)
    assert min(per_day) >= 1                          # 85점 신규를 위한 1건은 항상
    assert sum(per_day) / len(per_day) > 3            # 동점 순서 탓에 평균 3건 넘게 판다
