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
