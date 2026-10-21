"""
AI 판단 forward 평가 테스트 — 진입 시점(미래 참조 없음) · 초과수익 · 중복 제거 · IC · 기록 파싱.

실행: python3 -m pytest test_ai_forward_eval.py -q
네트워크를 쓰지 않는다 (가격은 합성 데이터).
"""
import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import market_hours as mh


def _fe():
    p = Path(__file__).parent / "research" / "ai_forward_eval.py"
    spec = importlib.util.spec_from_file_location("ai_forward_eval", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


fe = _fe()


def _et(y, m, d, hh, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=fe.ET).timestamp()


def _sessions(start: date, n: int) -> list:
    out, d = [], start
    while len(out) < n:
        if mh.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _closes(days, growth: dict) -> pd.DataFrame:
    """티커마다 하루 growth 배씩 오르는 종가 표."""
    idx = pd.DatetimeIndex(days)
    return pd.DataFrame({t: 100 * g ** np.arange(len(days)) for t, g in growth.items()}, index=idx)


def _judg(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    defaults = {**{f"stance_{k}": np.nan for k in fe.SOURCES}, "src": "scanner", "by": "rule"}
    for col, v in defaults.items():
        df[col] = df[col].fillna(v) if col in df.columns else v
    return df


def test_entry_is_first_close_after_judgment():
    days = _sessions(date(2026, 10, 19), 30)
    close = _closes(days, {"SPY": 1.0, "AAA": 1.0})
    df = _judg([
        {"ticker": "AAA", "action": "BUY", "score": 70.0, "ts": _et(2026, 10, 20, 10)},     # 장중 → 그날 종가
        {"ticker": "AAA", "action": "BUY", "score": 70.0, "ts": _et(2026, 10, 20, 16)},     # 마감 정각 → 다음날
        {"ticker": "AAA", "action": "BUY", "score": 70.0, "ts": _et(2026, 10, 24, 12)},     # 토요일 → 월요일
        {"ticker": "AAA", "action": "BUY", "score": 70.0, "ts": _et(2026, 11, 27, 14)},     # 조기폐장(13:00) 뒤 → 다음 거래일
    ])
    got = fe.score_judgments(df, close, horizons=(1,))["entry_date"].tolist()
    assert got == [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 26), date(2026, 11, 30)]
    assert fe.first_close_after(_et(2026, 11, 27, 12)) == date(2026, 11, 27)   # 조기폐장 전이면 그날


def test_excess_return_vs_spy_and_pending():
    days = _sessions(date(2026, 10, 19), 10)
    close = _closes(days, {"SPY": 1.01, "UP": 1.03})
    df = _judg([
        {"ticker": "UP", "action": "BUY", "score": 70.0, "ts": _et(2026, 10, 19, 11)},
        {"ticker": "UP", "action": "BUY", "score": 70.0, "ts": _et(2026, 10, 30, 11)},      # 마지막 세션(10/30) 진입
        {"ticker": "GONE", "action": "BUY", "score": 70.0, "ts": _et(2026, 10, 19, 11)},    # 가격 없음
        {"ticker": "UP", "action": "BUY", "score": 70.0, "ts": _et(2026, 11, 5, 11)},       # 아직 진입 종가 없음
    ])
    s = fe.score_judgments(df, close, horizons=(1, 5))
    assert s.loc[0, "ret_5"] == pytest.approx(1.03 ** 5 - 1)
    assert s.loc[0, "ex_5"] == pytest.approx(1.03 ** 5 - 1.01 ** 5)
    assert bool(s.loc[1, "pending_1"]) and np.isnan(s.loc[1, "ex_1"])           # 만기 전 = 대기
    assert not bool(s.loc[2, "pending_1"]) and np.isnan(s.loc[2, "ex_1"])       # 가격 없음 ≠ 대기
    assert s.loc[3, "entry_i"] == -1 and bool(s.loc[3, "pending_1"])


def test_dedupe_keeps_first_judgment_per_entry_day():
    days = _sessions(date(2026, 10, 19), 10)
    close = _closes(days, {"SPY": 1.0, "AAA": 1.0})
    df = _judg([
        {"ticker": "AAA", "action": "HOLD", "score": 55.0, "ts": _et(2026, 10, 20, 13)},
        {"ticker": "AAA", "action": "BUY", "score": 65.0, "ts": _et(2026, 10, 20, 10)},     # 같은 날 더 이른 판단
        {"ticker": "AAA", "action": "BUY", "score": 66.0, "ts": _et(2026, 10, 21, 10)},     # 다른 진입일
        {"ticker": "AAA", "action": "BUY", "score": 64.0, "ts": _et(2026, 10, 20, 11), "src": "engine"},
    ])
    d = fe.dedupe(fe.score_judgments(df, close, horizons=(1,)))
    assert len(d) == 3
    first = d[(d["src"] == "scanner") & (d["entry_date"] == date(2026, 10, 20))]
    assert first["score"].tolist() == [65.0]


def test_last_complete_session_waits_for_settled_close():
    assert fe.last_complete_session(_et(2026, 10, 21, 16, 10)) == date(2026, 10, 20)   # 마감 직후는 아직
    assert fe.last_complete_session(_et(2026, 10, 21, 16, 30)) == date(2026, 10, 21)
    assert fe.last_complete_session(_et(2026, 10, 24, 12)) == date(2026, 10, 23)       # 토요일 → 금요일
    assert fe.trading_days_after(date(2026, 10, 23), 1) == date(2026, 10, 26)


def _panel(n_days=45, n_names=20, signal=1.0, seed=0):
    """매일 n_names 종목 판단. 점수가 다음날 초과수익과 signal 만큼 같은 방향."""
    rng = np.random.default_rng(seed)
    days = _sessions(date(2026, 1, 5), n_days + 2)
    names = [f"T{i:02d}" for i in range(n_names)]
    px = {"SPY": np.full(len(days), 100.0)}
    for t in names:
        px[t] = np.full(len(days), 100.0)
    rows = []
    for i, d in enumerate(days[:n_days]):
        z = rng.normal(0, 1, n_names)
        for j, t in enumerate(names):
            ret = 0.01 * (signal * z[j] + rng.normal(0, 1))
            px[t][i + 1] = px[t][i] * (1 + ret)
            px[t][i + 2:] = px[t][i + 1]
            score = 50 + 10 * z[j]
            rows.append({"ticker": t, "score": score, "action": "BUY" if score >= 60 else "HOLD",
                         "ts": _et(d.year, d.month, d.day, 11), "stance_crowd": z[j] / 3})
    close = pd.DataFrame(px, index=pd.DatetimeIndex(days))
    return _judg(rows), close


def test_summary_detects_informative_scores():
    df, close = _panel(signal=1.0)
    g = fe.summarize_group(fe.dedupe(fe.score_judgments(df, close, horizons=(1,))), horizons=(1,))
    hz = g["horizons"]["1"]
    assert hz["score_ic"]["mean"] > 0.4 and hz["score_ic"]["label"] == "양(+)의 신호"
    assert hz["buy_minus_all"]["mean"] > 0 and hz["buy_minus_all"]["label"] == "양(+)의 신호"
    assert hz["source_ic"]["crowd"]["mean"] > 0.4                                # 같은 신호를 실은 소스
    assert "analyst" not in hz["source_ic"]                                    # 증거 없는 소스는 빠짐
    assert hz["score_bins"]["≥70"]["mean"] > hz["score_bins"]["35–50"]["mean"]


def test_summary_withholds_verdict_on_small_or_noisy_samples():
    df, close = _panel(n_days=10, signal=1.0)
    hz = fe.summarize_group(fe.score_judgments(df, close, horizons=(1,)), horizons=(1,))["horizons"]["1"]
    assert hz["score_ic"]["label"].startswith("보류")                            # 유효 표본 10 < 20
    df, close = _panel(n_days=45, signal=0.0, seed=3)
    hz = fe.summarize_group(fe.score_judgments(df, close, horizons=(1,)), horizons=(1,))["horizons"]["1"]
    assert hz["score_ic"]["label"] == "구별 못 함"
    df, close = _panel(n_days=45, signal=1.0)
    hz5 = fe.summarize_group(fe.score_judgments(df, close, horizons=(5,)), horizons=(5,))["horizons"]["5"]
    assert hz5["score_ic"]["eff"] == pytest.approx(hz5["score_ic"]["n_days"] / 5)   # 겹치는 창 보정


def test_load_judgments_parses_evidence_and_skips_bad_lines(tmp_path):
    rec = {"v": 1, "src": "scanner", "ts": _et(2026, 10, 20, 11), "by": "rule", "model": None,
           "verdicts": [{"ticker": "mmm", "action": "BUY", "score": 64.5, "conviction": 0.61, "by": "rule",
                         "evidence": [{"source_key": "analyst", "stance": 0.4},
                                      {"source_key": "crowd", "stance": -0.6},
                                      {"source_key": "unknown", "stance": 1.0}]}]}
    p = tmp_path / "influence_log-202610.jsonl"
    p.write_text(json.dumps(rec) + "\n{broken\n\n", encoding="utf-8")
    (tmp_path / "other.jsonl").write_text("ignored")
    files = fe.log_files([str(tmp_path)])
    assert files == [p.resolve()]
    df, info = fe.load_judgments(files)
    assert info["bad_lines"] == 1 and len(df) == 1
    r = df.iloc[0]
    assert r["ticker"] == "MMM" and r["stance_analyst"] == 0.4 and r["stance_crowd"] == -0.6
    assert np.isnan(r["stance_news"])
