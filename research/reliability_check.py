"""백테스트 신뢰성 점검 — 체결·손절·결정 주기 가정이 결과를 얼마나 바꾸나 (2026-10-21).

토너먼트(research/strategy_tournament.py)와 같은 개발 구간(2020–24)·S&P 500 시점별 편입 번들·
같은 매매 규칙(RUN_KW)으로, 체결 가정만 단계적으로 현실에 가깝게 바꿔 다시 돌린다.

  A 현행        : 결정한 날 종가에 체결, 손절·트레일링은 7거래일마다 종가로만 판정 (토너먼트 그대로)
  B 다음날 시가 : 결정은 종가까지의 정보로, 체결은 다음 거래일 시가 (같은 봉 체결 제거)
  C B + 매일 손절: 매 거래일 고가·저가로 손절·트레일링(증권사 stop 주문 근사, 갭은 시가 체결)
  D C + 매일 결정: 리밸런싱 판단도 매일 — 라이브(5분 루프)의 '자주 판단'에 가장 가까운 일봉 근사

새 전략 시도가 아니라 같은 전략의 체결 가정 민감도라 토너먼트 시도 수 N 에 넣지 않는다.
봉인 구간(2025~)은 쓰지 않는다. A 의 연속 점수 전략은 토너먼트 JSON 과 같아야 한다(재현 확인).

실행: python3 research/reliability_check.py [--workers 6]          # 대표 5개 전략, 자세히
      python3 research/reliability_check.py --all [--seeds 2]        # 26개 전략 전부, 무작위 동점 seed 평균
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np            # noqa: E402

import backtester as bt       # noqa: E402
import strategy_catalog as scat   # noqa: E402
from research.strategy_tournament import EXCLUDE, RUN_KW   # noqa: E402

RESULTS = ROOT / "research" / "results"
PRIOR = RESULTS / "strategy-tournament-2026-10-20.json"

VARIANTS = {
    "A": ("현행(종가 체결·7일마다 손절 판정)", {}),
    "B": ("다음날 시가 체결", {"execution": "next_open"}),
    "C": ("시가 체결 + 매일 손절", {"execution": "next_open", "daily_stops": True}),
    "D": ("C + 매일 결정(라이브 근사)", {"execution": "next_open", "daily_stops": True,
                                       "rebalance_days": 1}),
}
CONTINUOUS = ["dual_momentum", "momentum", "momentum_max", "sector_rotation"]
DISCRETE = "ichimoku"            # 동점이 많은 전략 — 무작위 동점 seed 평균
SEEDS_ABC = 5
SEEDS_D = 2

_BUNDLE = None


def _init(path: str):
    global _BUNDLE
    with open(path, "rb") as f:
        _BUNDLE = pickle.load(f)


def metrics(r, capital: float, cost_side: float) -> dict:
    tr = r.trades
    holds = [(t.exit_date - t.entry_date).days for t in tr]
    reasons = Counter(str(t.reason).split(" ")[0].split("(")[0] for t in tr)
    traded = sum((t.entry_price + t.exit_price) * t.shares for t in tr)
    years = max(1e-9, len(r.equity_curve) / 252)
    return {"ret": r.total_return, "cagr": r.cagr, "mdd": r.mdd, "sharpe": r.sharpe,
            "trades": len(tr), "avg_hold": float(np.mean(holds)) if holds else 0.0,
            "med_hold": float(np.median(holds)) if holds else 0.0,
            "reasons": dict(reasons),
            "cost_pct_per_year": traded * cost_side / capital / years,
            "turnover_per_year": traded / 2 / capital / years}


def _task(task):
    key, var, seed = task
    t0 = time.time()
    b = _BUNDLE if seed is None else dict(_BUNDLE, tie_break="random", tie_seed=seed)
    r = bt.run(strategy=key, prefetched=b, **{**RUN_KW, **VARIANTS[var][1]})
    m = metrics(r, RUN_KW["capital"], 0.001 + 0.0005)
    m["bench"] = r.benchmark
    return task, m, time.time() - t0


def tasks_all(seeds: int) -> list:
    """26개 전략 × 4 변형 × 무작위 동점 seed — 연속 점수 전략은 seed 와 무관하게 같은 값이 나온다."""
    keys = [k for k in scat.CATALOG if k not in EXCLUDE]
    out = [(k, var, s) for var in VARIANTS for k in keys for s in range(seeds)]
    return sorted(out, key=lambda t: t[1] != "D")


def tasks() -> list:
    out = []
    for var in VARIANTS:
        for k in CONTINUOUS:
            out.append((k, var, None))           # 연속 점수 — 동점이 거의 없어 seed 무관
        n = SEEDS_D if var == "D" else SEEDS_ABC
        out += [(DISCRETE, var, s) for s in range(n)]
    # 무거운 D 를 먼저 — 풀을 고르게 채운다
    return sorted(out, key=lambda t: t[1] != "D")


def aggregate(raw: dict) -> dict:
    rows: dict = {}
    for (k, var, seed), m in raw.items():
        rows.setdefault(var, {}).setdefault(k, []).append(m)
    out: dict = {}
    for var, by_k in rows.items():
        for k, ms in by_k.items():
            agg = {f: float(np.mean([m[f] for m in ms])) for f in
                   ("ret", "cagr", "mdd", "sharpe", "trades", "avg_hold", "med_hold",
                    "cost_pct_per_year", "turnover_per_year")}
            agg["sharpe_sd"] = float(np.std([m["sharpe"] for m in ms])) if len(ms) > 1 else 0.0
            agg["n_seeds"] = len(ms)
            rs = Counter()
            for m in ms:
                rs.update(m["reasons"])
            agg["reasons"] = {r: v / len(ms) for r, v in rs.most_common()}
            out.setdefault(var, {})[k] = agg
    return out


def verify(agg: dict) -> dict:
    """A 의 연속 점수 전략 = 토너먼트 원자료(동점 처리 없음) 그대로인지."""
    prior = json.loads(PRIOR.read_text())["rows"]["sp500"]
    diffs = {k: abs(agg["A"][k]["ret"] - prior[k]["ret"]) for k in CONTINUOUS if k in prior}
    return {"max_abs_ret_diff": max(diffs.values()) if diffs else None, "per_key": diffs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--all", action="store_true", help="26개 전략 전부")
    ap.add_argument("--seeds", type=int, default=2, help="--all 의 무작위 동점 seed 수")
    a = ap.parse_args()
    t0 = time.time()
    bundle = bt.load_research_bundle("sp500", bt.DEV_START, bt.DEV_END)
    path = bt.CACHE_DIR / "bundle_reliability.pkl"
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"번들 {bundle['meta']['loaded']}/{bundle['meta']['requested']} ({time.time() - t0:.0f}s)",
          flush=True)
    raw = {}
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                             initargs=(str(path),)) as ex:
        for task, m, dt in ex.map(_task, tasks_all(a.seeds) if a.all else tasks()):
            raw[task] = m
            print(f"  {task[1]} {task[0]:16} seed={task[2]} {m['ret']:+7.1%} 샤프 {m['sharpe']:5.2f} "
                  f"MDD {m['mdd']:6.1%} 거래 {m['trades']:4d} 보유중앙 {m['med_hold']:4.0f}일 "
                  f"({dt:.0f}s)", flush=True)
    path.unlink(missing_ok=True)
    agg = aggregate(raw)
    bench = next(iter(raw.values()))["bench"]
    out = {"date": date.today().isoformat(), "window": [bt.DEV_START, bt.DEV_END],
           "universe": "sp500", "run_kwargs": RUN_KW,
           "variants": {k: {"label": v[0], "kwargs": v[1]} for k, v in VARIANTS.items()},
           "seeds": {"ABC": SEEDS_ABC, "D": SEEDS_D}, "benchmark": bench,
           "mode": "all" if a.all else "focus",
           "verify_A_vs_tournament": None if a.all else verify(agg), "results": agg,
           "meta": {k: v for k, v in bundle["meta"].items() if isinstance(v, (str, int, float, bool))}}
    dest = RESULTS / f"reliability-{date.today().isoformat()}{'-all' if a.all else ''}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    v = out["verify_A_vs_tournament"]
    print(f"저장 {dest} · 재현 확인 {v and v['max_abs_ret_diff']} · 총 {time.time() - t0:.0f}s",
          flush=True)


if __name__ == "__main__":
    main()
