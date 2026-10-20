"""
규칙 전략 토너먼트 — 정직한 평가 기준으로 strategy_catalog 의 규칙 전략을 겨룬다.

기준(backtester 의 '정직한 평가 기준' 절):
  • 개발 구간 2020-01-01 ~ 2024-12-31 만 사용 (2025-01-01~ 봉인)
  • 같은 구간 SPY(수정주가)와 항상 같이 본다
  • 시도한 전략 수(전략 × 유니버스) 만큼 샤프를 깎는다 — Deflated Sharpe Ratio
  • 일간 수익률 상관 + 계층 군집으로 '계열'을 묶고, 계열별 대표만 고른다
    (대표 = S&P 500 유니버스 DSR 최고 — 손으로 고른 23종목은 선택 편향이 커서 보조)

실행 (긴 작업 — 백그라운드 권장):
  python3 research/strategy_tournament.py                      # 개발 구간 토너먼트
  python3 research/strategy_tournament.py --workers 6
  # 봉인 구간 1회 확인 — 사용자 승인 후, 개발 구간에서 고른 대표만:
  python3 research/strategy_tournament.py --holdout --keys a,b,c

결과: docs/strategy-tournament-<날짜>.md · research/results/strategy-tournament-<날짜>.json
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
HOLDOUT_MARK = ROOT / "research" / "results" / "holdout-opened.json"

_BUNDLE = None


def _init_worker(path: str):
    global _BUNDLE
    with open(path, "rb") as f:
        _BUNDLE = pickle.load(f)


def _run_one(key: str):
    t0 = time.time()
    r = bt.run(strategy=key, prefetched=_BUNDLE, **RUN_KW)
    return key, r, time.time() - t0


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
    end = (date.today() - timedelta(days=1)).isoformat()
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
    HOLDOUT_MARK.parent.mkdir(parents=True, exist_ok=True)
    HOLDOUT_MARK.write_text(json.dumps({"opened": date.today().isoformat(), "keys": keys,
                                        "end": end}, ensure_ascii=False))
    p = ROOT / "research" / "results" / f"holdout-{date.today().isoformat()}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    log(f"저장: {p.relative_to(ROOT)}")


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--threshold", type=float, default=bt.FAMILY_CORR, help="계열 병합 평균 상관 기준")
    ap.add_argument("--keys", default="", help="쉼표 구분 전략 키 (기본: ai_influencer 제외 전부)")
    ap.add_argument("--holdout", action="store_true", help="봉인 구간 1회 확인 (사용자 승인 후)")
    ap.add_argument("--force", action="store_true", help="봉인 구간 재실행 (비권장)")
    a = ap.parse_args(argv)

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
