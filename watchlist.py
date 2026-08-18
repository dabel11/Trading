"""
워치리스트 관리: JSON 파일로 영속 저장. GUI에서 추가/삭제 가능.

종목 출처(source)를 3가지로 구분해 관리한다:
  - manual : 사용자가 직접 추가 — 자동 정리(prune) 대상 아님, 항상 유지
  - auto   : 시장 자동 발굴(screener)이 편입 — 다음 스캔에서 교체 가능
  - held   : 현재 보유 포지션 — 매도되어 사라질 때까지 항상 유지(점수화 대상)

load()가 반환하는 활성 유니버스 stocks = manual ∪ auto ∪ held (중복 제거).
보유종목이 워치리스트에 항상 포함되므로, 보유종목이 매 사이클 제대로
점수화된다(빠지면 점수 0으로 오인돼 강제 매도되는 허점 방지).

레거시 파일({"stocks":[...]}만 있는 경우)은 기존 stocks 전부를 manual 로
간주해 안전하게 이관한다(사용자 큐레이션이 자동 정리로 지워지지 않게).
"""

import json
import time
from pathlib import Path
import yfinance as yf

from safe_store import atomic_write_json, safe_read_json

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

# 섹터 ETF는 별도 관리 (매매 대상 아님, 섹터 시그널용)
SECTOR_ETFS = ["XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLB","XLU","XLRE"]

# 기본 종목 (watchlist.json 없을 때)
DEFAULT_STOCKS = [
    "AAPL","MSFT","NVDA","META","GOOGL","AMZN","TSLA",
    "AMD","AVGO","CRM","ORCL","ADBE","NFLX","SHOP",
    "JPM","GS","MS","BAC",
    "XOM","CVX","LLY","UNH","JNJ",
]

# 섹터 매핑 (종목 추가 시 자동 감지 안 되면 'Unknown')
SECTOR_MAP = {
    "XLK": ["AAPL","MSFT","NVDA","AMD","AVGO","CRM","ORCL","ADBE","GOOGL",
             "META","NFLX","SHOP","INTC","QCOM","MU","AMAT","LRCX"],
    "XLF": ["JPM","GS","MS","BAC","WFC","C","BLK","SCHW","AXP","V","MA"],
    "XLE": ["XOM","CVX","COP","SLB","EOG","PXD","MPC","PSX"],
    "XLV": ["LLY","UNH","JNJ","PFE","MRK","ABBV","TMO","DHR","ABT","BMY"],
    "XLY": ["TSLA","AMZN","HD","MCD","NKE","SBUX","TJX","LOW","BKNG","CMG"],
    "XLI": ["CAT","DE","BA","GE","HON","UPS","RTX","LMT","UNP","ETN"],
    "XLP": ["PG","KO","PEP","COST","WMT","PM","MO","CL","KMB"],
    "XLB": ["LIN","APD","ECL","SHW","FCX","NEM","NUE","ALB"],
    "XLU": ["NEE","DUK","SO","D","AEP","EXC","XEL","ES"],
    "XLRE":["AMT","PLD","CCI","EQIX","PSA","DLR","O","WY"],
}

def _ticker_to_sector(ticker: str) -> str:
    for etf, tickers in SECTOR_MAP.items():
        if ticker in tickers:
            return etf
    return "XLK"   # 기본값: 테크


def _dedup(seq) -> list[str]:
    """순서 보존 중복 제거."""
    seen = set(); out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


# ─────────────────────────────────────────────────────────── 영속 구조(full)

def _load_full() -> dict:
    """정규화된 워치리스트 구조 반환.

    {stocks, manual, auto, held, auto_meta, auto_ts}
    레거시({"stocks":[...]})는 stocks→manual 로 승격해 안전 이관.
    """
    if not WATCHLIST_FILE.exists():
        m = list(DEFAULT_STOCKS)
        return {"stocks": list(m), "manual": m, "auto": [], "held": [],
                "auto_meta": {}, "auto_ts": 0}
    data = safe_read_json(WATCHLIST_FILE, default={"stocks": list(DEFAULT_STOCKS)})
    stocks = data.get("stocks") or list(DEFAULT_STOCKS)
    if "manual" not in data:
        # 레거시: 기존 큐레이션 전부 manual 로 본다(자동 정리 대상에서 보호)
        manual = list(stocks)
        auto, held = [], []
    else:
        manual = data.get("manual", [])
        auto   = data.get("auto", [])
        held   = data.get("held", [])
    return {
        "stocks":    _dedup(stocks),
        "manual":    _dedup(manual),
        "auto":      _dedup(auto),
        "held":      _dedup(held),
        "auto_meta": data.get("auto_meta", {}),
        "auto_ts":   float(data.get("auto_ts", 0) or 0),
    }


def _rebuild_stocks(full: dict) -> list[str]:
    """활성 유니버스 = manual ∪ auto ∪ held."""
    return _dedup(list(full.get("manual", [])) +
                  list(full.get("auto", [])) +
                  list(full.get("held", [])))


def _save_full(full: dict):
    full = dict(full)
    full["stocks"] = _rebuild_stocks(full)
    atomic_write_json(WATCHLIST_FILE, full)


# ─────────────────────────────────────────────────────────────── 공개 API

def load() -> list[str]:
    """활성 매매 유니버스(중복 제거)."""
    return _load_full()["stocks"]


def save(stocks: list[str]):
    """레거시 호환: 활성 목록만 통째로 저장.

    출처 구분 정보가 없으므로 manual 로 간주해 보존한다(자동 정리 안전).
    """
    full = _load_full()
    new = _dedup(stocks)
    held = [t for t in full.get("held", []) if t in new]
    auto = [t for t in full.get("auto", []) if t in new]
    manual = [t for t in new if t not in auto and t not in held]
    full.update(manual=manual, auto=auto, held=held)
    _save_full(full)


