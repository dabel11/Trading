#!/usr/bin/env python3
"""애널리스트 등급 변경 이벤트 스터디 — roster analyst 공신력(0.92, 손으로 정한 값)을 실측으로 대체하기 위한 연구.

insider_event_study.py 와 같은 방법·기준(진입·보유·비용·4개 기준선·종목·월 단위·2원 군집 t·달력시간
포트폴리오)을 그대로 재사용한다. 라이브 코드는 읽기만 한다.

데이터
  - Yahoo Finance 'Upgrades & Downgrades' 이력(yfinance Ticker.upgrades_downgrades): 증권사·이전/새 등급·
    행동(up/down/init/main/reit)·목표가·발표 시각. 무료·키 불필요. 종목당 1회 요청, 디스크 캐시.
  - 응답에 2025년 이후 행도 함께 오므로 디스크에 쓰기 전에 버린다(봉인 구간은 저장도 분석도 안 함).
  - 발표 시각은 UTC 로 보고 미 동부시각으로 바꾼다. `analyze` 의 발표일 전후 반응 표가 이 가정을 검증한다
    (장전 발표면 당일 시가 갭, 장중 발표면 당일 장중에 반응이 몰려야 한다).

측정
  - 보수적 진입: 발표일(동부) 다음 거래일 시가 — insider 와 같은 규칙. 빠른 진입: 장전(9:30 전) 발표는 당일 시가.
  - 상향은 매수 신호, 하향은 회피 신호(값이 음수여야 정보가 있다).
  - 라이브 신호 점검: providers.analyst 가 쓰는 Finnhub 추천 집계(강력매수~강력매도 인원)를 같은 이력으로 재구성
    (증권사별 최근 12개월 내 마지막 등급), 같은 stance 식으로 월말 횡단면 예측력 측정.

사용
  python3 -P research/analyst_event_study.py fetch      # 백그라운드 권장(종목 500개, 약 5~10분)
  python3 -P research/analyst_event_study.py build
  python3 -P research/analyst_event_study.py analyze --out <표.md>
가격·구성종목은 insider_event_study.py fetch-prices 캐시를 쓴다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))   # python -P 로도 같은 폴더 모듈을 읽게
import insider_event_study as ies                           # noqa: E402

AN = ies.CACHE.parent / "analyst"
RAW = AN / "yahoo_ud"            # 종목별 응답(2025 이전 행만) — 파싱만 하는 데이터
DERIVED = AN / "derived"

CONS_DAYS = 365                  # 컨센서스: 증권사별 최근 12개월 내 마지막 등급
MIN_CONS = 3                     # 합의 이탈 판정에 필요한 다른 증권사 수
BIG_IB = ["goldman", "morgan stanley", "jp morgan", "jpmorgan", "bank of america", "bofa", "merrill",
          "citigroup", "citi", "barclays", "ubs", "deutsche", "wells fargo"]

# 등급 문구 → 5단계(5 강력매수 … 1 강력매도). 수익률을 보기 전에 정한 사전(미매핑은 build 가 출력).
GRADE = {
    5: ["strong buy", "strong-buy", "top pick", "conviction buy", "buy (conviction)", "best idea"],
    4: ["buy", "outperform", "overweight", "accumulate", "positive", "sector outperform",
        "market outperform", "add", "moderate buy", "long-term buy", "long term buy", "outperformer",
        "above average", "speculative buy", "trading buy", "industry outperform", "overweight/buy",
        "buy/overweight", "sector overweight", "market overweight", "outperform (buy)", "perform/buy"],
    3: ["hold", "neutral", "market perform", "equal-weight", "equal weight", "sector perform",
        "peer perform", "in-line", "inline", "sector weight", "perform", "market weight", "mixed",
        "fair value", "average", "sector neutral", "industry perform", "industry neutral",
        "sector market weight", "neutral/hold", "hold/neutral", "core holding", "market neutral",
        "industry weight", "sector perform/hold", "in line"],
    2: ["underperform", "underweight", "reduce", "negative", "sector underperform", "moderate sell",
        "market underperform", "below average", "underperformer", "industry underperform",
        "sector underweight", "market underweight"],
    1: ["sell", "strong sell", "strong-sell"],
}
_LVL = {g: k for k, gs in GRADE.items() for g in gs}


def lvl(s: pd.Series) -> pd.Series:
    return s.fillna("").str.strip().str.lower().map(_LVL)


# ─────────────────────────────────────────────────────────────────────────
# 수집
# ─────────────────────────────────────────────────────────────────────────

def cmd_fetch(_args):
    import yfinance as yf
    try:
        import yf_compat
        yf_compat.install()
    except Exception:
        pass
    RAW.mkdir(parents=True, exist_ok=True)
    sp = ies.load_sp500()
    done = 0
    for t in sp["ticker"]:
        p = RAW / f"{t}.parquet"
        if p.exists():
            continue
        d = None
        for k in range(4):
            try:
                d = yf.Ticker(t).upgrades_downgrades
                break
            except Exception as e:                       # 429 등 — 잠시 쉬고 재시도
                time.sleep(15 * (k + 1))
                last = e
        else:
            print(f"  {t}: 실패 {last}", flush=True)
            continue
        if d is None or not len(d):
            d = pd.DataFrame(columns=["Firm", "ToGrade", "FromGrade", "Action", "priceTargetAction",
                                      "currentPriceTarget", "priorPriceTarget"],
                             index=pd.DatetimeIndex([], name="GradeDate"))
        d = d[d.index < ies.SEAL]                           # 봉인: 2025 이후 행은 저장하지 않는다
        d.reset_index().to_parquet(p)
        done += 1
        if done % 50 == 0:
            print(f"  {done}종목", flush=True)
        time.sleep(0.4)
    print(f"수집 완료: 신규 {done} · 캐시 {len(list(RAW.glob('*.parquet')))}", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# 이벤트 표
# ─────────────────────────────────────────────────────────────────────────

def _consensus_before(g: pd.DataFrame) -> np.ndarray:
    """종목 하나의 전체 등급 이력(시간순)에서, 각 행 직전 '다른 증권사' 최근 12개월 등급 평균(MIN_CONS 미만이면 NaN)."""
    book: dict = {}
    out = np.full(len(g), np.nan)
    ts = g["ts"].values
    firms = g["firm_key"].values
    lv = g["to_lvl"].values
    win = np.timedelta64(CONS_DAYS, "D")
    for i in range(len(g)):
        vals = [v for f, (v, t) in book.items() if f != firms[i] and t > ts[i] - win]
        if len(vals) >= MIN_CONS:
            out[i] = float(np.mean(vals))
        if np.isfinite(lv[i]):
            book[firms[i]] = (lv[i], ts[i])
    return out


def cmd_build(_args):
    sp = ies.load_sp500()
    sec = dict(zip(sp["ticker"], sp["sector"]))
    parts = []
    for p in sorted(RAW.glob("*.parquet")):
        d = pd.read_parquet(p)
        if len(d):
            d["ticker"] = p.stem
            parts.append(d)
    a = pd.concat(parts, ignore_index=True)
    a["ts"] = pd.to_datetime(a["GradeDate"])
    assert a["ts"].max() < ies.SEAL, "봉인 구간 행"
    et = a["ts"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    a["date"] = et.dt.tz_localize(None).dt.normalize()
    mins = et.dt.hour * 60 + et.dt.minute
    a["session"] = np.select([mins < 570, mins < 960], ["장전", "장중"], "장후")
    a["firm"] = a["Firm"].fillna("").str.strip()
    a["firm_key"] = a["firm"].str.lower()
    a["action"] = a["Action"].fillna("").str.strip().str.lower()
    a["to_lvl"] = lvl(a["ToGrade"])
    a["from_lvl"] = lvl(a["FromGrade"])
    a["pt_action"] = a["priceTargetAction"].fillna("").str.strip().str.lower()
    a["pt_cur"] = pd.to_numeric(a["currentPriceTarget"], errors="coerce")
    a["pt_prior"] = pd.to_numeric(a["priorPriceTarget"], errors="coerce")
    a = a.drop_duplicates(["ticker", "firm_key", "date", "action", "ToGrade"]).sort_values(["ticker", "ts"])

    unm = pd.concat([a["ToGrade"], a["FromGrade"]]).fillna("").str.strip()
    unm = unm[(unm != "") & unm.str.lower().map(_LVL).isna()].value_counts()
    print(f"미매핑 등급 문구 {unm.sum():,}건 / 상위: {unm.head(15).to_dict()}", flush=True)

    a["cons"] = np.nan
    for _, g in a.groupby("ticker", sort=False):
        a.loc[g.index, "cons"] = _consensus_before(g)
    a["big_ib"] = a["firm_key"].apply(lambda f: any(k in f for k in BIG_IB))
    a["sector"] = a["ticker"].map(sec)
    DERIVED.mkdir(parents=True, exist_ok=True)
    a.drop(columns=["GradeDate"]).to_parquet(DERIVED / "ratings_all.parquet")   # 컨센서스 재구성용 전체 이력

    # 이벤트: 등급 상·하향, 신규 커버리지, 등급 유지 중 목표가 상·하향
    kind = np.select(
        [a["action"] == "up", a["action"] == "down", a["action"] == "init",
         (a["action"].isin(["main", "reit"])) & (a["pt_action"] == "raises"),
         (a["action"].isin(["main", "reit"])) & (a["pt_action"] == "lowers")],
        ["상향", "하향", "신규", "목표가↑", "목표가↓"], "")
    ev = a[kind != ""].copy()
    ev["kind"] = kind[kind != ""]
    ev["notch"] = (ev["to_lvl"] - ev["from_lvl"]).abs()
    ev["pt_chg"] = np.where(ev["pt_prior"] > 0, ev["pt_cur"] / ev["pt_prior"] - 1, np.nan)
    ev["away"] = np.select(
        [ev["cons"].isna() | ev["to_lvl"].isna(),
         (ev["kind"] == "상향") & (ev["to_lvl"] > ev["cons"]),
         (ev["kind"] == "하향") & (ev["to_lvl"] < ev["cons"])], ["판정불가", "이탈", "이탈"], "수렴")
    # 클러스터: 같은 종목·같은 방향, 발표일 포함 직전 30일 내 서로 다른 증권사 수
    c = ev[["ticker", "kind", "date", "firm_key"]].rename(columns={
        "ticker": "issuer_cik", "kind": "side", "date": "filing_date", "firm_key": "owner_key"})
    ev["cluster"] = ies._cluster_counts(c)
    ev = ev[ev["date"].between(ies.STUDY_START, ies.STUDY_END)].reset_index(drop=True)
    ev.to_parquet(DERIVED / "events.parquet")
    print("이벤트 저장: " + ", ".join(f"{k} {v:,}" for k, v in ev["kind"].value_counts().items())
          + f" · 종목 {ev['ticker'].nunique()} · 연도별 상·하향 "
          + str(ev[ev['kind'].isin(['상향', '하향'])].groupby(ev['date'].dt.year).size().to_dict()), flush=True)


# ─────────────────────────────────────────────────────────────────────────
# 분석
# ─────────────────────────────────────────────────────────────────────────

def reactions(ev: pd.DataFrame, O: pd.DataFrame, C: pd.DataFrame) -> pd.DataFrame:
    """발표일 k(동부, 거래일 아니면 다음 거래일) 전후 구간별 SPY 대비 수익(비용 전)."""
    days = C.index
    col = {t: k for k, t in enumerate(C.columns)}
    Ov, Cv = O.values.astype(float), C.values.astype(float)
    js = col["SPY"]
    k = days.searchsorted(ev["date"].values, side="left")
    j = ev["ticker"].map(col).fillna(-1).astype(int).values
    ok = (j >= 0) & (k >= 2) & (k + 1 < len(days))
    out = {}
    segs = {"d-1": (lambda kk, jj: Cv[kk - 1, jj] / Cv[kk - 2, jj]),
            "갭0": (lambda kk, jj: Ov[kk, jj] / Cv[kk - 1, jj]),
            "장중0": (lambda kk, jj: Cv[kk, jj] / Ov[kk, jj]),
            "갭+1": (lambda kk, jj: Ov[kk + 1, jj] / Cv[kk, jj])}
    for name, f in segs.items():
        v = np.full(len(ev), np.nan)
        v[ok] = f(k[ok], j[ok]) - f(k[ok], np.full(ok.sum(), js))
        out[name] = v
    return pd.DataFrame(out, index=ev.index)


def _rx_row(name: str, d: pd.DataFrame) -> str:
    cells = [name, f"{len(d):,}"]
    for s in ("d-1", "갭0", "장중0", "갭+1"):
        st = ies.stat(d.rename(columns={s: "v"}), "v")
        cells.append(ies._mt(st))
    return "| " + " | ".join(cells) + " |"


def consensus_test(O: pd.DataFrame, C: pd.DataFrame) -> list[str]:
    """라이브 providers.analyst 근사: 월말마다 증권사별 최근 12개월 마지막 등급 → 인원 집계 →
    stance=(강매수 + 0.5매수 − 0.5매도 − 강매도)/전체. 수준(라이브)과 3개월 변화(후보)의 횡단면 예측력."""
    a = pd.read_parquet(DERIVED / "ratings_all.parquet")
    a = a[a["to_lvl"].notna()].sort_values("ts")
    w = {5: 1.0, 4: 0.5, 3: 0.0, 2: -0.5, 1: -1.0}
    months = pd.period_range("2017-09", "2024-12", freq="M")
    recs = []
    for t, g in a.groupby("ticker"):
        ts, fk, lv = g["ts"].values, g["firm_key"].values, g["to_lvl"].values
        for m in months:
            me = np.datetime64(m.end_time.normalize() + pd.Timedelta(days=1))
            sel = (ts < me) & (ts >= me - np.timedelta64(CONS_DAYS, "D"))
            if not sel.any():
                continue
            last = pd.DataFrame({"f": fk[sel], "l": lv[sel]}).groupby("f")["l"].last()
            recs.append((t, str(m), float(last.map(w).mean()), len(last)))
    lvl_df = pd.DataFrame(recs, columns=["ticker", "ym", "stance", "n_firms"])
    wide = lvl_df.pivot(index="ym", columns="ticker", values="stance")
    chg = wide - wide.shift(3)
    days = C.index
    col = {t: k for k, t in enumerate(C.columns)}
    Ov, Cv = O.values.astype(float), C.values.astype(float)
    js = col["SPY"]
    rows = []
    for ym in wide.index:
        if pd.Period(ym, "M") < pd.Period("2018-01", "M"):
            continue
        i1 = days.searchsorted(pd.Period(ym, "M").end_time.normalize(), side="right")
        for h in (20, 60):
            if i1 + h - 1 >= len(days):
                continue
            spy = Cv[i1 + h - 1, js] / Ov[i1, js] - 1
            for t in wide.columns:
                if t not in col:
                    continue
                r = Cv[i1 + h - 1, col[t]] / Ov[i1, col[t]] - 1
                if np.isfinite(r):
                    rows.append((ym, t, h, wide.at[ym, t], chg.at[ym, t], r - spy - ies.COST))
    d = pd.DataFrame(rows, columns=["ym", "ticker", "h", "stance", "chg", "net"])
    out = [f"- 종목·월 {len(d[d['h'] == 20]):,}개 중 등급 자료 있음 {d[d['h'] == 20]['stance'].notna().mean() * 100:.0f}% · "
           f"stance 중앙값 {d['stance'].median():+.2f} (0=전원 보유, +0.5=전원 매수) · "
           f"stance>0 비율 {(d['stance'] > 0).mean() * 100:.0f}%", ""]
    out += ["| 신호 | 보유 | 5분위(낮음) | 2 | 3 | 4 | 5분위(높음) | 5−1 차이 | 월별 IC (t) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for sig, lab in (("stance", "수준(라이브 방식)"), ("chg", "3개월 변화")):
        for h in (20, 60):
            x = d[(d["h"] == h) & d[sig].notna()].copy()
            x["q"] = x.groupby("ym")[sig].transform(
                lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 25 else np.nan)
            x = x.rename(columns={"net": f"n{h}"})
            qs = [ies.stat(x[x["q"] == q], f"n{h}") for q in range(5)]
            spread = x[x["q"] == 4].groupby("ym")[f"n{h}"].mean() - x[x["q"] == 0].groupby("ym")[f"n{h}"].mean()
            spread = spread.dropna()
            st = spread.mean() / (spread.std(ddof=1) / np.sqrt(len(spread)))
            ic = x.groupby("ym").apply(lambda g: g[sig].rank().corr(g[f"n{h}"].rank()) if len(g) >= 25 else np.nan,
                                       include_groups=False).dropna()
            ict = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic)))
            out.append(f"| {lab} | {h}일 | " + " | ".join(ies._pct(q.get("mean")) for q in qs)
                       + f" | {ies._pct(spread.mean())} (t {st:+.2f}) | {ic.mean():+.4f} ({ict:+.2f}) |")
    out.append("")
    out.append("- 60일 보유는 월말마다 겹치므로 t 가 과대일 수 있다. 5−1 차이 t 는 월별 스프레드 시계열 기준.")
    return out


def cmd_analyze(args):
    O, C = ies.load_prices()
    ev = pd.read_parquet(DERIVED / "events.parquet")
    ev["filing_date"] = ev["date"]                       # ies 함수는 'filing_date 다음 거래일 시가' 진입
    ev = ies.attach_returns(ev, O, C)
    ev = ev[ev["entry"].notna()].reset_index(drop=True)
    rx = reactions(ev, O, C)
    ev = pd.concat([ev, rx], axis=1)
    ev.to_parquet(DERIVED / "events_returns.parquet")
    base = ies.baselines(O, C)

    U, D = ev[ev["kind"] == "상향"], ev[ev["kind"] == "하향"]
    L: list[str] = []
    w = L.append
    w("## 데이터")
    w(f"- 이벤트(종목×증권사×발표일): " + ", ".join(f"{k} {v:,}" for k, v in ev['kind'].value_counts().items())
      + f" · 종목 {ev['ticker'].nunique()} · {ev['date'].min().date()} ~ {ev['date'].max().date()}")
    ud = ev[ev["kind"].isin(["상향", "하향"])]
    w("- 상·하향 연도별: " + ", ".join(f"{y} {n:,}" for y, n in ud.groupby(ud['date'].dt.year).size().items()))
    w("- 상·하향 발표 시각(동부): " + ", ".join(f"{k} {v / len(ud) * 100:.0f}%" for k, v in ud['session'].value_counts().items()))
    w(f"- 대형 IB 비중(상·하향): {ud['big_ib'].mean() * 100:.0f}% · 합의 판정 가능 {(ud['away'] != '판정불가').mean() * 100:.0f}%")
    w(f"- 사전 20일 SPY 대비: 상향 전 {ies._pct(U['pre20'].mean())} · 하향 전 {ies._pct(D['pre20'].mean())}")
    w("- 무작위 진입 기준선(비용 전 SPY 대비): " + ", ".join(f"{h}일 {ies._pct(base[h])}" for h in ies.HORIZONS))
    w("")

    w("## 발표일 전후 반응 (SPY 대비, 비용 전, 이벤트 단위) — 시각 가정 검증 + '진입 전에 이미 반영된 몫'")
    w("| 구분 | n | d−1 종가→종가 | 발표일 시가 갭 | 발표일 장중 | 다음날 시가 갭 |")
    w("|---|---:|---:|---:|---:|---:|")
    for kk in ("상향", "하향"):
        for ss in ("장전", "장중", "장후"):
            w(_rx_row(f"{kk} · {ss} 발표", ev[(ev["kind"] == kk) & (ev["session"] == ss)]))
    w("- 보수적 진입(다음 거래일 시가)은 '발표일 시가 갭·장중'과 (장후 발표의) '다음날 시가 갭'을 놓친다.")
    w("")

    cfg_u = {
        "U1 상향 전체": U.index == U.index,
        "U2 상향 × 대형 IB": U["big_ib"],
        "U3 매수 등급으로(→4·5)": U["to_lvl"] >= 4,
        "U4 매도에서 보유로(→3)": U["to_lvl"] == 3,
        "U5 2단계+ 상향": U["notch"] >= 2,
        "U6 상향 클러스터 2곳+": U["cluster"] >= 2,
        "U7 합의 이탈 상향": U["away"] == "이탈",
        "U8 합의 수렴 상향": U["away"] == "수렴",
        "U9 대형 IB × 합의 이탈": U["big_ib"] & (U["away"] == "이탈"),
    }
    cfg_d = {
        "D1 하향 전체": D.index == D.index,
        "D2 하향 × 대형 IB": D["big_ib"],
        "D3 매도 등급으로(→1·2)": D["to_lvl"] <= 2,
        "D4 매수에서 보유로(→3)": D["to_lvl"] == 3,
        "D5 2단계+ 하향": D["notch"] >= 2,
        "D6 하향 클러스터 2곳+": D["cluster"] >= 2,
        "D7 합의 이탈 하향": D["away"] == "이탈",
        "D8 합의 수렴 하향": D["away"] == "수렴",
        "D9 대형 IB × 합의 이탈": D["big_ib"] & (D["away"] == "이탈"),
    }
    X = ev[ev["kind"].isin(["신규", "목표가↑", "목표가↓"])]
    cfg_x = {
        "X1 신규 커버리지 매수(4·5)": (X["kind"] == "신규") & (X["to_lvl"] >= 4),
        "X2 신규 커버리지 보유·매도(≤3)": (X["kind"] == "신규") & (X["to_lvl"] <= 3),
        "X3 목표가 상향(등급 유지)": X["kind"] == "목표가↑",
        "X4 목표가 하향(등급 유지)": X["kind"] == "목표가↓",
        "X5 목표가 +10% 이상 상향": (X["kind"] == "목표가↑") & (X["pt_chg"] >= 0.10),
        "X6 목표가 −10% 이상 하향": (X["kind"] == "목표가↓") & (X["pt_chg"] <= -0.10),
    }
    sets = (("상향(매수 신호 — 양수여야 정보)", U, cfg_u), ("하향(회피 신호 — 음수여야 정보)", D, cfg_d),
            ("신규 커버리지·목표가 변경(보조)", X, cfg_x))
    for h in ies.HORIZONS:
        for title, S, cfg in sets:
            w(f"## {title} — {h}일 보유 (보수적 진입: 발표 다음 거래일 시가)")
            w(ies.HEAD)
            for k, m in cfg.items():
                w(ies.row(k, S[m], h))
            w("")

    # 빠른 진입: 장전 발표는 당일 시가
    fast = ev.copy()
    pre = fast["session"] == "장전"
    fast["filing_date"] = np.where(pre, fast["date"] - pd.Timedelta(days=1), fast["date"])
    fast = ies.attach_returns(fast, O, C)
    fast = fast[fast["entry"].notna()]
    FU, FD = fast[fast["kind"] == "상향"], fast[fast["kind"] == "하향"]
    w("## 빠른 진입 — 장전 발표는 당일 시가 진입 (나머지는 보수적 진입과 같음)")
    w(ies.HEAD)
    for h in ies.HORIZONS:
        w(ies.row(f"상향 전체 · {h}일", FU, h))
        w(ies.row(f"상향 × 대형 IB · {h}일", FU[FU["big_ib"]], h))
        w(ies.row(f"하향 전체 · {h}일", FD, h))
        w(ies.row(f"하향 × 대형 IB · {h}일", FD[FD["big_ib"]], h))
    w("")

    focus = [("U1 상향 전체", U, cfg_u), ("U2 상향 × 대형 IB", U, cfg_u), ("U7 합의 이탈 상향", U, cfg_u),
             ("D1 하향 전체", D, cfg_d), ("D2 하향 × 대형 IB", D, cfg_d), ("D7 합의 이탈 하향", D, cfg_d),
             ("X3 목표가 상향(등급 유지)", X, cfg_x),
             # 아래 둘은 본 표를 본 뒤 추가한 강건성 점검 대상(사전 지정 조합이지만 '주목'은 사후 선택)
             ("X1 신규 커버리지 매수(4·5)", X, cfg_x), ("D4 매수에서 보유로(→3)", D, cfg_d)]
    years = list(range(2018, 2025))
    for h in (20, 60):
        w(f"## 연도별 안정성 — {h}일 보유 (종목·월 단위 순초과: SPY 대비 / 사전수익 매칭 대비 (n))")
        w("| 조합 | " + " | ".join(str(y) for y in years) + " | 기대 부호 연도 SPY · 매칭 |")
        w("|---|" + "---:|" * (len(years) + 1))
        for k, S, cfg in focus:
            sub = ies.fm(S[cfg[k]])
            sign = -1 if k.startswith("D") else 1
            cells, pos, posm = [], 0, 0
            for y in years:
                sy = sub[sub["date"].dt.year == y]
                s, sm = ies.stat(sy, f"n{h}"), ies.stat(sy, f"mb{h}")
                if s["n"] < 3:
                    cells.append("—")
                    continue
                pos += sign * s["mean"] > 0
                posm += sign * sm.get("mean", np.nan) > 0
                cells.append(f"{ies._pct(s['mean'], 1)} / {ies._pct(sm.get('mean'), 1)} ({s['n']})")
            w(f"| {k} | " + " | ".join(cells) + f" | {pos}/{len(years)} · {posm}/{len(years)} |")
        w("")

    w("## 표본 반분 — 2018–2021 vs 2022–2024, 종목·월 단위")
    w("| 조합 | 보유 | 2018–21 SPY 대비 (t) | 매칭 대비 (t) | n | 2022–24 SPY 대비 (t) | 매칭 대비 (t) | n |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|")
    for k, S, cfg in focus:
        early = S["date"].dt.year <= 2021
        for h in ies.HORIZONS:
            da, db = ies.fm(S[cfg[k] & early]), ies.fm(S[cfg[k] & ~early])
            a_, b_ = ies.stat(da, f"n{h}"), ies.stat(db, f"n{h}")
            if a_["n"] < 3 or b_["n"] < 3:
                continue
            w(f"| {k} | {h}일 | {ies._mt(a_)} | {ies._mt(ies.stat(da, f'mb{h}'))} | {a_['n']:,} | {ies._mt(b_)} | "
              f"{ies._mt(ies.stat(db, f'mb{h}'))} | {b_['n']:,} |")
    w("")

    w("## 달력시간 포트폴리오 (신호 후 h일 보유·동일가중·진입마다 왕복 0.15%) — 하향은 '보유했다면'의 성과(음수 = 회피 가치)")
    w("| 조합 | 보유 | 연환산 초과(SPY) | t | 샤프(초과) | 연환산 초과(동일가중) | t | 보유일 비중 | 평균 종목수 | 진입 수 |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, S, cfg in focus:
        for h in ies.HORIZONS:
            c = ies.ctp(S[cfg[k]], h, O, C)
            w(f"| {k} | {h}일 | {ies._pct(c['spy_ann'], 1)} | {ies._t(c['spy_t'])} | {c['spy_sharpe']:+.2f} | "
              f"{ies._pct(c['ew_ann'], 1)} | {ies._t(c['ew_t'])} | {c['days_in'] * 100:.0f}% | "
              f"{c['avg_pos']:.1f} | {c['entries']:,} |")
    w("")

    w("## 라이브 신호 근사 — Finnhub 추천 집계 방식(providers.analyst)")
    L.extend(consensus_test(O, C))
    w("")

    out = Path(args.out) if args.out else DERIVED / "analyst_tables.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"표 저장: {out}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("build")
    a = sub.add_parser("analyze")
    a.add_argument("--out", default="")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "build": cmd_build, "analyze": cmd_analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
