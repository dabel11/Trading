#!/usr/bin/env python3
"""뉴스 논조 이벤트 스터디 — roster news 공신력(0.55, 손으로 정한 값)을 실측으로 대체하기 위한 연구.

insider_event_study.py·analyst_event_study.py 와 같은 방법·기준을 재사용한다. 라이브 코드는 읽기만 한다:
signals.sentiment 의 어휘 사전(_POS·_NEG)과 채점 함수(_lexicon_score) — 라이브 news 소스가 쓰는 바로 그것.

데이터
  - FNSPID(Dong 외 2024, HuggingFace Zihan1004/FNSPID) Stock_news 의 두 파일: All_external.csv(5.7GB,
    Benzinga 헤드라인, 2020-06 까지)와 nasdaq_exteral_data.csv(23GB, Nasdaq.com 기사·LSA 요약, 2023 까지).
    키·계정 불필요. 2024 는 없다. Alpaca 뉴스는 앱에 실제 키가 없어 쓰지 못했다(2026-10-20 확인).
  - 원 CSV 는 파싱만 한다(필요한 열만 청크로 읽고 S&P 500·기간 내 행만 남김). 봉인(2025+) 가드는 둔다.
  - 발표 시각은 일부가 어긋나 보여(예: 금요일 장중 기사가 금요일 02:30 ET) 날짜만 쓴다. 정합성은
    '하루 강한 논조 기사'의 전후 반응 표로 검증한다.

측정
  - 라이브 재현(providers.news): 매주 금요일, 종목별 최근 7일(금요일 포함) 기사 중 최신 40건의 헤드라인을
    _lexicon_score 로 채점 → stance=(점수−50)/50, 톤 긍정(>0.08)/부정(<−0.08). 다음 거래일 시가 진입
    5·20·60일 순초과. 주별 횡단면 5분위·순위상관(IC, Newey-West t).
  - 이벤트 표(다른 스터디와 같은 형식): 긍정·상위 5분위·부정·하위 5분위·기사량 급증 등을 이벤트로 보고
    4개 기준선·종목·월 단위·2원 군집 t·연도별·반분·달력시간 포트폴리오.
  - 변형: 헤드라인+LSA 요약(라이브의 '헤드라인+요약'에 더 가까움), 여러 종목에 같은 제목이 붙는 시황 기사 제외.

사용 (원 CSV 는 저장하지 않고 스트림으로 걸러도 된다)
  U=https://huggingface.co/datasets/Zihan1004/FNSPID/resolve/main/Stock_news
  curl -sL $U/All_external.csv        | python3 -P research/news_event_study.py ingest - --source external
  curl -sL $U/nasdaq_exteral_data.csv | python3 -P research/news_event_study.py ingest - --source nasdaq
  python3 -P research/news_event_study.py build
  python3 -P research/news_event_study.py analyze --out <표.md>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insider_event_study as ies                           # noqa: E402
import analyst_event_study as aes                           # noqa: E402  (발표 전후 반응 표 재사용)
from signals.sentiment import _NEG, _POS, _lexicon_score    # noqa: E402  (라이브 어휘 채점)

NW = ies.CACHE.parent / "news"
FILTERED = NW / "filtered"      # 원 CSV 에서 S&P 500·기간 내 행만 거른 것
DERIVED = NW / "derived"
DATA_START = pd.Timestamp("2017-12-01")                      # 2018-01 첫 주의 7일 창
DATA_END = pd.Timestamp("2024-01-01")                        # FNSPID 는 2023년까지
STUDY_END = pd.Timestamp("2023-12-31")
WINDOW_DAYS = 7
MAX_ART = 40                                                 # providers.news: arr[:40]
ROUNDUP_TICKERS = 5                                          # 같은 날 같은 제목이 5종목+ 에 붙으면 시황 기사


def _counts(texts: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """_lexicon_score 와 같은 토큰화로 기사별 긍·부정 단어 수."""
    pos = np.zeros(len(texts), int)
    neg = np.zeros(len(texts), int)
    for i, h in enumerate(texts.fillna("").values):
        for w in h.lower().replace(".", " ").replace(",", " ").split():
            if w in _POS:
                pos[i] += 1
            elif w in _NEG:
                neg[i] += 1
    return pos, neg


def _lex(pos: int, neg: int, n: int) -> float:
    """_lexicon_score 를 단어 수 합으로 다시 쓴 것(build 에서 원 함수와 일치 검증)."""
    if pos + neg == 0:
        return np.nan
    bull = pos / (pos + neg)
    buzz = min(1.0, n / 20.0)
    return round(min(100.0, max(0.0, ((bull - 0.5) * buzz + 0.5) * 100)), 1)


def cmd_ingest(args):
    """CSV(파일 또는 표준입력 스트림)를 청크로 읽어 S&P 500·기간 내 행만 남긴다 — 23GB 파일도 저장 없이 거른다."""
    src = sys.stdin.buffer if args.path == "-" else args.path
    universe = set(ies.load_sp500()["ticker"])
    parts, seen = [], 0
    for ch in pd.read_csv(src, usecols=["Date", "Article_title", "Stock_symbol", "Publisher", "Lsa_summary"],
                          dtype=str, chunksize=400_000, on_bad_lines="skip", encoding_errors="replace"):
        seen += len(ch)
        ch["ticker"] = ch["Stock_symbol"].fillna("").str.strip().str.upper().str.replace(".", "-", regex=False)
        ch = ch[ch["ticker"].isin(universe)]
        ts = pd.to_datetime(ch["Date"], errors="coerce", utc=True).dt.tz_localize(None)
        ch = ch.assign(ts=ts)
        ch = ch[(ch["ts"] >= DATA_START) & (ch["ts"] < DATA_END)]
        if len(ch):
            parts.append(ch[["ticker", "ts", "Article_title", "Lsa_summary", "Publisher"]])
        print(f"  {seen:,}행 읽음 · S&P500·기간 내 누적 {sum(len(p) for p in parts):,}", flush=True)
    df = pd.concat(parts, ignore_index=True)
    df["source"] = args.source
    FILTERED.mkdir(parents=True, exist_ok=True)
    df.to_parquet(FILTERED / f"{args.source}.parquet")
    print(f"걸러 저장: {args.source} {len(df):,}행 ({df['ts'].min()} ~ {df['ts'].max()})", flush=True)


def cmd_build(_args):
    files = sorted(FILTERED.glob("*.parquet"))
    if not files:
        raise SystemExit("ingest 먼저")
    parts = [pd.read_parquet(f) for f in files]
    a = pd.concat(parts, ignore_index=True).rename(columns={"Article_title": "title", "Lsa_summary": "summ",
                                                            "Publisher": "publisher"})
    a["publisher"] = a["publisher"].fillna("").replace("", "(미상)")
    assert a["ts"].max() < ies.SEAL
    a["title"] = a["title"].fillna("").str.strip()
    a["summ"] = a["summ"].fillna("").str.strip()
    a["date"] = a["ts"].dt.normalize()
    a = a[a["title"] != ""].drop_duplicates(["ticker", "date", "title"]).sort_values(["ticker", "ts"])
    a["n_tk"] = a.groupby(["date", "title"])["ticker"].transform("nunique")
    a["roundup"] = a["n_tk"] >= ROUNDUP_TICKERS
    a["pos"], a["neg"] = _counts(a["title"])
    a["pos2"], a["neg2"] = _counts(a["title"] + " " + a["summ"])
    a = a.reset_index(drop=True)
    DERIVED.mkdir(parents=True, exist_ok=True)
    a.drop(columns=["summ"]).to_parquet(DERIVED / "articles.parquet")
    # 재구현 검증: 무작위 창 300개에서 원 함수와 같은 점수
    rng = np.random.default_rng(7)
    for i in rng.choice(len(a), 300, replace=False):
        g = a.iloc[max(0, i - 12):i + 1]
        g = g[g["ticker"] == a.at[i, "ticker"]]
        want = _lexicon_score(list(g["title"]))
        got = _lex(int(g["pos"].sum()), int(g["neg"].sum()), len(g))
        assert (want is None and np.isnan(got)) or abs(want - got) < 1e-9, (want, got)
    yrs = a.groupby([a["ts"].dt.year, "source"]).size().to_dict()
    print(f"기사 저장: {len(a):,}건 · 종목 {a['ticker'].nunique()} · 연도별 {yrs} · 시황 기사 비중 "
          f"{a['roundup'].mean() * 100:.0f}% · 요약 있음 {(a['pos2'] + a['neg2'] > a['pos'] + a['neg']).mean() * 100:.0f}% "
          f"· 채점 재구현 300창 일치", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# 주간 신호(라이브 재현)
# ─────────────────────────────────────────────────────────────────────────

def weekly_signals(a: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """매주 마지막 거래일 F 에 종목별 [F−6, F] 날짜 기사 중 최신 40건으로 라이브 채점."""
    d = pd.Series(days[(days >= "2018-01-01") & (days <= STUDY_END)])
    fridays = d.groupby(d.dt.to_period("W")).max().values
    recs = []
    for t, g in a.groupby("ticker", sort=False):
        dt = g["date"].values
        cols = {k: g[k].values for k in ("pos", "neg", "pos2", "neg2", "roundup")}
        for F in fridays:
            hi = np.searchsorted(dt, F, side="right")
            lo = np.searchsorted(dt, F - np.timedelta64(WINDOW_DAYS - 1, "D"), side="left")
            if hi <= lo:
                continue
            s = slice(max(lo, hi - MAX_ART), hi)
            n = hi - max(lo, hi - MAX_ART)
            p, q = int(cols["pos"][s].sum()), int(cols["neg"][s].sum())
            p2, q2 = int(cols["pos2"][s].sum()), int(cols["neg2"][s].sum())
            keep = ~cols["roundup"][lo:hi]
            sub = slice(lo, hi)
            p3 = int(cols["pos"][sub][keep][-MAX_ART:].sum())
            q3 = int(cols["neg"][sub][keep][-MAX_ART:].sum())
            n3 = int(min(keep.sum(), MAX_ART))
            recs.append((t, F, hi - lo, n, _lex(p, q, n), _lex(p2, q2, n), _lex(p3, q3, n3) if n3 else np.nan, n3))
    w = pd.DataFrame(recs, columns=["ticker", "filing_date", "n_week", "n_used", "lex", "lex2", "lex3", "n3"])
    for c, s in (("lex", "stance"), ("lex2", "stance2"), ("lex3", "stance3")):
        w[s] = ((w[c] - 50) / 50).clip(-1, 1)
    # 기사량 급증: 이번 주 기사 수가 직전 12주 평균의 3배 이상이고 5건 이상
    w = w.sort_values(["ticker", "filing_date"])
    prev = (w.groupby("ticker")["n_week"].transform(lambda s: s.shift(1).rolling(12, min_periods=6).mean()))
    w["surge"] = (w["n_week"] >= 5) & (w["n_week"] >= 3 * prev.fillna(np.inf))
    return w.reset_index(drop=True)


def _nw_t(x: pd.Series, lag: int) -> float:
    x = x.dropna().values
    n = len(x)
    if n < 5:
        return np.nan
    e = x - x.mean()
    v = (e @ e) / n
    for k in range(1, lag + 1):
        v += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return x.mean() / np.sqrt(v / n) if v > 0 else np.nan


def xsection(w: pd.DataFrame, sig: str, label: str) -> list[str]:
    out = []
    for h in ies.HORIZONS:
        x = w[w[sig].notna() & w[f"n{h}"].notna()].copy()
        x["q"] = x.groupby("filing_date")[sig].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 50 else np.nan)
        qs = [ies.stat(x[x["q"] == q], f"n{h}") for q in range(5)]
        sp = (x[x["q"] == 4].groupby("filing_date")[f"n{h}"].mean()
              - x[x["q"] == 0].groupby("filing_date")[f"n{h}"].mean())
        ic = x.groupby("filing_date").apply(
            lambda g: g[sig].rank().corr(g[f"n{h}"].rank()) if len(g) >= 50 else np.nan,
            include_groups=False)
        lag = max(0, h // 5 - 1)
        out.append(f"| {label} | {h}일 | " + " | ".join(ies._pct(q.get("mean")) for q in qs)
                   + f" | {ies._pct(sp.mean())} (t {_nw_t(sp, lag):+.2f}) | {ic.mean():+.4f} (t {_nw_t(ic, lag):+.2f}) |")
    return out


def pre5_control(w: pd.DataFrame, C: pd.DataFrame) -> list[str]:
    """직전 1주 수익(주간 반전) 통제: 같은 '직전 5일 SPY 대비 수익' 5분위 안에서 논조 5분위 차이(Q5−Q1)."""
    days = C.index
    col = {t: k for k, t in enumerate(C.columns)}
    Cv = C.values.astype(float)
    js = col["SPY"]
    i0 = days.searchsorted(w["filing_date"].values, side="right") - 1
    j = w["ticker"].map(col).values.astype(int)
    x = w.assign(pre5=Cv[i0, j] / Cv[i0 - 5, j] - Cv[i0, js] / Cv[i0 - 5, js])
    x = x[x["stance"].notna() & x["pre5"].notna()].copy()
    q5 = lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 50 else np.nan  # noqa: E731
    x["sq"] = x.groupby("filing_date")["stance"].transform(q5)
    x["pq"] = x.groupby("filing_date")["pre5"].transform(q5)
    x = x.dropna(subset=["sq", "pq"])
    out = [f"- 논조와 직전 5일 수익의 순위상관 {x['stance'].corr(x['pre5'], method='spearman'):+.3f}"]
    for h in (5, 20):
        t = x.groupby(["filing_date", "pq", "sq"])[f"n{h}"].mean().unstack("sq")
        d = (t[4] - t[0]).groupby(level="filing_date").mean()
        t2 = x.groupby(["filing_date", "pq"])[f"n{h}"].mean().unstack("pq")
        d2 = t2[4] - t2[0]
        lag = max(0, h // 5 - 1)
        out.append(f"- {h}일: 직전 5일 수익 5분위 안에서 논조 5−1 차이 {ies._pct(d.mean(), 3)} (NW t {_nw_t(d, lag):+.2f}) · "
                   f"참고: 직전 5일 수익 자체의 5−1 차이(주간 반전) {ies._pct(d2.mean(), 3)} (NW t {_nw_t(d2, lag):+.2f})")
    return out


def cmd_analyze(args):
    O, C = ies.load_prices()
    a = pd.read_parquet(DERIVED / "articles.parquet")
    a = a[a["ticker"].isin(set(C.columns))]
    w = weekly_signals(a, C.index)
    sec = dict(zip(*ies.load_sp500()[["ticker", "sector"]].values.T))
    w["sector"] = w["ticker"].map(sec)
    w = ies.attach_returns(w, O, C)
    w = w[w["entry"].notna()].reset_index(drop=True)
    w.to_parquet(DERIVED / "weekly_returns.parquet")
    base = ies.baselines(O, C)
    L: list[str] = []
    wl = L.append

    sig = w[w["stance"].notna()]
    n_sw = len(C.columns) * len(w["filing_date"].unique())
    wl("## 데이터")
    yrs = a.groupby(a["ts"].dt.year).size()
    wl(f"- 기사(S&P 500 태그, 중복 제거): {len(a):,}건 · 종목 {a['ticker'].nunique()} · 연도별 "
       + ", ".join(f"{y} {n:,}" for y, n in yrs.items()))
    wl(f"- 시황 기사(같은 날 같은 제목이 {ROUNDUP_TICKERS}종목+): {a['roundup'].mean() * 100:.0f}% · "
       f"발행처 상위: " + ", ".join(f"{k} {v / len(a) * 100:.0f}%" for k, v in a['publisher'].value_counts().head(4).items()))
    wl(f"- 종목·주 {len(w):,}개(기사 1건+) 중 어휘 채점 가능 {len(sig):,} · 주당 기사 중앙값 {w['n_week'].median():.0f}건")
    tone = np.select([sig["stance"] > 0.08, sig["stance"] < -0.08], ["긍정", "부정"], "혼조")
    wl("- 라이브 톤 분포: " + ", ".join(f"{k} {v / len(sig) * 100:.0f}%" for k, v in pd.Series(tone).value_counts().items())
       + f" · stance 중앙값 {sig['stance'].median():+.2f}")
    wl("- 무작위 진입 기준선(비용 전 SPY 대비): " + ", ".join(f"{h}일 {ies._pct(base[h])}" for h in ies.HORIZONS))
    wl("")

    # 하루 강한 논조 기사(시황 기사 제외) 전후 반응 — 날짜 정합성 검증
    day = (a[~a["roundup"]].groupby(["ticker", "date"])[["pos", "neg"]].sum().reset_index())
    day["tone"] = (day["pos"] - day["neg"]) / (day["pos"] + day["neg"]).replace(0, np.nan)
    strong = day[((day["tone"] >= 0.6) & (day["pos"] >= 2)) | ((day["tone"] <= -0.6) & (day["neg"] >= 2))].copy()
    strong["kind"] = np.where(strong["tone"] > 0, "강한 긍정", "강한 부정")
    strong = strong[strong["date"].between("2018-01-01", STUDY_END)]
    strong["filing_date"] = strong["date"]
    strong = ies.attach_returns(strong.assign(sector=strong["ticker"].map(sec)), O, C)
    strong = strong[strong["entry"].notna()]
    strong = pd.concat([strong, aes.reactions(strong, O, C)], axis=1)
    wl("## 하루 강한 논조 기사 전후 반응 (SPY 대비, 비용 전, 시황 기사 제외) — 날짜 정합성 검증")
    wl("| 구분 | n | d−1 종가→종가 | 기록일 시가 갭 | 기록일 장중 | 다음날 시가 갭 |")
    wl("|---|---:|---:|---:|---:|---:|")
    for k in ("강한 긍정", "강한 부정"):
        wl(aes._rx_row(k, strong[strong["kind"] == k]))
    wl("- 기록일이 맞다면 반응은 d−1·당일에 몰리고, 진입(다음 거래일 시가) 뒤로는 남지 않아야 한다.")
    wl("")

    wl("## 라이브 재현 — 주별 횡단면 5분위 (종목·주 단위 평균 순초과 SPY 대비, 비용 차감)")
    wl("| 신호 | 보유 | 1분위(부정) | 2 | 3 | 4 | 5분위(긍정) | 5−1 차이 (NW t) | 주별 IC (NW t) |")
    wl("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    L.extend(xsection(w, "stance", "헤드라인(라이브 방식)"))
    L.extend(xsection(w, "stance2", "헤드라인+요약"))
    L.extend(xsection(w, "stance3", "시황 기사 제외"))
    wl("- 5분위는 그 주 채점 가능 종목 50개 이상일 때만. 20·60일은 주마다 겹쳐 Newey-West(시차 h/5−1)로 t 보정.")
    wl("")
    wl("### 주간 반전 통제 (라이브 방식 논조)")
    L.extend(pre5_control(w, C))
    wl("")

    q = w[w["stance"].notna()].copy()
    q["q"] = q.groupby("filing_date")["stance"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 50 else np.nan)
    q3 = w[w["stance3"].notna()].copy()
    q3["q"] = q3.groupby("filing_date")["stance3"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 50 else np.nan)
    cfg_p = {
        "P1 긍정 톤(stance>0.08, 라이브)": w[w["stance"] > 0.08],
        "P2 상위 5분위": q[q["q"] == 4],
        "P3 상위 5분위 × 기사 10건+": q[(q["q"] == 4) & (q["n_week"] >= 10)],
        "P4 상위 5분위(시황 기사 제외)": q3[q3["q"] == 4],
        "P5 긍정 × 기사량 급증": w[(w["stance"] > 0.08) & w["surge"]],
    }
    cfg_n = {
        "N1 부정 톤(stance<−0.08, 라이브)": w[w["stance"] < -0.08],
        "N2 하위 5분위": q[q["q"] == 0],
        "N3 하위 5분위 × 기사 10건+": q[(q["q"] == 0) & (q["n_week"] >= 10)],
        "N4 하위 5분위(시황 기사 제외)": q3[q3["q"] == 0],
        "N5 부정 × 기사량 급증": w[(w["stance"] < -0.08) & w["surge"]],
    }
    cfg_b = {"B1 기사량 급증(논조 무관)": w[w["surge"]]}
    for h in ies.HORIZONS:
        wl(f"## 긍정 뉴스(매수 신호 — 양수여야 정보) — {h}일 보유 (금요일 신호 → 다음 거래일 시가)")
        wl(ies.HEAD)
        for k, df in cfg_p.items():
            wl(ies.row(k, df, h))
        wl("")
        wl(f"## 부정 뉴스(회피 신호 — 음수여야 정보) — {h}일 보유")
        wl(ies.HEAD)
        for k, df in cfg_n.items():
            wl(ies.row(k, df, h))
        wl(ies.row("B1 기사량 급증(논조 무관)", cfg_b["B1 기사량 급증(논조 무관)"], h))
        wl("")

    focus = {"P1 긍정 톤(stance>0.08, 라이브)": cfg_p["P1 긍정 톤(stance>0.08, 라이브)"],
             "P2 상위 5분위": cfg_p["P2 상위 5분위"],
             "N1 부정 톤(stance<−0.08, 라이브)": cfg_n["N1 부정 톤(stance<−0.08, 라이브)"],
             "N2 하위 5분위": cfg_n["N2 하위 5분위"],
             "B1 기사량 급증(논조 무관)": cfg_b["B1 기사량 급증(논조 무관)"]}
    years = list(range(2018, 2024))
    for h in (5, 20):
        wl(f"## 연도별 안정성 — {h}일 보유 (종목·월 단위 순초과: SPY 대비 / 사전수익 매칭 대비 (n))")
        wl("| 조합 | " + " | ".join(str(y) for y in years) + " | 기대 부호 연도 SPY · 매칭 |")
        wl("|---|" + "---:|" * (len(years) + 1))
        for k, df in focus.items():
            sub = ies.fm(df)
            sign = -1 if k.startswith("N") else 1
            cells, pos, posm = [], 0, 0
            for y in years:
                sy = sub[sub["filing_date"].dt.year == y]
                s, sm = ies.stat(sy, f"n{h}"), ies.stat(sy, f"mb{h}")
                if s["n"] < 3:
                    cells.append("—")
                    continue
                pos += sign * s["mean"] > 0
                posm += sign * sm.get("mean", np.nan) > 0
                cells.append(f"{ies._pct(s['mean'], 1)} / {ies._pct(sm.get('mean'), 1)} ({s['n']})")
            wl(f"| {k} | " + " | ".join(cells) + f" | {pos}/{len(years)} · {posm}/{len(years)} |")
        wl("")

    wl("## 표본 반분 — 2018–2020 vs 2021–2023, 종목·월 단위")
    wl("| 조합 | 보유 | 2018–20 SPY 대비 (t) | 매칭 대비 (t) | n | 2021–23 SPY 대비 (t) | 매칭 대비 (t) | n |")
    wl("|---|---:|---:|---:|---:|---:|---:|---:|")
    for k, df in focus.items():
        early = df["filing_date"].dt.year <= 2020
        for h in ies.HORIZONS:
            da, db = ies.fm(df[early]), ies.fm(df[~early])
            a_, b_ = ies.stat(da, f"n{h}"), ies.stat(db, f"n{h}")
            if a_["n"] < 3 or b_["n"] < 3:
                continue
            wl(f"| {k} | {h}일 | {ies._mt(a_)} | {ies._mt(ies.stat(da, f'mb{h}'))} | {a_['n']:,} | {ies._mt(b_)} | "
               f"{ies._mt(ies.stat(db, f'mb{h}'))} | {b_['n']:,} |")
    wl("")

    wl("## 달력시간 포트폴리오 (신호 후 h일 보유·동일가중·진입마다 왕복 0.15%, 2018–2023) — 부정은 '보유했다면'의 성과")
    wl("| 조합 | 보유 | 연환산 초과(SPY) | t | 샤프(초과) | 연환산 초과(동일가중) | t | 보유일 비중 | 평균 종목수 | 진입 수 |")
    wl("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    O2, C2 = O[O.index <= STUDY_END], C[C.index <= STUDY_END]
    for k, df in focus.items():
        for h in ies.HORIZONS:
            c = ies.ctp(df, h, O2, C2)
            wl(f"| {k} | {h}일 | {ies._pct(c['spy_ann'], 1)} | {ies._t(c['spy_t'])} | {c['spy_sharpe']:+.2f} | "
               f"{ies._pct(c['ew_ann'], 1)} | {ies._t(c['ew_t'])} | {c['days_in'] * 100:.0f}% | "
               f"{c['avg_pos']:.1f} | {c['entries']:,} |")
    wl("")

    out = Path(args.out) if args.out else DERIVED / "news_tables.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"표 저장: {out}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("ingest")
    g.add_argument("path", help="CSV 경로 또는 - (표준입력)")
    g.add_argument("--source", required=True)
    sub.add_parser("build")
    a = sub.add_parser("analyze")
    a.add_argument("--out", default="")
    args = ap.parse_args()
    {"ingest": cmd_ingest, "build": cmd_build, "analyze": cmd_analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
