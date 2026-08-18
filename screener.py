"""
시장 자동 발굴(스크리너).

넓은 지수 유니버스(S&P 500 / 나스닥 100 …)를 주기적으로 훑어 유망 종목을
워치리스트에 자동 편입한다. 핵심은 '2단계 깔때기':

  ┌ 1단계  값싼 일괄 프리필터 ───────────────────────────────────┐
  │  지수 구성종목 수백 개를 yfinance 대량 다운로드(1회)로 받아   │
  │  종목별 위험조정 모멘텀·추세·유동성을 계산. 네트워크 호출이   │
  │  종목당 1회가 아니라 전체 1회 → 수백 종목도 수십 초.          │
  └──────────────────────────────────────────────────────────────┘
  ┌ 2단계  워치리스트 편입 ───────────────────────────────────────┐
  │  프리필터 상위 top_k 만 추려 watchlist.apply_screen() 으로     │
  │  병합. 보유종목·수동 추가는 보존, 자동 슬롯만 교체.            │
  └──────────────────────────────────────────────────────────────┘

본격 점수화(composite 등 펀더멘털/센티먼트 네트워크 호출)는 무겁기 때문에
'후보를 좁히는 데'는 쓰지 않는다. 프리필터로 좁힌 뒤, 실제 매수 판단은
자동매매 사이클의 전략 스코어러가 워치리스트에 대해 수행한다.

프리필터 점수(0~100):
  위험조정 모멘텀(12-1, 변동성으로 나눔)을 핵심으로, 추세(MA50 상회)와
  과열(52주 고점 근접) 가드를 더한다. 유동성(평균 달러 거래대금)은
  게이트로만 사용해 거래 불가능한 종목을 배제한다.
"""

import time
from typing import Optional

DEFAULT_UNIVERSES = ["S&P 500"]
MIN_DOLLAR_VOL = 2e7      # 일평균 거래대금 $20M 미만 배제(유동성 게이트)
MIN_PRICE = 5.0          # 동전주 배제
DEFAULT_TOP_K = 30
DEFAULT_CAP = 50


# ─────────────────────────────────────────────────── 1단계: 일괄 프리필터

def _bulk_history(tickers: list[str], period: str = "1y"):
    """yfinance 대량 다운로드 → {ticker: DataFrame(Close,Volume)} (실패 종목 제외)."""
    import yfinance as yf
    out: dict = {}
    if not tickers:
        return out
    try:
        raw = yf.download(tickers, period=period, interval="1d",
                          auto_adjust=True, progress=False, threads=True,
                          group_by="ticker")
    except Exception:
        return out

    # 단일/복수 티커에 따라 컬럼 구조가 다름 → 정규화
    if len(tickers) == 1:
        t = tickers[0]
        try:
            df = raw[["Close", "Volume"]].dropna()
            if len(df) >= 60:
                out[t] = df
        except Exception:
            pass
        return out

    for t in tickers:
        try:
            sub = raw[t]
            df = sub[["Close", "Volume"]].dropna()
            if len(df) >= 60:
                out[t] = df
        except Exception:
            continue
    return out


def _prefilter_score(df) -> Optional[dict]:
    """단일 종목 프리필터 점수. df: Close/Volume 일봉.

    Returns {"score","ram","mom","vol","dvol","above_ma"} 또는 None(부적격).
    """
    try:
        close = df["Close"].dropna()
        vol = df["Volume"].dropna()
    except Exception:
        return None
    n = len(close)
    if n < 60:
        return None
    last = float(close.iloc[-1])
    if last < MIN_PRICE:
        return None

    # 유동성: 최근 20일 평균 달러 거래대금
    try:
        dvol = float((close.tail(20) * vol.tail(20)).mean())
    except Exception:
        dvol = 0.0

    # 위험조정 모멘텀 (12-1: 최근 21일 skip, 변동성으로 정규화)
    look = min(252, n - 1)
    skip = 21 if n > 60 else 0
    base = float(close.iloc[-look])
    recent = float(close.iloc[-1 - skip]) if skip else last
    if base <= 0:
        return None
    mom = recent / base - 1.0
    rets = close.pct_change().dropna()
    annual_vol = float(rets.std()) * (252 ** 0.5)
    if annual_vol <= 0:
        return None
    ram = mom / annual_vol      # 샤프식 모멘텀

    # 추세 필터: 현재가가 50일선 위인가
    ma50 = float(close.tail(50).mean())
    above_ma = last > ma50

    # 52주 고점 대비 위치(과열 가드: 신고가 직전이면 약간 감점)
    hi = float(close.tail(min(252, n)).max())
    pos = (last / hi) if hi > 0 else 0.0

    # ── 점수 합성 ──
    # 위험조정 모멘텀을 tanh 로 부드럽게 압축 → 강세장에서도 100에 몰리지
    # 않고 상위권 변별력 유지(랭킹 안정). ram≈±5 까지 자연스럽게 분산.
    import math
    s = 50.0 + 45.0 * math.tanh(ram / 2.5)
    s += 6.0 if above_ma else -10.0  # 추세 동조
    if pos >= 0.98:                  # 신고가 직전 — 추격 과열 약간 감점
        s -= 4.0
    elif pos <= 0.80:                # 고점 대비 20%+ 하락 — 약세 감점
        s -= 6.0
    s = max(0.0, min(100.0, s))

    return {"score": round(s, 1), "ram": round(ram, 3), "mom": round(mom, 3),
            "vol": round(annual_vol, 3), "dvol": dvol, "above_ma": above_ma,
            "pos52": round(pos, 3)}


