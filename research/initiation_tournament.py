#!/usr/bin/env python3
"""애널리스트 '신규 커버리지 매수' 20일 전략 1개(사전 등록)를 규칙 전략 토너먼트 기준으로 측정한다.

사전 등록(2026-10-21, 실행 전 커밋 — 정의·상수를 결과를 본 뒤 바꾸지 않는다)
  전략 initiation_buy_20d — 신규 커버리지 매수 20거래일
    근거: 애널리스트 이벤트 스터디 X1(신규 커버리지 매수·강력매수 → 20일 매칭 대비 +0.47%, t 2.29, 반분 양쪽 +,
          6/7년, 달력시간 SPY 대비 연 +9.7% t 3.18). 약 70칸 중 사후 주목한 후보다.
    이벤트 = Yahoo 등급 이력의 행동 init 이면서 새 등급 4·5(매수·강력매수). 발표일(동부) 의 첫 거래일 k,
            장후(16:00 ET 이후) 발표는 k+1 — 그날 종가 판단에 쓸 수 없는 정보는 다음 날부터.
    나이 a = (오늘 거래일 위치) − (그 종목의 가장 최근 이벤트 거래일). 0 ≤ a < 20 이면
    점수 = 100 − 2·a  (당일 100 → 19일째 62, 매수 문턱 60 위), 그 밖은 0 → 엔진 매도 문턱(35) 아래라
            20거래일이 지나면 다음 리밸런싱에 판다. 같은 나이 동점은 토너먼트 표준 무작위 동점 처리(seed 평균).
  그 밖의 모든 것은 토너먼트와 같다: RUN_KW, 개발 구간 2020-01-01~2024-12-31 번들, 무작위 동점 seed 10,
  상장 252봉 미만 신규 후보 제외(엔진 고정 규칙 — 스터디에선 상장 1년 미만 종목이 이벤트의 약 1/6, 효과가 더 컸다).
  시도 수: --prior auto 누적(토너먼트 52 + 필터 208 + 동점 재실행 104 + 반전 2 = 366) + 이번 2.
  판정: 토너먼트 기준 3개(샤프 ≥ SPY · MDD ≥ SPY−5%p · 운 보정 샤프 > 0), S&P 500(시점별 편입) 기준.

정직성
  • 사후에 고른 후보를 같은 시기(스터디 2018–2024 ⊃ 개발 구간)로 다시 재는 표본 내 확인이다. 통과해도 확인이
    아니고, 실패하면 후보를 버린다. 봉인 구간(2025+)은 이미 1회 쓰였고 열지 않는다.
  • 라이브 코드·strategy_catalog·backtester 는 건드리지 않는다. 점수는 작업 프로세스 안에서만 등록한다.

실행 (백그라운드 권장):
  TRADE_BT_CACHE=~/.cache/trade_research/bt_cache python3 -P research/initiation_tournament.py
"""
from __future__ import annotations

import argparse
import json
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
import pandas as pd                     # noqa: E402

import backtester as bt                 # noqa: E402
import strategy_tournament as st        # noqa: E402
from reversal_tournament import FILTER_RAW, holding_days   # noqa: E402

KEY = "initiation_buy_20d"
NAME = "신규 커버리지 매수 20일"
WINDOW = 20
SEEDS = 10
ANALYST_RAW = Path.home() / ".cache" / "trade_research" / "analyst" / "yahoo_ud"


