#!/usr/bin/env python3
"""시장 군중 모멘텀 측정 — roster crowd 공신력(0.32, 손으로 정한 값)을 실측으로 대체하기 위한 연구.

insider·analyst·news 스터디와 같은 방법·기준을 재사용한다. 라이브 코드는 읽기만 했다(providers.crowd 의 식).

라이브 신호(providers.crowd)
  최근 일봉 종가 c 로 r5=c[-1]/c[-6]−1, r20=c[-1]/c[-21]−1, mom=0.6·r5+0.4·r20,
  점수=clip(50+mom·250, 0, 100), stance=(점수−50)/50 = clip(5·mom, −1, 1).
  가격은 Nasdaq 차트 API(분할 반영 확인: NVDA 2024-06-10 10:1 분할 전후 연속) → Yahoo 폴백.
  여기서는 수정주가(yfinance) 종가로 같은 식을 매주 마지막 거래일에 계산한다.

측정
  - 주별 횡단면 5분위·순위상관(IC, Newey-West t): 라이브 stance, 구성요소 r5·r20 각각, 참고로 12−1개월 모멘텀
    (Jegadeesh·Titman 1993 — 학계 표준 모멘텀, 라이브 아님).
  - 이벤트 표: 강세·약세·상·하위 5분위를 이벤트로 보고 4개 기준선·종목·월 단위·2원 군집 t·연도별·반분·
    달력시간 포트폴리오. 단 '사전수익 매칭' 기준선은 직전 20일 수익으로 짝을 짓기 때문에 가격 신호 자체를
    지워 버린다 — crowd 에서는 참고만 하고, 판정은 IC·SPY·자기종목 평균 대비로 한다.

사용
  python3 -P research/crowd_study.py analyze --out <표.md>      # 가격 캐시(insider_event_study fetch-prices)만 필요
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insider_event_study as ies                           # noqa: E402
from news_event_study import _nw_t, xsection                # noqa: E402

DERIVED = ies.CACHE.parent / "crowd" / "derived"


def weekly_panel(C: pd.DataFrame) -> pd.DataFrame:
    days = C.index
    stocks = [t for t in C.columns if t != "SPY" and t not in ies.SECTOR_ETF.values()]
    Cv = C[stocks].values.astype(float)
    d = pd.Series(days[(days >= ies.STUDY_START) & (days <= ies.STUDY_END)])
    fridays = d.groupby(d.dt.to_period("W")).max()
    parts = []
    for F in fridays:
        i = days.get_loc(F)
        c = Cv[i]
        r5 = c / Cv[i - 5] - 1
        r20 = c / Cv[i - 20] - 1
        m121 = Cv[i - 21] / Cv[i - 252] - 1 if i >= 252 else np.full(len(stocks), np.nan)
        mom = 0.6 * r5 + 0.4 * r20
        score = np.clip(50 + mom * 250, 0, 100)
        # p5·p20 = 과거 수익(신호). r5·r20 은 ies.attach_returns 가 '미래' 수익 열 이름으로 쓰므로 피한다.
        parts.append(pd.DataFrame({"ticker": stocks, "filing_date": F, "p5": r5, "p20": r20, "m121": m121,
                                   "stance": (score - 50) / 50}))
    w = pd.concat(parts, ignore_index=True)
    return w[w["stance"].notna()].reset_index(drop=True)


def cmd_analyze(args):
    O, C = ies.load_prices()
    w = weekly_panel(C)
    sec = dict(zip(*ies.load_sp500()[["ticker", "sector"]].values.T))
    w["sector"] = w["ticker"].map(sec)
    w = ies.attach_returns(w, O, C)
    w = w[w["entry"].notna()].reset_index(drop=True)
    DERIVED.mkdir(parents=True, exist_ok=True)
    w.to_parquet(DERIVED / "weekly_returns.parquet")
    base = ies.baselines(O, C)
    L: list[str] = []
    wl = L.append

    wl("## 데이터")
    tone = np.select([w["stance"] > 0.08, w["stance"] < -0.08], ["강세", "약세"], "중립")
    wl(f"- 종목·주 {len(w):,}개 · 종목 {w['ticker'].nunique()} · 주 {w['filing_date'].nunique()} "
       f"({w['filing_date'].min().date()} ~ {w['filing_date'].max().date()})")
    wl("- 라이브 stance 분포(±0.08 기준): " + ", ".join(f"{k} {v / len(w) * 100:.0f}%" for k, v in pd.Series(tone).value_counts().items())
       + f" · 중앙값 {w['stance'].median():+.2f} · ±1(포화) {(w['stance'].abs() >= 0.999).mean() * 100:.0f}%")
    wl(f"- stance 와 같은 주 시장(SPY) 20일 수익의 상관 — 절대 모멘텀이라 시장 방향을 따라간다: "
       f"{w.groupby('filing_date')['stance'].mean().corr(w.groupby('filing_date')['p20'].median()):+.2f} (주별 평균 stance vs 종목 중앙 p20)")
    wl("- 무작위 진입 기준선(비용 전 SPY 대비): " + ", ".join(f"{h}일 {ies._pct(base[h])}" for h in ies.HORIZONS))
    wl("")

    wl("## 주별 횡단면 5분위 (종목·주 단위 평균 순초과 SPY 대비, 비용 차감)")
    wl("| 신호 | 보유 | 1분위(약세) | 2 | 3 | 4 | 5분위(강세) | 5−1 차이 (NW t) | 주별 IC (NW t) |")
    wl("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    L.extend(xsection(w, "stance", "라이브 stance(0.6·r5+0.4·r20)"))
    L.extend(xsection(w, "p5", "r5 성분 (직전 1주)"))
    L.extend(xsection(w, "p20", "r20 성분 (직전 1개월)"))
    L.extend(xsection(w[w["m121"].notna()], "m121", "참고: 12−1개월 모멘텀"))
    wl("- 5분위는 그 주 종목 50개 이상. 20·60일은 주마다 겹쳐 Newey-West(시차 h/5−1). 12−1개월은 가격 이력 때문에 2018-09부터.")
    wl("")

    def quint(col):
        x = w[w[col].notna()].copy()
        x["q"] = x.groupby("filing_date")[col].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 50 else np.nan)
        return x

    qs, q5, qm = quint("stance"), quint("p5"), quint("m121")
    cfg_up = {
        "C1 강세(stance>+0.25)": w[w["stance"] > 0.25],
        "C2 stance 상위 5분위": qs[qs["q"] == 4],
        "C5 1주 승자(r5 상위 5분위)": q5[q5["q"] == 4],
        "M1 참고: 12−1개월 모멘텀 상위 5분위": qm[qm["q"] == 4],
    }
    cfg_dn = {
        "C3 약세(stance<−0.25)": w[w["stance"] < -0.25],
        "C4 stance 하위 5분위": qs[qs["q"] == 0],
        "C6 1주 패자(r5 하위 5분위)": q5[q5["q"] == 0],
        "M2 참고: 12−1개월 모멘텀 하위 5분위": qm[qm["q"] == 0],
    }
    for h in ies.HORIZONS:
        wl(f"## 강세 쪽(라이브가 매수 쪽으로 미는 종목 — 양수여야 정보) — {h}일 보유 (금요일 종가 신호 → 다음 거래일 시가)")
        wl(ies.HEAD)
        for k, df in cfg_up.items():
            wl(ies.row(k, df, h))
        wl("")
        wl(f"## 약세 쪽(라이브가 매도 쪽으로 미는 종목 — 음수여야 정보) — {h}일 보유")
        wl(ies.HEAD)
        for k, df in cfg_dn.items():
            wl(ies.row(k, df, h))
        wl("- '사전수익 매칭 대비'는 직전 20일 수익으로 짝을 지어 가격 신호를 지우므로 crowd 에선 참고용.")
        wl("")

    focus = {**{k: cfg_up[k] for k in ("C2 stance 상위 5분위", "C5 1주 승자(r5 상위 5분위)", "M1 참고: 12−1개월 모멘텀 상위 5분위")},
             **{k: cfg_dn[k] for k in ("C4 stance 하위 5분위", "C6 1주 패자(r5 하위 5분위)")}}
    years = list(range(2018, 2025))
    for h in (5, 20):
        wl(f"## 연도별 안정성 — {h}일 보유 (종목·월 단위 순초과: SPY 대비 / 자기종목 평균 대비 (n))")
        wl("| 조합 | " + " | ".join(str(y) for y in years) + " | 기대 부호 연도 SPY · 자기종목 |")
        wl("|---|" + "---:|" * (len(years) + 1))
        for k, df in focus.items():
            sub = ies.fm(df)
            sign = -1 if k[:2] in ("C4", "C6") else 1
            cells, pos, posm = [], 0, 0
            for y in years:
                sy = sub[sub["filing_date"].dt.year == y]
                s, sm = ies.stat(sy, f"n{h}"), ies.stat(sy, f"ab{h}")
                if s["n"] < 3:
                    cells.append("—")
                    continue
                pos += sign * s["mean"] > 0
                posm += sign * sm.get("mean", np.nan) > 0
                cells.append(f"{ies._pct(s['mean'], 1)} / {ies._pct(sm.get('mean'), 1)} ({s['n']})")
            wl(f"| {k} | " + " | ".join(cells) + f" | {pos}/{len(years)} · {posm}/{len(years)} |")
        wl("")

    wl("## 표본 반분 — 2018–2021 vs 2022–2024, 주별 IC (NW t)")
    wl("| 신호 | 보유 | 2018–21 IC (t) | 2022–24 IC (t) |")
    wl("|---|---:|---:|---:|")
    for sig, lab in (("stance", "라이브 stance"), ("p5", "r5 성분"), ("p20", "r20 성분"), ("m121", "참고: 12−1개월")):
        for h in (5, 20):
            cells = []
            for lo, hi in ((2018, 2021), (2022, 2024)):
                x = w[w[sig].notna() & w[f"n{h}"].notna() & w["filing_date"].dt.year.between(lo, hi)]
                ic = x.groupby("filing_date").apply(
                    lambda g: g[sig].rank().corr(g[f"n{h}"].rank()) if len(g) >= 50 else np.nan,
                    include_groups=False)
                cells.append(f"{ic.mean():+.4f} ({_nw_t(ic, max(0, h // 5 - 1)):+.2f})")
            wl(f"| {lab} | {h}일 | " + " | ".join(cells) + " |")
    wl("")

    wl("## 시장 수준 효과 — 주별 평균 stance(절대 모멘텀이라 시장 방향을 따라감)가 SPY 를 예고하나")
    wl("| 보유 | 주 수 | 평균 stance 상위 1/3 주의 SPY 수익 | 하위 1/3 주의 SPY 수익 | 차이 | 시계열 상관 (NW t) |")
    wl("|---:|---:|---:|---:|---:|---:|")
    days = C.index
    so, sc = O["SPY"].values.astype(float), C["SPY"].values.astype(float)
    ms = w.groupby("filing_date")["stance"].mean()
    i1 = days.searchsorted(ms.index.values, side="right")
    for h in ies.HORIZONS:
        ok = i1 + h - 1 < len(days)
        fwd = pd.Series(np.where(ok, sc[np.minimum(i1 + h - 1, len(days) - 1)] / so[np.minimum(i1, len(days) - 1)] - 1,
                                 np.nan), index=ms.index).dropna()
        m = ms.loc[fwd.index]
        hi, lo = fwd[m >= m.quantile(2 / 3)], fwd[m <= m.quantile(1 / 3)]
        z = (m - m.mean()) * (fwd - fwd.mean())
        corr = m.corr(fwd)
        tt = _nw_t(z / (m.std() * fwd.std()), max(0, h // 5 - 1))
        wl(f"| {h}일 | {len(fwd)} | {ies._pct(hi.mean())} | {ies._pct(lo.mean())} | {ies._pct(hi.mean() - lo.mean())} | "
           f"{corr:+.3f} ({tt:+.2f}) |")
    wl("- 라이브 판단에서 crowd 의 '전 종목 공통' 부분은 시장이 오른 뒤 매수 쪽, 내린 뒤 매도 쪽으로 점수를 민다. "
       "이게 SPY 의 다음 움직임을 맞히는지 본다(양수여야 정보).")
    wl("")

    wl("## 달력시간 포트폴리오 (매주 신호 종목을 h일 보유·동일가중·진입마다 왕복 0.15%)")
    wl("| 조합 | 보유 | 연환산 초과(SPY) | t | 샤프(초과) | 연환산 초과(동일가중) | t | 보유일 비중 | 평균 종목수 | 진입 수 |")
    wl("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, df in focus.items():
        for h in ies.HORIZONS:
            c = ies.ctp(df, h, O, C)
            wl(f"| {k} | {h}일 | {ies._pct(c['spy_ann'], 1)} | {ies._t(c['spy_t'])} | {c['spy_sharpe']:+.2f} | "
               f"{ies._pct(c['ew_ann'], 1)} | {ies._t(c['ew_t'])} | {c['days_in'] * 100:.0f}% | "
               f"{c['avg_pos']:.1f} | {c['entries']:,} |")
    wl("")

    out = Path(args.out) if args.out else DERIVED / "crowd_tables.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"표 저장: {out}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("--out", default="")
    args = ap.parse_args()
    cmd_analyze(args)


if __name__ == "__main__":
    main()
