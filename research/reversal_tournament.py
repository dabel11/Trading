#!/usr/bin/env python3
"""단기 반전 전략 1개(사전 등록)를 규칙 전략 토너먼트 기준으로 측정한다.

사전 등록(2026-10-21, 실행 전 커밋 — 상수·정의를 결과를 본 뒤 바꾸지 않는다)
  전략 reversal_1w — 업종 대비 1주 반전
    근거: crowd 스터디(직전 1주 승자 다음 주 부진)·필터 토너먼트(넓은 유니버스에서 단기 승자 매수가 비쌈),
          문헌상 반전은 업종 대비로 잴 때 강하다(Hameed & Mian 2015; Da, Liu & Schaumburg 2014).
    x    = (종가[i]/종가[i−5] − 1) − (섹터 ETF 종가[i]/종가[i−5] − 1)    섹터 ETF 없으면 SPY
    점수 = 50 + 50·tanh(−x / 0.10)
           → x = −2% 에서 59.9(매수 문턱 60), x = +3% 에서 35.4(매도 문턱 35) — 엔진 기본값 그대로.
           처음엔 clip(50 − 500·x, 0, 100) 으로 짰으나 급락 주(2023-03 SVB 주간 등)에 100 으로 포화해
           상위가 동점이 됐다(점수 분포만 보고 확인, 성과는 보기 전). 같은 문턱을 유지하는 매끄러운 포화로 바꿨다.
  그 밖의 모든 것은 토너먼트와 같다: RUN_KW(시드·문턱·손절 7%·트레일링 15%·7거래일 리밸런싱·비용 0.15%·
  약세장 신규 매수 보류), 개발 구간 2020-01-01~2024-12-31 번들, 무작위 동점 seed 10(필터 토너먼트 기준선과 같음).
  시도 수: 이전 260(토너먼트 52 + 필터 208) + 이번 2(전략 1 × 유니버스 2) → DSR 의 N·V 에 합친다.
  판정: 토너먼트 기준 3개(샤프 ≥ SPY · MDD ≥ SPY−5%p · 운 보정 샤프 > 0), S&P 500(시점별 편입) 기준.

정직성
  • 반전이라는 착상은 2018–2024 데이터(crowd 스터디)에서 나왔고 개발 구간이 그 안에 있다 — 표본 내 확인.
  • 봉인 구간(2025+)은 이미 1회 쓰였다(holdout-opened.json). 열지 않는다. 다음 확인은 forward 페이퍼 운영.
  • 라이브 코드·strategy_catalog·backtester 는 건드리지 않는다. 전략 점수는 이 스크립트의 작업 프로세스 안에서만
    backtester._strategy_score_bt 를 감싸 등록한다.

실행 (백그라운드 권장):
  TRADE_BT_CACHE=~/.cache/trade_research/bt_cache python3 -P research/reversal_tournament.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADE_NOTIFY_DISABLED", "1")

import numpy as np                      # noqa: E402

import backtester as bt                 # noqa: E402
import strategy_tournament as st        # noqa: E402

KEY = "reversal_1w"
NAME = "업종 대비 1주 반전"
LOOKBACK = 5
SCALE = 0.10                                        # tanh 척도(상대 수익 10%)
SEEDS = 10
FILTER_RAW = "filter-raw-2026-10-20-10s-*.pkl"      # 필터 토너먼트 'base' = 동점 처리 고친 기존 26전략


def reversal_score(ticker, stock_data, etf_data, idx) -> float:
    df = stock_data.get(ticker)
    if df is None or idx < LOOKBACK:
        return 0.0
    c = df["Close"]
    c0, c1 = float(c.iloc[idx - LOOKBACK]), float(c.iloc[idx])
    if not (c0 > 0 and c1 > 0):
        return 0.0
    r = c1 / c0 - 1
    etf = bt.TICKER_SECTOR_MAP.get(ticker)
    ref = None
    if etf and etf in etf_data:
        e = etf_data[etf]["Close"]
        e0, e1 = float(e.iloc[idx - LOOKBACK]), float(e.iloc[idx])
        if e0 > 0 and e1 > 0:
            ref = e1 / e0 - 1
    if ref is None:                                   # 섹터 ETF 없음 → SPY
        s = st._BUNDLE["spy_close"]
        ref = float(s.iloc[idx]) / float(s.iloc[idx - LOOKBACK]) - 1
    return round(50.0 + 50.0 * math.tanh(-(r - ref) / SCALE), 4)


def _init(path: str):
    """작업 프로세스: 번들 적재 + reversal_1w 점수 등록(이 프로세스 안에서만)."""
    st._init_worker(path)
    orig = bt._strategy_score_bt

    def patched(strategy, ticker, stock_data, etf_data, idx):
        if strategy == KEY:
            return reversal_score(ticker, stock_data, etf_data, idx)
        return orig(strategy, ticker, stock_data, etf_data, idx)

    bt._strategy_score_bt = patched


def run_universe(u: str, bundle: dict, seeds: int, workers: int, log) -> list:
    path = bt.CACHE_DIR / f"reversal_bundle_{u}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    tasks = [(KEY, "random", s) for s in range(seeds)]
    out = {}
    t0 = time.time()
    try:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(str(path),)) as ex:
            for task, r, al, dt in ex.map(st._run_task, tasks):
                out[task] = r
                log(f"  {u:9} seed {task[2]:>2} {r.total_return:+8.1%} 샤프 {r.sharpe:5.2f} 거래 {len(r.trades)} ({dt:.0f}s)")
    finally:
        path.unlink(missing_ok=True)
    return [out[t] for t in tasks]


def holding_days(rs: list) -> float:
    d = [(x.exit_date - x.entry_date).days for r in rs for x in r.trades
         if getattr(x, "exit_date", None) is not None and getattr(x, "entry_date", None) is not None]
    return float(np.median(d)) if d else float("nan")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=SEEDS)
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    a = ap.parse_args()
    today = date.today().isoformat()
    js = st.RESULTS_DIR / f"strategy-tournament-{today}-reversal.json"

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    raws = sorted(bt.CACHE_DIR.glob(FILTER_RAW))
    if not raws:
        raise SystemExit(f"기존 26전략 기준선({FILTER_RAW})이 {bt.CACHE_DIR} 에 없음 — filter_tournament 먼저")
    base_raw = pickle.loads(raws[-1].read_bytes())
    new = {}
    metas = {}
    for u in ("sp500", "watchlist"):
        b = bt.load_research_bundle(u, bt.DEV_START, bt.DEV_END)
        metas[u] = b["meta"]
        log(f"[{u}] 번들 {b['meta']['loaded']}/{b['meta']['requested']}종목")
        new[u] = run_universe(u, b, a.seeds, a.workers, log)

    prior = st.load_prior("auto", exclude=js)
    prior_trials = [t for p in prior for t in p["trials"]]
    agg = {u: {k: st.aggregate_seeds(rs) for k, rs in base_raw[(u, "base")].items()} for u in new}
    for u in new:
        agg[u][KEY] = st.aggregate_seeds(new[u])
    new_trials = [{"variant": "reversal", "universe": u, "key": KEY,
                   "sr": bt.sharpe_moments(agg[u][KEY][0].daily_returns)["sr"]} for u in new]
    srs = [t["sr"] for t in prior_trials + new_trials]
    n_trials, sr_var = len(srs), float(np.var(srs, ddof=1))
    log(f"시도 수 N={n_trials} (이전 {len(prior_trials)} + 이번 {len(new_trials)}) · V={sr_var:.3e}")

    summ, rows, bench = {}, {}, {}
    for u in new:
        summ[u] = bt.summarize_results({k: v[0] for k, v in agg[u].items()}, n_trials=n_trials,
                                       sr_var=sr_var, family_threshold=bt.FAMILY_CORR)
        bench[u] = summ[u]["benchmark"]
        rows[u] = {}
        for r in summ[u]["rows"]:
            row = st.apply_seed_stats(r, agg[u][r["key"]][1], bench[u], n_trials, sr_var)
            row["verdict"] = st.dev_verdict(row, bench[u])
            rows[u][r["key"]] = row
    corr, fam = st.combined_families(summ, bt.FAMILY_CORR)
    c = corr[KEY].drop(KEY).sort_values(ascending=False)
    extra = {u: {"median_hold_days": holding_days(new[u]),
                 "trades_per_seed": float(np.mean([len(r.trades) for r in new[u]]))} for u in new}
    out = {"date": today, "window": [bt.DEV_START, bt.DEV_END], "tag": "reversal", "seeds": a.seeds,
           "tie_break": "random", "run_kwargs": st.RUN_KW,
           "preregistered": {"key": KEY, "name": NAME, "lookback": LOOKBACK, "scale": SCALE,
                             "score": "50 + 50·tanh(−(r5 − 섹터ETF r5)/0.10); 섹터 ETF 없으면 SPY"},
           "n_trials": n_trials, "sr_var": sr_var,
           "prior": [{"path": str(p["path"].relative_to(ROOT)), "n_trials": len(p["trials"])} for p in prior],
           "new_trials": new_trials, "benchmark": bench, "extra": extra,
           "family": fam.get(KEY), "family_members": sorted(k for k, f in fam.items() if f == fam.get(KEY)),
           "corr_top": {k: round(float(v), 3) for k, v in c.head(5).items()},
           "rows": {u: {k: st._clean_row(r) for k, r in rows[u].items()} for u in new},
           "meta": metas}
    js.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    log(f"저장: {js}")
    for u in new:
        r, b = rows[u][KEY], bench[u]
        rank = sorted(rows[u], key=lambda k: -rows[u][k]["sharpe"]).index(KEY) + 1
        log(f"[{u}] {KEY}: 샤프 {r['sharpe']:.2f}±{r.get('sharpe_sd', 0):.2f} (SPY {b['sharpe']:.2f}) "
            f"총수익 {r['ret']:+.1%} MDD {r['mdd']:.1%} 보정샤프 {r['sr_deflated']:+.2f} DSR {r['dsr']:.3f} "
            f"거래 {r['trades']:.0f} → {'통과' if r['verdict']['all'] else '탈락'} · 샤프 순위 {rank}/{len(rows[u])}")
    log(f"계열 {fam.get(KEY)}: {out['family_members']} · 상관 상위 {out['corr_top']}")


if __name__ == "__main__":
    main()
