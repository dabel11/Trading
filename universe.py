"""
외부 종목 유니버스 — Wikipedia에서 지수 구성종목을 동적으로 가져옴.
API 키 불필요. 결과는 디스크에 캐시.
"""
import json
import time
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "universe_cache.json"
CACHE_TTL = 86400  # 1일


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if time.time() - data.get("_ts", 0) < CACHE_TTL:
                return data
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    data["_ts"] = time.time()
    try:
        CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _fetch_wiki_table(url: str, symbol_cols: list[str]) -> list[str]:
    import pandas as pd
    import urllib.request
    from io import StringIO
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    tables = pd.read_html(StringIO(html))
    for tbl in tables:
        cols = [str(c).strip() for c in tbl.columns]
        for sc in symbol_cols:
            if sc in cols:
                syms = tbl[sc].astype(str).str.strip().tolist()
                # BRK.B → BRK-B (yfinance 표기)
                clean = [s.replace(".", "-") for s in syms
                         if s and s != "nan" and len(s) <= 6 and s.replace("-","").isalpha()]
                if len(clean) >= 20:
                    return clean
    return []


# 지수 정의: 이름 → (위키 URL, 심볼 컬럼 후보들)
INDEXES = {
    "S&P 500":      ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ["Symbol"]),
    "나스닥 100":   ("https://en.wikipedia.org/wiki/Nasdaq-100", ["Ticker", "Symbol"]),
    "다우 30":      ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", ["Symbol"]),
}


# 나스닥100 하드코딩 폴백 — 위키 Nasdaq-100 페이지가 navbox 템플릿(스크랩 불가)으로
# 바뀌어 _fetch_wiki_table 이 0종목을 반환한다(2026 확인). 구성종목은 연 1회 수준으로만
# 바뀌므로 하드코딩이 깨진 스크랩보다 안정적이다.
NASDAQ100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AXON", "AZN", "BIIB", "BKNG",
    "BKR", "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD",
    "CSCO", "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC",
    "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR",
    "MCHP", "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MSTR", "MU",
    "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD",
    "PEP", "PLTR", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM",
    "TMUS", "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

# 스크랩이 깨졌을 때 쓸 하드코딩 폴백
_FALLBACKS = {"나스닥 100": NASDAQ100}


def get_index(name: str) -> list[str]:
    """지수 구성종목 티커 목록 반환 (캐시)."""
    cache = _load_cache()
    if name in cache and isinstance(cache[name], list) and cache[name]:
        return cache[name]
    if name not in INDEXES:
        return []
    url, col = INDEXES[name]
    try:
        syms = _fetch_wiki_table(url, col)
    except Exception:
        syms = []
    if not syms and name in _FALLBACKS:     # 스크랩 실패 → 하드코딩 폴백
        syms = list(_FALLBACKS[name])
    if syms:
        cache[name] = syms
        _save_cache(cache)
    return syms


# 국내 주요 종목 (코스피200 핵심 + 코스닥 대형)
KR_UNIVERSE = [
    "005930.KS","000660.KS","373220.KS","207940.KS","005380.KS","000270.KS",
    "005490.KS","035420.KS","035720.KS","051910.KS","006400.KS","068270.KS",
    "105560.KS","055550.KS","012330.KS","003670.KS","066570.KS","323410.KS",
    "259960.KS","042700.KS","086520.KQ","247540.KQ","091990.KQ","196170.KQ",
]


def available_indexes() -> list[str]:
    return list(INDEXES.keys()) + ["코스피·코스닥"]


def get_kr_universe() -> list[str]:
    return list(KR_UNIVERSE)


def get_combined(names: list[str]) -> list[str]:
    """여러 지수 합집합 (중복 제거, 순서 유지)."""
    seen = set(); out = []
    for n in names:
        for s in get_index(n):
            if s not in seen:
                seen.add(s); out.append(s)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# S&P 500 시점별 구성 근사 (백테스트 연구용 — 라이브 경로 미사용)
