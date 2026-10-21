"""
정직한 평가 기준 테스트 — SPY 벤치마크 · 봉인 구간 · DSR · 전략 계열 군집 · 시점별 편입.

실행: python3 -m pytest test_backtest_eval.py -q
네트워크를 쓰지 않는다 (yfinance·위키는 합성 데이터/monkeypatch 로 대체).
"""
import math

import numpy as np
import pandas as pd
import pytest

import backtester as bt
import universe as uni


# ─────────────────────────────────────────────────────────────────────────────
# Deflated Sharpe Ratio (Bailey & López de Prado 2014)
# ─────────────────────────────────────────────────────────────────────────────

def test_dsr_matches_paper_example():
    """논문 수치 예: 연율 SR 2.5 · T=1250 · N=100 · V[SR]=0.5(연율) · 왜도 −3 · 첨도 10 → DSR≈0.9004."""
    sr = 2.5 / math.sqrt(250)
    d = bt.deflated_sharpe(sr, 1250, -3.0, 10.0, n_trials=100, sr_var=0.5 / 250)
    assert d["dsr"] == pytest.approx(0.9004, abs=0.002)


def test_expected_max_sharpe_grows_with_trials():
    v = 0.002
    assert bt.expected_max_sharpe(1, v) == 0.0            # 고른 게 없으면 운 보정 없음
    vals = [bt.expected_max_sharpe(n, v) for n in (2, 10, 50, 500)]
    assert all(a < b for a, b in zip(vals, vals[1:]))     # 많이 시도할수록 운의 최대치가 커진다
    assert bt.expected_max_sharpe(10, 0.0) == 0.0


def test_dsr_is_half_at_expected_max():
    """관측 샤프가 운의 기대 최대치와 같으면 DSR=0.5, 보정 샤프=0."""
    n, v = 26, 0.0004
    sr0 = bt.expected_max_sharpe(n, v)
    d = bt.deflated_sharpe(sr0, 1258, 0.0, 3.0, n, v)
    assert d["dsr"] == pytest.approx(0.5, abs=1e-9)
    assert d["sr_deflated"] == pytest.approx(0.0, abs=1e-9)


def test_dsr_falls_with_more_trials_and_fat_left_tail():
    base = dict(sr=0.08, T=1258, n_trials=10, sr_var=0.0004)
    normal = bt.deflated_sharpe(skew=0.0, kurt=3.0, **base)["dsr"]
    more_trials = bt.deflated_sharpe(skew=0.0, kurt=3.0, **{**base, "n_trials": 200})["dsr"]
    fat_left = bt.deflated_sharpe(skew=-2.0, kurt=12.0, **base)["dsr"]
    assert more_trials < normal
    assert fat_left < normal


def test_sharpe_moments_normal_returns():
    r = pd.Series(np.random.RandomState(0).normal(0.001, 0.01, 20000))
    m = bt.sharpe_moments(r)
    assert m["T"] == 20000
    assert m["sr"] == pytest.approx(0.1, abs=0.02)
    assert abs(m["skew"]) < 0.1 and m["kurt"] == pytest.approx(3.0, abs=0.15)
    flat = bt.sharpe_moments(pd.Series([0.0] * 50))       # 거래 없음 → 정의 불가 대신 중립값
    assert flat["sr"] == 0.0 and flat["kurt"] == 3.0


# ─────────────────────────────────────────────────────────────────────────────
# SPY 벤치마크 · 성과 지표
# ─────────────────────────────────────────────────────────────────────────────

def test_perf_stats_known_curve():
    c = pd.Series([100.0, 110.0, 99.0, 120.0])
    s = bt.perf_stats(c)
    assert s["ret"] == pytest.approx(0.20)
    assert s["mdd"] == pytest.approx(99 / 110 - 1)
    assert s["cagr"] == pytest.approx(1.2 ** (252 / 4) - 1)


def test_benchmark_uses_same_window_only():
    idx = pd.bdate_range("2020-01-01", periods=300)
    spy = pd.Series(np.linspace(100, 400, 300), index=idx)
    window = idx[100:200]
    b = bt.benchmark_stats(spy, window)
    assert b["ret"] == pytest.approx(spy.iloc[199] / spy.iloc[100] - 1)
    assert b["start"] == str(window[0].date()) and b["end"] == str(window[-1].date())
    assert bt.benchmark_stats(None, window) is None