def add(ticker: str) -> dict:
    """
    종목 수동 추가. yfinance로 유효성 검사. (manual 출처 → 자동 정리 안 됨)
    Returns: {"ok": bool, "name": str, "sector": str, "error": str}
    """
    ticker = ticker.upper().strip()
    full = _load_full()
    if ticker in full["stocks"]:
        # 이미 자동 편입돼 있던 종목이면 manual 로 승격(고정)
        if ticker in full["auto"]:
            full["auto"].remove(ticker)
            if ticker not in full["manual"]:
                full["manual"].append(ticker)
            _save_full(full)
            return {"ok": True, "ticker": ticker, "name": ticker,
                    "sector": _ticker_to_sector(ticker), "promoted": True}
        return {"ok": False, "error": f"{ticker} 이미 추가된 종목입니다."}

    # yfinance 유효성 검사
    try:
        info = yf.Ticker(ticker).fast_info
        price = getattr(info, "last_price", None)
        if not price:
            return {"ok": False, "error": f"{ticker} — 가격 정보를 가져올 수 없습니다."}
        name = yf.Ticker(ticker).info.get("shortName", ticker)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    full["manual"].append(ticker)
    _save_full(full)

    sector = _ticker_to_sector(ticker)
    return {"ok": True, "ticker": ticker, "name": name, "sector": sector}


def remove(ticker: str) -> bool:
    """수동 제거. 보유(held) 종목은 보호 — 매도 전에는 지울 수 없다."""
    full = _load_full()
    if ticker not in full["stocks"]:
        return False
    if ticker in full.get("held", []):
        return False   # 보유 중 — 강제 매도 방지 위해 워치리스트에 유지
    for key in ("manual", "auto"):
        if ticker in full[key]:
            full[key].remove(ticker)
    full.get("auto_meta", {}).pop(ticker, None)
    _save_full(full)
    return True


# ─────────────────────────────────────────── 보유종목 동기화 / 자동 편입

def sync_holdings(held: list[str]) -> list[str]:
    """현재 보유 포지션을 워치리스트에 반영.

    보유종목은 항상 유니버스에 포함돼 매 사이클 제대로 점수화된다
    (워치리스트 밖이면 점수 0으로 오인돼 강제 매도되는 허점 방지).
    더 이상 보유하지 않는 종목은 held 에서 빠지지만, manual/auto 로
    들어와 있으면 그 출처에 따라 유지/정리된다. 활성 stocks 를 반환.
    """
    full = _load_full()
    held = _dedup([t.upper() for t in (held or [])])
    if full.get("held", []) == held:
        return full["stocks"]
    full["held"] = held
    _save_full(full)
    return full["stocks"]


def apply_screen(discovered: list[dict], cap: int = 50) -> dict:
    """스크리너 발굴 결과를 워치리스트에 병합.

    discovered: [{"ticker","score",...}, ...] (점수 내림차순 권장)
    우선순위: held(항상) → manual(항상) → 자동 발굴 상위 (cap 까지).
    manual/held 는 절대 건드리지 않고, auto 슬롯만 새 발굴로 교체한다.
    Returns 요약 dict.
    """
    full = _load_full()
    manual = list(full["manual"])
    held   = list(full["held"])
    protected = _dedup(held + manual)
    room = max(0, int(cap) - len(protected))

    # 점수 내림차순 — 호출자 정렬에 의존하지 않고 항상 고점수 우선 편입
    disc = sorted([d for d in discovered if d.get("ticker")],
                  key=lambda d: float(d.get("score", 0)), reverse=True)
    new_auto, meta = [], {}
    for d in disc:
        if len(new_auto) >= room:
            break
        t = str(d["ticker"]).upper()
        if t in protected or t in new_auto:
            continue
        new_auto.append(t)
        meta[t] = {"score": round(float(d.get("score", 0)), 1),
                   "added": time.strftime("%Y-%m-%d")}

    prev_auto = set(full.get("auto", []))
    added   = [t for t in new_auto if t not in prev_auto]
    dropped = [t for t in prev_auto if t not in new_auto]

    full["auto"] = new_auto
    full["auto_meta"] = meta
    full["auto_ts"] = time.time()
    _save_full(full)
    return {
        "added": added, "dropped": dropped,
        "auto_count": len(new_auto), "manual_count": len(manual),
        "held_count": len(held), "total": len(full["stocks"]),
    }


def sources() -> dict:
    """티커 → 출처('manual'|'auto'|'held') 매핑 (held 우선)."""
    full = _load_full()
    out = {}
    for t in full.get("manual", []): out[t] = "manual"
    for t in full.get("auto", []):   out.setdefault(t, "auto")
    for t in full.get("held", []):   out[t] = "held"
    return out


def auto_age_sec() -> float | None:
    """마지막 자동 스캔 이후 경과 초. 스캔 이력 없으면 None."""
    ts = _load_full().get("auto_ts", 0)
    return (time.time() - ts) if ts else None


def get_info_batch(tickers: list[str]) -> list[dict]:
    """설정 페이지 테이블용 종목 정보 조회."""
    results = []
    for t in tickers:
        try:
            info  = yf.Ticker(t).fast_info
            price = getattr(info, "last_price", 0)
            results.append({
                "ticker":  t,
                "price":   price,
                "sector":  _ticker_to_sector(t),
            })
        except Exception:
            results.append({"ticker": t, "price": 0, "sector": "?"})
    return results