def screen(universe_names: Optional[list[str]] = None,
           top_k: int = DEFAULT_TOP_K,
           min_dollar_vol: float = MIN_DOLLAR_VOL,
           exclude: Optional[set] = None,
           batch: int = 120) -> list[dict]:
    """지수 유니버스를 프리필터링해 상위 후보 반환.

    Returns: [{"ticker","score","ram","mom","vol","dvol",...}] 점수 내림차순.
    """
    import universe as uni
    names = universe_names or DEFAULT_UNIVERSES
    tickers = uni.get_combined([n for n in names if n in
                                ("S&P 500", "나스닥 100", "다우 30")])
    if not tickers:
        return []
    exclude = {t.upper() for t in (exclude or set())}
    tickers = [t for t in tickers if t.upper() not in exclude]

    # 대량 다운로드는 배치로 쪼개 안정성 확보(yfinance 다종목 한계 회피)
    hist: dict = {}
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        hist.update(_bulk_history(chunk))

    ranked = []
    for t, df in hist.items():
        m = _prefilter_score(df)
        if not m:
            continue
        if m["dvol"] < min_dollar_vol:
            continue   # 유동성 게이트
        m["ticker"] = t
        ranked.append(m)

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:max(1, int(top_k))]


# ─────────────────────────────────────────── 2단계: 워치리스트 자동 편입

def should_run(interval_sec: float) -> bool:
    """마지막 스캔 이후 interval_sec 경과했으면 True (시간 게이트)."""
    import watchlist as wl
    age = wl.auto_age_sec()
    return age is None or age >= float(interval_sec)


def discover(held: Optional[list[str]] = None,
             universe_names: Optional[list[str]] = None,
             top_k: int = DEFAULT_TOP_K,
             cap: int = DEFAULT_CAP,
             min_dollar_vol: float = MIN_DOLLAR_VOL) -> dict:
    """시장 스캔 → 워치리스트 자동 편입(보유·수동 보존). 요약 dict 반환.

    held 가 주어지면 먼저 보유종목을 워치리스트에 동기화한 뒤,
    보유·수동을 제외한 발굴 후보로 자동 슬롯을 채운다.
    """
    import watchlist as wl
    held = [t.upper() for t in (held or [])]
    if held is not None:
        wl.sync_holdings(held)

    full = wl._load_full()
    # 발굴 단계에서 보유·수동은 후보에서 제외(이미 확정 슬롯이므로 중복 방지)
    exclude = set(full.get("manual", [])) | set(full.get("held", []))
    candidates = screen(universe_names=universe_names, top_k=top_k,
                        min_dollar_vol=min_dollar_vol, exclude=exclude)
    summary = wl.apply_screen(candidates, cap=cap)
    summary["scanned"] = True
    summary["candidates"] = candidates
    summary["ts"] = time.time()
    return summary


def maybe_discover(held: Optional[list[str]] = None,
                   interval_sec: float = 14400,   # 기본 4시간
                   **kw) -> Optional[dict]:
    """시간 게이트 통과 시에만 discover() 실행. 아니면 None."""
    if not should_run(interval_sec):
        return None
    try:
        return discover(held=held, **kw)
    except Exception as e:
        return {"scanned": False, "error": str(e)}
