"""
EV(기대값) 판단 엔진(core/ev.py) 테스트 — 시뮬레이션 정확도·판단 규칙·안전 방향.

실행: python3 -m pytest test_ev.py -q
"""
from types import SimpleNamespace

import numpy as np
import pytest

from core import ev


def _series(n=160, drift=0.0, amp=0.02, seed=0):
    rng = np.random.default_rng(seed)
    return list(100 * np.exp(np.cumsum(drift + amp * rng.standard_normal(n))))


def _pos(entry, peak=None, held=5, max_hold=30):
    return SimpleNamespace(entry_price=entry, peak_price=peak or entry,
                           days_held=lambda: held, max_hold_days=lambda: max_hold)


# ── 시뮬레이션 정확도 ────────────────────────────────────────────────────
def test_simulation_matches_analytic_barrier_probability():
    mu, s, a, b = 0.0008, 0.02, 0.15, 0.07
    ret, _ = ev.simulate(100, 100, 100, mu, s, sl=b, tp=a, trail=1e-9,
                         horizon=2000, n=20000, seed=1)
    bg = 0.5826 * s        # 일 단위 감시 보정(Broadie-Glasserman)
    ana = ev.p_hit_up_first(np.log(1 + a) + bg, -np.log(1 - b) + bg, mu - s * s / 2, s)
    assert abs(float(np.mean(ret > 0)) - ana) < 0.03


def test_analytic_driftless_is_distance_ratio():
    assert ev.p_hit_up_first(0.1, 0.05, 0.0, 0.02) == pytest.approx(0.05 / 0.15)


def test_zero_edge_has_near_zero_ev():
    ret, _ = ev.simulate(100, 100, 100, 0.0, 0.02, 0.07, 0.15, 0.07, 30, n=20000, seed=2)
    assert abs(float(np.mean(ret))) < 0.003


def test_exit_rules_bound_losses():
    ret, days = ev.simulate(100, 100, 100, -0.002, 0.02, 0.07, 0.15, 0.07, 60, n=4000, seed=3)
    # 손절은 종가 기준이라 갭만큼 넘을 수 있지만, 대부분 -7% 근처에서 끊긴다
    assert np.percentile(ret, 5) > -0.15
    assert days.max() <= 60


def test_deterministic_per_ticker_and_day():
    cl = _series(seed=4)
    p = ev.EVParams()
    a = ev.evaluate("AAA", 75, cl, cl[-1], p, 0.6, today="2026-10-19")
    b = ev.evaluate("AAA", 75, cl, cl[-1], p, 0.6, today="2026-10-19")
    assert a.ev == b.ev and a.p_win == b.p_win


# ── 판단 규칙 ────────────────────────────────────────────────────────────
def test_insufficient_data_waits_or_keeps_rules():
    p = ev.EVParams()
    new = ev.evaluate("AAA", 90, [100, 101, 102], 102, p, 0.9)
    assert new.action == "WAIT" and "부족" in new.reason
    held = ev.evaluate("AAA", 90, [100, 101], 101, p, 0.9, position=_pos(100))
    assert held.action == "HOLD"


def test_stronger_signal_means_higher_ev_and_threshold_gates():
    flat = _series(drift=0.0, amp=0.014, seed=5)
    p = ev.EVParams(ev_min=0.005)
    weak = ev.evaluate("AAA", 62, flat, flat[-1], p, 0.4)
    strong = ev.evaluate("AAA", 90, flat, flat[-1], p, 0.9)
    assert strong.ev > weak.ev
    assert strong.action == "BUY"
    assert weak.action == "WAIT" and "매수 기준" in weak.reason


def test_held_negative_drift_sells_and_rule_breach_sells():
    falling = _series(drift=-0.004, amp=0.015, seed=6)
    p = ev.EVParams()
    d = ev.evaluate("AAA", 20, falling, falling[-1], p, 0.9,
                    position=_pos(falling[-1] * 1.02))
    assert d.action == "SELL" and d.ev < p.ev_exit
    stop = ev.evaluate("AAA", 70, _series(seed=7), 90.0, p, 0.5, position=_pos(100.0))
    assert stop.action == "SELL" and "청산 규칙" in stop.reason


def test_decide_only_candidates_and_holdings():
    cl = _series(seed=8)
    p = ev.EVParams(min_score=60)
    out = ev.decide([("BUYME", 80), ("LOW", 40), ("HELDLOW", 30)],
                    {"BUYME": cl[-1], "LOW": cl[-1], "HELDLOW": cl[-1]},
                    {"HELDLOW": _pos(cl[-1])}, p, lambda t: cl)
    assert {d.ticker for d in out} == {"BUYME", "HELDLOW"}


# ── 안전 방향: 점수 조정은 매수를 막거나 매도를 추가만 ─────────────────────
def test_gate_scores_only_blocks_buys_or_adds_sells():
    p = ev.EVParams(min_score=60, sell_score=35)
    D = ev.EVDecision
    decs = [D("W", "WAIT", False, 80), D("B", "BUY", False, 80),
            D("S", "SELL", True, 50), D("H", "HOLD", True, 20)]
    g = ev.gate_scores({"W": 80, "B": 80, "S": 50, "H": 20, "X": 70}, decs, p)
    assert g["W"] < 60                 # 대기 → 매수 문턱 아래
    assert g["B"] == 80                # 매수 → 그대로
    assert g["S"] < 35                 # 매도 → 매도 문턱 아래
    assert g["H"] == 20                # 보유 판단이 낮은 점수를 올려 청산을 막지 않음
    assert g["X"] == 70                # 판단 없는 종목은 그대로
    assert all(g[k] <= v for k, v in {"W": 80, "B": 80, "S": 50, "H": 20, "X": 70}.items())


def test_params_from_cfg_and_save_load(tmp_path):
    p = ev.EVParams.from_cfg({"stop_loss": 0.05, "take_profit": 0.2, "trail": 0.1,
                              "ev_min": 0.01, "ev_cost_bps": 10, "min_score": 65})
    assert (p.sl, p.tp, p.trail, p.ev_min, p.cost_side, p.min_score) == (0.05, 0.2, 0.1, 0.01, 0.001, 65)
    cl = _series(seed=9)
    decs = [ev.evaluate("AAA", 80, cl, cl[-1], p, 0.7)]
    f = tmp_path / "ev.json"
    ev.save(decs, p, path=f)
    got = ev.load(path=f)
    assert got["rows"][0]["ticker"] == "AAA" and got["params"]["ev_min"] == 0.01
    assert sum(got["counts"].values()) == 1


def test_telegram_ev_command_renders_summary():
    import telegram_bot as tg
    cl = _series(seed=10)
    p = ev.EVParams()
    s = {"ev": ev.summary([ev.evaluate("AAA", 85, cl, cl[-1], p, 0.8),
                           ev.evaluate("BBB", 62, cl, cl[-1], ev.EVParams(ev_min=0.5), 0.3)]),
         "last_run": "10:00:00"}
    out = tg.cmd_ev(s)
    assert "AAA" in out and "BBB" in out and "대기" in out
    assert "EV 판단" in tg._ev_line(s)
    assert "기록이 없습니다" in tg.cmd_ev({})
