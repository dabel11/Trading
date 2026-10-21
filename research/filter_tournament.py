#!/usr/bin/env python3
"""매수 보류 필터 2개를 규칙 전략 토너먼트(개발 구간)에 넣어 측정한다.

필터(이벤트 스터디에서 나온 '비용 없는' 후보 — docs/analyst-event-study·crowd-study 2026-10-20):
  analyst : 애널리스트 '매수→보유' 하향(Yahoo 등급 이력) 발표 거래일부터 5거래일 동안 그 종목 신규 매수 보류
  crowd   : 의사결정일 기준 직전 5일 수익이 그날 S&P 500(시점별 구성) 상위 20% 면 신규 매수 보류
            (crowd 스터디: 금요일 상위 5분위 → 다음 5거래일 부진. 그 조건을 결정 시점에 그대로 적용한다.
             '상위 20% 였던 날부터 5일 연장' 판은 후보 종목·일의 절반을 막아 가벼운 필터가 아니게 되므로
             수익률을 보기 전에 버렸다 — 스모크 테스트 보류 비율 23종목 50%)
변형: base(필터 없음) · analyst · crowd · both. 나머지는 strategy_tournament 와 완전히 같다
(같은 RUN_KW·번들·무작위 동점 처리 seed — 변형 간 seed 를 맞춘 짝 비교).

구현: backtester.run 은 '보유하지 않은 종목은 members[t][idx] 가 False 면 신규 후보에서 뺀다'.
필터는 그 배열에 보류일을 AND 로 겹칠 뿐이라 backtester·라이브 코드는 건드리지 않는다
(23종목 유니버스는 members 가 없으므로 전부 True 배열을 만들어 겹친다 — 필터 없는 base 는 원 번들 그대로).

정직성:
  • 개발 구간 2020-01-01~2024-12-31 만. 봉인 구간은 이미 1회 쓰였고(holdout-opened.json) 열지 않는다.
  • 두 필터는 2018–2024 데이터에서 찾았으므로 이 구간 결과는 표본 내 확인이다(낙관 편향). 확인의 몫은 forward 운영.
  • 결과 JSON 은 strategy-tournament-*-filters.json 이름·new_trials 형식이라 이후 토너먼트의
    --prior auto 가 이번 시도들을 DSR 의 N·V 에 자동으로 합친다.

실행 (긴 작업 — 백그라운드):
  TRADE_BT_CACHE=~/.cache/trade_research/bt_cache python3 -P research/filter_tournament.py --seeds 10
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
import pandas as pd                     # noqa: E402

import backtester as bt                 # noqa: E402
import strategy_catalog as scat         # noqa: E402
import strategy_tournament as st        # noqa: E402

VARIANTS = ("base", "analyst", "crowd", "both")
HOLD_DAYS = 5                            # 신호 거래일 포함 5거래일 보류
CROWD_Q = 0.80                           # 직전 5일 수익 상위 20%
ANALYST_EVENTS = Path.home() / ".cache" / "trade_research" / "analyst" / "derived" / "events.parquet"
ANALYST_RAW = Path.home() / ".cache" / "trade_research" / "analyst" / "yahoo_ud"


def _spread_block(sig: np.ndarray) -> np.ndarray:
    """sig[i]=True 인 날부터 HOLD_DAYS 거래일(i..i+4) 보류."""
    out = np.zeros_like(sig, dtype=bool)
    for k in range(HOLD_DAYS):
        out[k:] |= sig[:len(sig) - k]
    return out


def crowd_threshold(sp: dict) -> np.ndarray:
    """날짜별 S&P 500(그날 구성종목) 직전 5일 수익의 80분위수."""
    sd, mem = sp["stock_data"], sp["members"]
    T = len(sp["date_index"])
    R = np.full((T, len(sd)), np.nan)
    M = np.zeros((T, len(sd)), bool)
    for j, (t, df) in enumerate(sd.items()):
        c = df["Close"].to_numpy(dtype=float)
        R[5:, j] = c[5:] / c[:-5] - 1
        M[:, j] = mem[t] if t in mem else False
    R[~M] = np.nan
    with np.errstate(all="ignore"):
        return np.nanquantile(R, CROWD_Q, axis=1)


def crowd_blocks(b: dict, thr: np.ndarray) -> dict:
    out = {}
    for t, df in b["stock_data"].items():
        c = df["Close"].to_numpy(dtype=float)
        r5 = np.full(len(c), np.nan)
        r5[5:] = c[5:] / c[:-5] - 1
        with np.errstate(invalid="ignore"):
            out[t] = np.nan_to_num(r5, nan=-np.inf) >= thr      # 의사결정일 당일 상위 20% 만
    return out


def _yahoo_extra(tickers: list[str]) -> pd.DataFrame:
    """분석 캐시에 없는 종목(과거 구성종목 등)의 Yahoo 등급 이력 — 2025 이전 행만 저장(봉인)."""
    import yfinance as yf
    rows = []
    for t in tickers:
        p = ANALYST_RAW / f"{t}.parquet"
        if not p.exists():
            try:
                d = yf.Ticker(bt.YF_ALIASES.get(t, t)).upgrades_downgrades
            except Exception:
                d = None
            if d is None or not len(d):
                d = pd.DataFrame(columns=["Firm", "ToGrade", "FromGrade", "Action"],
                                 index=pd.DatetimeIndex([], name="GradeDate"))
            d = d[d.index < bt.HOLDOUT_START]
            d.reset_index().to_parquet(p)
            time.sleep(0.4)
        d = pd.read_parquet(p)
        if len(d):
            d["ticker"] = t
            rows.append(d)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def analyst_events(tickers: list[str], log) -> pd.DataFrame:
    """'매수→보유' 하향(D4: 행동 down · 새 등급 보유 수준) — (티커, 동부 날짜).
    현 S&P 500 은 애널리스트 스터디 캐시, 그 밖(과거 구성종목·23종목 중 비 S&P)은 Yahoo 에서 보충."""
    import analyst_event_study as aes
    ev = pd.read_parquet(ANALYST_EVENTS)
    ev = ev[(ev["kind"] == "하향") & (ev["to_lvl"] == 3)][["ticker", "date"]]
    extra = [t for t in tickers if not (ANALYST_RAW / f"{t}.parquet").exists()]
    if extra:
        log(f"  분석 캐시에 없는 종목 {len(extra)}개 Yahoo 이력 수집")
        x = _yahoo_extra(extra)
        if len(x):
            x["ts"] = pd.to_datetime(x["GradeDate"])
            et = x["ts"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
            x["date"] = et.dt.tz_localize(None).dt.normalize()
            lv = aes.lvl(x["ToGrade"])
            add = x[(x["Action"].fillna("").str.lower() == "down") & (lv == 3)][["ticker", "date"]]
            ev = pd.concat([ev, add], ignore_index=True)
    ev = ev[ev["date"] < pd.Timestamp(bt.HOLDOUT_START)].drop_duplicates()
    with_hist = sum(1 for t in tickers if (ANALYST_RAW / f"{t}.parquet").exists()
                    and len(pd.read_parquet(ANALYST_RAW / f"{t}.parquet")))
    log(f"  매수→보유 하향 {len(ev):,}건 · 등급 이력 있는 종목 {with_hist}/{len(tickers)}")
    return ev


def analyst_blocks(b: dict, ev: pd.DataFrame) -> dict:
    di = b["date_index"]
    out = {t: np.zeros(len(di), bool) for t in b["stock_data"]}
    for t, d in zip(ev["ticker"], ev["date"]):
        if t in out:
            k = int(di.searchsorted(d, side="left"))      # 발표일(거래일 아니면 다음 거래일)
            if k < len(di):
                sig = np.zeros(len(di), bool)
                sig[k] = True
                out[t] |= _spread_block(sig)
    return out


def variant_bundle(b: dict, blocks: dict | None) -> dict:
    if blocks is None:
        return b
    base = b.get("members")
    T = len(b["date_index"])
    mem = {}
    for t in b["stock_data"]:
        m = base[t] if base is not None and t in base else (np.zeros(T, bool) if base is not None
                                                          else np.ones(T, bool))
        mem[t] = m & ~blocks.get(t, np.zeros(T, bool))
    return dict(b, members=mem)


def block_share(b: dict, blocks: dict) -> float:
    """평가 구간 '신규 후보 가능 종목·일' 중 보류된 비율."""
    e = b["eval_start_idx"]
    mem = b.get("members")
    tot = blk = 0
    for t, blk_t in blocks.items():
        m = mem[t][e:] if mem is not None and t in mem else np.ones(len(blk_t) - e, bool)
        tot += int(m.sum())
        blk += int((m & blk_t[e:]).sum())
    return blk / tot if tot else 0.0


def run_variant(u: str, v: str, bundle: dict, keys: list[str], seeds: int, workers: int, log) -> dict:
    path = bt.CACHE_DIR / f"filter_bundle_{u}_{v}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    tasks = [(k, "random", s) for s in range(seeds) for k in keys]
    out = {}
    t0 = time.time()
    try:
        with ProcessPoolExecutor(max_workers=workers, initializer=st._init_worker,
                                 initargs=(str(path),)) as ex:
            for task, r, al, dt in ex.map(st._run_task, tasks):
                out[task] = r
                if len(out) % max(1, len(tasks) // 10) == 0:
                    log(f"  {u:9} {v:8} {len(out)}/{len(tasks)} ({time.time() - t0:.0f}s)")
    finally:
        path.unlink(missing_ok=True)
    return {k: [out[(k, "random", s)] for s in range(seeds)] for k in keys}


LABEL = {"base": "필터 없음", "analyst": "애널리스트 하향 필터", "crowd": "1주 승자 필터", "both": "두 필터"}


def write_report(js: Path, doc: Path):
    from scipy.stats import binomtest
    doc.parent.mkdir(parents=True, exist_ok=True)
    d = json.loads(js.read_text())
    rows, paired, bench = d["rows"], d["paired"], d["benchmark"]
    keys = sorted({k.split("|")[2] for k in rows})
    U = ("sp500", "watchlist")
    L: list[str] = []
    w = L.append
    w(f"# 매수 보류 필터 토너먼트 — {d['date']}")
    w("")
    w(f"개발 구간 **{d['window'][0]} ~ {d['window'][1]}**, 규칙 전략 {len(keys)}개 × 유니버스 2개 × 변형 4개, "
      f"무작위 동점 처리 seed {d['seeds']}개(변형 간 같은 seed 로 짝 비교). 봉인 구간은 열지 않았다. "
      "생성: `research/filter_tournament.py`.")
    w("")
    w("## 변형별 요약")
    w("")
    w("| 유니버스 | 변형 | 후보 보류 비율 | 샤프 개선 전략 수 (부호 검정 p) | 평균 Δ샤프 | 중앙 Δ샤프 | 평균 Δ총수익 | 평균 ΔMDD | 평균 Δ거래 수 | 토너먼트 기준 3개 통과 | SPY 샤프 이상 |")
    w("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for u in U:
        for v in ("base", "analyst", "crowd", "both"):
            rr = [rows[f"{u}|{v}|{k}"] for k in keys]
            npass = sum(bool(r["verdict"]["all"]) for r in rr)
            nspy = sum(r["sharpe"] >= bench[u]["sharpe"] for r in rr)
            if v == "base":
                w(f"| {UNIVERSE[u]} | {LABEL[v]} | — | — | — | — | — | — | — | {npass} | {nspy} |")
                continue
            ps = [paired[f"{u}|{v}|{k}"] for k in keys]
            ds = np.array([p["sharpe"]["mean"] for p in ps])
            up = int((ds > 0).sum())
            pv = binomtest(up, len(ds), 0.5).pvalue
            w(f"| {UNIVERSE[u]} | {LABEL[v]} | {d['block_share'][u][v]:.1%} | {up}/{len(ds)} (p {pv:.2f}) | "
              f"{ds.mean():+.3f} | {np.median(ds):+.3f} | "
              f"{np.mean([p['total_return']['mean'] for p in ps]) * 100:+.1f}%p | "
              f"{np.mean([p['mdd']['mean'] for p in ps]) * 100:+.1f}%p | "
              f"{np.mean([p['trades']['mean'] for p in ps]):+.0f} | {npass} | {nspy} |")
    w("")
    w(f"- SPY 같은 구간 샤프 {bench['sp500']['sharpe']:.2f}, MDD {bench['sp500']['mdd'] * 100:.1f}%. "
      "토너먼트 기준 3개 = 샤프 ≥ SPY · MDD ≥ SPY−5%p · 운 보정 샤프 > 0 (DSR 은 이전 토너먼트 시도까지 합친 "
      f"N={d['n_trials']} 로 계산).")
    w("- Δ = 필터 변형 − 필터 없음, 같은 seed 끼리 짝지어 seed 평균. ΔMDD 는 양수가 개선(낙폭 축소).")
    w("")
    for u in U:
        w(f"## 전략별 — {UNIVERSE[u]}")
        w("")
        w("| 전략 | 필터 없음 샤프 (±sd) | 애널리스트 Δ샤프 (t) | 1주 승자 Δ샤프 (t) | 두 필터 Δ샤프 (t) | 두 필터 Δ총수익 | 두 필터 ΔMDD | 두 필터 기준 통과 |")
        w("|---|---:|---:|---:|---:|---:|---:|:-:|")
        order = sorted(keys, key=lambda k: -rows[f"{u}|base|{k}"]["sharpe"])
        for k in order:
            b = rows[f"{u}|base|{k}"]
            cells = [f"{st._name(k)} `{k}`", f"{b['sharpe']:.2f} ± {b.get('sharpe_sd', 0):.2f}"]
            for v in ("analyst", "crowd", "both"):
                p = paired[f"{u}|{v}|{k}"]["sharpe"]
                ok_t = p["t"] == p["t"] and abs(p["t"]) < 1000      # seed 간 차이가 0 이면(동점 없음) t 무의미
                cells.append(f"{p['mean']:+.3f} ({p['t']:+.1f})" if ok_t else f"{p['mean']:+.3f} (seed 무관)")
            pb = paired[f"{u}|both|{k}"]
            vb, v0 = rows[f"{u}|both|{k}"]["verdict"]["all"], b["verdict"]["all"]
            cells += [f"{pb['total_return']['mean'] * 100:+.1f}%p", f"{pb['mdd']['mean'] * 100:+.1f}%p",
                      ("✅" if vb else "—") + (" (원래 ✅)" if v0 else "")]
            w("| " + " | ".join(cells) + " |")
        w("")
    w("- t = 같은 seed 짝 차이의 평균 / 표준오차(seed 간). '(seed 무관)' = 동점이 없어 seed 마다 결과가 같은 전략. "
      "전략 사이에는 상관이 있어 전략 수만큼 독립 증거가 아니다.")
    doc.write_text("\n".join(L) + "\n", encoding="utf-8")


UNIVERSE = {"sp500": "S&P 500 (시점별 편입 근사)", "watchlist": "기존 23종목 (손으로 고름)"}
TABLES = Path.home() / ".cache" / "trade_research" / "filter" / "filter_tables.md"   # 문서 부록용 표


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--keys", default="")
    ap.add_argument("--out", default="", help="결과 JSON 경로(기본: research/results/strategy-tournament-<날짜>-filters.json)")
    ap.add_argument("--report", metavar="JSON", help="재계산 없이 이 결과 JSON 으로 표 문서만 다시 쓴다")
    a = ap.parse_args()
    if a.report:
        write_report(Path(a.report), TABLES)
        print(f"표: {TABLES}")
        return
    keys = [k for k in a.keys.split(",") if k] or [k for k in scat.CATALOG if k not in st.EXCLUDE]
    today = date.today().isoformat()
    st.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    js = Path(a.out) if a.out else st.RESULTS_DIR / f"strategy-tournament-{today}-filters.json"
    import hashlib
    kh = hashlib.sha1(",".join(keys).encode()).hexdigest()[:8]
    raw_path = bt.CACHE_DIR / f"filter-raw-{today}-{a.seeds}s-{kh}.pkl"      # 중단 후 이어받기용

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    bundles = {u: bt.load_research_bundle(u, bt.DEV_START, bt.DEV_END) for u in ("sp500", "watchlist")}
    for u, b in bundles.items():
        log(f"[{u}] 번들 {b['meta']['loaded']}/{b['meta']['requested']}종목 평가 {b['meta']['eval_start']}~{b['meta']['eval_end']}")
    assert bundles["sp500"]["date_index"].equals(bundles["watchlist"]["date_index"]), "날짜축 불일치"
    thr = crowd_threshold(bundles["sp500"])
    all_tk = sorted(set(bundles["sp500"]["stock_data"]) | set(bundles["watchlist"]["stock_data"]))
    aev = analyst_events(all_tk, log)
    info, raw = {}, {}
    if raw_path.exists():
        raw = pickle.loads(raw_path.read_bytes())          # 중단 후 이어서
    for u, b in bundles.items():
        blk = {"analyst": analyst_blocks(b, aev), "crowd": crowd_blocks(b, thr)}
        blk["both"] = {t: blk["analyst"][t] | blk["crowd"][t] for t in b["stock_data"]}
        info[u] = {v: block_share(b, blk[v]) for v in ("analyst", "crowd", "both")}
        log(f"[{u}] 보류 비율(후보 종목·일): " + ", ".join(f"{v} {s:.1%}" for v, s in info[u].items()))
        for v in VARIANTS:
            if (u, v) in raw:
                continue
            raw[(u, v)] = run_variant(u, v, variant_bundle(b, blk.get(v)), keys, a.seeds, a.workers, log)
            raw_path.write_bytes(pickle.dumps(raw))

    # ── 집계: 토너먼트와 같은 지표·판정 ─────────────────────────────────────────
    prior = st.load_prior("auto", exclude=js)
    prior_trials = [t for p in prior for t in p["trials"]]
    agg, stats = {}, {}
    for (u, v), res in raw.items():
        for k, rs in res.items():
            agg[(u, v, k)], stats[(u, v, k)] = st.aggregate_seeds(rs)
    new_trials = [{"variant": f"filter:{v}", "universe": u, "key": k,
                   "sr": bt.sharpe_moments(agg[(u, v, k)].daily_returns)["sr"]}
                  for (u, v, k) in agg]
    srs = [t["sr"] for t in prior_trials + new_trials]
    n_trials, sr_var = len(srs), float(np.var(srs, ddof=1))
    log(f"시도 수 N={n_trials} (이전 {len(prior_trials)} + 이번 {len(new_trials)}) · V={sr_var:.3e}")

    rows, bench = {}, {}
    for u in bundles:
        for v in VARIANTS:
            summ = bt.summarize_results({k: agg[(u, v, k)] for k in keys}, n_trials=n_trials, sr_var=sr_var,
                                        family_threshold=bt.FAMILY_CORR)
            bench[u] = summ["benchmark"]
            for r in summ["rows"]:
                row = st.apply_seed_stats(r, stats[(u, v, r["key"])], bench[u], n_trials, sr_var)
                row["verdict"] = st.dev_verdict(row, bench[u])
                rows[(u, v, r["key"])] = row
    # 짝 비교: 같은 seed 의 (필터 − base) 샤프·수익·MDD·거래 수
    paired = {}
    for (u, v), res in raw.items():
        if v == "base":
            continue
        for k in keys:
            b0, b1 = raw[(u, "base")][k], res[k]
            d = {m: np.array([getattr(x1, m) - getattr(x0, m) for x0, x1 in zip(b0, b1)])
                 for m in ("sharpe", "total_return", "mdd")}
            d["trades"] = np.array([len(x1.trades) - len(x0.trades) for x0, x1 in zip(b0, b1)])
            paired[(u, v, k)] = {m: {"mean": float(x.mean()),
                                     "t": float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))
                                     if len(x) > 1 and x.std(ddof=1) > 0 else float("nan")}
                                 for m, x in d.items()}
    out = {"date": today, "window": [bt.DEV_START, bt.DEV_END], "tag": "filters", "seeds": a.seeds,
           "tie_break": "random", "run_kwargs": st.RUN_KW, "variants": list(VARIANTS),
           "filters": {"hold_days": HOLD_DAYS, "crowd_quantile": CROWD_Q,
                       "analyst": "Yahoo action=down & 새 등급 보유(3) — 발표 거래일부터 5거래일 신규 매수 보류",
                       "crowd": "의사결정일 직전 5일 수익 ≥ 그날 S&P500 구성종목 80분위 — 그날 신규 매수 보류"},
           "block_share": info, "n_trials": n_trials, "sr_var": sr_var,
           "prior": [{"path": str(p["path"].relative_to(ROOT)), "n_trials": len(p["trials"])} for p in prior],
           "new_trials": new_trials, "benchmark": bench,
           "rows": {f"{u}|{v}|{k}": st._clean_row(r) for (u, v, k), r in rows.items()},
           "paired": {f"{u}|{v}|{k}": p for (u, v, k), p in paired.items()},
           "meta": {u: b["meta"] for u, b in bundles.items()}}
    js.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    log(f"저장: {js}")
    write_report(js, TABLES)
    log(f"표: {TABLES}")


if __name__ == "__main__":
    main()
