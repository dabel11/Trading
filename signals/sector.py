"""
Sector rotation signal. Score 0-100.

Logic:
  1. Measure momentum (30-day return) for each sector ETF.
  2. Rank sectors best → worst.
  3. Map each ticker to its sector ETF.
  4. Score = rank percentile of the ticker's sector.

매핑 개선: 정적 표(주요 종목) + yfinance 섹터 동적 조회(디스크 캐시) 폴백 →
대부분의 종목이 중립 50 에 머물던 문제 해소. 통신서비스(XLC) 섹터도 추가.
"""

import json
from pathlib import Path
import yfinance as yf
from config import SECTOR_LOOKBACK_DAYS

# Ticker → sector ETF mapping (정적, 자주 쓰는 종목)
TICKER_SECTOR = {
    # Technology (XLK)
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "AVGO": "XLK",
    "CRM": "XLK", "ORCL": "XLK", "ADBE": "XLK", "CSCO": "XLK", "ACN": "XLK",
    "INTC": "XLK", "QCOM": "XLK", "TXN": "XLK", "IBM": "XLK", "NOW": "XLK",
    "AMAT": "XLK", "MU": "XLK", "ADI": "XLK", "LRCX": "XLK", "KLAC": "XLK",
    "SHOP": "XLK", "PLTR": "XLK", "SNOW": "XLK", "PANW": "XLK", "CRWD": "XLK",
    # Communication Services (XLC)
    "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "CMCSA": "XLC", "T": "XLC", "VZ": "XLC", "TMUS": "XLC", "CHTR": "XLC",
    # Financials (XLF)
    "JPM": "XLF", "GS": "XLF", "MS": "XLF", "BAC": "XLF", "WFC": "XLF",
    "C": "XLF", "BLK": "XLF", "SCHW": "XLF", "AXP": "XLF", "SPGI": "XLF",
    "V": "XLF", "MA": "XLF", "PYPL": "XLF",
    # Energy (XLE)
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE", "EOG": "XLE",
    "MPC": "XLE", "PSX": "XLE", "OXY": "XLE",
    # Healthcare (XLV)
    "LLY": "XLV", "UNH": "XLV", "JNJ": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "PFE": "XLV", "TMO": "XLV", "ABT": "XLV", "DHR": "XLV", "AMGN": "XLV",
    "BMY": "XLV", "GILD": "XLV", "CVS": "XLV", "ISRG": "XLV",
    # Consumer Discretionary (XLY)
    "TSLA": "XLY", "AMZN": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "LOW": "XLY", "SBUX": "XLY", "TJX": "XLY", "BKNG": "XLY", "GM": "XLY",
    "F": "XLY",
    # Consumer Staples (XLP)
    "PG": "XLP", "KO": "XLP", "PEP": "XLP", "COST": "XLP", "WMT": "XLP",
    "MDLZ": "XLP", "CL": "XLP", "MO": "XLP", "PM": "XLP",
    # Industrials (XLI)
    "CAT": "XLI", "BA": "XLI", "HON": "XLI", "UPS": "XLI", "GE": "XLI",
    "RTX": "XLI", "LMT": "XLI", "DE": "XLI", "UNP": "XLI", "MMM": "XLI",
    # Materials (XLB)
    "LIN": "XLB", "APD": "XLB", "SHW": "XLB", "FCX": "XLB", "NEM": "XLB",
    # Utilities (XLU)
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "D": "XLU",
    # Real Estate (XLRE)
    "AMT": "XLRE", "PLD": "XLRE", "CCI": "XLRE", "EQIX": "XLRE",
}

SECTOR_ETFS = ["XLK", "XLC", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP",
               "XLB", "XLU", "XLRE"]

# yfinance 의 sector 문자열 → SPDR 섹터 ETF
YF_SECTOR_TO_ETF = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}

_CACHE_FILE = Path(__file__).resolve().parent.parent / "sector_cache.json"
_dyn_cache: dict[str, str] | None = None
_sector_ranks: dict[str, float] | None = None


def _load_dyn_cache() -> dict[str, str]:
    global _dyn_cache
    if _dyn_cache is None:
        try:
            _dyn_cache = json.loads(_CACHE_FILE.read_text()) if _CACHE_FILE.exists() else {}
        except Exception:
            _dyn_cache = {}
    return _dyn_cache


def _save_dyn_cache():
    try:
        _CACHE_FILE.write_text(json.dumps(_dyn_cache, ensure_ascii=False))
    except Exception:
        pass


def _dynamic_sector(ticker: str) -> str | None:
    """정적 표에 없는 종목의 섹터 ETF 를 yfinance 로 1회 조회 후 캐시."""
    cache = _load_dyn_cache()
    if ticker in cache:
        return cache[ticker] or None
    etf = None
    try:
        info = yf.Ticker(ticker).info or {}
        etf = YF_SECTOR_TO_ETF.get(info.get("sector"))
    except Exception:
        etf = None
    cache[ticker] = etf or ""      # 실패도 빈 문자열로 캐시(반복 호출 방지)
    _save_dyn_cache()
    return etf


def _compute_sector_ranks() -> dict[str, float]:
    """Returns ETF → percentile rank (0-100), cached per session."""
    period = f"{SECTOR_LOOKBACK_DAYS + 5}d"
    returns = {}
    for etf in SECTOR_ETFS:
        try:
            df = yf.download(etf, period=period, interval="1d", progress=False, auto_adjust=True)
            if df is not None and len(df) >= 5:
                ret = (df["Close"].squeeze().iloc[-1] / df["Close"].squeeze().iloc[0]) - 1
                returns[etf] = float(ret)
        except Exception:
            returns[etf] = 0.0

    if not returns:
        return {}

    # Rank and convert to 0-100 percentile
    sorted_etfs = sorted(returns, key=returns.get)
    n = len(sorted_etfs)
    if n == 1:
        return {sorted_etfs[0]: 50.0}
    return {etf: round((rank / (n - 1)) * 100, 1) for rank, etf in enumerate(sorted_etfs)}


def refresh_ranks():
    """Call once at start of each session to populate sector ranks."""
    global _sector_ranks
    _sector_ranks = _compute_sector_ranks()


def score(ticker: str) -> float:
    global _sector_ranks
    if _sector_ranks is None:
        refresh_ranks()

    sector_etf = TICKER_SECTOR.get(ticker) or _dynamic_sector(ticker)
    if sector_etf is None:
        return 50.0  # unknown sector → neutral

    return _sector_ranks.get(sector_etf, 50.0)