# ─────────────────────────────────────────────────────────────────────────────
# 상관행렬 + 계층 군집 → 전략 계열
# ─────────────────────────────────────────────────────────────────────────────

def test_families_group_correlated_strategies():
    rs = np.random.RandomState(1)
    n = 1000
    f1, f2 = rs.normal(0, 0.01, n), rs.normal(0, 0.01, n)
    rets = pd.DataFrame({
        "A1": f1 + rs.normal(0, 0.002, n), "A2": f1 + rs.normal(0, 0.002, n),
        "A3": f1 + rs.normal(0, 0.002, n),
        "B1": f2 + rs.normal(0, 0.002, n), "B2": f2 + rs.normal(0, 0.002, n),
        "C": rs.normal(0, 0.01, n), "FLAT": np.zeros(n)})
    corr, fam = bt.strategy_families(rets, threshold=0.7)
    assert fam["A1"] == fam["A2"] == fam["A3"] == 1          # 가장 큰 계열이 1번
    assert fam["B1"] == fam["B2"] == 2
    assert len({fam["C"], fam["FLAT"], 1, 2}) == 4            # 독립·무거래 전략은 단독 계열
    assert corr.loc["A1", "A2"] > 0.9


def test_family_threshold_controls_merging():
    rs = np.random.RandomState(2)
    f = rs.normal(0, 0.01, 2000)
    rets = pd.DataFrame({"X": f + rs.normal(0, 0.01, 2000), "Y": f + rs.normal(0, 0.01, 2000)})
    rho = rets.corr().loc["X", "Y"]                           # ≈ 0.5
    assert bt.strategy_families(rets, threshold=rho - 0.05)[1] == {"X": 1, "Y": 1}
    assert len(set(bt.strategy_families(rets, threshold=rho + 0.05)[1].values())) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 봉인 구간 가드
# ─────────────────────────────────────────────────────────────────────────────

def test_holdout_window_is_sealed():
    assert bt.check_window(bt.DEV_START, bt.DEV_END) == ("2020-01-01", "2024-12-31")
    with pytest.raises(ValueError, match="holdout=True"):
        bt.check_window("2020-01-01", "2025-01-01")
    with pytest.raises(ValueError, match="holdout=True"):
        bt.load_research_bundle("watchlist", "2024-06-01", "2025-06-30")   # 다운로드 전에 막힌다
    assert bt.check_window("2025-01-01", "2026-10-17", holdout=True)[0] == "2025-01-01"
    with pytest.raises(ValueError):
        bt.check_window("2024-01-01", "2023-01-01")


# ─────────────────────────────────────────────────────────────────────────────
# S&P 500 시점별 구성 근사
# ─────────────────────────────────────────────────────────────────────────────

def test_sp500_membership_rewinds_changes():
    current = ["AAA", "BBB", "CCC"]
    changes = [("2021-06-01", "CCC", "DDD"),        # CCC 편입 · DDD 편출
               ("2020-03-01", "BBB", "")]           # BBB 단독 편입
    dates = pd.to_datetime(["2020-01-02", "2020-06-01", "2021-05-31", "2021-06-01"])
    m = uni.sp500_membership(current, changes, dates)
    at = lambda i: {t for t, v in m.items() if v[i]}
    assert at(0) == {"AAA", "DDD"}
    assert at(1) == {"AAA", "BBB", "DDD"}
    assert at(2) == {"AAA", "BBB", "DDD"}
    assert at(3) == {"AAA", "BBB", "CCC"}           # 효력일 당일부터 반영


def test_parse_sp500_changes_wiki_layout():
    cols = pd.MultiIndex.from_tuples([
        ("Effective Date", "Effective Date"), ("Added", "Ticker"), ("Added", "Security"),
        ("Removed", "Ticker"), ("Removed", "Security"), ("Reason", "Reason")])
    tbl = pd.DataFrame([["March 2, 2020", "BRK.B", "Berkshire", np.nan, np.nan, "x"],
                        ["December 21, 2020", "TSLA", "Tesla", "AIV", "Aimco", "y"]],
                       columns=cols)
    out = uni.parse_sp500_changes(tbl)
    assert out == [("2020-12-21", "TSLA", "AIV"), ("2020-03-02", "BRK-B", "")]


