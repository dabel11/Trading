"""
규칙 전략 토너먼트 — 정직한 평가 기준으로 strategy_catalog 의 규칙 전략을 겨룬다.

기준(backtester 의 '정직한 평가 기준' 절):
  • 개발 구간 2020-01-01 ~ 2024-12-31 만 사용 (2025-01-01~ 봉인)
  • 같은 구간 SPY(수정주가)와 항상 같이 본다
  • 시도한 전략 수(전략 × 유니버스) 만큼 샤프를 깎는다 — Deflated Sharpe Ratio
  • 일간 수익률 상관 + 계층 군집으로 '계열'을 묶고, 계열별 대표만 고른다
    (대표 = S&P 500 유니버스 DSR 최고 — 손으로 고른 23종목은 선택 편향이 커서 보조)

동점 처리: 이산 점수 전략은 수십~백수십 종목이 같은 점수다. 기본은 무작위 동점 처리를 seed K개로
  평균(지표 = seed 평균 ± sd, DSR·상관 = 평균 곡선). 이전 토너먼트 시도는 DSR 의 N·V 에 합친다.

실행 (긴 작업 — 백그라운드 권장):
  python3 research/strategy_tournament.py                      # 개발 구간, 무작위 동점 K=20
  python3 research/strategy_tournament.py --tie-break random,dollar_volume --verify-prior
  python3 research/strategy_tournament.py --tie-break none     # 이전(입력 순서) 흐름 — 같은 날짜 결과를 덮어씀
  # 봉인 구간 1회 확인 — 사용자 승인 후, 개발 구간에서 고른 대표만:
  python3 research/strategy_tournament.py --holdout --keys a,b,c

결과: docs/strategy-tournament-<날짜>[-<tag>].md · research/results/strategy-tournament-<날짜>[-<tag>].json
라이브 경로(portfolio·core)는 건드리지 않는다 — backtester.run 이 결정 로직을 그대로 빌려 쓴다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADE_NOTIFY_DISABLED", "1")

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

import backtester as bt       # noqa: E402
import strategy_catalog as scat  # noqa: E402
from config import MIN_SCORE_TO_BUY, STOP_LOSS_PCT, TAKE_PROFIT_PCT  # noqa: E402

UNIVERSES = ("watchlist", "sp500")
PRIMARY = "sp500"
UNIVERSE_LABEL = {"sp500": "S&P 500 (시점별 편입 근사)", "watchlist": "기존 23종목 (손으로 고름)"}
EXCLUDE = {"ai_influencer"}          # LLM·실시간 의견 기반 — 과거 시점 재현 불가

# 모든 전략에 같은 규칙(시드·문턱·손절·익절·트레일링) — 신호만 다르게.
# use_trend_filter=False: 백테스트 전용 추가 보수 필터를 끄고 라이브와 같은 결정 경로.
RUN_KW = dict(capital=10_000.0, min_score=float(MIN_SCORE_TO_BUY),
              stop_loss=float(STOP_LOSS_PCT), take_profit=float(TAKE_PROFIT_PCT),
              trailing_stop=0.15, use_trend_filter=False, adaptive_regime=True,
              dynamic=True, ev_filter=False)

# 봉인 구간 통과 기준 (개발 구간에서 고른 대표에만 1회 적용)
PASS_MDD_SLACK = 0.05                # MDD 가 SPY 보다 5%p 넘게 나쁘면 탈락
RESULTS_DIR = ROOT / "research" / "results"
HOLDOUT_MARK = RESULTS_DIR / "holdout-opened.json"
TIE_SEEDS = 20                       # 무작위 동점 처리: 전략마다 이만큼 seed 를 돌려 평균

_BUNDLE = None
_ALPHA: dict = {}


def _init_worker(path: str):
    global _BUNDLE, _ALPHA
    with open(path, "rb") as f:
        _BUNDLE = pickle.load(f)
    names = sorted(_BUNDLE["stock_data"])
    _ALPHA = {t: i / max(1, len(names) - 1) for i, t in enumerate(names)}


def _run_one(key: str):
    t0 = time.time()
    r = bt.run(strategy=key, prefetched=_BUNDLE, **RUN_KW)
    return key, r, time.time() - t0


def alpha_position(r, alpha: dict) -> float:
    """산 종목들의 알파벳 위치 평균(0=맨 앞 'A…', 1=맨 뒤). 편향 없으면 ≈ 0.5."""
    pos = [alpha[t] for t, _ in {(x.ticker, x.entry_date) for x in r.trades} if t in alpha]
    return float(np.mean(pos)) if pos else float("nan")


def _run_task(task):
    """(전략, tie_break, seed) 1건. tie_break=None 이면 예전 동작(입력 순서)."""
    key, mode, seed = task
    t0 = time.time()
    b = _BUNDLE if mode is None else dict(_BUNDLE, tie_break=mode, tie_seed=seed)
    r = bt.run(strategy=key, prefetched=b, **RUN_KW)
    return task, r, alpha_position(r, _ALPHA), time.time() - t0


def run_universe(universe: str, keys: list[str], start: str, end: str,
                 holdout: bool, workers: int, log) -> tuple[dict, dict]:
    t0 = time.time()
    bundle = bt.load_research_bundle(universe, start, end, holdout=holdout)
    meta = bundle["meta"]
    log(f"[{universe}] 번들 {meta['loaded']}/{meta['requested']}종목 "
        f"({time.time() - t0:.0f}s) 평가 {meta['eval_start']}~{meta['eval_end']}")
    path = bt.CACHE_DIR / f"bundle_{universe}_{start}_{end}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    results: dict = {}
    if workers <= 1:
        _init_worker(str(path))
        it = map(_run_one, keys)
        for k, r, dt in it:
            results[k] = r
            log(f"  {universe:9} {k:20} {r.total_return:+8.1%} 샤프 {r.sharpe:5.2f} ({dt:.0f}s)")
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(str(path),)) as ex:
            for k, r, dt in ex.map(_run_one, keys):
                results[k] = r
                log(f"  {universe:9} {k:20} {r.total_return:+8.1%} 샤프 {r.sharpe:5.2f} ({dt:.0f}s)")
    try:
        path.unlink()
    except OSError:
        pass
    return results, meta


def run_tasks(universe: str, tasks: list, start: str, end: str, workers: int, log) -> tuple[dict, dict]:
    """개발 구간 번들 하나로 (전략, tie_break, seed) 작업들을 돌린다 → ({작업: (결과, 알파벳 위치)}, meta)."""
    t0 = time.time()
    bundle = bt.load_research_bundle(universe, start, end)
    meta = bundle["meta"]
    log(f"[{universe}] 번들 {meta['loaded']}/{meta['requested']}종목 ({time.time() - t0:.0f}s) "
        f"평가 {meta['eval_start']}~{meta['eval_end']} · 작업 {len(tasks)}건")
    path = bt.CACHE_DIR / f"bundle_{universe}_{start}_{end}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    del bundle
    out: dict = {}
    try:
        if workers <= 1:
            _init_worker(str(path))
            it = map(_run_task, tasks)
            ex = None
        else:
            ex = ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(str(path),))
            it = ex.map(_run_task, tasks)
        for task, r, al, dt in it:
            out[task] = (r, al)
            k, mode, seed = task
            log(f"  {universe:9} {str(mode or 'none'):13} {'' if seed is None else seed:>2} {k:20} "
                f"{r.total_return:+8.1%} 샤프 {r.sharpe:5.2f} 알파벳 {al:.2f} ({dt:.0f}s) "
                f"[{len(out)}/{len(tasks)}]")
        if ex:
            ex.shutdown()
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return out, meta


def _spread(xs) -> dict:
    a = np.asarray(xs, dtype=float)
    return {"mean": float(a.mean()), "sd": float(a.std(ddof=1)) if len(a) > 1 else 0.0,
            "min": float(a.min()), "max": float(a.max())}


def aggregate_seeds(rs: list) -> tuple:
    """같은 전략의 seed 별 결과 → (평균 곡선 BacktestResult, seed 통계).

    평균 곡선 = 시드를 K 등분해 seed 마다 한 계좌씩 굴린 합과 같다 — 동점 선택의 운을 평균낸
    '전략의 기대 수익 흐름'이라 DSR·상관에 쓴다. 다만 K 계좌로 분산된 효과가 있어 한 계좌의
    샤프보다 높게 나온다. 그래서 보고하는 수익·샤프·MDD 는 seed 평균 ± 표준편차(한 계좌 기대치)."""
    curve = pd.concat([r.equity_curve for r in rs], axis=1).mean(axis=1)
    mean = bt.BacktestResult(trades=[], equity_curve=curve, start_capital=rs[0].start_capital,
                             benchmark=rs[0].benchmark)
    seed = {"ret": _spread([r.total_return for r in rs]), "cagr": _spread([r.cagr for r in rs]),
            "mdd": _spread([r.mdd for r in rs]), "sharpe": _spread([r.sharpe for r in rs]),
            "win": _spread([r.win_rate for r in rs]), "trades": _spread([len(r.trades) for r in rs])}
    return mean, {"n_seeds": len(rs), "seed": seed, "mdds": [r.mdd for r in rs],
                  "moments": [bt.sharpe_moments(r.daily_returns) for r in rs]}


def apply_seed_stats(row: dict, st: dict, bench: dict, n_trials: int, sr_var: float) -> dict:
    """summarize_results 의 평균 곡선 행 → 보고용 행. 수익·샤프·MDD·승률·거래 = seed 평균
    (curve_* 에 평균 곡선 값 보존). DSR·보정 샤프·왜도·첨도·상관은 평균 곡선 그대로."""
    row = dict(row)
    row.update(curve_ret=row["ret"], curve_sharpe=row["sharpe"], curve_mdd=row["mdd"],
               n_seeds=st["n_seeds"])
    for m, s in st["seed"].items():
        row[m] = s["mean"]
        row[f"{m}_sd"], row[f"{m}_min"], row[f"{m}_max"] = s["sd"], s["min"], s["max"]
    ds = [bt.deflated_sharpe(mo["sr"], mo["T"], mo["skew"], mo["kurt"], n_trials, sr_var)
          for mo in st["moments"]]
    sharpes = [mo["sr"] * math.sqrt(252) for mo in st["moments"]]
    row["seed_dsr_median"] = float(np.median([d["dsr"] for d in ds]))
    row["seed_sr_deflated_mean"] = float(np.mean([d["sr_deflated"] for d in ds]))
    row["seed_beat_spy"] = float(np.mean([s >= bench["sharpe"] for s in sharpes]))
    row["seed_pass_all"] = float(np.mean([
        s >= bench["sharpe"] and m >= bench["mdd"] - PASS_MDD_SLACK and d["sr_deflated"] > 0
        for s, m, d in zip(sharpes, st["mdds"], ds)]))
    return row


def load_prior(spec: str, exclude: Path | None = None) -> list[dict]:
    """이전 토너먼트 결과(JSON) → 그때의 시도들. DSR 의 N·V 에 넣어 '재시도'를 숨기지 않는다.

    spec: "auto"(research/results/strategy-tournament-*.json 전부) · "none" · 쉼표 구분 경로.
    새 형식은 new_trials(그 실행에서 새로 한 시도만)를 쓰고, 옛 형식은 rows 전부가 시도다
    — 재실행 결과가 앞선 시도를 다시 담고 있어도 이중으로 세지 않는다."""
    if spec == "none":
        return []
    paths = (sorted(RESULTS_DIR.glob("strategy-tournament-*.json")) if spec == "auto"
             else [Path(p) if Path(p).is_absolute() else ROOT / p for p in spec.split(",") if p])
    out = []
    for p in paths:
        if exclude is not None and p.resolve() == exclude.resolve():
            continue
        d = json.loads(p.read_text())
        trials = d.get("new_trials") or [
            {"variant": "none", "universe": u, "key": k, "sr": r["sharpe"] / math.sqrt(252)}
            for u, rows in d["rows"].items() for k, r in rows.items()]
        out.append({"path": p, "data": d, "trials": trials})
    return out


# ── 선정 ─────────────────────────────────────────────────────────────────────

def pooled_trials(all_results: dict) -> tuple[int, float]:
    """전략 × 유니버스 전체 시도 → (N, 시도 샤프(일간) 분산)."""
    srs = [bt.sharpe_moments(r.daily_returns)["sr"]
           for res in all_results.values() for r in res.values()]
    return len(srs), (float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0)


def combined_families(summaries: dict, threshold: float) -> tuple[pd.DataFrame, dict]:
    """유니버스별 상관행렬의 평균으로 한 번에 군집 — 계열 정의가 유니버스마다 갈리지 않게."""
    mats = [s["corr"] for s in summaries.values()]
    keys = list(mats[0].columns)
    avg = sum(m.reindex(index=keys, columns=keys).fillna(0.0) for m in mats) / len(mats)
    for k in keys:
        avg.loc[k, k] = 1.0
    return avg, bt.cluster_by_correlation(avg, threshold)


def pick_representatives(families: dict, rows: dict, primary: str) -> dict:
    """계열별 대표: 주 유니버스 DSR 최고(동률이면 샤프)."""
    by_fam: dict = {}
    for k, f in families.items():
        by_fam.setdefault(f, []).append(k)
    reps = {}
    for f, members in sorted(by_fam.items()):
        reps[f] = max(members, key=lambda k: (rows[primary][k]["dsr"], rows[primary][k]["sharpe"]))
    return reps


# ── 실행 파라미터 (라이브 설정 맞춤용) ──────────────────────────────────────────

def effective_params() -> list[tuple[str, object, str, str]]:
    """백테스트가 실제로 쓴 값 전부 → [(파라미터, 값, autotrader_config 키, 비고)].
    run() 기본값 + RUN_KW + config 를 합쳐 '실제로 적용된 값'을 만든다."""
    import inspect
    import config as cfg
    import portfolio as pf
    d = {k: v.default for k, v in inspect.signature(bt.run).parameters.items()
         if v.default is not inspect.Parameter.empty}
    d.update(RUN_KW)
    fill = {"max_positions": cfg.MAX_POSITIONS, "max_position_pct": cfg.MAX_POSITION_PCT,
            "sell_score": cfg.SELL_SCORE_THRESHOLD, "hold_strong": cfg.HOLD_DAYS_STRONG,
            "hold_medium": cfg.HOLD_DAYS_MEDIUM, "rebalance_days": bt.REBALANCE_DAYS}
    for k, v in fill.items():
        if d.get(k) is None:
            d[k] = v
    ev_on = bool(d["ev_filter"])
    return [
        ("유니버스", f"{UNIVERSE_LABEL[PRIMARY]} (보조: {UNIVERSE_LABEL['watchlist']})", "—",
         "라이브 워치리스트와 다름 — 아래 '설정으로 못 맞추는 차이' 참고"),
        ("min_score (매수 문턱)", d["min_score"], "min_score", ""),
        ("sell_score (점수 붕괴 매도)", d["sell_score"], "sell_score", ""),
        ("stop_loss (손절)", d["stop_loss"], "stop_loss", ""),
        ("take_profit (익절 기준 — 도달 후 트레일링)", d["take_profit"], "take_profit", ""),
        ("trailing_stop (고점 대비 되돌림)", d["trailing_stop"], "trail", ""),
        ("hold_strong / hold_medium (보유 일수)", f"{d['hold_strong']} / {d['hold_medium']}",
         "hold_strong / hold_medium", ""),
        ("dynamic (점수 비례 배분)", d["dynamic"], "dynamic", ""),
        ("max_positions / max_position_pct", f"{d['max_positions']} / {d['max_position_pct']}",
         "— (config.py)", f"lot_plan 최소 단위 ${pf.MIN_POSITION_VALUE:,.0f} — 라이브와 같은 코드"),
        ("ev_filter (EV 판단)", ev_on, "ev_enabled",
         "**라이브는 키가 없으면 켜짐(core/cycle.py 기본 True) — 반드시 `false` 로 명시**"
         if not ev_on else "켬"),
        ("ev_min / ev_exit", f"{d['ev_min']} / {d['ev_exit']}", "ev_min / ev_exit",
         "ev_filter=False 라 미사용" if not ev_on else ""),
        ("adaptive_regime (약세장 신규 매수 보류)", d["adaptive_regime"], "— (라이브 고정 동작)", "라이브와 동일"),
        ("use_trend_filter (백테스트 전용 중립장 필터)", d["use_trend_filter"], "—",
         "끔 = 라이브와 동일"),
        ("risk_sizing", d["risk_sizing"], "—", "구 옵션, 미사용"),
        ("capital (시드)", d["capital"], "—", "lot_plan 이 자본에 따라 종목 수 조절"),
        ("commission (편도 수수료)", d["commission"], "—", "백테스트 비용 가정"),
        ("slippage (편도)", d["slippage"], "—", "백테스트 비용 가정"),
        ("rebalance_days (재검토 주기)", d["rebalance_days"], "— (interval)",
         "백테스트는 7거래일마다 종가 1회 — 라이브는 interval 초마다"),
    ]


def dev_verdict(row: dict, bench: dict) -> dict:
    """봉인 구간 통과 기준을 개발 구간에 그대로 대 본 결과(참고용 — 선정 기준 아님)."""
    s = row["sharpe"] >= bench["sharpe"]
    m = row["mdd"] >= bench["mdd"] - PASS_MDD_SLACK
    d = row["sr_deflated"] > 0
    return {"sharpe": s, "mdd": m, "dsr": d, "all": s and m and d}


# ── 리포트 ───────────────────────────────────────────────────────────────────

def _pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{d}f}%"


def _num(x, d=2):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def _name(k):
    return scat.CATALOG.get(k, {}).get("name", k)


def _cat(k):
    return scat.CATALOG.get(k, {}).get("category", "")


def _family_label(members):
    c = Counter(_cat(k) for k in members).most_common()
    return "·".join(x for x, _ in c[:2])


def _result_table(rows: dict, fam: dict, bench: dict) -> list[str]:
    out = ["| # | 전략 | 계열 | 총수익 | CAGR | MDD | 샤프 | 샤프−SPY | DSR | 보정 샤프 | 거래 | 승률 |",
           "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    order = sorted(rows, key=lambda k: -rows[k]["dsr"])
    for i, k in enumerate(order, 1):
        r = rows[k]
        out.append(f"| {i} | {_name(k)} `{k}` | {fam[k]} | {_pct(r['ret'], 0)} | {_pct(r['cagr'])} | "
                   f"{_pct(r['mdd'])} | {_num(r['sharpe'])} | {r['sharpe'] - bench['sharpe']:+.2f} | "
                   f"{_num(r['dsr'], 3)} | {r['sr_deflated']:+.2f} | {r['trades']} | {r['win'] * 100:.0f}% |")
    return out


def write_report(path: Path, ctx: dict):
    rows, fam, reps, bench = ctx["rows"], ctx["families"], ctx["reps"], ctx["bench"]
    metas, corr = ctx["metas"], ctx["corr"]
    P = PRIMARY
    sp = metas[P]
    by_fam: dict = {}
    for k, f in fam.items():
        by_fam.setdefault(f, []).append(k)
    n_beat = {u: sum(1 for r in rows[u].values() if r["sharpe"] >= bench[u]["sharpe"]) for u in rows}
    n_dsr = {u: sum(1 for r in rows[u].values() if r["dsr"] > 0.5) for u in rows}
    n_dsr95 = {u: sum(1 for r in rows[u].values() if r["dsr"] >= 0.95) for u in rows}
    n_all = {u: sum(1 for r in rows[u].values() if dev_verdict(r, bench[u])["all"]) for u in rows}
    n_two = {u: [k for k, r in rows[u].items()
                 if sum(dev_verdict(r, bench[u])[c] for c in ("sharpe", "mdd", "dsr")) >= 2
                 and not dev_verdict(r, bench[u])["all"]] for u in rows}
    off = corr.to_numpy()[~np.eye(len(corr), dtype=bool)]
    dev_cands = [reps[f] for f in sorted(reps)
                 if sum(dev_verdict(rows[P][reps[f]], bench[P])[c] for c in ("sharpe", "mdd", "dsr")) >= 2]

    L = [f"# 규칙 전략 토너먼트 — {ctx['today']}",
         "",
         f"개발 구간 **{ctx['start']} ~ {ctx['end']}** (평가 {sp['eval_start']} ~ {sp['eval_end']}). "
         f"봉인 구간 {bt.HOLDOUT_START}~ 은 열지 않았다. 생성: `research/strategy_tournament.py`.",
         "",
         "## 요약 (자동 생성)",
         "",
         f"- 시도 수 N = **{ctx['n_trials']}** (규칙 전략 {len(ctx['keys'])}개 × 유니버스 {len(rows)}개). "
         f"운으로 기대되는 최대 샤프 SR0 = **{rows[P][ctx['keys'][0]]['sr0']:.2f}** (연율).",
         *[f"- {UNIVERSE_LABEL[u]}: 샤프가 같은 구간 SPY({bench[u]['sharpe']:.2f}) 이상 **{n_beat[u]}개**, "
           f"DSR>0.5(운 보정 후 양의 실력) **{n_dsr[u]}개**, DSR≥0.95(통상 유의) **{n_dsr95[u]}개** / {len(rows[u])}"
           for u in rows],
         *[f"- **봉인 구간 통과 기준을 개발 구간에 그대로 대 보면** ({UNIVERSE_LABEL[u]}): 세 조건 모두 만족 "
           f"**{n_all[u]}개**" + (f", 두 조건만 만족: {', '.join(f'`{k}`' for k in n_two[u])}" if n_two[u] else "")
           for u in rows],
         f"- 계열 {len(by_fam)}개 (평균 상관 ≥ {ctx['threshold']:.2f} 이면 같은 계열; 전략 간 평균 상관의 "
         f"중앙값 {np.median(off):.2f}, 범위 {off.min():.2f}~{off.max():.2f}). 대표는 {UNIVERSE_LABEL[P]} DSR 최고:",
         ""]
    L += ["| 계열 | 성격 | 구성 | 대표 | 대표 샤프 (S&P/23) | SPY 샤프 | 대표 DSR (S&P/23) | 대표 MDD (S&P) | 개발 구간 SPY 대비 |",
          "|---|---|---|---|---:|---:|---:|---:|---|"]
    for f in sorted(by_fam):
        members = by_fam[f]
        rep = reps[f]
        rp, rw = rows[P][rep], rows["watchlist"][rep] if "watchlist" in rows else None
        beat = rp["sharpe"] >= bench[P]["sharpe"]
        mdd_ok = rp["mdd"] >= bench[P]["mdd"] - PASS_MDD_SLACK
        verdict = ("샤프 ≥ SPY" if beat else "샤프 < SPY") + (" · MDD 허용" if mdd_ok else " · MDD 초과")
        L.append(f"| {f} | {_family_label(members)} | {', '.join(f'`{k}`' for k in members)} | "
                 f"**{_name(rep)}** `{rep}` | {_num(rp['sharpe'])} / {_num(rw['sharpe']) if rw else '—'} | "
                 f"{_num(bench[P]['sharpe'])} | {_num(rp['dsr'], 3)} / {_num(rw['dsr'], 3) if rw else '—'} | "
                 f"{_pct(rp['mdd'])} | {verdict} |")

    L += ["", "## 평가 기준 (고정)", "",
          f"- **구간**: 개발 {ctx['start']}~{ctx['end']}. 지표 워밍업으로 그 이전 {bt.WARMUP_CAL_DAYS}일 데이터를 "
          f"쓰되 매매·평가는 {sp['eval_start']} 부터. {bt.HOLDOUT_START}~ 은 `holdout=True` 없이는 "
          "`check_window` 가 데이터 다운로드 전에 거부한다.",
          "- **벤치마크**: 같은 거래일의 SPY(yfinance `auto_adjust` 수정주가 = 배당 재투자 근사). "
          "샤프는 무위험이자 0, 일간 수익률 ×√252 — 전략과 같은 정의.",
          f"- **매매 규칙(전 전략 동일)**: 시드 ${RUN_KW['capital']:,.0f}, 매수 문턱 {RUN_KW['min_score']:.0f}점, "
          f"손절 {RUN_KW['stop_loss']:.0%}, 익절 기준 {RUN_KW['take_profit']:.0%}, 트레일링 {RUN_KW['trailing_stop']:.0%}, "
          f"{bt.REBALANCE_DAYS}거래일마다 리밸런싱, 수수료 0.1%·슬리피지 0.05%, 약세장 신규 매수 보류(라이브와 동일), "
          "백테스트 전용 중립장 필터 끔(라이브와 동일), EV 판단 끔. 전략별 권장 청산값(EXIT_PROFILES)은 쓰지 않았다 "
          "— 신호만 비교하기 위해.",
          "- **시점별 편입(S&P 500)**: `universe.get_index('S&P 500')` 현재 구성에서 출발해 위키 "
          "'Historical components of the S&P 500' 변경 이력을 되감아 날짜별 구성을 근사. 그날 구성종목이 아니면 "
          f"신규 매수 후보에서 제외(보유 중이면 규칙대로 관리). 상장 후 {bt.MIN_HISTORY_BARS}봉 미만은 신규 후보 제외. "
          "섹터 신호용으로 GICS 섹터 → 섹터 ETF 매핑(커뮤니케이션은 기존 관례대로 XLK).",
          f"- **운 보정 샤프(DSR)**: Bailey & López de Prado (2014). 일간 샤프·왜도·첨도, 관측 수 T, "
          f"시도 수 N={ctx['n_trials']}, 시도들 일간 샤프의 분산 V={ctx['sr_var']:.2e} 로 "
          "SR0 = √V·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))) 를 구하고 DSR = PSR(SR0). "
          "'보정 샤프' = 연율 샤프 − SR0 (> 0 ⇔ DSR > 0.5).",
          f"- **계열**: 두 유니버스 일간 수익률 상관행렬의 평균 → 거리 1−ρ 평균 연결 계층 군집, "
          f"평균 상관 ≥ {ctx['threshold']:.2f} 인 동안 병합. 대표 = {UNIVERSE_LABEL[P]} DSR 최고(동률이면 샤프).",
          "", "## SPY 기준선 (같은 구간)", "",
          "| 유니버스 | 구간 | 총수익 | CAGR | MDD | 샤프 |", "|---|---|---:|---:|---:|---:|"]
    for u in rows:
        b = bench[u]
        L.append(f"| {UNIVERSE_LABEL[u]} | {b['start']}~{b['end']} | {_pct(b['ret'], 1)} | {_pct(b['cagr'])} | "
                 f"{_pct(b['mdd'])} | {_num(b['sharpe'])} |")

    for u in ([P] + [x for x in rows if x != P]):
        L += ["", f"## 전체 결과 — {UNIVERSE_LABEL[u]}", "",
              "DSR 내림차순. `샤프−SPY` 는 같은 구간 SPY 샤프와의 차이.", ""]
        L += _result_table(rows[u], fam, bench[u])

    L += ["", "## 대표별 실행 파라미터 — 라이브 `autotrader_config.json` 맞춤용", "",
          f"모든 대표({', '.join(f'`{reps[f]}`' for f in sorted(reps))})가 **같은 값**으로 돌았다 "
          "(신호만 다르고 매매 규칙은 동일). 실거래·페이퍼를 이 결과에 맞추려면 아래 값을 그대로 쓴다.", "",
          "| 파라미터 | 백테스트 값 | autotrader_config 키 | 비고 |", "|---|---|---|---|"]
    for name, val, key, note in effective_params():
        L.append(f"| {name} | `{val}` | {key} | {note} |")
    L += ["", "**설정으로 못 맞추는 차이** (라이브가 백테스트와 달라지는 지점):", "",
          "- 유니버스: 백테스트는 '그날 S&P 500 구성종목' 전체에서 고른다. 라이브는 워치리스트(+자동 발굴)에서 "
          "고르므로, 결과를 재현하려면 라이브 후보를 현재 S&P 500 구성종목으로 넓혀야 한다.",
          "- 판단 주기: 백테스트는 7거래일마다 종가로 1회 판단·체결, 손절·트레일링도 그때만 확인. "
          "라이브는 interval(현재 300초)마다 판단하고 틱 가드가 손절을 즉시 건다 — 매매 횟수와 체결가가 달라진다.",
          "- 비용: 수수료 0.1%·슬리피지 0.05% 는 가정이다. Alpaca 수수료 0 이면 실제 비용은 더 낮을 수 있다.",
          "- `strategy` 키: 각 대표의 전략 키(위 표의 대표)를 그대로 넣는다. 전략별 권장 청산값(EXIT_PROFILES)은 "
          "적용하지 않은 결과이므로 앱의 '권장 청산값 채우기'를 누르면 안 된다.",
          ]

    L += ["", "## 봉인 구간 확인 — 통과 기준 (미실행)", "",
          f"아래 대표 {len(reps)}개만, 사용자 승인 후 **1회** 실행한다 (실행 시 `{HOLDOUT_MARK.relative_to(ROOT)}` 에 "
          "기록되고 재실행은 거부된다).", "",
          "```bash",
          "python3 research/strategy_tournament.py --holdout --keys " + ",".join(reps[f] for f in sorted(reps)),
          "```", "",
          f"구간 {bt.HOLDOUT_START} ~ 실행 전일, 같은 규칙. **통과 판정은 {UNIVERSE_LABEL[P]} 결과로 한다** "
          "(23종목은 참고로 병기). 대표마다 세 조건을 **모두** 만족해야 통과:", "",
          "1. **샤프 ≥ SPY 샤프** (같은 구간)",
          f"2. **MDD ≥ SPY MDD − {PASS_MDD_SLACK:.0%}p** (낙폭이 SPY 보다 {PASS_MDD_SLACK:.0%}p 넘게 깊으면 탈락)",
          "3. **DSR > 0** — DSR 은 확률(0~1)이라 문자 그대로는 항상 참이므로 '보정 샤프 > 0' "
          "(= DSR 확률 > 0.5) 로 해석한다. 봉인 구간의 N 은 확인하는 대표 수. 통상 유의 기준(DSR ≥ 0.95)도 병기한다.",
          "", "**열기 전에 정할 것 — 확인 대상 범위** (봉인 구간 DSR 의 N 이 달라진다. 열고 나서 바꾸면 스누핑):", "",
          f"- (가) 사전 규칙대로 계열 대표 {len(reps)}개 전부 — N={len(reps)}. 위 명령.",
          f"- (나) 개발 구간({UNIVERSE_LABEL[P]})에서 통과 기준 3개 중 2개 이상을 만족한 대표만 — "
          + (f"{', '.join(f'`{k}`' for k in dev_cands)} (N={len(dev_cands)}): "
             f"`--keys {','.join(dev_cands)}`" if dev_cands else "해당 없음") + ". "
          "개발 구간 결과로 후보를 거르는 것은 정당하다(봉인 구간은 독립 데이터). 개발 구간에서 이미 진 대표를 "
          "넣으면 N 만 커져 남은 후보의 DSR 이 불리해진다.",
          "", "## 한계 — 이 결과를 그대로 믿으면 안 되는 이유", ""]
    miss = sp.get("missing", [])
    eff = sp.get("effectively_missing", [])
    L += [f"- **생존 편향(남아 있음)**: 개발 구간에 S&P 500 구성종목이던 {sp['requested']}개 중 "
          f"{len(miss)}개는 yfinance 에 이력이 없어(인수·합병·파산으로 상장폐지) 빠졌고, "
          f"추가로 {len([t for t in eff if t not in miss])}개는 편입 기간의 절반 이상 시세가 없다. "
          "구성종목-일 기준 시세 커버리지 "
          f"**{sp.get('member_day_coverage', 1.0):.1%}**. 빠진 종목에는 파산·국유화된 은행(SIVB·FRC 등 — "
          "전략이 샀다면 손실)과 프리미엄 인수(ATVI·XLNX 등 — 이득)가 섞여 있어 방향이 한쪽으로 정해지진 않지만, "
          "최악의 폭락 종목이 빠지는 건 확실하다. 티커만 바뀐 종목은 별칭으로 이었다: "
          + ", ".join(f"{a}→{b}" for a, b in sorted(sp.get("aliases", {}).items())) + ".",
          f"  - 빠진 종목: {', '.join(miss)}",
          f"  - 편입 기간 시세 부족: {', '.join(t for t in eff if t not in miss) or '없음'}",
          f"- **편입 이력 근사**: 위키 변경 이력은 '선별' 목록이다(개발 구간 내 {sp.get('changes_in_window', '?')}건). "
          f"스냅샷 {sp.get('snapshot', '?')} 기준 현재 구성 {sp.get('current_members', '?')}개에서 되감았다. "
          "누락·날짜 오차가 있을 수 있다.",
          "- **23종목 유니버스는 선택 편향**: 2026년에 오늘의 승자(NVDA·AVGO·LLY 등)를 보고 고른 목록이라 "
          "추세·모멘텀 계열에 구조적으로 유리하다. 대표 선정에는 쓰지 않고 참고로만 병기했다.",
          "- **같은 봉 체결**: 신호를 그날 종가로 계산하고 그날 종가(+슬리피지)로 체결한다(장 마감 동시호가 근사). "
          "손절·트레일링은 리밸런싱 날에만 판정 — 라이브 틱 감시보다 느슨하다.",
          "- **DSR 의 N 은 하한**: 이전에 앱에서 시도한 비교(EV 켬/끔, 기간·파라미터 바꾸기)는 N 에 넣지 못했다. "
          "실제 시도는 더 많으므로 DSR 은 이보다 낙관적일 수 있다.",
          "- **제외**: `ai_influencer` (LLM·실시간 의견 기반이라 과거 시점 재현 불가).",
          "", "<details><summary>부록 — 평균 일간 수익률 상관행렬</summary>", ""]
    keys = list(corr.columns)
    L.append("| | " + " | ".join(f"`{k}`" for k in keys) + " |")
    L.append("|---" * (len(keys) + 1) + "|")
    for a in keys:
        L.append(f"| `{a}` | " + " | ".join(f"{corr.loc[a, b]:.2f}" for b in keys) + " |")
    L += ["", "</details>", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def _clean_row(r: dict) -> dict:
    return {k: (None if isinstance(v, float) and math.isnan(v) else v)
            for k, v in r.items() if k != "corr"}


# ── 봉인 구간 (사용자 승인 후 1회) ──────────────────────────────────────────

def run_holdout(keys: list[str], workers: int, force: bool, log):
    if HOLDOUT_MARK.exists() and not force:
        raise SystemExit(f"봉인 구간은 이미 열렸습니다: {HOLDOUT_MARK.read_text()} — 재실행은 데이터 스누핑. "
                         "정말 필요하면 --force (사유를 기록할 것).")
    today = date.today().isoformat()
    end = (date.today() - timedelta(days=1)).isoformat()
    # 여는 순간 기록한다 — 중간에 실패해도 이미 일부를 본 것이므로 재실행은 막는다
    HOLDOUT_MARK.parent.mkdir(parents=True, exist_ok=True)
    mark = {"opened": today, "keys": keys, "window": [bt.HOLDOUT_START, end],
            "judged_on": PRIMARY, "status": "started"}
    HOLDOUT_MARK.write_text(json.dumps(mark, ensure_ascii=False))
    out = {}
    for u in UNIVERSES:
        res, meta = run_universe(u, keys, bt.HOLDOUT_START, end, True, workers, log)
        summ = bt.summarize_results(res, n_trials=len(keys))
        b = summ["benchmark"]
        rows = {}
        for r in summ["rows"]:
            r = _clean_row(r)
            r["pass_sharpe"] = r["sharpe"] >= b["sharpe"]
            r["pass_mdd"] = r["mdd"] >= b["mdd"] - PASS_MDD_SLACK
            r["pass_dsr"] = r["sr_deflated"] > 0
            r["pass"] = r["pass_sharpe"] and r["pass_mdd"] and r["pass_dsr"]
            rows[r["key"]] = r
            log(f"  [{u}] {r['key']:20} 샤프 {r['sharpe']:.2f} (SPY {b['sharpe']:.2f}) "
                f"MDD {r['mdd']:.1%} (SPY {b['mdd']:.1%}) DSR {r['dsr']:.3f} → "
                f"{'통과' if r['pass'] else '탈락'}")
        out[u] = {"rows": rows, "benchmark": b, "meta": meta}
    mark.update(status="done", passed=[k for k, r in out[PRIMARY]["rows"].items() if r["pass"]])
    HOLDOUT_MARK.write_text(json.dumps(mark, ensure_ascii=False))
    p = ROOT / "research" / "results" / f"holdout-{today}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    doc = ROOT / "docs" / f"strategy-holdout-{today}.md"
    write_holdout_report(doc, out, keys, end, today)
    log(f"저장: {p.relative_to(ROOT)} · {doc.relative_to(ROOT)}")


def write_holdout_report(path: Path, out: dict, keys: list[str], end: str, today: str):
    P = PRIMARY
    L = [f"# 봉인 구간 확인 — {today}", "",
         f"구간 **{bt.HOLDOUT_START} ~ {end}** (평가 {out[P]['meta']['eval_start']} ~ {out[P]['meta']['eval_end']}). "
         f"개발 구간 토너먼트에서 사용자가 고른 대표 {', '.join(f'`{k}`' for k in keys)} 만 1회 확인 "
         f"(N={len(keys)}). 매매 규칙·파라미터는 개발 구간과 동일 — `docs/strategy-tournament-*.md` 참고.", "",
         f"**통과 판정은 {UNIVERSE_LABEL[P]} 결과로 한다.** 기준(사전 고정): 샤프 ≥ SPY 샤프 · "
         f"MDD ≥ SPY MDD − {PASS_MDD_SLACK:.0%}p · 보정 샤프 > 0 (DSR 확률 > 0.5).", ""]
    for u in [P] + [x for x in out if x != P]:
        b = out[u]["benchmark"]
        L += [f"## {UNIVERSE_LABEL[u]}" + ("" if u == P else " — 참고"), "",
              "| 전략 | 총수익 | CAGR | MDD | 샤프 | DSR | 보정 샤프 | 거래 | 샤프 ≥ SPY | MDD 허용 | 보정 샤프 > 0 | 판정 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|:-:|:-:|:-:|:-:|",
              f"| SPY | {_pct(b['ret'], 1)} | {_pct(b['cagr'])} | {_pct(b['mdd'])} | {_num(b['sharpe'])} "
              "| — | — | — | — | — | — | 기준 |"]
        ok = lambda v: "✅" if v else "❌"
        for k, r in out[u]["rows"].items():
            L.append(f"| {_name(k)} `{k}` | {_pct(r['ret'], 1)} | {_pct(r['cagr'])} | {_pct(r['mdd'])} | "
                     f"{_num(r['sharpe'])} | {_num(r['dsr'], 3)} | {r['sr_deflated']:+.2f} | {r['trades']} | "
                     f"{ok(r['pass_sharpe'])} | {ok(r['pass_mdd'])} | {ok(r['pass_dsr'])} | "
                     f"**{'통과' if r['pass'] else '탈락'}** |")
        L.append("")
    m = out[P]["meta"]
    L += ["## 참고", "",
          f"- S&P 500 구성종목 {m.get('requested')}개 중 {len(m.get('missing', []))}개 시세 없음, "
          f"구성종목-일 커버리지 {m.get('member_day_coverage', 1.0):.1%}.",
          f"- 이 확인은 `{HOLDOUT_MARK.relative_to(ROOT)}` 에 기록됐고 재실행은 거부된다. "
          "결과를 보고 기준·파라미터를 바꿔 다시 돌리면 봉인 구간도 개발 구간이 된다.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


# ── 동점 처리 재실행 (개발 구간) ──────────────────────────────────────────────
# 2026-10-20 봉인 구간 사후 감사: 이산 점수 전략은 S&P 500 에서 수십~백수십 종목이 동점이고,
# 동점이 번들 입력 순서(알파벳순)로 갈려 'A' 종목만 샀다. 동점 처리를 바꿔 개발 구간만 다시 고른다.

TIE_LABEL = {"random": "무작위 (seed 평균)", "dollar_volume": f"거래대금 큰 순 ({bt.TIE_DV_WINDOW}일 평균)",
             "none": "입력 순서 = 알파벳순 (이전)"}


def run_tiebreak(variants: list[str], keys: list[str], seeds: int, prior_spec: str,
                 verify_prior: bool, workers: int, threshold: float, tag: str, log) -> dict:
    """동점 처리 방식(variants, 첫 번째가 주 방식)별로 개발 구간 토너먼트를 다시 돌린다.

    • random 은 seed 0..K−1 을 돌려 평균 — 지표는 seed 평균 ± 표준편차, DSR·상관은 평균 곡선
    • DSR 의 N·V 에 이전 토너먼트 시도(prior)와 이번 모든 방식의 시도를 합친다(재시도 보정)
    • verify_prior: tie_break 없이 다시 돌려 이전 결과가 그대로 재현되는지 확인(새 시도 아님)"""
    start, end = bt.DEV_START, bt.DEV_END
    today = date.today().isoformat()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    js = RESULTS_DIR / f"strategy-tournament-{today}-{tag}.json"
    doc = ROOT / "docs" / f"strategy-tournament-{today}-{tag}.md"
    prior = load_prior(prior_spec, exclude=js)
    modes = {v: (None if v == "none" else v) for v in variants}
    raw, metas = {}, {}
    for u in UNIVERSES:
        tasks = [(k, modes[v], s) for v in variants
                 for s in (range(seeds) if v == "random" else [None]) for k in keys]
        if verify_prior and "none" not in variants:
            tasks += [(k, None, None) for k in keys]
        raw[u], metas[u] = run_tasks(u, tasks, start, end, workers, log)

    agg, stats, alpha = {}, {}, {}
    for v in variants:
        agg[v], stats[v], alpha[v] = {}, {}, {}
        for u in UNIVERSES:
            agg[v][u], stats[v][u], alpha[v][u] = {}, {}, {}
            for k in keys:
                ts = sorted((t for t in raw[u] if t[0] == k and t[1] == modes[v]),
                            key=lambda t: -1 if t[2] is None else t[2])
                agg[v][u][k], stats[v][u][k] = aggregate_seeds([raw[u][t][0] for t in ts])
                alpha[v][u][k] = float(np.nanmean([raw[u][t][1] for t in ts]))

    prior_trials = [t for p in prior for t in p["trials"]]
    new_trials = [{"variant": v, "universe": u, "key": k,
                   "sr": bt.sharpe_moments(agg[v][u][k].daily_returns)["sr"]}
                  for v in variants if v != "none" or not prior for u in UNIVERSES for k in keys]
    srs = [t["sr"] for t in prior_trials + new_trials]
    n_trials, sr_var = len(srs), float(np.var(srs, ddof=1))
    log(f"시도 수 N={n_trials} (이전 {len(prior_trials)} + 이번 {len(new_trials)}) · V={sr_var:.3e}")

    res = {}
    for v in variants:
        summ = {u: bt.summarize_results(agg[v][u], n_trials=n_trials, sr_var=sr_var,
                                        family_threshold=threshold) for u in UNIVERSES}
        bench = {u: summ[u]["benchmark"] for u in UNIVERSES}
        rows = {u: {r["key"]: {**apply_seed_stats(r, stats[v][u][r["key"]], bench[u], n_trials, sr_var),
                               "alpha": alpha[v][u][r["key"]]}
                    for r in summ[u]["rows"]} for u in UNIVERSES}
        corr, fam = combined_families(summ, threshold)
        res[v] = {"rows": rows, "bench": bench, "corr": corr, "families": fam,
                  "reps": pick_representatives(fam, rows, PRIMARY),
                  "corr_u": {u: summ[u]["corr"] for u in UNIVERSES}}

    before = next((p for p in prior if p["trials"] and p["trials"][0]["variant"] == "none"), None)
    verify = None
    if verify_prior and before:
        base = {u: {k: raw[u][(k, None, None)] for k in keys} for u in UNIVERSES}
        brows = before["data"]["rows"]
        diffs = [max(abs(base[u][k][0].total_return - brows[u][k]["ret"]),
                     abs(base[u][k][0].sharpe - brows[u][k]["sharpe"]))
                 for u in UNIVERSES for k in keys if k in brows.get(u, {})]
        verify = {"max_abs_diff": max(diffs) if diffs else None, "n": len(diffs),
                  "alpha": {u: {k: base[u][k][1] for k in keys} for u in UNIVERSES}}
        log(f"이전 결과 재현: {len(diffs)}건, 수익률·샤프 최대 차이 {verify['max_abs_diff']:.2e}")

    ctx = dict(today=today, start=start, end=end, keys=keys, variants=variants, seeds=seeds,
               res=res, metas=metas, n_trials=n_trials, sr_var=sr_var, threshold=threshold,
               prior=prior, prior_trials=prior_trials, new_trials=new_trials, before=before,
               verify=verify, tag=tag)
    write_tiebreak_report(doc, ctx)
    with open(bt.CACHE_DIR / f"tiebreak-ctx-{today}-{tag}.pkl", "wb") as f:
        pickle.dump(ctx, f)             # --rewrite-report 로 다시 계산 없이 보고서만 고칠 때
    js.write_text(json.dumps({
        "date": today, "window": [start, end], "tag": tag, "tie_breaks": variants,
        "primary": variants[0], "seeds": seeds, "run_kwargs": RUN_KW,
        "n_trials": n_trials, "sr_var": sr_var, "threshold": threshold,
        "prior": [{"path": str(p["path"].relative_to(ROOT)), "n_trials": len(p["trials"])} for p in prior],
        "new_trials": new_trials, "verify_prior": verify,
        "variants": {v: {"benchmark": r["bench"], "families": r["families"],
                         "representatives": r["reps"],
                         "rows": {u: {k: _clean_row(x) for k, x in r["rows"][u].items()} for u in UNIVERSES},
                         "corr_avg": r["corr"].round(4).to_dict()} for v, r in res.items()},
        "meta": metas,
    }, ensure_ascii=False, indent=1, default=str))
    log(f"저장: {doc.relative_to(ROOT)} · {js.relative_to(ROOT)}")
    P, v0 = PRIMARY, variants[0]
    for f, k in sorted(res[v0]["reps"].items()):
        r = res[v0]["rows"][P][k]
        log(f"  계열 {f}: 대표 {k} — S&P 샤프 {r['sharpe']:.2f}±{r.get('sharpe_sd', 0):.2f} "
            f"(SPY {res[v0]['bench'][P]['sharpe']:.2f}) MDD {r['mdd']:.1%} DSR {r['dsr']:.3f} "
            f"→ {'생존' if dev_verdict(r, res[v0]['bench'][P])['all'] else '탈락'}")
    return ctx


def _pm(r: dict, m: str, fmt) -> str:
    """seed 평균 ± 표준편차 (seed 1개면 값만)."""
    if r.get("n_seeds", 1) <= 1:
        return fmt(r[m])
    return f"{fmt(r[m])} ± {fmt(r[f'{m}_sd']).lstrip('+')}"


def _sh(x):
    return _num(x)


def _pp(x):
    return _pct(x, 1)


def _ok(v):
    return "✅" if v else "❌"


def _verdict_cells(r, b):
    v = dev_verdict(r, b)
    return f"{_ok(v['sharpe'])} | {_ok(v['mdd'])} | {_ok(v['dsr'])} | **{'생존' if v['all'] else '탈락'}**"


def write_tiebreak_report(path: Path, ctx: dict):
    P, W = PRIMARY, "watchlist"
    v0 = ctx["variants"][0]
    R = ctx["res"][v0]
    rows, bench, fam, reps, corr = R["rows"], R["bench"], R["families"], R["reps"], R["corr"]
    sp = ctx["metas"][P]
    n, V = ctx["n_trials"], ctx["sr_var"]
    sr0 = rows[P][ctx["keys"][0]]["sr0"]
    before = ctx["before"]
    brow = before["data"]["rows"] if before else {}
    bbench = before["data"]["benchmark"] if before else {}
    bfam = {k: int(f) for k, f in (before["data"]["families"] if before else {}).items()}
    breps = {int(f): k for f, k in (before["data"]["representatives"] if before else {}).items()}
    verify = ctx["verify"]
    by_fam: dict = {}
    for k, f in fam.items():
        by_fam.setdefault(f, []).append(k)

    def counts(rws, b):
        return {"beat": sum(r["sharpe"] >= b["sharpe"] for r in rws.values()),
                "dsr": sum(r["dsr"] > 0.5 for r in rws.values()),
                "mdd": sum(r["mdd"] >= b["mdd"] - PASS_MDD_SLACK for r in rws.values()),
                "all": [k for k, r in rws.items() if dev_verdict(r, b)["all"]],
                "two": [k for k, r in rws.items() if not dev_verdict(r, b)["all"]
                        and sum(dev_verdict(r, b)[c] for c in ("sharpe", "mdd", "dsr")) >= 2]}

    after_c = {u: counts(rows[u], bench[u]) for u in UNIVERSES}
    before_c = {u: counts(brow[u], bbench[u]) for u in UNIVERSES} if before else {}

    def bdsr_new(k, u="sp500"):
        """이전(알파벳순) 결과를 이번 N·V 로 다시 보정한 DSR — N 차이를 뺀 비교용."""
        r = brow[u][k]
        return bt.deflated_sharpe(r["sharpe"] / math.sqrt(252), r["n_obs"], r["skew"], r["kurt"], n, V)

    cmd = ("python3 research/strategy_tournament.py --tie-break " + ",".join(ctx["variants"])
           + f" --seeds {ctx['seeds']} --prior " + ",".join(str(p["path"].relative_to(ROOT)) for p in ctx["prior"])
           + (" --verify-prior" if verify else "") + f" --tag {ctx['tag']}")
    surv = after_c[P]["all"]
    rep_set, brep_set = set(reps.values()), set(breps.values())
    L = [f"# 규칙 전략 토너먼트 재실행 — 동점 처리 수정 ({ctx['today']})", "",
         f"개발 구간 **{ctx['start']} ~ {ctx['end']}** (평가 {sp['eval_start']} ~ {sp['eval_end']}) 만 다시 돌렸다. "
         f"봉인 구간 {bt.HOLDOUT_START}~ 은 **열지 않았다** — 이미 `ichimoku` 1회 확인에 쓰였다 "
         f"(`{HOLDOUT_MARK.relative_to(ROOT)}`). 이전 결과: `docs/strategy-tournament-2026-10-20.md`, "
         "결함 감사: `docs/strategy-holdout-2026-10-20.md` '사후 감사' 절.", "",
         f"생성: `{cmd}`", "",
         "## 요약 (자동 생성)", "",
         f"- **동점 처리**: 주 방식 **{TIE_LABEL[v0]}**, seed K={ctx['seeds']}. "
         + (f"비교 방식: {', '.join(TIE_LABEL[v] for v in ctx['variants'][1:])}. " if len(ctx["variants"]) > 1 else "")
         + "매매 규칙·파라미터·평가 기준은 이전과 같다(동점을 가르는 순서만 바뀜).",
         f"- **시도 수 N = {n}** = 이전 토너먼트 {len(ctx['prior_trials'])} + 이번 {len(ctx['new_trials'])} "
         f"(동점 처리 {len([v for v in ctx['variants'] if v != 'none'])}방식 × 전략 {len(ctx['keys'])} × 유니버스 {len(UNIVERSES)}). "
         f"재시도이므로 이전 시도를 N·V 에 합쳤다. 운으로 기대되는 최대 샤프 SR0 = **{sr0:.2f}** (연율; 이전 N=52 일 때 "
         f"{brow[P][ctx['keys'][0]]['sr0']:.2f})." if before else f"- 시도 수 N = {n}, SR0 = {sr0:.2f}.",
         f"- **개발 구간 생존 (S&P 500, 통과 기준 세 조건 모두)**: "
         + (", ".join(f"**`{k}`**" for k in surv) if surv else "**없음 (0개)**")
         + (f" — 이전(알파벳순) {len(before_c[P]['all'])}개." if before else "."),
         *[f"- {UNIVERSE_LABEL[u]}: 샤프(seed 평균) ≥ SPY {bench[u]['sharpe']:.2f} **{after_c[u]['beat']}개**"
           + (f" (이전 {before_c[u]['beat']})" if before else "")
           + f", DSR>0.5 **{after_c[u]['dsr']}개**" + (f" (이전 {before_c[u]['dsr']})" if before else "")
           + f", MDD 허용 {after_c[u]['mdd']}개, 세 조건 모두 **{len(after_c[u]['all'])}개**"
           + (f" (이전 {len(before_c[u]['all'])})" if before else "")
           + (f"; 두 조건만: {', '.join(f'`{k}`' for k in after_c[u]['two'])}" if after_c[u]["two"] else "")
           + f" / {len(rows[u])}" for u in [P, W]],
         f"- **계열 {len(by_fam)}개** (이전 {len(set(bfam.values()))}개). 대표 = 계열별 S&P 500 DSR 최고. "
         + (f"대표가 바뀜: 빠짐 {', '.join(f'`{k}`' for k in sorted(brep_set - rep_set)) or '없음'} · "
            f"새로 들어옴 {', '.join(f'`{k}`' for k in sorted(rep_set - brep_set)) or '없음'}." if before else ""),
         ]
    if verify:
        al_b = verify["alpha"][P].get("ichimoku")
        al_a = rows[P].get("ichimoku", {}).get("alpha")
        L.append(f"- **재현·편향 확인**: tie_break 없이 다시 돌린 {verify['n']}개가 이전 JSON 과 수익률·샤프 최대 차이 "
                 f"{verify['max_abs_diff']:.1e} (기존 동작 보존). S&P 500 에서 산 종목의 알파벳 위치(0=맨 앞, 0.5=편향 없음): "
                 + (f"`ichimoku` {al_b:.2f} → {al_a:.2f}, " if al_b is not None and al_a is not None else "")
                 + f"26개 평균 {np.nanmean(list(verify['alpha'][P].values())):.2f} → "
                 f"{np.nanmean([r['alpha'] for r in rows[P].values()]):.2f}.")
    L += ["", "## 동점 처리 — 무엇을 바꿨나", "",
          "- `backtester.run()` 에 연구용 번들 키 `tie_break`·`tie_seed` 를 추가했다(`bt.tie_order`). "
          "키가 없으면 예전과 똑같이 안정 정렬(입력 순서). 라이브 경로(`portfolio.py`·`core/`)·`load_market_data`·"
          "`_strategy_score_bt`·`run()`/`compare_strategies()` 시그니처는 바꾸지 않았다.",
          f"- **무작위**: 리밸런싱 날마다 (seed, 날짜 인덱스)로 종목 순서를 섞어 동점을 가른다. 종목 이름순으로 섞으므로 "
          f"번들 입력 순서와 무관하고, 같은 seed 면 결과가 재현된다. 전략마다 seed 0~{ctx['seeds'] - 1} 을 돌렸다.",
          "  - 보고하는 **수익·CAGR·MDD·샤프·승률·거래 수 = seed 평균 (± 표준편차)** — 한 계좌가 기대할 값. "
          "통과 기준(샤프·MDD)도 이 값으로 판정했다.",
          "  - **DSR·보정 샤프·상관·계열 = seed 평균 곡선**(시드를 K 등분해 seed 마다 굴린 계좌들의 합)의 일간 수익률. "
          "평균 곡선은 동점 선택이 K 갈래로 분산된 포트폴리오라 한 계좌보다 샤프가 높게 나온다(아래 표의 '평균 곡선 샤프'). "
          "그래서 DSR 은 낙관 쪽이며, 보수적 확인으로 seed 별 DSR 의 중앙값도 병기했다.",
          f"- **거래대금(비교용)**: 동점이면 최근 {bt.TIE_DV_WINDOW}거래일 평균 거래대금(종가×거래량)이 큰 종목부터. "
          "결정적이라 seed 가 필요 없지만, 동점이 많은 전략에서는 사실상 '조건 통과 종목 중 대형·유동성주'를 사는 "
          "**대형주 기울기가 생긴다** — 신호가 아니라 전략 자체가 바뀌는 셈이라 주 방식으로 쓰지 않고 비교만 했다.",
          "- **라이브는 그대로다**: 라이브(`core/cycle.py` → `portfolio.generate_orders`)도 같은 정렬이라 동점이면 "
          "후보 목록 순서(워치리스트 순서)대로 산다. 이번 범위 밖이라 고치지 않았다 — 이산 점수 전략을 라이브에 "
          "올린다면 먼저 고쳐야 한다(아래 '다음 단계').", ""]

    if before:
        L += ["## 이전(알파벳순) ↔ 이후(무작위) — S&P 500", "",
              f"이전 DSR 은 그때의 N=52 기준, '이전 DSR(N={n})' 은 이전 결과를 이번 N·V 로 다시 보정한 값(N 차이를 뺀 비교). "
              "알파벳 위치 = 산 종목의 이름순 위치 평균(0=맨 앞, 0.5=편향 없음). 이후 DSR 내림차순.", "",
              "| 전략 | 이전 샤프 | 이후 샤프 (평균 ± sd) | Δ샤프 | 이전 MDD | 이후 MDD | 이전 수익 | 이후 수익 | "
              f"이전 DSR (N=52) | 이전 DSR (N={n}) | 이후 DSR | 알파벳 위치 이전→이후 | 계열 대표 이전 → 이후 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for k in sorted(rows[P], key=lambda k: -rows[P][k]["dsr"]):
            a, b = rows[P][k], brow[P][k]
            ab = verify["alpha"][P].get(k) if verify else None
            L.append(f"| {_name(k)} `{k}` | {_sh(b['sharpe'])} | {_pm(a, 'sharpe', _sh)} | "
                     f"{a['sharpe'] - b['sharpe']:+.2f} | {_pp(b['mdd'])} | {_pp(a['mdd'])} | {_pct(b['ret'], 0)} | "
                     f"{_pct(a['ret'], 0)} | {_num(b['dsr'], 3)} | {_num(bdsr_new(k)['dsr'], 3)} | {_num(a['dsr'], 3)} | "
                     f"{_num(ab) if ab is not None else '—'} → {_num(a['alpha'])} | "
                     f"`{breps.get(bfam.get(k), '—')}` → `{reps[fam[k]]}` |")
        L.append("")

    L += ["## 계열·대표 (이후)", "",
          f"두 유니버스 평균 곡선 일간 수익률 상관행렬의 평균 → 평균 연결 군집, 평균 상관 ≥ {ctx['threshold']:.2f} 이면 같은 계열. "
          f"대표 = S&P 500 DSR 최고(동률이면 샤프). 판정은 통과 기준 세 조건(개발 구간에 그대로 대 본 것).", "",
          "| 계열 | 구성 | 대표 | 이전 대표(구성원들의 이전 계열) | 대표 샤프 S&P (평균 ± sd) | SPY | 대표 DSR (S&P/23) | "
          "seed DSR 중앙값 | 대표 MDD (S&P) | 샤프≥SPY | MDD 허용 | 보정 샤프>0 | 판정 |",
          "|---|---|---|---|---:|---:|---:|---:|---:|:-:|:-:|:-:|:-:|"]
    for f in sorted(by_fam):
        rep = reps[f]
        rp, rw = rows[P][rep], rows[W][rep]
        old = sorted({breps[bfam[k]] for k in by_fam[f] if k in bfam})
        L.append(f"| {f} | {', '.join(f'`{k}`' for k in by_fam[f])} | **{_name(rep)}** `{rep}` | "
                 f"{', '.join(f'`{k}`' for k in old) or '—'} | {_pm(rp, 'sharpe', _sh)} | {_sh(bench[P]['sharpe'])} | "
                 f"{_num(rp['dsr'], 3)} / {_num(rw['dsr'], 3)} | {_num(rp['seed_dsr_median'], 3)} | {_pp(rp['mdd'])} | "
                 f"{_verdict_cells(rp, bench[P])} |")

    for u in (P, W):
        L += ["", f"## 전체 결과 — {UNIVERSE_LABEL[u]} · {TIE_LABEL[v0]}", "",
              "DSR(평균 곡선) 내림차순. 수익·MDD·샤프·거래는 seed 평균 (± sd), [최소~최대] 는 seed 별 샤프 범위. "
              "'SPY 넘은 seed' = 샤프가 SPY 이상인 seed 비율.", "",
              "| # | 전략 | 계열 | 총수익 | MDD | 샤프 | [최소~최대] | 샤프−SPY | 평균 곡선 샤프 | DSR | 보정 샤프 | "
              "seed DSR 중앙값 | SPY 넘은 seed | 거래 | 알파벳 위치 | 판정 |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|"]
        for i, k in enumerate(sorted(rows[u], key=lambda k: -rows[u][k]["dsr"]), 1):
            r = rows[u][k]
            L.append(f"| {i} | {_name(k)} `{k}` | {fam[k]} | {_pm(r, 'ret', lambda x: _pct(x, 0))} | "
                     f"{_pm(r, 'mdd', _pp)} | {_pm(r, 'sharpe', _sh)} | [{_sh(r['sharpe_min'])}~{_sh(r['sharpe_max'])}] | "
                     f"{r['sharpe'] - bench[u]['sharpe']:+.2f} | {_sh(r['curve_sharpe'])} | {_num(r['dsr'], 3)} | "
                     f"{r['sr_deflated']:+.2f} | {_num(r['seed_dsr_median'], 3)} | {r['seed_beat_spy']:.0%} | "
                     f"{r['trades']:.0f} | {_num(r['alpha'])} | {'생존' if dev_verdict(r, bench[u])['all'] else '—'} |")

    for v in ctx["variants"][1:]:
        Rv = ctx["res"][v]
        cv = counts(Rv["rows"][P], Rv["bench"][P])
        L += ["", f"## 비교 — {TIE_LABEL[v]}", "",
              f"같은 N={n}·V 로 보정. S&P 500 에서 샤프 ≥ SPY {cv['beat']}개, DSR>0.5 {cv['dsr']}개, "
              f"세 조건 모두 **{len(cv['all'])}개**" + (f" ({', '.join(f'`{k}`' for k in cv['all'])})" if cv["all"] else "")
              + f". 계열 {len(set(Rv['families'].values()))}개, 대표: "
              + ", ".join(f"`{k}`" for _, k in sorted(Rv["reps"].items())) + ".",
              "동점이 많은 전략일수록 '조건 통과 대형주' 전략으로 바뀐다는 점을 감안해서 읽어야 한다.", "",
              f"| 전략 | 샤프 {TIE_LABEL[v0]} | 샤프 {TIE_LABEL[v]} | MDD (무작위/거래대금) | DSR (무작위/거래대금) | "
              f"알파벳 위치 (거래대금) | 판정 ({TIE_LABEL[v]}) |",
              "|---|---:|---:|---:|---:|---:|:-:|"]
        for k in sorted(Rv["rows"][P], key=lambda k: -Rv["rows"][P][k]["dsr"]):
            a, d = rows[P][k], Rv["rows"][P][k]
            L.append(f"| {_name(k)} `{k}` | {_pm(a, 'sharpe', _sh)} | {_sh(d['sharpe'])} | {_pp(a['mdd'])} / {_pp(d['mdd'])} | "
                     f"{_num(a['dsr'], 3)} / {_num(d['dsr'], 3)} | {_num(d['alpha'])} | "
                     f"{'생존' if dev_verdict(d, Rv['bench'][P])['all'] else '탈락'} |")

    L += ["", "## 평가 기준 (고정 — 이전과 동일)", "",
          f"- 구간·SPY 벤치마크·매매 규칙·시점별 편입은 `docs/strategy-tournament-2026-10-20.md` '평가 기준' 절과 같다 "
          f"(시드 ${RUN_KW['capital']:,.0f}, 문턱 {RUN_KW['min_score']:.0f}, 손절 {RUN_KW['stop_loss']:.0%}, 익절 기준 "
          f"{RUN_KW['take_profit']:.0%}, 트레일링 {RUN_KW['trailing_stop']:.0%}, {bt.REBALANCE_DAYS}거래일 리밸런싱, EV 끔).",
          f"- 통과 기준(사전 고정, 바꾸지 않음): ① 샤프 ≥ SPY 샤프 ② MDD ≥ SPY MDD − {PASS_MDD_SLACK:.0%}p "
          "③ 보정 샤프 > 0 (= DSR 확률 > 0.5). 여기서는 개발 구간에 그대로 대 본 것 — 봉인 구간 판정이 아니다.",
          f"- DSR: N={n} (이전 {len(ctx['prior_trials'])} + 이번 {len(ctx['new_trials'])}), 시도들 일간 샤프 분산 V={V:.2e}, "
          "SR0 = √V·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))). 무작위 방식의 시도 샤프는 평균 곡선 값. "
          "seed 는 고른 게 아니라 평균낸 것이라 시도로 세지 않았다. tie_break 없는 재현 확인 실행도 이전 시도와 같아 세지 않았다.",
          "", "## SPY 기준선 (같은 구간)", "",
          "| 유니버스 | 구간 | 총수익 | CAGR | MDD | 샤프 |", "|---|---|---:|---:|---:|---:|"]
    for u in (P, W):
        b = bench[u]
        L.append(f"| {UNIVERSE_LABEL[u]} | {b['start']}~{b['end']} | {_pp(b['ret'])} | {_pp(b['cagr'])} | "
                 f"{_pp(b['mdd'])} | {_sh(b['sharpe'])} |")
    L += ["", "## 다음 단계", "",
          f"- **봉인 구간은 다시 열지 않는다.** {bt.HOLDOUT_START}~ 은 `ichimoku` 확인에 이미 한 번 쓰였고, 그 결과를 본 뒤 "
          "동점 처리를 고쳤으므로 같은 구간으로 다시 판정하면 스누핑이다(`--holdout` 은 표식 때문에 거부된다).",
          "- **다음 표본 외 검증은 앞으로의 페이퍼 운영(forward)** — 개발 구간 후보를 고정하고, 라이브와 같은 설정으로 "
          "지금부터 쌓이는 날짜에서만 SPY 대비 같은 세 기준으로 판정한다. 기간·판정 시점은 시작 전에 정한다.",
          "- **이산 점수 전략을 페이퍼·라이브에 올린다면 라이브 동점 처리를 먼저 고친다** — 지금 라이브는 동점이면 "
          "후보 목록(워치리스트) 순서대로 사므로 백테스트(무작위)와 다른 종목을 산다.",
          "- 개발 구간 생존 전략이 없으면 통과 기준을 낮추지 않고 실거래를 미룬다(10/20 사용자 결정 방침).",
          "", "## 한계", "",
          f"- 생존 편향·편입 이력 근사·같은 봉 체결·23종목 선택 편향은 이전과 같다(구성종목-일 시세 커버리지 "
          f"{sp.get('member_day_coverage', 1.0):.1%}, 시세 없는 과거 구성종목 {len(sp.get('missing', []))}개).",
          "- 무작위 동점 처리는 '조건을 통과한 종목 중 아무거나'를 산다. 이산 점수 전략에 원래 순위 정보가 없다는 뜻이고, "
          "seed 간 편차(± sd)가 크면 결과가 신호보다 선택의 운에 달렸다는 뜻이다.",
          "- 평균 곡선 DSR 은 분산 효과로 한 계좌보다 낙관적이다. seed DSR 중앙값과 함께 봐야 한다.",
          "- 이번 재실행 자체도 개발 구간을 한 번 더 본 것이다 — N 에 넣었지만, 개발 구간 결과만으로 고른 전략은 "
          "표본 외 검증 없이 실거래로 가면 안 된다.",
          "", "<details><summary>부록 — 평균 곡선 일간 수익률 상관행렬(두 유니버스 평균)</summary>", ""]
    keys = list(corr.columns)
    L.append("| | " + " | ".join(f"`{k}`" for k in keys) + " |")
    L.append("|---" * (len(keys) + 1) + "|")
    for a_ in keys:
        L.append(f"| `{a_}` | " + " | ".join(f"{corr.loc[a_, b_]:.2f}" for b_ in keys) + " |")
    L += ["", "</details>", ""]
    path.write_text("\n".join(L), encoding="utf-8")


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--threshold", type=float, default=bt.FAMILY_CORR, help="계열 병합 평균 상관 기준")
    ap.add_argument("--keys", default="", help="쉼표 구분 전략 키 (기본: ai_influencer 제외 전부)")
    ap.add_argument("--holdout", action="store_true", help="봉인 구간 1회 확인 (사용자 승인 후)")
    ap.add_argument("--force", action="store_true", help="봉인 구간 재실행 (비권장)")
    ap.add_argument("--tie-break", default="random",
                    help="동점 처리 방식(쉼표 구분, 첫 번째가 주 방식): random · dollar_volume · none(이전=입력 순서). "
                         "none 하나만 주면 이전 토너먼트와 같은 흐름")
    ap.add_argument("--seeds", type=int, default=TIE_SEEDS, help="random 방식 seed 수 K")
    ap.add_argument("--prior", default="auto",
                    help="DSR 의 N·V 에 넣을 이전 토너먼트 JSON (auto=research/results 전부, none, 쉼표 구분 경로)")
    ap.add_argument("--verify-prior", action="store_true",
                    help="tie_break 없이 다시 돌려 이전 결과 재현·알파벳 편향 확인 (시도로 세지 않음)")
    ap.add_argument("--tag", default="tiebreak", help="결과 파일 접미사 (strategy-tournament-<날짜>-<tag>)")
    ap.add_argument("--rewrite-report", metavar="DATE",
                    help="그날 --tag 실행의 저장된 결과(.bt_cache)로 보고서만 다시 쓴다 (재계산 없음)")
    a = ap.parse_args(argv)
    if a.rewrite_report:
        with open(bt.CACHE_DIR / f"tiebreak-ctx-{a.rewrite_report}-{a.tag}.pkl", "rb") as f:
            ctx = pickle.load(f)
        return write_tiebreak_report(ROOT / "docs" / f"strategy-tournament-{a.rewrite_report}-{a.tag}.md", ctx)

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    keys = [k for k in a.keys.split(",") if k] or [k for k in scat.CATALOG if k not in EXCLUDE]
    unknown = [k for k in keys if k not in scat.CATALOG or k in EXCLUDE]
    if unknown:
        raise SystemExit(f"알 수 없거나 제외된 전략: {unknown}")
    try:
        import yf_compat
        yf_compat.patch_tz_cache()
        yf_compat.raise_fd_limit()
    except Exception:
        pass

    if a.holdout:
        if not a.keys:
            raise SystemExit("--holdout 은 개발 구간에서 고른 대표만 --keys 로 명시해야 합니다.")
        return run_holdout(keys, a.workers, a.force, log)

    variants = [v for v in a.tie_break.split(",") if v]
    bad = [v for v in variants if v != "none" and v not in bt.TIE_BREAKS]
    if bad or not variants:
        raise SystemExit(f"알 수 없는 --tie-break: {bad or a.tie_break} (가능: none, {', '.join(bt.TIE_BREAKS)})")
    if variants != ["none"]:
        return run_tiebreak(variants, keys, a.seeds, a.prior, a.verify_prior, a.workers,
                            a.threshold, a.tag, log)

    start, end = bt.DEV_START, bt.DEV_END
    all_res, metas = {}, {}
    for u in UNIVERSES:
        all_res[u], metas[u] = run_universe(u, keys, start, end, False, a.workers, log)
    n_trials, sr_var = pooled_trials(all_res)
    summaries = {u: bt.summarize_results(all_res[u], n_trials=n_trials, sr_var=sr_var,
                                         family_threshold=a.threshold) for u in UNIVERSES}
    corr, fam = combined_families(summaries, a.threshold)
    rows = {u: {r["key"]: r for r in summaries[u]["rows"]} for u in UNIVERSES}
    reps = pick_representatives(fam, rows, PRIMARY)
    bench = {u: summaries[u]["benchmark"] for u in UNIVERSES}

    today = date.today().isoformat()
    ctx = dict(today=today, start=start, end=end, keys=keys, rows=rows, families=fam,
               reps=reps, bench=bench, metas=metas, corr=corr, n_trials=n_trials,
               sr_var=sr_var, threshold=a.threshold)
    doc = ROOT / "docs" / f"strategy-tournament-{today}.md"
    write_report(doc, ctx)
    res_dir = ROOT / "research" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    js = res_dir / f"strategy-tournament-{today}.json"
    js.write_text(json.dumps({
        "date": today, "window": [start, end], "run_kwargs": RUN_KW,
        "effective_params": [list(map(str, p)) for p in effective_params()],
        "n_trials": n_trials, "sr_var": sr_var, "threshold": a.threshold,
        "benchmark": bench, "families": fam, "representatives": reps,
        "rows": {u: {k: _clean_row(r) for k, r in rows[u].items()} for u in UNIVERSES},
        "corr_avg": corr.round(4).to_dict(),
        "corr": {u: summaries[u]["corr"].round(4).to_dict() for u in UNIVERSES},
        "meta": metas,
    }, ensure_ascii=False, indent=1, default=str))
    log(f"저장: {doc.relative_to(ROOT)} · {js.relative_to(ROOT)}")
    for f in sorted(reps):
        k = reps[f]
        log(f"  계열 {f}: 대표 {k} — S&P 샤프 {rows[PRIMARY][k]['sharpe']:.2f} "
            f"(SPY {bench[PRIMARY]['sharpe']:.2f}) DSR {rows[PRIMARY][k]['dsr']:.3f}")


if __name__ == "__main__":
    main()