# ──────────────────────────────────────────────────────────────────────────────
# 위키 'Selected changes to the list of S&P 500 components' 표는 2026 현재
# 'Historical components of the S&P 500' 문서로 옮겨져 있다. 현재 구성종목에서
# 출발해 변경 이력을 최신→과거로 되감으면 과거 시점의 구성을 근사할 수 있다.
# 한계: 표가 '선별(selected)' 이력이라 누락이 있을 수 있고, 티커 변경(FB→META 등)은
# 편입/편출로 기록되지 않아 현재 티커로 이어 붙는다.

SP500_URL = INDEXES["S&P 500"][0]
SP500_CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"


def _yf_symbol(s) -> str:
    """위키 표기 → yfinance 표기 (BRK.B → BRK-B). 비정상 값은 ''."""
    s = str(s).strip().upper()
    if not s or s == "NAN":
        return ""
    s = s.replace(".", "-")
    return s if (len(s) <= 6 and s.replace("-", "").isalpha()) else ""


def _read_html_tables(url: str):
    import pandas as pd
    import urllib.request
    from io import StringIO
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    return pd.read_html(StringIO(html))


def parse_sp500_changes(tbl) -> list[tuple[str, str, str]]:
    """변경 이력 표(DataFrame) → [(YYYY-MM-DD, 편입 티커, 편출 티커)] (빈 칸은 '')."""
    import pandas as pd
    cols = [" ".join(str(x) for x in c) if isinstance(c, tuple) else str(c)
            for c in tbl.columns]
    t = tbl.copy()
    t.columns = cols

    def _col(*need):
        for c in cols:
            if all(n.lower() in c.lower() for n in need):
                return c
        raise KeyError(need)
    c_date = _col("date")
    c_add, c_rem = _col("added", "ticker"), _col("removed", "ticker")
    out = []
    for _, r in t.iterrows():
        d = pd.to_datetime(r[c_date], errors="coerce")
        if pd.isna(d):
            continue
        out.append((d.strftime("%Y-%m-%d"), _yf_symbol(r[c_add]), _yf_symbol(r[c_rem])))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def fetch_sp500_snapshot() -> dict:
    """현재 구성(티커·GICS 섹터) + 변경 이력 스냅샷. 네트워크 호출 — 결과는 호출자가 캐시.

    구성종목 티커는 get_index('S&P 500') 를 쓰고, GICS 섹터만 같은 표에서 보강한다."""
    members = list(get_index("S&P 500"))
    sectors: dict = {}
    try:
        for tbl in _read_html_tables(SP500_URL):
            if "Symbol" in tbl.columns and "GICS Sector" in tbl.columns:
                for s, g in zip(tbl["Symbol"], tbl["GICS Sector"]):
                    if _yf_symbol(s):
                        sectors[_yf_symbol(s)] = str(g)
                break
    except Exception:
        pass
    changes = []
    for tbl in _read_html_tables(SP500_CHANGES_URL):
        try:
            changes = parse_sp500_changes(tbl)
        except KeyError:
            continue
        if changes:
            break
    if not members or not changes:
        raise RuntimeError("S&P 500 구성/변경 이력을 가져오지 못했습니다")
    return {"fetched": time.strftime("%Y-%m-%d"), "members": members,
            "sectors": sectors, "changes": changes}


def sp500_membership(current: list[str], changes: list[tuple[str, str, str]],
                     dates) -> dict[str, list[bool]]:
    """시점별 구성 근사 → {티커: [dates 각 날짜에 구성종목이었는지]}.

    현재 구성에서 출발해, 각 날짜 D 보다 '뒤에' 효력이 생긴 변경을 되감는다
    (편입 티커 제거, 편출 티커 복원). 효력일 당일부터 편입·편출이 반영된다."""
    import pandas as pd
    ds = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates]
    chg = sorted(changes, key=lambda x: x[0], reverse=True)
    cur = {s for s in current if s}
    order = sorted(range(len(ds)), key=lambda i: ds[i], reverse=True)
    rows: dict[int, set] = {}
    j = 0
    for i in order:
        while j < len(chg) and chg[j][0] > ds[i]:
            _, added, removed = chg[j]
            if added:
                cur.discard(added)
            if removed:
                cur.add(removed)
            j += 1
        rows[i] = set(cur)
    tickers = sorted(set().union(*rows.values())) if rows else []
    return {t: [t in rows[i] for i in range(len(ds))] for t in tickers}