# ─────────────────────────────────────────────────────────────────────────────
# run() — 연구용 번들(워밍업·시점별 편입·이력 부족) + 벤치마크
# ─────────────────────────────────────────────────────────────────────────────

def _px(prices, idx):
    p = np.asarray(prices, dtype=float)
    return pd.DataFrame({"Open": p, "High": p * 1.01, "Low": p * 0.99, "Close": p,
                         "Volume": np.full(len(p), 1e6)}, index=idx)


def _research_bundle(n=420, eval_start=300):
    idx = pd.bdate_range("2019-01-01", periods=n)
    up = np.linspace(50, 150, n)
    late = up.copy(); late[:250] = np.nan                     # 250일째 상장
    sd = {"MEM": _px(up, idx), "NONMEM": _px(up * 1.1, idx), "LATE": _px(late, idx)}
    spy = pd.Series(np.linspace(300, 450, n), index=idx)
    return {"stock_data": sd, "etf_data": {}, "date_index": idx, "spy_close": spy,
            "spy_ma200": spy.rolling(200, min_periods=100).mean(),
            "spy_ma50": spy.rolling(50, min_periods=25).mean(),
            "eval_start_idx": eval_start,
            "members": {"MEM": np.ones(n, bool), "NONMEM": np.zeros(n, bool),
                        "LATE": np.ones(n, bool)},
            "n_valid": {t: df["Close"].notna().cumsum().to_numpy() for t, df in sd.items()},
            "min_history": 252, "sector_map": {"MEM": "XLK"}}


def _run(bundle, **kw):
    return bt.run(strategy="momentum", capital=10_000, min_score=20, max_positions=3,
                  use_trend_filter=False, adaptive_regime=False, prefetched=bundle, **kw)


def test_run_research_bundle_respects_warmup_members_and_history():
    b = _research_bundle()
    r = _run(b)
    eval_day0 = b["date_index"][300].date()
    assert len(r.equity_curve) == 120 and r.equity_curve.index[0].date() == eval_day0
    traded = {t.ticker for t in r.trades}
    assert "MEM" in traded                                   # 구성종목은 매수
    assert "NONMEM" not in traded                            # 비구성종목은 신규 후보 아님
    assert "LATE" not in traded                              # 상장 후 252봉 미만(170봉) → 후보 아님
    assert all(t.entry_date >= eval_day0 for t in r.trades)  # 워밍업 구간엔 매매 없음
    assert r.benchmark["ret"] == pytest.approx(
        b["spy_close"].iloc[-1] / b["spy_close"].iloc[300] - 1)
    assert "MEM" not in bt.TICKER_SECTOR_MAP                 # 섹터 보강은 런 동안만


def test_run_plain_bundle_unchanged_and_has_benchmark():
    """연구용 키가 없으면 예전처럼 첫날부터 평가하고, SPY 가 있으면 벤치마크를 붙인다."""
    b = _research_bundle()
    plain = {k: b[k] for k in ("stock_data", "etf_data", "date_index",
                               "spy_close", "spy_ma200", "spy_ma50")}
    plain["stock_data"] = {"MEM": b["stock_data"]["MEM"], "NONMEM": b["stock_data"]["NONMEM"]}
    r = _run(plain)
    assert len(r.equity_curve) == len(b["date_index"])
    assert {t.ticker for t in r.trades} >= {"MEM", "NONMEM"}
    assert r.benchmark is not None and "SPY 같은 구간" in r.report()


