#!/usr/bin/env python3
"""내부자 매매(SEC Form 4) 이벤트 스터디 — 손으로 정한 insider 공신력(0.78)을 실측으로 대체하기 위한 연구.

라이브 코드 경로 밖의 연구 스크립트다. 라이브 모듈은 읽기만 한다(universe.get_index, yf_compat.install).

데이터
  - SEC EDGAR 'Insider Transactions Data Sets'(분기 ZIP): SEC 가 Form 3/4/5 XML 을 직접 파싱해
    공개하는 공식 구조화 데이터. 회사별 Form 4 XML 수십만 건 대신 분기 파일 40개로 같은 내용을
    얻는다. `verify` 가 무작위 표본을 EDGAR 원본 Form 4 XML 과 대조해 파싱 정확도를 확인한다.
  - 요청: SEC_USER_AGENT('이름 이메일') 헤더 필수, 초당 8회 이하(공식 한도 10회), 디스크 캐시.
  - 내려받은 파일은 파싱만 한다(ZIP 은 풀지 않고 메모리에서 TSV 만 읽음).
  - 가격: yfinance 수정주가(분할·배당 반영) 시가·종가.

기간
  - 측정: 공시일 2018-01-01 ~ 2024-12-31. 2015~2017 은 정기/기회주의 분류용 과거 이력으로만 쓴다.
  - 2025-01-01 이후는 최종 검증용 봉인 구간 — SEC 파일도 가격도 받지 않는다. 보유 기간이
    2024-12-31 을 넘는 이벤트는 그 보유 기간 측정에서 뺀다.

측정
  - 진입: 공시일(filing date) 다음 거래일 시가. 보유 h=5·20·60 거래일, 청산은 h번째 거래일 종가.
  - 초과수익: 같은 구간 SPY 대비(주), 섹터 SPDR ETF 대비, 현 S&P500 동일가중 대비(생존 편향 보정),
    종목 자체 평균 대비(종목 드리프트 보정). 순수익은 왕복 0.15% 차감.
  - t-stat: 진입월 × 종목 2원 군집 표준오차(같은 시기·같은 종목 이벤트의 상관 보정).
  - 달력시간 포트폴리오: 신호 후 h일 보유 동일가중 — 겹치는 이벤트를 종목 단위로 묶은 '실제 운용' 수치.
  - 라이브 신호 점검: providers.insider 가 쓰는 Finnhub MSPR 의 근사(P/S 주식수 순매수 비율).

사용
  python3 -P research/insider_event_study.py fetch-prices                     # 구성종목·섹터·가격
  SEC_USER_AGENT="이름 이메일" python3 -P research/insider_event_study.py fetch-sec   # 백그라운드 권장
  python3 -P research/insider_event_study.py build                            # 이벤트 표
  python3 -P research/insider_event_study.py analyze --out <표.md>
  SEC_USER_AGENT="이름 이메일" python3 -P research/insider_event_study.py verify --n 40
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
import time
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
warnings.filterwarnings("ignore", "Mean of empty slice", RuntimeWarning)
if str(ROOT) not in sys.path:          # python -P 로 실행해도 universe·yf_compat 를 읽을 수 있게
    sys.path.insert(0, str(ROOT))

# ── 기간·봉인 ─────────────────────────────────────────────────────────────
SEAL = pd.Timestamp("2025-01-01")          # 이 날짜부터는 봉인 — 받지도 쓰지도 않는다
STUDY_START = pd.Timestamp("2018-01-01")
STUDY_END = pd.Timestamp("2024-12-31")
HIST_FIRST_YEAR = 2015                    # 2018 이벤트의 과거 3년 이력
PRICE_START = "2017-09-01"                # 사전 20일 수익 계산 여유
HORIZONS = (5, 20, 60)
COST = 0.0015                              # 왕복 거래비용
CLUSTER_DAYS = 30                          # 클러스터: 공시일 포함 직전 30일 내 서로 다른 매수 내부자 수
MIN_VALUE = 10_000                         # 이 금액 미만 공시는 제외 — 배당재투자·급여공제 등 계획 매수(예: ED·SPG
                                           # 임원 월 $100 미만 P 공시)가 클러스터·직급 표본을 오염시킨다.
                                           # 수익률을 보기 전에 정한 값(분석 결과로 고른 값 아님).

CACHE = Path(os.environ.get("TRADE_RESEARCH_CACHE",
                            Path.home() / ".cache" / "trade_research")) / "insider"
RAW = CACHE / "sec_raw"          # SEC 원본 — 파싱만 하는 데이터
PRICES = CACHE / "prices"
DERIVED = CACHE / "derived"

FORM345_URL = ("https://www.sec.gov/files/structureddata/data/"
               "insider-transactions-data-sets/{y}q{q}_form345.zip")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

SECTOR_ETF = {
    "Information Technology": "XLK", "Health Care": "XLV", "Financials": "XLF",
    "Consumer Discretionary": "XLY", "Communication Services": "XLC", "Industrials": "XLI",
    "Consumer Staples": "XLP", "Energy": "XLE", "Utilities": "XLU",
    "Real Estate": "XLRE", "Materials": "XLB",
}

VALUE_BINS = [MIN_VALUE, 50_000, 250_000, 1_000_000, np.inf]
VALUE_LABELS = ["$10k–50k", "$50k–250k", "$250k–1M", "≥$1M"]
ROLE_ORDER = ["CEO·CFO", "기타 임원", "이사(비임원)", "10% 주주", "기타"]
CLS_ORDER = ["기회주의적", "정기적", "미분류"]
_CEO_CFO = re.compile(r"\bCEO\b|\bCFO\b|CHIEF\s+EXEC|CHIEF\s+FIN|PRINCIPAL\s+EXEC|PRINCIPAL\s+FIN",
                      re.I)


# ─────────────────────────────────────────────────────────────────────────
# SEC 요청
# ─────────────────────────────────────────────────────────────────────────

class SecClient:
    """선언된 User-Agent · 초당 8회 이하 · 일시 오류 재시도."""
    MIN_GAP = 0.125

    def __init__(self, ua: str):
        if not ua or "@" not in ua:
            raise SystemExit("SEC_USER_AGENT 에 '이름 이메일' 형식의 연락처가 필요합니다(SEC 규정).")
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self._last = 0.0
        self._lock = threading.Lock()

    def get(self, url: str, tries: int = 4) -> bytes:
        r = None
        for k in range(tries):
            with self._lock:
                wait = self._last + self.MIN_GAP - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            r = self.s.get(url, timeout=180)
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                raise FileNotFoundError(url)
            # 403(한도 초과·미선언 도구)·429·5xx: SEC 는 10분 차단 후 풀어 준다
            time.sleep(min(120, 10 * 2 ** k))
        raise RuntimeError(f"SEC 요청 실패 {r.status_code if r is not None else '?'}: {url}")


def _ua() -> str:
    return os.environ.get("SEC_USER_AGENT", "").strip()


def fetch_sec(client: SecClient):
    RAW.mkdir(parents=True, exist_ok=True)
    for y in range(HIST_FIRST_YEAR, STUDY_END.year + 1):
        assert pd.Timestamp(f"{y}-01-01") < SEAL, "봉인 구간 파일 요청 금지"
        for q in (1, 2, 3, 4):
            p = RAW / f"{y}q{q}_form345.zip"
            if p.exists() and zipfile.is_zipfile(p):
                continue
            t0 = time.time()
            data = client.get(FORM345_URL.format(y=y, q=q))
            tmp = p.with_suffix(".part")
            tmp.write_bytes(data)
            if not zipfile.is_zipfile(tmp):
                tmp.unlink()
                raise RuntimeError(f"ZIP 아님: {y}q{q}")
            tmp.rename(p)
            print(f"  {p.name} {len(data) / 1e6:.1f}MB {time.time() - t0:.0f}s", flush=True)
    p = RAW / "company_tickers.json"
    if not p.exists():
        p.write_bytes(client.get(TICKERS_URL))
    print("SEC 수집 완료", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# 구성종목·가격
# ─────────────────────────────────────────────────────────────────────────

def load_sp500() -> pd.DataFrame:
    """universe.py 의 S&P 500 구성종목 + 같은 위키 표의 GICS 섹터·CIK."""
    path = DERIVED / "sp500.csv"
    if path.exists():
        return pd.read_csv(path, dtype={"ticker": str, "sector": str}).astype({"cik": "Int64"})
    import urllib.request
    import universe
    tickers = universe.get_index("S&P 500")
    if len(tickers) < 400:
        raise SystemExit(f"S&P 500 구성종목을 못 읽음({len(tickers)})")
    url = universe.INDEXES["S&P 500"][0]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    tbl = next(t for t in pd.read_html(io.StringIO(html))
               if "Symbol" in t.columns and "GICS Sector" in t.columns)
    tbl["ticker"] = tbl["Symbol"].astype(str).str.strip().str.replace(".", "-", regex=False)
    meta = tbl.drop_duplicates("ticker").set_index("ticker")
    rows = []
    for t in tickers:
        m = meta.loc[t] if t in meta.index else None
        cik = m.get("CIK") if m is not None else None
        rows.append({"ticker": t,
                     "sector": m["GICS Sector"] if m is not None else None,
                     "cik": int(cik) if cik is not None and pd.notna(cik) else None})
    df = pd.DataFrame(rows).astype({"cik": "Int64"})
    DERIVED.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def fetch_prices(tickers: list[str]):
    import yfinance as yf
    try:
        import yf_compat
        yf_compat.install()
    except Exception:
        pass
    PRICES.mkdir(parents=True, exist_ok=True)
    syms = sorted(set(tickers) | {"SPY"} | set(SECTOR_ETF.values()))
    end = SEAL.strftime("%Y-%m-%d")          # yfinance end 는 미포함 → 2024-12-31 까지

    def _dl(chunk):
        df = yf.download(chunk, start=PRICE_START, end=end, auto_adjust=True,
                         progress=False, threads=True, group_by="column")
        return df["Open"], df["Close"]

    opens, closes = [], []
    for k in range(0, len(syms), 60):
        o, c = _dl(syms[k:k + 60])
        opens.append(o)
        closes.append(c)
        print(f"  가격 {min(k + 60, len(syms))}/{len(syms)}", flush=True)
    o = pd.concat(opens, axis=1)
    c = pd.concat(closes, axis=1)
    miss = [s for s in syms if s not in c.columns or c[s].notna().sum() == 0]
    if miss:                                  # 한 번 더 개별 시도
        for s in miss:
            try:
                oo, cc = _dl([s])
                o[s], c[s] = oo[s], cc[s]
            except Exception:
                pass
        miss = [s for s in syms if s not in c.columns or c[s].notna().sum() == 0]
    o.index = pd.to_datetime(o.index).tz_localize(None)
    c.index = pd.to_datetime(c.index).tz_localize(None)
    o, c = o[o.index < SEAL], c[c.index < SEAL]       # 봉인 가드
    o.to_parquet(PRICES / "open.parquet")
    c.to_parquet(PRICES / "close.parquet")
    print(f"가격 저장: {c.shape[1]}종목 × {c.shape[0]}일 ({c.index.min().date()}~{c.index.max().date()})"
          f" · 실패 {len(miss)}: {miss[:20]}", flush=True)


def load_prices() -> tuple[pd.DataFrame, pd.DataFrame]:
    o = pd.read_parquet(PRICES / "open.parquet")
    c = pd.read_parquet(PRICES / "close.parquet")
    assert c.index.max() < SEAL and o.index.max() < SEAL, "봉인 구간 가격이 섞여 있음"
    days = c.index[c["SPY"].notna()]
    return o.reindex(days), c.reindex(days)


# ─────────────────────────────────────────────────────────────────────────
# SEC 분기 파일 파싱 → 이벤트
# ─────────────────────────────────────────────────────────────────────────

def _read_tsv(z: zipfile.ZipFile, name: str, cols: list[str]) -> pd.DataFrame:
    names = {Path(n).name.upper(): n for n in z.namelist()}
    with z.open(names[name]) as f:
        df = pd.read_csv(f, sep="\t", dtype=str, quoting=3, keep_default_na=False,
                         encoding_errors="replace", on_bad_lines="skip",
                         usecols=lambda c: c in cols)
    return df


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.str.strip().str.title(), format="%d-%b-%Y", errors="coerce")


def parse_quarters() -> tuple[pd.DataFrame, pd.DataFrame]:
    """반환: (거래 행 P/S — 전 종목, 보고자 행)."""
    trs, subs, owns = [], [], []
    files = sorted(RAW.glob("*_form345.zip"))
    if not files:
        raise SystemExit("SEC 파일 없음 — fetch-sec 먼저")
    for p in files:
        y = int(p.name[:4])
        assert y < SEAL.year, "봉인 구간 파일"
        with zipfile.ZipFile(p) as z:
            s = _read_tsv(z, "SUBMISSION.TSV", ["ACCESSION_NUMBER", "FILING_DATE",
                                                "DOCUMENT_TYPE", "ISSUERCIK"])
            s = s[s["DOCUMENT_TYPE"].str.strip() == "4"]          # 원본 Form 4 만(정정·Form 5 제외)
            t = _read_tsv(z, "NONDERIV_TRANS.TSV", [
                "ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
            t = t[t["TRANS_CODE"].str.strip().isin(["P", "S"])]
            o = _read_tsv(z, "REPORTINGOWNER.TSV", ["ACCESSION_NUMBER", "RPTOWNERCIK",
                                                    "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
                                                    "RPTOWNER_TITLE"])
        acc = set(s["ACCESSION_NUMBER"])
        t = t[t["ACCESSION_NUMBER"].isin(acc)]
        o = o[o["ACCESSION_NUMBER"].isin(set(t["ACCESSION_NUMBER"]))]
        subs.append(s)
        trs.append(t)
        owns.append(o)
        print(f"  {p.name}: Form4 {len(s):,} · P/S 행 {len(t):,}", flush=True)
    s = pd.concat(subs, ignore_index=True).drop_duplicates("ACCESSION_NUMBER")
    t = pd.concat(trs, ignore_index=True)
    o = pd.concat(owns, ignore_index=True).drop_duplicates()
    t = t.merge(s, on="ACCESSION_NUMBER", how="inner")
    t["side"] = t["TRANS_CODE"].str.strip()
    t["filing_date"] = _date(t["FILING_DATE"])
    t["trans_date"] = _date(t["TRANS_DATE"])
    t["shares"] = pd.to_numeric(t["TRANS_SHARES"], errors="coerce")
    t["price"] = pd.to_numeric(t["TRANS_PRICEPERSHARE"], errors="coerce")
    t["issuer_cik"] = pd.to_numeric(t["ISSUERCIK"], errors="coerce").astype("Int64")
    ad = t["TRANS_ACQUIRED_DISP_CD"].str.strip()
    ok = (t["shares"] > 0) & t["filing_date"].notna() & (
        ((t["side"] == "P") & (ad != "D")) | ((t["side"] == "S") & (ad != "A")))
    t = t[ok].copy()
    t["value"] = np.where(t["price"] > 0, t["shares"] * t["price"], np.nan)
    t = t.rename(columns={"ACCESSION_NUMBER": "accession"})
    return (t[["accession", "issuer_cik", "side", "filing_date", "trans_date",
               "shares", "price", "value"]], o)


def _owner_table(o: pd.DataFrame) -> pd.DataFrame:
    """공시 1건 = 보고자 1명 이상(공동 보고). 대표 키 = 가장 작은 CIK, 관계·직함은 합집합."""
    o = o.copy()
    o["cik"] = pd.to_numeric(o["RPTOWNERCIK"], errors="coerce")
    rel = o["RPTOWNER_RELATIONSHIP"].fillna("").str.upper()
    o["is_dir"] = rel.str.contains("DIRECTOR")
    o["is_off"] = rel.str.contains("OFFICER")
    o["is_ten"] = rel.str.contains("TEN") | rel.str.contains("10")
    o["title"] = o["RPTOWNER_TITLE"].fillna("").str.strip()
    g = o.groupby("ACCESSION_NUMBER")
    out = pd.DataFrame({
        "owner_key": g["cik"].min(),
        "owner_name": g["RPTOWNERNAME"].first(),
        "n_owners": g["cik"].nunique(),
        "is_dir": g["is_dir"].any(), "is_off": g["is_off"].any(), "is_ten": g["is_ten"].any(),
        "title": g["title"].agg(lambda x: " / ".join(sorted({v for v in x if v}))),
    })
    out.index.name = "accession"
    return out.reset_index()


def _role(r) -> str:
    if r.is_off and _CEO_CFO.search(r.title or ""):
        return "CEO·CFO"
    if r.is_off:
        return "기타 임원"
    if r.is_dir:
        return "이사(비임원)"
    if r.is_ten:
        return "10% 주주"
    return "기타"


def _classify(ev: pd.DataFrame, hist: pd.DataFrame) -> pd.Series:
    """Cohen·Malloy·Pomorski(2012): 직전 3개 연도 모두 거래가 있고 같은 달에 3년 연속 거래 → 정기적,
    3개 연도 모두 거래는 있으나 그 패턴이 없으면 기회주의적, 이력 부족은 미분류.
    이력 = 그 내부자의 전 종목 공개시장 P·S 거래월(거래일 기준)."""
    book: dict = {}
    for k, y, m in hist[["owner_key", "y", "m"]].itertuples(index=False):
        book.setdefault(k, {}).setdefault(y, set()).add(m)
    out = []
    for k, t in zip(ev["owner_key"], ev["filing_date"].dt.year):
        yrs = book.get(k, {})
        ms = [yrs.get(t - d) for d in (1, 2, 3)]
        if not all(ms):
            out.append("미분류")
        else:
            out.append("정기적" if ms[0] & ms[1] & ms[2] else "기회주의적")
    return pd.Series(out, index=ev.index)


def _cluster_counts(ev: pd.DataFrame) -> pd.Series:
    """같은 종목·같은 방향에서 공시일 포함 직전 CLUSTER_DAYS 일 안에 공시한 서로 다른 내부자 수
    (MIN_VALUE 이상 공시만 센다)."""
    out = pd.Series(0, index=ev.index)
    win = np.timedelta64(CLUSTER_DAYS, "D")
    for _, g in ev.sort_values("filing_date").groupby(["issuer_cik", "side"]):
        d = g["filing_date"].values
        own = g["owner_key"].values
        cnt: dict = {}
        lo = 0
        hi = 0
        res = np.zeros(len(g), int)
        for i in range(len(g)):
            while hi < len(g) and d[hi] <= d[i]:          # 같은 날 공시까지 포함(진입은 다음 날 시가)
                cnt[own[hi]] = cnt.get(own[hi], 0) + 1
                hi += 1
            while d[lo] < d[i] - win:
                cnt[own[lo]] -= 1
                if cnt[own[lo]] == 0:
                    del cnt[own[lo]]
                lo += 1
            res[i] = len(cnt)
        out.loc[g.index] = res
    return out


def cmd_build(_args):
    sp = load_sp500()
    sec_map = {}
    p = RAW / "company_tickers.json"
    if p.exists():
        for v in json.loads(p.read_text()).values():
            sec_map.setdefault(str(v["ticker"]).upper().replace(".", "-"), int(v["cik_str"]))
    sp["cik"] = sp["cik"].fillna(sp["ticker"].map(sec_map)).astype("Int64")
    sp_known = sp.dropna(subset=["cik"]).drop_duplicates("cik")      # GOOGL/GOOG 등 → 첫 티커
    cik2tk = dict(zip(sp_known["cik"].astype(int), sp_known["ticker"]))
    cik2sec = dict(zip(sp_known["cik"].astype(int), sp_known["sector"]))

    t, o = parse_quarters()
    own = _owner_table(o)
    t = t.merge(own[["accession", "owner_key"]], on="accession", how="left")

    # 정기/기회주의 분류용 이력: 전 종목 P·S 거래월
    h = t[t["trans_date"].between("2005-01-01", STUDY_END)].dropna(subset=["owner_key"])
    hist = pd.DataFrame({"owner_key": h["owner_key"], "y": h["trans_date"].dt.year,
                         "m": h["trans_date"].dt.month}).drop_duplicates()

    # S&P 500 종목만, 공시 1건·방향별 합산
    t = t[t["issuer_cik"].isin(cik2tk.keys())]
    acc = (t.groupby(["accession", "side"])
             .agg(issuer_cik=("issuer_cik", "first"), filing_date=("filing_date", "first"),
                  trans_date=("trans_date", "min"), shares=("shares", "sum"),
                  value=("value", "sum"), n_rows=("shares", "size"))
             .reset_index().merge(own, on="accession", how="left"))
    acc["issuer_cik"] = acc["issuer_cik"].astype(int)
    DERIVED.mkdir(parents=True, exist_ok=True)
    acc.to_parquet(DERIVED / "accessions.parquet")

    # 이벤트 = (종목, 내부자, 공시일, 방향). 같은 날 여러 건은 합친다.
    ev = (acc.dropna(subset=["owner_key"])
             .groupby(["issuer_cik", "owner_key", "filing_date", "side"])
             .agg(trans_date=("trans_date", "min"), shares=("shares", "sum"),
                  value=("value", "sum"), n_filings=("accession", "size"),
                  accession=("accession", "first"), owner_name=("owner_name", "first"),
                  is_dir=("is_dir", "any"), is_off=("is_off", "any"), is_ten=("is_ten", "any"),
                  title=("title", "first"))
             .reset_index())
    ev["ticker"] = ev["issuer_cik"].map(cik2tk)
    ev["sector"] = ev["issuer_cik"].map(cik2sec)
    ev["role"] = [_role(r) for r in ev.itertuples(index=False)]
    ev["cls"] = _classify(ev, hist)
    ev["small"] = ~(ev["value"] >= MIN_VALUE)
    ev["cluster"] = _cluster_counts(ev[~ev["small"]]).reindex(ev.index).fillna(0).astype(int)
    ev["lag_days"] = (ev["filing_date"] - ev["trans_date"]).dt.days
    ev = ev[ev["filing_date"].between(STUDY_START, STUDY_END)].reset_index(drop=True)
    ev.to_parquet(DERIVED / "events.parquet")

    # 라이브 MSPR 근사용 월별 P·S 주식수(S&P 500, 공시월 기준)
    m = t.assign(ym=t["filing_date"].dt.to_period("M"))
    mon = m.pivot_table(index=["issuer_cik", "ym"], columns="side", values="shares",
                        aggfunc="sum", fill_value=0).reset_index()
    mon["ticker"] = mon["issuer_cik"].map(cik2tk)
    mon["ym"] = mon["ym"].astype(str)
    mon.to_parquet(DERIVED / "monthly_ps.parquet")

    n_p, n_s = (ev["side"] == "P").sum(), (ev["side"] == "S").sum()
    print(f"이벤트 저장: 매수 {n_p:,} · 매도 {n_s:,} · 종목 {ev['ticker'].nunique()} "
          f"(S&P500 {len(sp)} 중 CIK 매핑 {len(cik2tk)})", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# 수익률
# ─────────────────────────────────────────────────────────────────────────

def _fwd(o: np.ndarray, c: np.ndarray, h: int) -> np.ndarray:
    """모든 시작일 i 에 대해 i일 시가 → i+h-1일 종가 수익."""
    out = np.full(o.shape, np.nan)
    out[:len(o) - h + 1] = c[h - 1:] / o[:len(o) - h + 1] - 1
    return out


def attach_returns(ev: pd.DataFrame, O: pd.DataFrame, C: pd.DataFrame) -> pd.DataFrame:
    ev = ev.copy()
    days = C.index
    stocks = [t for t in C.columns if t != "SPY" and t not in SECTOR_ETF.values()]
    col = {t: k for k, t in enumerate(C.columns)}
    Ov, Cv = O.values.astype(float), C.values.astype(float)
    i1 = days.searchsorted(ev["filing_date"].values, side="right")     # 공시일 다음 거래일
    j = ev["ticker"].map(col).fillna(-1).astype(int).values
    jetf = ev["sector"].map(SECTOR_ETF).map(col).fillna(-1).astype(int).values
    jspy = col["SPY"]
    ok_i = (i1 < len(days)) & (j >= 0)
    ev["entry"] = pd.NaT
    ev.loc[ok_i, "entry"] = days[i1[ok_i]]
    ev["ym"] = ev["entry"].dt.to_period("M").astype(str)
    S = [col[t] for t in stocks]

    # 사전 20일 초과수익(종가 기준, 공시일까지)
    i0 = i1 - 1
    pre = np.full(len(ev), np.nan)
    okp = ok_i & (i0 - 20 >= 0)
    pre[okp] = (Cv[i0[okp], j[okp]] / Cv[i0[okp] - 20, j[okp]]
                - Cv[i0[okp], jspy] / Cv[i0[okp] - 20, jspy])
    ev["pre20"] = pre        # i0 = i1-1 이므로 아래 P20[i1] 과 같은 정의

    in_study = (days >= STUDY_START)
    # 사전 20일 초과수익 매칭 기준선: 내부자는 하락 뒤에 사고(역추세) 상승 뒤에 판다. 같은 해·같은 사전수익
    # 십분위의 '아무 종목·아무 날' 평균을 빼면 단기 반전·모멘텀 효과를 걷어낸 내부자 고유 정보만 남는다.
    P20 = np.full(Cv.shape, np.nan)
    P20[21:] = Cv[20:-1] / Cv[:-21] - (Cv[20:-1, [jspy]] / Cv[:-21, [jspy]])   # 진입일 i 기준 (i-21 → i-1)
    edges = np.nanquantile(P20[in_study][:, S], np.linspace(0.1, 0.9, 9))
    dec_all = np.where(np.isfinite(P20), np.digitize(P20, edges), -1)
    yr_all = np.repeat(days.year.values[:, None], Cv.shape[1], axis=1)
    keep = np.zeros(Cv.shape, bool)
    keep[np.ix_(in_study, S)] = True
    ev_dec = np.where(np.isfinite(pre), np.digitize(pre, edges), -1)
    ev_yr = np.where(ok_i, days.year.values[np.minimum(i1, len(days) - 1)], -1)
    for h in HORIZONS:
        F = _fwd(Ov, Cv, h)                                   # 일×종목 h일 수익
        ex = F - F[:, [jspy]]                                 # SPY 대비
        ew = np.nanmean(F[:, S], axis=1)                      # 현 구성종목 동일가중
        base = np.nanmean(np.where(in_study[:, None], ex, np.nan), axis=0)   # 종목 자체 평균
        ie = i1 + h - 1
        ok = ok_i & (ie < len(days))
        r = np.full(len(ev), np.nan)
        r[ok] = F[i1[ok], j[ok]]
        ev[f"r{h}"] = r
        ev[f"x{h}"] = r - np.where(ok, F[np.minimum(i1, len(days) - 1), jspy], np.nan)
        ev[f"n{h}"] = ev[f"x{h}"] - COST                      # 순초과수익(주 지표)
        sec = np.full(len(ev), np.nan)
        oks = ok & (jetf >= 0)
        sec[oks] = r[oks] - F[i1[oks], jetf[oks]]
        ev[f"sx{h}"] = sec - COST
        ewv = np.full(len(ev), np.nan)
        ewv[ok] = r[ok] - ew[i1[ok]]
        ev[f"ew{h}"] = ewv - COST
        bv = np.full(len(ev), np.nan)
        bv[ok] = ev[f"x{h}"].values[ok] - base[j[ok]]
        ev[f"ab{h}"] = bv - COST
        m_ = keep & np.isfinite(ex) & (dec_all >= 0)
        cell = pd.Series(ex[m_]).groupby([yr_all[m_], dec_all[m_]]).mean()
        mb = pd.Series(list(zip(ev_yr, ev_dec))).map(cell).values.astype(float)
        ev[f"mb{h}"] = ev[f"x{h}"].values - mb - COST
    return ev


def baselines(O: pd.DataFrame, C: pd.DataFrame) -> dict:
    """무작위 진입 기준선 — 현 구성종목 아무 날 진입 시 평균 h일 SPY 대비 초과수익(비용 전)."""
    days = C.index
    stocks = [t for t in C.columns if t != "SPY" and t not in SECTOR_ETF.values()]
    Ov, Cv = O[stocks].values, C[stocks].values
    so, sc = O["SPY"].values, C["SPY"].values
    m = (days >= STUDY_START)
    out = {}
    for h in HORIZONS:
        F = _fwd(Ov, Cv, h) - _fwd(so[:, None], sc[:, None], h)
        out[h] = float(np.nanmean(F[m]))
    return out


# ─────────────────────────────────────────────────────────────────────────
# 통계
# ─────────────────────────────────────────────────────────────────────────

def _clsum(e: np.ndarray, g: np.ndarray) -> float:
    s = pd.Series(e).groupby(g).sum().values
    return float((s ** 2).sum())


def stat(df: pd.DataFrame, col: str) -> dict:
    x = df[col].values.astype(float)
    ok = np.isfinite(x)
    x = x[ok]
    n = len(x)
    if n < 3:
        return {"n": n}
    mo = df["ym"].values[ok]
    fi = df["ticker"].values[ok]
    mean = x.mean()
    e = x - mean
    both = np.char.add(mo.astype(str), fi.astype(str))
    v = _clsum(e, mo) + _clsum(e, fi) - _clsum(e, both)      # 2원 군집(진입월 × 종목)
    if v <= 0:
        v = _clsum(e, mo)
    gm = min(len(set(mo)), len(set(fi)))
    v *= gm / max(gm - 1, 1)
    se = np.sqrt(v) / n
    sd = x.std(ddof=1)
    return {"n": n, "mean": mean, "med": float(np.median(x)), "t": mean / se if se > 0 else np.nan,
            "t_naive": mean / (sd / np.sqrt(n)) if sd > 0 else np.nan, "hit": float((x > 0).mean())}


def _pct(v, d=2):
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:+.{d}f}%"


def _t(v):
    return "—" if v is None or not np.isfinite(v) else f"{v:+.2f}"


def fm(df: pd.DataFrame) -> pd.DataFrame:
    """종목·진입월당 첫 이벤트 1건. 같은 내부자가 매달 사는 종목(TPL·OXY 등)이 표본을 지배하지 않게 한다."""
    return df.sort_values("filing_date").drop_duplicates(["ticker", "ym"])


def _mt(st: dict) -> str:
    return "—" if st.get("n", 0) < 3 else f"{_pct(st['mean'])} ({_t(st['t'])})"


def row(name: str, df: pd.DataFrame, h: int) -> str:
    d = fm(df)
    s = stat(d, f"n{h}")
    raw = stat(df, f"n{h}")
    if s["n"] < 3:
        return f"| {name} | {s['n']} |" + " — |" * 8
    vc = df.loc[df[f"n{h}"].notna(), "ticker"].value_counts()
    top = f"{vc.index[0]} {vc.iloc[0] / vc.sum() * 100:.0f}%" if len(vc) else "—"
    cells = [name, f"{s['n']:,}", _pct(s["mean"]), _pct(s["med"]), _t(s["t"]), f"{s['hit'] * 100:.0f}%",
             _pct(stat(d, f"sx{h}").get("mean")), _mt(stat(d, f"ab{h}")), _mt(stat(d, f"mb{h}")),
             f"{raw['n']:,}건 {_pct(raw.get('mean'))}", top]
    return "| " + " | ".join(cells) + " |"


HEAD = ("| 구분 | n(종목·월) | 평균 순초과(SPY) | 중앙값 | t(군집) | 승률 | 섹터ETF 대비 | 자기종목 평균 대비 (t) "
        "| 사전수익 매칭 대비 (t) | 원표본 | 최다 종목(원표본) |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")


def ctp(ev: pd.DataFrame, h: int, O: pd.DataFrame, C: pd.DataFrame) -> dict:
    """달력시간 포트폴리오: 신호 종목을 진입일부터 h거래일 보유(겹치면 연장), 동일가중.
    포지션이 없는 날은 SPY 보유(초과 0). 신규 진입마다 왕복 COST 차감."""
    days = C.index
    stocks = [t for t in C.columns if t != "SPY" and t not in SECTOR_ETF.values()]
    col = {t: k for k, t in enumerate(stocks)}
    T, N = len(days), len(stocks)
    A = np.zeros((T, N), bool)
    i1 = days.searchsorted(ev["filing_date"].values, side="right")
    jj = ev["ticker"].map(col)
    for i, j in zip(i1, jj):
        if pd.isna(j) or i + h - 1 >= T:
            continue
        A[i:i + h, int(j)] = True
    St = A & ~np.vstack([np.zeros((1, N), bool), A[:-1]])
    Ov, Cv = O[stocks].values, C[stocks].values
    so, sc = O["SPY"].values, C["SPY"].values
    rcc = np.vstack([np.full((1, N), np.nan), Cv[1:] / Cv[:-1] - 1])
    roc = Cv / Ov - 1
    R = np.where(St, roc, rcc)
    scc = np.r_[np.nan, sc[1:] / sc[:-1] - 1]
    soc = sc / so - 1
    Rb = np.where(St, soc[:, None], scc[:, None])
    ewcc = np.nanmean(rcc, axis=1)
    ewoc = np.nanmean(roc, axis=1)
    Rew = np.where(St, ewoc[:, None], ewcc[:, None])
    act = A & np.isfinite(R)
    na = act.sum(1)
    cost = COST * (St & act).sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        p_spy = np.where(na > 0, (np.where(act, R - Rb, 0).sum(1) - cost) / np.maximum(na, 1), 0.0)
        p_ew = np.where(na > 0, (np.where(act, R - Rew, 0).sum(1) - cost) / np.maximum(na, 1), 0.0)
    m = (days >= STUDY_START)
    out = {"days_in": float((na[m] > 0).mean()), "avg_pos": float(na[m][na[m] > 0].mean())
           if (na[m] > 0).any() else 0.0, "entries": int((St & act)[m].sum())}
    for k, p in (("spy", p_spy[m]), ("ew", p_ew[m])):
        mu, sd = p.mean(), p.std(ddof=1)
        out[f"{k}_ann"] = mu * 252
        out[f"{k}_t"] = mu / (sd / np.sqrt(len(p))) if sd > 0 else np.nan
        out[f"{k}_sharpe"] = mu / sd * np.sqrt(252) if sd > 0 else np.nan
    return out


# ─────────────────────────────────────────────────────────────────────────
# 분석 → 마크다운 표
# ─────────────────────────────────────────────────────────────────────────

def _vbin(v):
    return pd.cut(v, VALUE_BINS, labels=VALUE_LABELS, right=False)


def mspr_test(O: pd.DataFrame, C: pd.DataFrame) -> list[str]:
    """라이브 providers.insider 근사: 월별 MSPR=(매수-매도)/(매수+매도)×100 (P/S 주식수, 공시월),
    월말마다 최근 4개월 중 자료 있는 마지막 3개월 평균 → 다음 거래일 시가 진입 h일 순초과수익."""
    mon = pd.read_parquet(DERIVED / "monthly_ps.parquet")
    for c in ("P", "S"):
        if c not in mon:
            mon[c] = 0.0
    mon["mspr"] = (mon["P"] - mon["S"]) / (mon["P"] + mon["S"]) * 100
    wide = mon.pivot_table(index="ym", columns="ticker", values="mspr")
    months = pd.period_range("2017-09", "2024-12", freq="M").astype(str)
    wide = wide.reindex(months)
    days = C.index
    col = {t: k for k, t in enumerate(C.columns)}
    Ov, Cv = O.values.astype(float), C.values.astype(float)
    jspy = col["SPY"]
    recs = []
    for k in range(3, len(months)):
        ym = months[k]
        if pd.Period(ym, "M") < pd.Period("2018-01", "M"):
            continue
        win = wide.iloc[k - 3:k + 1]
        sig = win.apply(lambda s: s.dropna().iloc[-3:].mean() if s.notna().any() else np.nan)
        me = pd.Period(ym, "M").end_time.normalize()
        i1 = days.searchsorted(me, side="right")
        for h in (20, 60):
            if i1 + h - 1 >= len(days):
                continue
            for t in C.columns:
                if t == "SPY" or t in SECTOR_ETF.values():
                    continue
                j = col[t]
                r = Cv[i1 + h - 1, j] / Ov[i1, j] - 1
                b = Cv[i1 + h - 1, jspy] / Ov[i1, jspy] - 1
                if not np.isfinite(r):
                    continue
                recs.append((ym, t, h, sig.get(t, np.nan), r - b - COST))
    d = pd.DataFrame(recs, columns=["ym", "ticker", "h", "mspr", "net"])
    d["bucket"] = np.select([d["mspr"].isna(), d["mspr"] > 5, d["mspr"] < -5],
                            ["자료 없음", "순매수(MSPR>+5)", "순매도(MSPR<−5)"], "중립(±5)")
    d20 = d[(d["h"] == 20) & d["mspr"].notna()]
    head = [f"- 자료 있는 종목·월 {len(d20):,}개 중 순매도(MSPR<−5) {(d20['mspr'] < -5).mean() * 100:.0f}%, "
            f"MSPR ≤ −90 {(d20['mspr'] <= -90).mean() * 100:.0f}%, 중앙값 {d20['mspr'].median():+.0f} "
            f"(전체 종목·월 중 자료 없음 {d[d['h'] == 20]['mspr'].isna().mean() * 100:.0f}%)", ""]
    lines = head + ["| MSPR 구간 | 보유 | n(종목·월) | 평균 순초과(SPY) | t(군집) | 승률 |",
             "|---|---:|---:|---:|---:|---:|"]
    for h in (20, 60):
        dh = d[d["h"] == h].rename(columns={"net": f"n{h}"})
        for b in ["순매수(MSPR>+5)", "중립(±5)", "순매도(MSPR<−5)", "자료 없음"]:
            s = stat(dh[dh["bucket"] == b], f"n{h}")
            if s["n"] < 3:
                continue
            lines.append(f"| {b} | {h}일 | {s['n']:,} | {_pct(s['mean'])} | {_t(s['t'])} | "
                         f"{s['hit'] * 100:.0f}% |")
    # 월별 횡단면 순위상관(IC): 자료 있는 종목만
    ics = []
    for h in (20, 60):
        dh = d[(d["h"] == h) & d["mspr"].notna()]
        ic = dh.groupby("ym").apply(
            lambda g: g["mspr"].rank().corr(g["net"].rank()) if len(g) >= 20 else np.nan,
            include_groups=False).dropna()
        tt = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if len(ic) > 2 else np.nan
        ics.append(f"- {h}일 보유: 월별 순위상관(IC) 평균 {ic.mean():+.4f}, t {tt:+.2f}, {len(ic)}개월"
                   + (" (보유기간이 겹쳐 t 과대 가능)" if h == 60 else ""))
    return lines + [""] + ics


def cmd_analyze(args):
    O, C = load_prices()
    ev = pd.read_parquet(DERIVED / "events.parquet")
    ev = attach_returns(ev, O, C)
    ev = ev[ev["entry"].notna()].reset_index(drop=True)
    ev["vbin"] = _vbin(ev["value"])
    ev.to_parquet(DERIVED / "events_returns.parquet")
    P0 = ev[ev["side"] == "P"]
    P, S = P0[~P0["small"]], ev[(ev["side"] == "S") & ~ev["small"]]
    base = baselines(O, C)
    L: list[str] = []
    w = L.append

    w("## 데이터")
    w(f"- 측정 이벤트(내부자×종목×공시일, ${MIN_VALUE:,} 이상): 매수 {len(P):,} · 매도 {len(S):,} · "
      f"종목 {ev['ticker'].nunique()} · 공시일 {ev['filing_date'].min().date()} ~ {ev['filing_date'].max().date()}")
    w(f"- ${MIN_VALUE:,} 미만으로 제외: 매수 {int(P0['small'].sum()):,} "
      f"(상위 종목 {', '.join(P0[P0['small']]['ticker'].value_counts().head(4).index)})")
    w(f"- 매수 이벤트 분류: " + ", ".join(f"{k} {v:,}" for k, v in P['cls'].value_counts().items()))
    w(f"- 매수 직급: " + ", ".join(f"{k} {v:,}" for k, v in P['role'].value_counts().items()))
    w(f"- 매수 공시 지연(거래일→공시일) 중앙값 {P['lag_days'].median():.0f}일, 90% {P['lag_days'].quantile(.9):.0f}일")
    w(f"- 매수 전 20일 SPY 대비 수익 평균 {_pct(P['pre20'].mean())} (중앙값 {_pct(P['pre20'].median())}) — "
      f"매도 전 {_pct(S['pre20'].mean())}")
    w("- 무작위 진입 기준선(현 S&P500 구성종목·아무 날 진입·비용 전 SPY 대비 평균): "
      + ", ".join(f"{h}일 {_pct(base[h])}" for h in HORIZONS))
    w("")

    for h in HORIZONS:
        w(f"## 매수 vs 매도 — {h}일 보유")
        w(HEAD)
        w(row(f"매수(P) 전체 — ${MIN_VALUE // 1000}k 미만 포함", P0, h))
        w(row("매수(P) 전체", P, h))
        w(row("매도(S) 전체", S, h))
        w("")

    for h in HORIZONS:
        w(f"## 매수 분해 — {h}일 보유")
        w(HEAD)
        for c in CLS_ORDER:
            w(row(f"분류: {c}", P[P["cls"] == c], h))
        for r_ in ROLE_ORDER:
            w(row(f"직급: {r_}", P[P["role"] == r_], h))
        for v in VALUE_LABELS:
            w(row(f"금액: {v}", P[P["vbin"] == v], h))
        for k, m in (("1명", P["cluster"] == 1), ("2명", P["cluster"] == 2), ("3명+", P["cluster"] >= 3)):
            w(row(f"클러스터 {CLUSTER_DAYS}일: {k}", P[m], h))
        w("")

    # 사전에 정한 조합 격자(데이터를 본 뒤 고른 조합이 아님)
    opp = P["cls"] == "기회주의적"
    cfg = {
        "A 매수 전체": P.index == P.index,
        "B 기회주의적": opp,
        "C CEO·CFO": P["role"] == "CEO·CFO",
        "D 클러스터 2명+": P["cluster"] >= 2,
        "E 클러스터 3명+": P["cluster"] >= 3,
        "F 기회주의적 × CEO·CFO": opp & (P["role"] == "CEO·CFO"),
        "G 기회주의적 × 임원(전체)": opp & P["role"].isin(["CEO·CFO", "기타 임원"]),
        "H 기회주의적 × 이사": opp & (P["role"] == "이사(비임원)"),
        "I 기회주의적 × 클러스터 2명+": opp & (P["cluster"] >= 2),
        "J 기회주의적 × $100k+": opp & (P["value"] >= 100_000),
        "K 미분류 제외(분류 가능) × 클러스터 2명+": (P["cls"] != "미분류") & (P["cluster"] >= 2),
        "L 10% 주주 제외": P["role"] != "10% 주주",
        "M 기회주의적 × 10% 주주 제외": opp & (P["role"] != "10% 주주"),
        "N 비정기적(기회주의적+미분류)": P["cls"] != "정기적",
        "R 정기적": P["cls"] == "정기적",
        "O 비정기적 × 임원(전체)": (P["cls"] != "정기적") & P["role"].isin(["CEO·CFO", "기타 임원"]),
        "Q 비정기적 × 클러스터 2명+": (P["cls"] != "정기적") & (P["cluster"] >= 2),
    }
    for h in HORIZONS:
        w(f"## 사전 지정 조합 — {h}일 보유")
        w(HEAD)
        for k, m in cfg.items():
            w(row(k, P[m], h))
        w("")

    w("## 매도 분해(참고)")
    for h in (20, 60):
        w(f"### {h}일 보유 (값이 음수일수록 매도가 하락을 예고)")
        w(HEAD)
        for c in CLS_ORDER:
            w(row(f"매도 분류: {c}", S[S["cls"] == c], h))
        for r_ in ROLE_ORDER[:3]:
            w(row(f"매도 직급: {r_}", S[S["role"] == r_], h))
        w(row("매도 클러스터 3명+", S[S["cluster"] >= 3], h))
        w(row("기회주의적 × CEO·CFO 매도", S[(S["cls"] == "기회주의적") & (S["role"] == "CEO·CFO")], h))
        w("")

    focus = ["A 매수 전체", "B 기회주의적", "N 비정기적(기회주의적+미분류)", "C CEO·CFO",
             "D 클러스터 2명+", "F 기회주의적 × CEO·CFO", "I 기회주의적 × 클러스터 2명+",
             "O 비정기적 × 임원(전체)", "Q 비정기적 × 클러스터 2명+", "R 정기적"]
    years = sorted(P["filing_date"].dt.year.unique())
    for h in (20, 60):
        w(f"## 연도별 안정성 — {h}일 보유 (종목·월 단위 순초과: SPY 대비 / 사전수익 매칭 대비 (n))")
        w("| 조합 | " + " | ".join(str(y) for y in years) + " | 양(+) 연도 SPY · 매칭 |")
        w("|---|" + "---:|" * (len(years) + 1))
        for k in focus:
            sub = fm(P[cfg[k]])
            cells, pos, posm = [], 0, 0
            for y in years:
                sy = sub[sub["filing_date"].dt.year == y]
                s, sm = stat(sy, f"n{h}"), stat(sy, f"mb{h}")
                if s["n"] < 3:
                    cells.append("—")
                    continue
                pos += s["mean"] > 0
                posm += sm.get("mean", np.nan) > 0
                cells.append(f"{_pct(s['mean'], 1)} / {_pct(sm.get('mean'), 1)} ({s['n']})")
            w(f"| {k} | " + " | ".join(cells) + f" | {pos}/{len(years)} · {posm}/{len(years)} |")
        w("")

    w("## 표본 반분 — 2018–2021(탐색) vs 2022–2024(확인), 종목·월 단위")
    w("| 조합 | 보유 | 2018–21 SPY 대비 (t) | 매칭 대비 (t) | n | 2022–24 SPY 대비 (t) | 매칭 대비 (t) | n |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|")
    early = P["filing_date"].dt.year <= 2021
    for k in focus:
        for h in HORIZONS:
            da, db = fm(P[cfg[k] & early]), fm(P[cfg[k] & ~early])
            a, b = stat(da, f"n{h}"), stat(db, f"n{h}")
            if a["n"] < 3 or b["n"] < 3:
                continue
            w(f"| {k} | {h}일 | {_mt(a)} | {_mt(stat(da, f'mb{h}'))} | {a['n']:,} | {_mt(b)} | "
              f"{_mt(stat(db, f'mb{h}'))} | {b['n']:,} |")
    w("")

    w("## 달력시간 포트폴리오 (신호 후 h일 보유·동일가중·진입마다 왕복 0.15%)")
    w("| 조합 | 보유 | 연환산 초과(SPY) | t | 샤프(초과) | 연환산 초과(동일가중) | t | 보유일 비중 | 평균 종목수 | 진입 수 |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k in focus:
        for h in HORIZONS:
            c = ctp(P[cfg[k]], h, O, C)
            w(f"| {k} | {h}일 | {_pct(c['spy_ann'], 1)} | {_t(c['spy_t'])} | {c['spy_sharpe']:+.2f} | "
              f"{_pct(c['ew_ann'], 1)} | {_t(c['ew_t'])} | {c['days_in'] * 100:.0f}% | "
              f"{c['avg_pos']:.1f} | {c['entries']:,} |")
    w("")

    w("## 라이브 신호 근사 — Finnhub MSPR 방식(providers.insider)")
    L.extend(mspr_test(O, C))
    w("")

    out = Path(args.out) if args.out else DERIVED / "insider_tables.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"표 저장: {out}", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# 원본 대조
# ─────────────────────────────────────────────────────────────────────────

def _xml_totals(raw: bytes, code: str) -> tuple[float, float, set]:
    root = ET.fromstring(raw)
    sh = val = 0.0
    for tx in root.iter("nonDerivativeTransaction"):
        c = tx.findtext("transactionCoding/transactionCode", "").strip()
        if c != code:
            continue
        s = float(tx.findtext("transactionAmounts/transactionShares/value", "0") or 0)
        p = float(tx.findtext("transactionAmounts/transactionPricePerShare/value", "0") or 0)
        sh += s
        val += s * p if p > 0 else 0
    issuer = {e.text.strip().lstrip("0") for e in root.iter("issuerCik") if e.text}
    return sh, val, issuer


def cmd_verify(args):
    client = SecClient(_ua())
    acc = pd.read_parquet(DERIVED / "accessions.parquet")
    acc = acc[acc["filing_date"].between(STUDY_START, STUDY_END)]
    smp = pd.concat([acc[acc["side"] == "P"].sample(args.n, random_state=7),
                     acc[acc["side"] == "S"].sample(max(args.n // 2, 1), random_state=7)])
    xdir = RAW / "form4_xml"
    xdir.mkdir(parents=True, exist_ok=True)
    ok = bad = 0
    for r in smp.itertuples(index=False):
        a = r.accession.replace("-", "")
        p = xdir / f"{a}.xml"
        if not p.exists():
            base = f"https://www.sec.gov/Archives/edgar/data/{r.issuer_cik}/{a}"
            idx = json.loads(client.get(f"{base}/index.json"))
            names = [it["name"] for it in idx["directory"]["item"]
                     if it["name"].lower().endswith(".xml")]
            raw = None
            for nm in names:
                b = client.get(f"{base}/{nm}")
                if b"<ownershipDocument" in b[:4000]:
                    raw = b
                    break
            if raw is None:
                print(f"  {r.accession}: Form 4 XML 없음")
                bad += 1
                continue
            p.write_bytes(raw)
        sh, val, iss = _xml_totals(p.read_bytes(), r.side)
        good = (abs(sh - r.shares) <= max(1, 0.005 * r.shares)
                and (not r.value or abs(val - r.value) <= max(1, 0.005 * r.value))
                and str(r.issuer_cik) in iss)
        ok += good
        bad += not good
        if not good:
            print(f"  불일치 {r.accession} {r.side}: 데이터셋 {r.shares:,.0f}주 ${r.value:,.0f} / "
                  f"원본 {sh:,.0f}주 ${val:,.0f} issuer {iss}")
    print(f"원본 대조: 일치 {ok} / {ok + bad}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-prices")
    sub.add_parser("fetch-sec")
    sub.add_parser("build")
    a = sub.add_parser("analyze")
    a.add_argument("--out", default="")
    v = sub.add_parser("verify")
    v.add_argument("--n", type=int, default=40)
    args = ap.parse_args()
    if args.cmd == "fetch-prices":
        sp = load_sp500()
        print(f"S&P 500 {len(sp)}종목 · 섹터 {sp['sector'].notna().sum()} · CIK {sp['cik'].notna().sum()}",
              flush=True)
        fetch_prices(sp["ticker"].tolist())
    elif args.cmd == "fetch-sec":
        fetch_sec(SecClient(_ua()))
    elif args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "analyze":
        cmd_analyze(args)
    elif args.cmd == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
