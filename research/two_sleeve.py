"""
코어-새틀라이트(두 바구니) 백테스트 — 코어는 SPY 보유, 단기 바구니는 고변동 종목만 매매.

설계(실행 전 고정 — 결과를 보고 바꾸지 않는다):
  • 구간: 개발 2020-01-01 ~ 2024-12-31 만. 봉인 구간은 이미 1회 썼으므로(ichimoku) 여기서 고른
    것의 표본 외 검증은 forward 페이퍼로 한다.
  • 코어: SPY(수정주가) 첫날 매수 후 보유 — 수수료·슬리피지 1회, 소수점 주식 근사.
  • 단기 바구니 유니버스: 그날 S&P 500 구성종목(상장 후 252봉 이상) 중 직전 VOL_WINDOW 거래일
    일간 로그수익률 표준편차 상위 VOL_TOP 종목. 그날 종가까지만 본다(점수·체결과 같은 정보 시점).
    → backtester 의 members(신규 매수 후보 마스크)를 이 집합으로 좁힌다. 보유 종목은 집합에서
    빠져도 규칙대로 관리(손절·트레일링·점수 하락 매도).
  • 단기 전략: 평균회귀·진폭 매매 계열 4개(SAT_KEYS). 매매 규칙은 토너먼트와 같다(RUN_KW).
    동점 = 최근 20일 평균 거래대금 큰 순(dollar_volume — 라이브와 같은 규칙).
  • 대조군: 같은 4개를 S&P 500 전체에서 — 고변동 필터 자체가 보탬이 되는지.
    참고선: 타이밍 없이 고변동 집합 동일가중 보유(7거래일마다 교체) — 전략이 '고변동 종목을
    들고 있는 것' 이상을 하는지.
  • 비율: 총 $10,000, 코어/단기 90/10·80/20·70/30·50/50. 단기 바구니는 실제 바구니 금액으로 따로
    굴린다(소액 단위 정책 lot_plan 이 그대로 걸린다). 합산 = 리밸런싱 없음(자본 따로)과
    분기 리밸런싱(근사 — 단기 바구니 일간 수익률이 금액과 무관하다고 보고 비중만 되돌림).
    같은 비율의 'SPY + 현금'도 같이 본다 — 낙폭이 준 게 단기 바구니 덕인지, 그냥 덜 담아서인지.
  • 판정: 단기 전략 = 토너먼트 통과 기준(SPY 샤프 이상 · MDD 가 SPY 보다 5%p 넘게 나쁘지 않음 ·
    보정 샤프 > 0). DSR 시도 수 N = 이전 토너먼트 시도(research/results/strategy-tournament-*.json)
    + 이번 8개(4 전략 × 2 유니버스).

실행 (수 분 — 백그라운드 권장):
  python3 research/two_sleeve.py
결과: docs/two-sleeve-<날짜>.md · research/results/two-sleeve-<날짜>.json
라이브 경로(portfolio·core)는 건드리지 않는다 — backtester.run 을 그대로 쓴다.
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
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADE_NOTIFY_DISABLED", "1")

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

import backtester as bt       # noqa: E402
import strategy_catalog as scat  # noqa: E402
from config import STOP_LOSS_PCT  # noqa: E402
from research import strategy_tournament as st  # noqa: E402

SAT_KEYS = ("mean_reversion", "rsi2", "zscore", "stochastic")
VOL_WINDOW = 60                      # 직전 60거래일(약 3개월) 일간 로그수익률 표준편차
VOL_TOP = 100                        # 그날 후보 중 상위 100종목(S&P 500 의 약 20%)
TIE_BREAK = "dollar_volume"
TOTAL = 10_000.0
CORE_WEIGHTS = (0.9, 0.8, 0.7, 0.5)
COST = 0.001 + 0.0005                # backtester.run 기본 수수료 + 슬리피지 (편도)
REBAL = bt.REBALANCE_DAYS
UNI_LABEL = {"hv": f"고변동 상위 {VOL_TOP}", "sp500": "S&P 500 전체"}
COVID = ("2020-02-19", "2020-03-23")  # SPY 고점 → 저점
RESULTS_DIR = ROOT / "research" / "results"

_BASE = None
_HV = None


# ── 고변동 유니버스 ───────────────────────────────────────────────────────────

def high_vol_members(bundle: dict, window: int = VOL_WINDOW, top: int = VOL_TOP):
    """시점별 고변동 집합 → ({티커: bool 배열}, 선택 행렬, 변동성 행렬, 후보 행렬).

    후보 = 그날 S&P 500 구성종목 & 상장 후 min_history 봉 이상. 변동성은 그날 종가까지의
    window 일 로그수익률 표준편차(일간). 후보 중 큰 순 top 개."""
    sd, mem, nv = bundle["stock_data"], bundle["members"], bundle["n_valid"]
    mh = int(bundle["min_history"])
    idx = bundle["date_index"]
    tickers = sorted(mem)
    close = pd.DataFrame({t: sd[t]["Close"].to_numpy(dtype=float) for t in tickers}, index=idx)
    vol = np.log(close).diff().rolling(window, min_periods=window).std()
    elig = pd.DataFrame({t: mem[t] & (nv[t] >= mh) for t in tickers}, index=idx)
    rank = vol.where(elig).rank(axis=1, ascending=False, method="first")
    pick = rank <= top
    return {t: pick[t].to_numpy(dtype=bool) for t in tickers}, pick, vol, elig


def universe_stats(bundle: dict, pick: pd.DataFrame, vol: pd.DataFrame, elig: pd.DataFrame) -> dict:
    """고변동 집합이 어떤 모습인지 — 크기·교체율·변동성·자주 뽑힌 종목·섹터."""
    ev = int(bundle["eval_start_idx"])
    p, v, e = pick.iloc[ev:], vol.iloc[ev:], elig.iloc[ev:]
    counts = p.sum(axis=1)
    reb = p.iloc[::REBAL]
    stay, prev = [], None
    for _, row in reb.iterrows():
        cur = set(row.index[row.to_numpy()])
        if prev:
            stay.append(len(cur & prev) / max(1, len(cur)))
        prev = cur
    ann = math.sqrt(252)
    days = p.sum(axis=0)
    days = days[days > 0].sort_values(ascending=False)
    sectors = bt.load_sp500_snapshot().get("sectors", {})
    sec = Counter()
    for t, n in days.items():
        sec[sectors.get(t, "(구성에서 빠진 종목)")] += int(n)
    tot = sum(sec.values()) or 1
    return {"days": int(len(p)), "count_mean": float(counts.mean()), "count_min": int(counts.min()),
            "stay_per_rebalance": float(np.mean(stay)) if stay else float("nan"),
            "vol_selected_median": float(np.nanmedian(v.where(p).to_numpy()) * ann),
            "vol_all_median": float(np.nanmedian(v.where(e).to_numpy()) * ann),
            "distinct_names": int(len(days)),
            "top_names": {t: float(n / len(p)) for t, n in days.head(15).items()},
            "sectors": {s: n / tot for s, n in sec.most_common()}}


def equal_weight_curve(bundle: dict, pick: pd.DataFrame, capital: float = TOTAL) -> pd.Series:
    """참고선: 고변동 집합 동일가중 보유, REBAL 거래일마다 그날 집합으로 교체(타이밍 없음).
    교체 비용 = 바뀐 비중 × 편도 비용 × 2(판 쪽 + 산 쪽). 사이 날의 비중 표류는 무시(근사)."""
    ev = int(bundle["eval_start_idx"])
    idx = bundle["date_index"]
    close = pd.DataFrame({t: bundle["stock_data"][t]["Close"].to_numpy(dtype=float)
                          for t in pick.columns}, index=idx)
    rets = close.pct_change().fillna(0.0).to_numpy()
    pk = pick.to_numpy()
    eq, held, out = capital, None, []
    for i in range(ev, len(idx)):
        if held is not None and held.any():
            eq *= 1.0 + float(rets[i, held].mean())
        if (i - ev) % REBAL == 0 and pk[i].any():
            new = pk[i]
            changed = 1.0 if held is None else 1.0 - (held & new).sum() / new.sum()
            eq *= 1.0 - changed * COST * (1 if held is None else 2)
            held = new
        out.append(eq)
    return pd.Series(out, index=idx[ev:])


# ── 실행 ─────────────────────────────────────────────────────────────────────

def _init_worker(path: str):
    global _BASE, _HV
    with open(path, "rb") as f:
        d = pickle.load(f)
    _BASE, _HV = d["base"], d["hv"]


def _run_task(task):
    """(전략, 유니버스 'hv'|'sp500', 시작 자본) 1건."""
    key, uni, cap = task
    t0 = time.time()
    b = dict(_BASE, tie_break=TIE_BREAK, members=_HV if uni == "hv" else _BASE["members"])
    r = bt.run(strategy=key, prefetched=b, **dict(st.RUN_KW, capital=float(cap)))
    return task, r, time.time() - t0


def run_tasks(bundle: dict, hv: dict, tasks: list, workers: int, log) -> dict:
    path = bt.CACHE_DIR / "bundle_two_sleeve.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({"base": bundle, "hv": hv}, f, protocol=pickle.HIGHEST_PROTOCOL)
    out: dict = {}
    try:
        if workers <= 1:
            _init_worker(str(path))
            it, ex = map(_run_task, tasks), None
        else:
            ex = ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(str(path),))
            it = ex.map(_run_task, tasks)
        for task, r, dt in it:
            out[task] = r
            k, uni, cap = task
            log(f"  {uni:5} ${cap:>6,.0f} {k:15} {r.total_return:+8.1%} 샤프 {r.sharpe:5.2f} "
                f"거래 {len(r.trades):4} ({dt:.0f}s) [{len(out)}/{len(tasks)}]")
        if ex:
            ex.shutdown()
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return out


# ── 지표 ─────────────────────────────────────────────────────────────────────

def curve_stats(curve: pd.Series, spy: pd.Series) -> dict:
    """곡선 → perf_stats + SPY 상관·베타 · 2020 폭락 구간 하락 · 연도별 수익."""
    c = pd.Series(curve, dtype=float).dropna()
    out = bt.perf_stats(c)
    j = pd.concat([c.pct_change(), spy.reindex(c.index).ffill().pct_change()], axis=1).dropna()
    a, s = j.iloc[:, 0], j.iloc[:, 1]
    out["corr_spy"] = float(a.corr(s)) if a.std() > 0 else 0.0
    out["beta"] = float(np.cov(a, s)[0, 1] / s.var()) if s.var() > 0 else 0.0
    out["covid_dd"] = float(c.loc[COVID[0]:COVID[1]].min() / c.loc[:COVID[0]].max() - 1)
    ye = c.groupby(c.index.year).last()
    base = ye.shift(1)
    base.iloc[0] = c.iloc[0]
    out["yearly"] = {int(y): float(v) for y, v in (ye / base - 1).items()}
    return out


def trade_stats(r) -> dict:
    tr = r.trades
    if not tr:
        return {"n": 0, "win": 0.0, "avg": 0.0, "worst": 0.0, "beyond_stop": 0.0, "hold": 0.0,
                "reasons": {}}
    pnl = np.array([t.pnl_pct for t in tr])
    return {"n": len(tr), "win": float((pnl > 0).mean()), "avg": float(pnl.mean()),
            "worst": float(pnl.min()), "beyond_stop": float((pnl < -float(STOP_LOSS_PCT)).mean()),
            "hold": float(r.avg_hold_days),
            "reasons": dict(Counter(t.reason.split(" ")[0] for t in tr).most_common(6))}


def sleeve_cap(core_w: float) -> float:
    """단기 바구니 금액 — 부동소수 오차로 $999.99 가 되면 lot_plan 이 종목 수를 하나 덜 잡는다."""
    return round((1 - core_w) * TOTAL, 2)


def core_curve(spy: pd.Series, index, amount: float) -> pd.Series:
    s = spy.reindex(index).ffill()
    return amount * (1 - COST) * s / s.iloc[0]


def combine(core_w: float, spy: pd.Series, sat: pd.Series, quarterly: bool) -> pd.Series:
    """코어(SPY) + 단기 바구니 곡선 → 합산 곡선 (시작 TOTAL).

    quarterly=False: 자본 따로 — 두 곡선을 그냥 더한다.
    quarterly=True : 분기 첫 거래일에 목표 비중으로 되돌림(옮긴 금액 × 편도 비용 × 2). 단기 바구니
                     수익률을 금액과 무관하다고 보는 근사(실제로는 금액이 바뀌면 lot_plan 도 바뀜)."""
    idx = sat.index
    core = core_curve(spy, idx, core_w * TOTAL)
    if not quarterly:
        return core + sat
    rc, rs = core.pct_change().fillna(0.0).to_numpy(), sat.pct_change().fillna(0.0).to_numpy()
    q = idx.to_period("Q")
    vc, vs = float(core.iloc[0]), float(sat.iloc[0])
    out = []
    for i in range(len(idx)):
        if i:
            vc *= 1 + rc[i]
            vs *= 1 + rs[i]
            if q[i] != q[i - 1]:
                tot = vc + vs
                moved = abs(vs - (1 - core_w) * tot)
                tot -= moved * COST * 2
                vc, vs = core_w * tot, (1 - core_w) * tot
        out.append(vc + vs)
    return pd.Series(out, index=idx)


def name(k: str) -> str:
    return f"{scat.CATALOG.get(k, {}).get('name', k)} `{k}`"


def pct(x, d=1) -> str:
    return "—" if x is None or x != x else f"{x:+.{d}%}"


# ── 보고서 ───────────────────────────────────────────────────────────────────

def write_report(ctx: dict, path: Path):
    spy = ctx["spy"]
    L = [f"# 코어 SPY + 단기 고변동 바구니 백테스트 — {ctx['date']}", "",
         f"개발 구간 **{bt.DEV_START} ~ {bt.DEV_END}** (평가 {ctx['meta']['eval_start']} ~ "
         f"{ctx['meta']['eval_end']}). 봉인 구간은 열지 않았다. 생성: `research/two_sleeve.py`.", ""]

    L += ["## 요약 (자동 생성)", ""]
    passed = [r for r in ctx["strat_rows"] if r["uni"] == "hv" and r["pass_all"]]
    beat = [r for r in ctx["strat_rows"] if r["uni"] == "hv" and r["sharpe"] >= spy["sharpe"]]
    L.append(f"- 단기 전략 단독($10,000, 고변동 상위 {VOL_TOP}): SPY 샤프({spy['sharpe']:.2f}) 이상 "
             f"**{len(beat)}개** / {len(SAT_KEYS)}, 통과 기준 세 조건 모두 만족 **{len(passed)}개**"
             + (f" ({', '.join('`' + r['key'] + '`' for r in passed)})" if passed else "") + ".")
    L.append(f"- 고변동 필터 효과(같은 전략, S&P 500 전체 대비 샤프): " + ", ".join(
        f"`{k}` {ctx['hv_vs_all'][k]:+.2f}" for k in SAT_KEYS) + ".")
    best = max(ctx["combo_rows"], key=lambda r: r["none"]["sharpe"])
    L.append(f"- 합산(리밸런싱 없음) 샤프 최고: 코어 {best['core_w']:.0%} + `{best['key']}` → "
             f"샤프 {best['none']['sharpe']:.2f} · MDD {best['none']['mdd']:.1%} "
             f"(SPY 100%: 샤프 {ctx['spy100']['sharpe']:.2f} · MDD {ctx['spy100']['mdd']:.1%}). "
             f"합산 {len(ctx['combo_rows'])}조합 중 SPY 100% 샤프 이상 "
             f"**{sum(r['none']['sharpe'] >= ctx['spy100']['sharpe'] for r in ctx['combo_rows'])}개**.")
    L.append(f"- 참고선(고변동 상위 {VOL_TOP} 동일가중 보유, 타이밍 없음): 총수익 {pct(ctx['ref']['ret'])} · "
             f"샤프 {ctx['ref']['sharpe']:.2f} · MDD {ctx['ref']['mdd']:.1%}.")
    L.append("")

    L += ["## 설계 (실행 전 고정)", "",
          "- **코어**: SPY(수정주가) 첫날 매수 후 보유. 매수 비용 1회(수수료 0.1% + 슬리피지 0.05%), 소수점 주식 근사.",
          f"- **단기 바구니 유니버스**: 그날 S&P 500 구성종목(시점별 편입 근사, 상장 후 252봉 이상) 중 "
          f"직전 {VOL_WINDOW}거래일 일간 로그수익률 표준편차 상위 {VOL_TOP}종목. 그날 종가까지만 본다 — "
          "점수 계산·체결과 같은 정보 시점이라 미래 참조 없음. 신규 매수 후보만 이 집합으로 좁히고, "
          "보유 종목은 집합에서 빠져도 규칙대로 관리한다.",
          "- **단기 전략**: " + ", ".join(name(k) for k in SAT_KEYS) + " — 평균회귀·진폭 매매 계열. "
          "매매 규칙은 토너먼트와 같다(시드별 자본, 매수 문턱 60점, 손절 7%, 익절 기준 20%, 트레일링 15%, "
          "7거래일 리밸런싱, 약세장 신규 매수 보류, EV 끔, 수수료 0.1%·슬리피지 0.05%).",
          "- **동점 처리**: 같은 점수면 최근 20일 평균 거래대금 큰 순(`tie_break=dollar_volume`, 라이브와 같은 규칙).",
          "- **대조군**: 같은 4개 전략을 S&P 500 전체에서. **참고선**: 고변동 집합 동일가중 보유(7거래일마다 교체, 타이밍 없음).",
          "- **비율**: 총 $10,000, 코어/단기 " + "·".join(f"{w:.0%}/{1 - w:.0%}" for w in CORE_WEIGHTS)
          + ". 단기 바구니는 실제 금액으로 따로 굴린다 — 소액 단위 정책(`lot_plan`, 최소 단위 $500)이 그대로 걸린다. "
          "합산은 ① 리밸런싱 없음(자본 따로) ② 분기 리밸런싱(근사). 같은 비율의 'SPY + 현금'을 같이 본다.",
          f"- **판정(단기 전략)**: 토너먼트 통과 기준 그대로 — SPY 샤프 이상 · MDD 가 SPY 보다 "
          f"{st.PASS_MDD_SLACK:.0%}p 넘게 나쁘지 않음 · 보정 샤프 > 0. DSR 시도 수 N = **{ctx['n_trials']}** "
          f"(이전 토너먼트 {ctx['n_prior']} + 이번 {ctx['n_trials'] - ctx['n_prior']}), "
          f"V = {ctx['sr_var']:.2e}, SR0 = {ctx['sr0']:.2f}(연율).", ""]

    u = ctx["ustats"]
    L += [f"## 고변동 상위 {VOL_TOP} — 어떤 종목들인가", "",
          f"- 평가 구간 하루 평균 **{u['count_mean']:.0f}종목**(최소 {u['count_min']}), 리밸런싱(7거래일) 사이 "
          f"**{u['stay_per_rebalance']:.0%}** 가 그대로 남는다. 5년간 한 번이라도 뽑힌 종목 {u['distinct_names']}개.",
          f"- 연율 변동성 중앙값: 선택 **{u['vol_selected_median']:.0%}** vs 후보 전체 {u['vol_all_median']:.0%}.",
          "- 섹터(선택된 날 수 가중, 현재 GICS): " + ", ".join(
              f"{s} {v:.0%}" for s, v in list(u["sectors"].items())[:6]) + ".",
          "- 가장 자주 뽑힌 종목(평가일 중 비율): " + ", ".join(
              f"{t} {v:.0%}" for t, v in u["top_names"].items()) + ".", ""]

    L += ["## 단기 전략 단독 — $10,000", "",
          "| 전략 | 유니버스 | 총수익 | CAGR | MDD | 샤프 | 보정 샤프 | DSR | SPY 상관 | 베타 | 2020 폭락 | 2022 | 거래 | 승률 | −7% 초과 손실 | 판정 |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in ctx["strat_rows"]:
        verdict = ("통과" if r["pass_all"] else
                   " · ".join(x for x, ok in (("샤프<SPY", r["sharpe_ok"]), ("MDD 초과", r["mdd_ok"]),
                                              ("보정 샤프≤0", r["dsr_ok"])) if not ok))
        L.append(f"| {name(r['key'])} | {UNI_LABEL[r['uni']]} | {pct(r['ret'], 0)} | {pct(r['cagr'])} | "
                 f"{r['mdd']:.1%} | {r['sharpe']:.2f} | {r['sr_deflated']:+.2f} | {r['dsr']:.3f} | "
                 f"{r['corr_spy']:.2f} | {r['beta']:.2f} | {r['covid_dd']:.1%} | {pct(r['yearly'].get(2022))} | "
                 f"{r['trades']['n']} | {r['trades']['win']:.0%} | {r['trades']['beyond_stop']:.0%} | {verdict} |")
    ref = ctx["ref"]
    L.append(f"| 참고선: 동일가중 보유 | {UNI_LABEL['hv']} | {pct(ref['ret'], 0)} | {pct(ref['cagr'])} | "
             f"{ref['mdd']:.1%} | {ref['sharpe']:.2f} | — | — | {ref['corr_spy']:.2f} | {ref['beta']:.2f} | "
             f"{ref['covid_dd']:.1%} | {pct(ref['yearly'].get(2022))} | — | — | — | 참고 |")
    L.append(f"| **SPY** | — | {pct(spy['ret'], 0)} | {pct(spy['cagr'])} | {spy['mdd']:.1%} | "
             f"{spy['sharpe']:.2f} | — | — | 1.00 | 1.00 | {spy['covid_dd']:.1%} | "
             f"{pct(spy['yearly'].get(2022))} | — | — | — | 기준 |")
    L += ["", "`−7% 초과 손실` = 손절선(7%)보다 더 잃고 끝난 거래 비율. 백테스트는 손절을 리밸런싱 날(7거래일)에만 "
          "평가해서 라이브(틱 감시)보다 크게 나온다 — 다만 고변동 종목일수록 갭 하락으로 손절선을 건너뛰는 "
          "실제 위험도 크다.", ""]

    L += ["## 단기 바구니 금액별 — 소액 단위 정책 효과 (고변동 상위 100)", "",
          "| 전략 | " + " | ".join(f"${(1 - w) * TOTAL:,.0f}" for w in CORE_WEIGHTS) + " | $10,000 |",
          "|---|" + "---:|" * (len(CORE_WEIGHTS) + 1)]
    for k in SAT_KEYS:
        cells = []
        for cap in [sleeve_cap(w) for w in CORE_WEIGHTS] + [TOTAL]:
            s = ctx["sleeve"][k][f"{cap:.0f}"]
            cells.append(f"{pct(s['ret'], 0)} · 샤프 {s['sharpe']:.2f} · MDD {s['mdd']:.0%}")
        L.append(f"| {name(k)} | " + " | ".join(cells) + " |")
    L += ["", "각 칸 = 총수익 · 샤프 · MDD. $2,500 미만이면 `lot_plan` 이 종목 수를 줄인다"
          "($1,000 → 2종목, $2,000 → 4종목).", ""]

    for quarterly, title in ((False, "리밸런싱 없음 (자본 따로)"), (True, "분기 리밸런싱 (근사)")):
        tag = "q" if quarterly else "none"
        L += [f"## 합산 포트폴리오 — {title}", "",
              "| 단기 바구니 | " + " | ".join(f"코어 {w:.0%} / 단기 {1 - w:.0%}" for w in CORE_WEIGHTS) + " |",
              "|---|" + "---:|" * len(CORE_WEIGHTS)]
        for k in SAT_KEYS:
            cells = []
            for w in CORE_WEIGHTS:
                c = next(r for r in ctx["combo_rows"] if r["key"] == k and r["core_w"] == w)[tag]
                cells.append(f"{pct(c['ret'], 0)} · 샤프 {c['sharpe']:.2f} · MDD {c['mdd']:.1%}")
            L.append(f"| {name(k)} | " + " | ".join(cells) + " |")
        cells = [f"{pct(c['ret'], 0)} · 샤프 {c['sharpe']:.2f} · MDD {c['mdd']:.1%}"
                 for c in (ctx["ref_combo"][f"{w}"][tag] for w in CORE_WEIGHTS)]
        L.append("| 참고선: 동일가중 보유 | " + " | ".join(cells) + " |")
        cells = [f"{pct(c['ret'], 0)} · 샤프 {c['sharpe']:.2f} · MDD {c['mdd']:.1%}"
                 for c in (ctx["cash_combo"][f"{w}"] for w in CORE_WEIGHTS)]
        L.append("| (단기 바구니 = 현금) | " + " | ".join(cells) + " |")
        s1 = ctx["spy100"]
        L += ["", f"기준: **SPY 100%** {pct(s1['ret'], 0)} · 샤프 {s1['sharpe']:.2f} · MDD {s1['mdd']:.1%} "
              "(매수 비용 1회 포함).", ""]

    L += ["## 연도별 수익", "",
          "| 곡선 | " + " | ".join(str(y) for y in ctx["years"]) + " |",
          "|---|" + "---:|" * len(ctx["years"])]
    L.append("| **SPY** | " + " | ".join(pct(spy["yearly"].get(y)) for y in ctx["years"]) + " |")
    for r in ctx["strat_rows"]:
        if r["uni"] == "hv":
            L.append(f"| {name(r['key'])} (고변동, $10k) | "
                     + " | ".join(pct(r["yearly"].get(y)) for y in ctx["years"]) + " |")
    L.append("| 참고선: 동일가중 보유 | " + " | ".join(pct(ref["yearly"].get(y)) for y in ctx["years"]) + " |")
    L.append("")

    L += ["## 한계", "",
          "- 시점별 S&P 500 구성은 위키 변경 이력으로 되감은 근사이고, 상장폐지·인수된 과거 구성종목 일부는 시세가 없다"
          f"(받은 종목 {ctx['meta']['loaded']}/{ctx['meta']['requested']}). 고변동 종목일수록 이런 탈락(파산 등)이 많아 "
          "결과가 좋게 기울 수 있다.",
          "- 일봉 백테스트라 손절·트레일링은 7거래일마다 평가된다. 장중 진폭을 노리는 매매는 재현하지 못한다.",
          "- 분기 리밸런싱 합산은 근사다(단기 바구니 금액이 바뀌면 실제로는 `lot_plan` 종목 수도 바뀜).",
          "- 전략·비율을 이 표에서 고르는 것 자체가 또 한 번의 선택이다. 봉인 구간은 이미 썼으므로 "
          "여기서 고른 조합의 표본 외 검증은 forward 페이퍼로만 한다.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    a = ap.parse_args(argv)
    t_start = time.time()

    def log(msg):
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    try:
        import yf_compat
        yf_compat.patch_tz_cache()
        yf_compat.raise_fd_limit()
    except Exception:
        pass

    bundle = bt.load_research_bundle("sp500", bt.DEV_START, bt.DEV_END)
    meta = bundle["meta"]
    log(f"번들 {meta['loaded']}/{meta['requested']}종목 · 평가 {meta['eval_start']}~{meta['eval_end']}")
    hv, pick, vol, elig = high_vol_members(bundle)
    ustats = universe_stats(bundle, pick, vol, elig)
    log(f"고변동 집합: 평균 {ustats['count_mean']:.0f}종목 · 변동성 중앙값 "
        f"{ustats['vol_selected_median']:.0%} (전체 {ustats['vol_all_median']:.0%})")

    caps = [TOTAL] + [sleeve_cap(w) for w in CORE_WEIGHTS]
    tasks = [(k, "sp500", TOTAL) for k in SAT_KEYS] + [(k, "hv", c) for c in caps for k in SAT_KEYS]
    res = run_tasks(bundle, hv, tasks, a.workers, log)

    spy_px = bundle["spy_close"]
    eval_idx = bundle["date_index"][int(bundle["eval_start_idx"]):]
    spy = curve_stats(spy_px.reindex(eval_idx), spy_px)

    # 단기 전략 단독 — 토너먼트 기준, 이전 시도까지 합친 N 으로 DSR
    full = {f"{k}@{u}": res[(k, u, TOTAL)] for u in ("hv", "sp500") for k in SAT_KEYS}
    prior = st.load_prior("auto")
    prior_srs = [t["sr"] for p in prior for t in p["trials"]]
    srs = prior_srs + [bt.sharpe_moments(r.daily_returns)["sr"] for r in full.values()]
    n_trials, sr_var = len(srs), float(np.var(srs, ddof=1))
    summ = bt.summarize_results(full, n_trials=n_trials, sr_var=sr_var)
    strat_rows = []
    for row in summ["rows"]:
        k, u = row["key"].split("@")
        r = full[row["key"]]
        cs = curve_stats(r.equity_curve, spy_px)
        out = {"key": k, "uni": u, **{x: row[x] for x in ("ret", "cagr", "mdd", "sharpe", "dsr",
                                                          "sr_deflated", "skew", "kurt")},
               **{x: cs[x] for x in ("corr_spy", "beta", "covid_dd", "yearly")},
               "trades": trade_stats(r)}
        out["sharpe_ok"] = bool(out["sharpe"] >= spy["sharpe"])
        out["mdd_ok"] = bool(out["mdd"] >= spy["mdd"] - st.PASS_MDD_SLACK)
        out["dsr_ok"] = bool(out["sr_deflated"] > 0)
        out["pass_all"] = out["sharpe_ok"] and out["mdd_ok"] and out["dsr_ok"]
        strat_rows.append(out)
    by = {(r["key"], r["uni"]): r for r in strat_rows}
    hv_vs_all = {k: by[(k, "hv")]["sharpe"] - by[(k, "sp500")]["sharpe"] for k in SAT_KEYS}

    sleeve = {k: {f"{c:.0f}": {**bt.perf_stats(res[(k, 'hv', c)].equity_curve),
                               "trades": len(res[(k, 'hv', c)].trades)} for c in caps} for k in SAT_KEYS}

    ref_curve = equal_weight_curve(bundle, pick)
    ref = curve_stats(ref_curve, spy_px)
    combo_rows, ref_combo, cash_combo = [], {}, {}
    for w in CORE_WEIGHTS:
        cap = sleeve_cap(w)
        for k in SAT_KEYS:
            sat = res[(k, "hv", cap)].equity_curve
            combo_rows.append({"key": k, "core_w": w,
                               "none": curve_stats(combine(w, spy_px, sat, False), spy_px),
                               "q": curve_stats(combine(w, spy_px, sat, True), spy_px)})
        rsat = ref_curve * cap / ref_curve.iloc[0]
        ref_combo[f"{w}"] = {"none": curve_stats(combine(w, spy_px, rsat, False), spy_px),
                             "q": curve_stats(combine(w, spy_px, rsat, True), spy_px)}
        cash = pd.Series(cap, index=eval_idx)
        cash_combo[f"{w}"] = curve_stats(combine(w, spy_px, cash, False), spy_px)
    spy100 = curve_stats(core_curve(spy_px, eval_idx, TOTAL), spy_px)

    today = date.today().isoformat()
    ctx = {"date": today, "meta": meta, "spy": spy, "spy100": spy100, "ustats": ustats,
           "strat_rows": strat_rows, "hv_vs_all": hv_vs_all, "sleeve": sleeve, "ref": ref,
           "combo_rows": combo_rows, "ref_combo": ref_combo, "cash_combo": cash_combo,
           "n_trials": n_trials, "n_prior": len(prior_srs), "sr_var": sr_var,
           "sr0": bt.expected_max_sharpe(n_trials, sr_var) * math.sqrt(252),
           "years": sorted(spy["yearly"])}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    js = RESULTS_DIR / f"two-sleeve-{today}.json"
    doc = ROOT / "docs" / f"two-sleeve-{today}.md"
    params = {"sat_keys": SAT_KEYS, "vol_window": VOL_WINDOW, "vol_top": VOL_TOP,
              "tie_break": TIE_BREAK, "total": TOTAL, "core_weights": CORE_WEIGHTS, "cost": COST,
              "run_kwargs": st.RUN_KW, "window": [bt.DEV_START, bt.DEV_END],
              "prior": [str(p["path"].relative_to(ROOT)) for p in prior]}
    js.write_text(json.dumps({"params": params, **{k: v for k, v in ctx.items() if k != "date"}},
                             ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(ctx, doc)
    log(f"완료 {time.time() - t_start:.0f}s → {doc.relative_to(ROOT)} · {js.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