def test_compare_strategies_adds_honest_metrics(monkeypatch):
    b = _research_bundle()
    plain = {k: b[k] for k in ("stock_data", "etf_data", "date_index",
                               "spy_close", "spy_ma200", "spy_ma50")}
    plain["stock_data"] = {"MEM": b["stock_data"]["MEM"], "NONMEM": b["stock_data"]["NONMEM"]}
    monkeypatch.setattr(bt, "load_market_data", lambda *a, **k: plain)
    rows = bt.compare_strategies(["momentum", "golden_cross", "rsi2"], ["MEM", "NONMEM"],
                                 "2019-01-01", "2020-08-01", 10_000, 20, 0.07, 0.2)
    assert [r["ret"] for r in rows] == sorted((r["ret"] for r in rows), reverse=True)
    for r in rows:
        assert {"key", "ret", "cagr", "mdd", "sharpe", "win", "eq"} <= set(r)   # 기존 키 유지
        assert r["n_trials"] == 3 and 0.0 <= r["dsr"] <= 1.0
        assert r["family"] >= 1 and r["spy_sharpe"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# 동점 처리 (tie_break) — 이산 점수 전략이 알파벳순으로 사던 결함
# ─────────────────────────────────────────────────────────────────────────────

_TIE_NAMES = [f"T{i:02d}" for i in range(20)]


def _tie_bundle(names=_TIE_NAMES, volumes=None, **extra):
    """같은 가격 경로 20종목 → 모든 리밸런싱에서 점수가 전부 동점."""
    b = _research_bundle()
    idx = b["date_index"]
    up = np.linspace(50, 150, len(idx))
    sd = {}
    for t in names:
        df = _px(up, idx)
        if volumes:
            df["Volume"] = float(volumes[t])
        sd[t] = df
    b.update(stock_data=sd, members={t: np.ones(len(idx), bool) for t in names},
             n_valid={t: df["Close"].notna().cumsum().to_numpy() for t, df in sd.items()},
             sector_map={}, **extra)
    return b


def _first_buys(r):
    d0 = min(t.entry_date for t in r.trades)
    return sorted(t.ticker for t in r.trades if t.entry_date == d0)


def test_tie_break_absent_keeps_input_order():
    """키가 없거나 None 이면 예전과 똑같이 입력 순서 앞쪽을 산다 (기존 동작 보존)."""
    a = _run(_tie_bundle())
    b = _run(_tie_bundle(tie_break=None, tie_seed=7))
    assert _first_buys(a) == _TIE_NAMES[:3]
    pd.testing.assert_series_equal(a.equity_curve, b.equity_curve)
    assert [t.ticker for t in a.trades] == [t.ticker for t in b.trades]


def test_tie_break_random_is_reproducible_per_seed():
    r1 = _run(_tie_bundle(tie_break="random", tie_seed=3))
    r2 = _run(_tie_bundle(tie_break="random", tie_seed=3))
    assert [(t.ticker, t.entry_date) for t in r1.trades] == [(t.ticker, t.entry_date) for t in r2.trades]
    pd.testing.assert_series_equal(r1.equity_curve, r2.equity_curve)
    picks = {tuple(_first_buys(_run(_tie_bundle(tie_break="random", tie_seed=s)))) for s in range(6)}
    assert len(picks) > 1                                    # seed 가 다르면 동점 선택도 다르다


def test_tie_break_random_ignores_input_order():
    """같은 seed 면 번들의 종목 입력 순서를 뒤집어도 같은 종목을 산다 — 순서 편향 없음."""
    fwd = _run(_tie_bundle(tie_break="random", tie_seed=11))
    rev = _run(_tie_bundle(names=_TIE_NAMES[::-1], tie_break="random", tie_seed=11))
    assert [(t.ticker, t.entry_date) for t in fwd.trades] == [(t.ticker, t.entry_date) for t in rev.trades]


def test_tie_break_random_has_no_alphabetical_bias():
    """여러 seed 에 걸쳐 동점 20종목이 고르게 뽑힌다(입력 순서 앞쪽 몰림 없음)."""
    from collections import Counter
    cnt = Counter()
    for s in range(40):
        cnt.update(_first_buys(_run(_tie_bundle(tie_break="random", tie_seed=s))))
    assert sum(cnt.values()) == 120                          # seed 당 3종목
    rank = {t: i for i, t in enumerate(_TIE_NAMES)}
    mean_pct = sum(rank[t] * c for t, c in cnt.items()) / sum(cnt.values()) / (len(_TIE_NAMES) - 1)
    assert 0.38 < mean_pct < 0.62                            # 입력 순서였다면 0.05
    assert len(cnt) >= 16 and max(cnt.values()) <= 15        # 기대 6회/종목 — 특정 종목 쏠림 없음


def test_tie_order_random_depends_only_on_seed_and_day():
    sd = {t: None for t in _TIE_NAMES}
    f = bt.tie_order("random", sd, seed=5)
    assert f(10) == bt.tie_order("random", dict(reversed(list(sd.items()))), seed=5)(10)
    assert f(10) != f(11) and sorted(f(10).values()) == list(range(len(_TIE_NAMES)))
    assert bt.tie_order(None, sd) is None
    with pytest.raises(ValueError):
        bt.tie_order("alphabetical", sd)


def test_tie_break_dollar_volume_prefers_liquid_names():
    vols = {t: 1e6 * (i + 1) for i, t in enumerate(_TIE_NAMES)}   # T19 가 거래대금 최대
    r = _run(_tie_bundle(volumes=vols, tie_break="dollar_volume"))
    assert _first_buys(r) == ["T17", "T18", "T19"]


# ─────────────────────────────────────────────────────────────────────────────
# 디스크 캐시
# ─────────────────────────────────────────────────────────────────────────────

class _FakeTicker:
    """yf.Ticker 대역 — 종목별 재시도 경로를 네트워크 없이 검증."""
    errors: dict = {}

    def __init__(self, t):
        self.t = t

    def history(self, **kw):
        from yfinance import exceptions as yfe
        e = self.errors.get(self.t, "missing")
        if e == "missing":
            raise yfe.YFTzMissingError(self.t)
        raise yfe.YFRateLimitError()


def test_download_prices_cache_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(bt, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(bt.time, "sleep", lambda s: None)
    idx = pd.bdate_range("2020-01-01", periods=60)
    good = _px(np.linspace(10, 20, 60), idx)
    calls = []

    def fake_download(batch, **kw):
        calls.append(list(batch))
        parts = {(c, "AAA"): good[c] for c in ("Close", "Open", "High", "Low", "Volume")}
        for t in batch:
            if t != "AAA":
                for c in ("Close", "Open", "High", "Low", "Volume"):
                    parts[(c, t)] = pd.Series(np.nan, index=idx)
        return pd.DataFrame(parts)
    monkeypatch.setattr(bt.yf, "download", fake_download)
    monkeypatch.setattr(bt.yf, "Ticker", _FakeTicker)
    _FakeTicker.errors = {"GONE": "missing", "BUSY": "ratelimit"}
    px, missing = bt.download_prices(["AAA", "GONE", "BUSY"], "2020-01-01", "2020-04-01")
    assert set(px) == {"AAA"} and missing == ["BUSY", "GONE"]
    import json as _json
    recorded = _json.loads((tmp_path / "prices" / "2020-01-01_2020-04-01" / "_missing.json").read_text())
    assert recorded == ["GONE"]                    # 레이트리밋은 영구 누락으로 기록하지 않는다

    def boom(*a, **k):
        raise AssertionError("캐시·확정 누락 종목은 다시 받지 않아야 한다")
    monkeypatch.setattr(bt.yf, "download", boom)
    px2, missing2 = bt.download_prices(["AAA", "GONE"], "2020-01-01", "2020-04-01")
    assert missing2 == ["GONE"] and len(calls) == 1
    pd.testing.assert_series_equal(px2["AAA"]["Close"], good["Close"], check_names=False,
                                   check_freq=False)


# ─────────────────────────────────────────────────────────────────────────────
# 토너먼트 스크립트 — 대표 선정 · 봉인 구간 1회 가드
# ─────────────────────────────────────────────────────────────────────────────

def _tournament():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).parent / "research" / "strategy_tournament.py"
    spec = importlib.util.spec_from_file_location("strategy_tournament", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_tournament_picks_best_dsr_per_family():
    st = _tournament()
    fam = {"a": 1, "b": 1, "c": 2}
    rows = {"sp500": {"a": {"dsr": 0.40, "sharpe": 0.9}, "b": {"dsr": 0.55, "sharpe": 0.5},
                      "c": {"dsr": 0.10, "sharpe": 0.2}}}
    assert st.pick_representatives(fam, rows, "sp500") == {1: "b", 2: "c"}


def test_tournament_holdout_requires_keys_and_runs_once(monkeypatch, tmp_path):
    st = _tournament()
    with pytest.raises(SystemExit, match="--keys"):
        st.main(["--holdout"])
    mark = tmp_path / "holdout-opened.json"
    mark.write_text('{"opened": "2026-10-21"}')
    monkeypatch.setattr(st, "HOLDOUT_MARK", mark)
    monkeypatch.setattr(st, "run_universe", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("이미 열린 봉인 구간을 다시 돌리면 안 된다")))
    with pytest.raises(SystemExit, match="이미 열렸습니다"):
        st.main(["--holdout", "--keys", "momentum_max"])


def _curve_result(vals, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(vals))
    return bt.BacktestResult(trades=[], equity_curve=pd.Series(vals, index=idx, dtype=float),
                             start_capital=float(vals[0]))


def test_tournament_aggregate_seeds_mean_curve_and_spread():
    st = _tournament()
    a = _curve_result([100, 110, 99, 120])
    b = _curve_result([100, 90, 95, 100])
    mean, s = st.aggregate_seeds([a, b])
    assert list(mean.equity_curve) == [100, 100, 97, 110]            # 평균 곡선 = seed 계좌들의 합/K
    assert s["n_seeds"] == 2 and s["mdds"] == [a.mdd, b.mdd]
    assert s["seed"]["ret"]["mean"] == pytest.approx((0.20 + 0.0) / 2)
    assert s["seed"]["sharpe"]["min"] == min(a.sharpe, b.sharpe)
    assert s["seed"]["sharpe"]["sd"] == pytest.approx(np.std([a.sharpe, b.sharpe], ddof=1))
    bench = {"sharpe": 0.0, "mdd": -0.5}
    row = st.apply_seed_stats({"ret": mean.total_return, "sharpe": mean.sharpe, "mdd": mean.mdd},
                              s, bench, n_trials=10, sr_var=1e-4)
    assert row["sharpe"] == pytest.approx(s["seed"]["sharpe"]["mean"])   # 보고 = seed 평균
    assert row["curve_sharpe"] == mean.sharpe                             # 평균 곡선 값은 따로 보존
    assert 0.0 <= row["seed_beat_spy"] <= 1.0 and 0.0 <= row["seed_dsr_median"] <= 1.0


def test_tournament_prior_trials_counted_once(tmp_path, monkeypatch):
    """옛 형식(rows 전부가 시도)과 새 형식(new_trials 만)을 섞어도 이전 시도를 두 번 세지 않는다."""
    import json as _json
    st = _tournament()
    old = tmp_path / "strategy-tournament-2026-10-20.json"
    old.write_text(_json.dumps({"rows": {"sp500": {"a": {"sharpe": 0.5 * math.sqrt(252)},
                                                   "b": {"sharpe": 0.0}},
                                         "watchlist": {"a": {"sharpe": 0.1 * math.sqrt(252)}}}}))
    new = tmp_path / "strategy-tournament-2026-10-20-tiebreak.json"
    new.write_text(_json.dumps({"rows": {"sp500": {"a": {}, "b": {}}},
                                "new_trials": [{"variant": "random", "universe": "sp500",
                                                "key": "a", "sr": 0.2}]}))
    monkeypatch.setattr(st, "RESULTS_DIR", tmp_path)
    prior = st.load_prior("auto")
    assert sum(len(p["trials"]) for p in prior) == 4                 # 3(옛) + 1(새) — 새 파일의 rows 는 무시
    srs = sorted(t["sr"] for p in prior for t in p["trials"])
    assert srs == pytest.approx([0.0, 0.1, 0.2, 0.5])
    assert [p["path"] for p in st.load_prior("auto", exclude=new)] == [old]   # 쓰려는 결과 파일은 제외
    assert st.load_prior("none") == []
    assert len(st.load_prior(str(old))[0]["trials"]) == 3


def test_tournament_rejects_unknown_tie_break():
    st = _tournament()
    with pytest.raises(SystemExit, match="tie-break"):
        st.main(["--tie-break", "alphabetical", "--keys", "momentum"])


# ─────────────────────────────────────────────────────────────────────────────
# 체결 현실성 옵션 — execution="next_open" · daily_stops (기본값은 예전 동작)
# ─────────────────────────────────────────────────────────────────────────────

def _gap_bundle(day2: dict | None = None):
    """MEM 하나가 오르다 eval 첫날(300) 매수. day2 = {열: entry 대비 배율} 로 302일 봉을 덮어쓴다.
    Open 은 평소 종가의 +2% — 종가 체결과 시가 체결이 구분되게."""
    b = _research_bundle()
    idx = b["date_index"]
    df = b["stock_data"]["MEM"].copy()
    df["Open"] = df["Close"] * 1.02
    entry = float(df["Close"].iloc[300]) * (1 + 0.0005)
    if day2:
        for col, k in day2.items():
            df.iloc[302:, df.columns.get_loc(col)] = entry * k   # 이후는 그 수준에서 평평
    b["stock_data"] = {"MEM": df}
    b["members"] = {"MEM": np.ones(len(idx), bool)}
    b["n_valid"] = {"MEM": df["Close"].notna().cumsum().to_numpy()}
    return b, entry


def test_next_open_fills_at_next_day_open():
    b, _ = _gap_bundle()
    d = b["stock_data"]["MEM"]
    r_close = _run(b)
    r_open = _run(b, execution="next_open")
    t0, t1 = r_close.trades[0], r_open.trades[0]
    assert t0.entry_date == b["date_index"][300].date()
    assert t0.entry_price == pytest.approx(d["Close"].iloc[300] * 1.0005)
    assert t1.entry_date == b["date_index"][301].date()          # 결정 다음 날
    assert t1.entry_price == pytest.approx(d["Open"].iloc[301] * 1.0005)


def test_daily_stop_gap_down_fills_at_open():
    b, entry = _gap_bundle({"Open": 0.90, "High": 0.91, "Low": 0.88, "Close": 0.90})
    r = _run(b, stop_loss=0.07, daily_stops=True)
    t = r.trades[0]
    assert t.reason.startswith("stop_loss") and t.exit_date == b["date_index"][302].date()
    assert t.exit_price == pytest.approx(entry * 0.90 * (1 - 0.0005))   # 선(−7%)이 아니라 갭 시가


def test_daily_stop_intraday_touch_fills_at_stop_level():
    b, entry = _gap_bundle({"Open": 0.97, "High": 0.98, "Low": 0.92, "Close": 0.95})
    r = _run(b, stop_loss=0.07, daily_stops=True)
    t = r.trades[0]
    assert t.reason.startswith("stop_loss") and t.exit_date == b["date_index"][302].date()
    assert t.exit_price == pytest.approx(entry * 0.93 * (1 - 0.0005))
    # 예전 방식(리밸런싱 날 종가로만 판정)은 302일 장중 −8% 를 못 보고 계속 보유한다
    r_old = _run(b, stop_loss=0.07)
    assert r_old.trades[0].exit_date != b["date_index"][302].date()


def test_execution_option_validated():
    b, _ = _gap_bundle()
    with pytest.raises(ValueError):
        _run(b, execution="vwap")


def test_live_equivalent_kwargs_mirror_live_config():
    cfg = {"strategy": "momentum", "stop_loss": 0.07, "take_profit": 0.15, "trail": 0.07,
           "min_score": 60, "sell_score": 35, "hold_strong": 60, "hold_medium": 30,
           "dynamic": True}
    kw = bt.live_equivalent_kwargs(cfg)
    assert kw["trailing_stop"] == 0.07 and kw["take_profit"] == 0.15
    assert kw["ev_filter"] is True                           # 키 없으면 라이브처럼 켜짐
    assert kw["execution"] == "next_open" and kw["daily_stops"] and kw["rebalance_days"] == 1
    assert bt.live_equivalent_kwargs({**cfg, "ev_enabled": False})["ev_filter"] is False
    b, _ = _gap_bundle()
    r = bt.run(strategy="momentum", capital=10_000, prefetched=b,
               **{**kw, "ev_filter": False, "min_score": 20})
    assert r.trades and r.trades[0].entry_date == b["date_index"][301].date()
    with pytest.raises(ValueError):
        bt.live_equivalent_kwargs({**cfg, "strategy": "ai_influencer"})