def initiation_events(tickers) -> pd.DataFrame:
    import analyst_event_study as aes
    parts = []
    for t in tickers:
        p = ANALYST_RAW / f"{t}.parquet"
        if p.exists():
            d = pd.read_parquet(p)
            if len(d):
                d["ticker"] = t
                parts.append(d)
    a = pd.concat(parts, ignore_index=True)
    a["ts"] = pd.to_datetime(a["GradeDate"])
    assert a["ts"].max() < pd.Timestamp(bt.HOLDOUT_START), "봉인 구간 행"
    et = a["ts"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    a["date"] = et.dt.tz_localize(None).dt.normalize()
    a["after_close"] = et.dt.hour >= 16
    ok = (a["Action"].fillna("").str.strip().str.lower() == "init") & (aes.lvl(a["ToGrade"]) >= 4)
    return a.loc[ok, ["ticker", "date", "after_close"]].drop_duplicates()


def last_event_index(b: dict, ev: pd.DataFrame) -> dict:
    """종목별 배열: 각 거래일 위치에서 '가장 최근 이벤트 거래일 위치'(없으면 −1)."""
    di = b["date_index"]
    T = len(di)
    out = {}
    for t in b["stock_data"]:
        sig = np.zeros(T, bool)
        for d, ac in ev.loc[ev["ticker"] == t, ["date", "after_close"]].itertuples(index=False):
            k = int(di.searchsorted(d, side="left")) + (1 if ac else 0)
            if k < T:
                sig[k] = True
        idx = np.where(sig, np.arange(T), -1)
        out[t] = np.maximum.accumulate(idx)
    return out


def initiation_score(ticker, stock_data, etf_data, idx) -> float:
    last = st._BUNDLE["init_last"].get(ticker)
    if last is None or idx >= len(last) or last[idx] < 0:
        return 0.0
    a = idx - int(last[idx])
    return float(100 - 2 * a) if 0 <= a < WINDOW else 0.0


def _init(path: str):
    st._init_worker(path)
    orig = bt._strategy_score_bt

    def patched(strategy, ticker, stock_data, etf_data, idx):
        if strategy == KEY:
            return initiation_score(ticker, stock_data, etf_data, idx)
        return orig(strategy, ticker, stock_data, etf_data, idx)

    bt._strategy_score_bt = patched


def run_universe(u: str, bundle: dict, seeds: int, workers: int, log) -> list:
    path = bt.CACHE_DIR / f"initiation_bundle_{u}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    tasks = [(KEY, "random", s) for s in range(seeds)]
    out = {}
    try:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(str(path),)) as ex:
            for task, r, al, dt in ex.map(st._run_task, tasks):
                out[task] = r
                log(f"  {u:9} seed {task[2]:>2} {r.total_return:+8.1%} 샤프 {r.sharpe:5.2f} 거래 {len(r.trades)} ({dt:.0f}s)")
    finally:
        path.unlink(missing_ok=True)
    return [out[t] for t in tasks]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=SEEDS)
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    a = ap.parse_args()
    today = date.today().isoformat()
    js = st.RESULTS_DIR / f"strategy-tournament-{today}-initiation.json"

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    raws = sorted(bt.CACHE_DIR.glob(FILTER_RAW))
    if not raws:
        raise SystemExit(f"기존 26전략 기준선({FILTER_RAW})이 {bt.CACHE_DIR} 에 없음 — filter_tournament 먼저")
    base_raw = pickle.loads(raws[-1].read_bytes())
    new, metas, cover = {}, {}, {}
    for u in ("sp500", "watchlist"):
        b = bt.load_research_bundle(u, bt.DEV_START, bt.DEV_END)
        metas[u] = b["meta"]
        ev = initiation_events(list(b["stock_data"]))
        b["init_last"] = last_event_index(b, ev)
        e = b["eval_start_idx"]
        n_ev = int(sum(((np.diff(np.r_[-1, v]) > 0) & (np.arange(len(v)) >= e)).sum() for v in b["init_last"].values()))
        cover[u] = {"events_in_eval": n_ev, "tickers_with_history": int(sum((ANALYST_RAW / f"{t}.parquet").exists()
                                                                          for t in b["stock_data"]))}
        log(f"[{u}] 번들 {b['meta']['loaded']}/{b['meta']['requested']}종목 · 평가 구간 이벤트 {n_ev:,}건")
        new[u] = run_universe(u, b, a.seeds, a.workers, log)

    prior = st.load_prior("auto", exclude=js)
    prior_trials = [t for p in prior for t in p["trials"]]
    agg = {u: {k: st.aggregate_seeds(rs) for k, rs in base_raw[(u, "base")].items()} for u in new}
    for u in new:
        agg[u][KEY] = st.aggregate_seeds(new[u])
    new_trials = [{"variant": "initiation", "universe": u, "key": KEY,
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
                 "trades_per_seed": float(np.mean([len(r.trades) for r in new[u]])), **cover[u]} for u in new}
    out = {"date": today, "window": [bt.DEV_START, bt.DEV_END], "tag": "initiation", "seeds": a.seeds,
           "tie_break": "random", "run_kwargs": st.RUN_KW,
           "preregistered": {"key": KEY, "name": NAME, "window": WINDOW,
                             "event": "Yahoo action=init & 새 등급 4·5, 장후 발표는 다음 거래일",
                             "score": "0 ≤ a < 20 이면 100 − 2a (a = 최근 이벤트 후 거래일 수), 아니면 0"},
           "n_trials": n_trials, "sr_var": sr_var,
           "prior": [{"path": str(p["path"].relative_to(ROOT)), "n_trials": len(p["trials"])} for p in prior],
           "new_trials": new_trials, "benchmark": bench, "extra": extra,
           "family": fam.get(KEY), "family_members": sorted(k for k, f in fam.items() if f == fam.get(KEY)),
           "corr_top": {k: round(float(v), 3) for k, v in c.head(5).items()},
           "rows": {u: {k: st._clean_row(r) for k, r in rows[u].items()} for u in new},
           "meta": metas}
    js.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    raw_out = bt.CACHE_DIR / f"initiation-raw-{today}.pkl"                # 진단용(거래 내역)
    raw_out.write_bytes(pickle.dumps(new))
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
