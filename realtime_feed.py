"""
실시간 가격 피드 — WebSocket 스트림 + 백그라운드 폴링 캐시.

우선순위:
  1. Finnhub WebSocket    (API 키 있을 때 — 체결 즉시 푸시, 무료 50종목)
  2. Finnhub REST /quote  (API 키 있을 때, 60 req/min — prev/고저 보강 + 폴백)
  3. yfinance             (폴백, 무제한 — 시장 자동 발굴(screener)도 이 경로)

WebSocket 은 가격(price)을 밀리초~초 단위로 갱신하고, REST/yfinance 폴링은
전일종가·고저 등 부가 필드를 채우는 보조 역할을 계속한다. 키가 없거나
연결이 끊기면 자동으로 폴링만으로 동작(무중단 폴백).

모듈-레벨 딕셔너리(_cache)에 결과를 저장하므로
Streamlit 리렌더링 간에도 데이터가 유지됩니다.

사용:
    from realtime_feed import subscribe, get_price, get_snapshot
    subscribe(["AAPL","MSFT"], api_key="...", interval=5)
    data = get_price("AAPL")
    # -> {"price":190.0,"prev":188.0,"change_pct":0.011,"high":191,"low":187,"ts":...}
"""

import json
import threading
import time
import requests
from collections import deque

# ── 모듈-레벨 공유 상태 ──────────────────────────────────────────────────────
_cache:      dict[str, dict]   = {}   # {ticker: quote_dict}
_subscribed: dict[str, float]  = {}   # {ticker: priority}  (높을수록 자주 갱신)
_api_key:    str               = ""
_interval:   float             = 5.0  # 폴링 주기 (초)
_lock        = threading.Lock()
_thread: threading.Thread | None = None
_change_log: deque             = deque(maxlen=200)  # 가격 변동 이력

# ── 포커스(현재 보고 있는 종목) — 1초 간격 전용 패스트레인 ───────────────────
_focus:        dict[str, float] = {}   # {ticker: 마지막 터치 시각}
_focus_thread: threading.Thread | None = None
_FOCUS_TTL     = 8.0   # 이 시간(초) 동안 재호출 없으면 포커스 자동 해제 (창 닫힘 추정)

# ── WebSocket 스트림 (Finnhub — 체결 즉시 푸시, 무료 50종목) ─────────────────
_WS_MAX_SYMBOLS = 50                      # Finnhub 무료 플랜 한도
_ws_thread: threading.Thread | None = None
_ws_app = None                            # websocket.WebSocketApp
_ws_connected: bool = False
_ws_symbols: set = set()                  # 현재 ws 구독 중인 심볼
_ws_last_msg: float = 0.0

# ── 틱 콜백 (이벤트 구동 소비자 — core.guard 의 즉시 손절 등) ────────────────
_tick_callbacks: list = []


def on_tick(cb):
    """ws 체결 틱마다 cb(ticker, price, ts) 호출 등록.
    콜백은 ws 스레드에서 불리므로 즉시 반환해야 한다(무거운 일은 큐로)."""
    if cb not in _tick_callbacks:
        _tick_callbacks.append(cb)


# ── 피드 건강 상태 (silent 실패 표면화) ──────────────────────────────────────
_last_error:        str   = ""
_last_error_ts:     float = 0.0
_consecutive_fails: int   = 0
_last_success_ts:   float = 0.0


def feed_health() -> dict:
    """피드 상태. UI에서 '데이터 멈춤' 경고에 사용."""
    age = time.time() - _last_success_ts if _last_success_ts else 9e9
    return {
        "ok":           _consecutive_fails == 0 and _last_success_ts > 0,
        "last_error":   _last_error,
        "fails":        _consecutive_fails,
        "stale_sec":    age,
        "n_cached":     len(_cache),
        "ws":           _ws_connected,
        "ws_symbols":   len(_ws_symbols),
        "ws_stale_sec": (time.time() - _ws_last_msg) if _ws_last_msg else None,
    }


# ── 공개 API ─────────────────────────────────────────────────────────────────

def subscribe(tickers: list[str], api_key: str = "", interval: float = 5.0,
              priority: float = 1.0):
    """tickers 를 실시간 폴링 대상에 추가."""
    global _api_key, _interval
    if api_key and "your_" not in api_key:
        _api_key = api_key
    _interval = interval
    with _lock:
        for t in tickers:
            _subscribed[t.upper()] = priority
    _ensure_running()
    _ensure_ws()
    _ws_sync_symbols()


def set_focus(tickers: list[str]):
    """현재 보고 있는 종목을 1초 간격으로 우선 갱신 (상세 창 등).
    매 호출마다 터치 시각을 갱신 — 일정 시간 호출이 끊기면(창 닫힘) 자동 해제."""
    now = time.time()
    with _lock:
        for t in tickers:
            if t:
                _focus[t.upper()] = now
    _ensure_focus_running()


def unsubscribe(tickers: list[str]):
    with _lock:
        for t in tickers:
            _subscribed.pop(t.upper(), None)
            _cache.pop(t.upper(), None)


def get_price(ticker: str) -> dict | None:
    """단일 종목 최신 시세 반환. 없으면 None."""
    return _cache.get(ticker.upper())


def get_snapshot(tickers: list[str]) -> dict[str, dict]:
    """여러 종목 시세를 한 번에 반환."""
    up = [t.upper() for t in tickers]
    return {t: _cache[t] for t in up if t in _cache}


def get_all() -> dict[str, dict]:
    return dict(_cache)


def last_updated(ticker: str) -> float:
    """마지막 갱신 epoch 초 (없으면 0)."""
    return _cache.get(ticker.upper(), {}).get("ts", 0)


def freshness(ticker: str) -> str:
    """'3.2s ago' 형식 문자열."""
    age = time.time() - last_updated(ticker)
    if age < 60:
        return f"{age:.0f}초 전"
    return f"{age/60:.0f}분 전"


def recent_changes(n: int = 10) -> list[dict]:
    return list(_change_log)[-n:]


# ── 내부 구현: WebSocket 스트림 ──────────────────────────────────────────────

def _ws_pick_symbols() -> list:
    """ws 구독 대상 — 우선순위 높은 순(보유 종목 우선) 상위 50개."""
    with _lock:
        ranked = sorted(_subscribed, key=lambda t: -_subscribed[t])
    # 국내(.KS/.KQ)·지수(^) 심볼은 Finnhub 미지원 → 폴링 경로에 맡김
    us = [t for t in ranked if not (t.endswith((".KS", ".KQ")) or t.startswith("^"))]
    return us[:_WS_MAX_SYMBOLS]


def _ws_on_message(_ws, message: str):
    global _ws_last_msg, _last_success_ts
    try:
        m = json.loads(message)
        if m.get("type") != "trade":
            return
        now = time.time()
        _ws_last_msg = now
        _last_success_ts = now
        for tr in m.get("data", []):
            tk = str(tr.get("s", "")).upper()
            p = tr.get("p")
            if not tk or not p:
                continue
            old = _cache.get(tk, {})
            prev = old.get("prev") or 0.0
            if old.get("price") and abs(float(p) - old["price"]) > 0.001:
                _change_log.append({"ticker": tk, "from": old["price"],
                                    "to": float(p), "ts": now})
            d = dict(old)        # prev/open/high/low 등 폴링이 채운 필드 유지
            d.update({
                "price": float(p),
                "change": float(p) - prev if prev else old.get("change", 0.0),
                "change_pct": ((float(p) - prev) / prev) if prev else
                              old.get("change_pct", 0.0),
                "source": "finnhub-ws", "ts": now,
            })
            _cache[tk] = d
            for cb in _tick_callbacks:
                try:
                    cb(tk, float(p), now)
                except Exception:
                    pass
    except Exception:
        pass


def _ws_sync_symbols():
    """구독 목록이 바뀌면 ws 서버에 subscribe/unsubscribe 동기화."""
    global _ws_symbols
    if not _ws_connected or _ws_app is None:
        return
    want = set(_ws_pick_symbols())
    try:
        for t in want - _ws_symbols:
            _ws_app.send(json.dumps({"type": "subscribe", "symbol": t}))
        for t in _ws_symbols - want:
            _ws_app.send(json.dumps({"type": "unsubscribe", "symbol": t}))
        _ws_symbols = want
    except Exception:
        pass


def _ws_loop():
    """WebSocket 연결 루프 — 끊기면 지수 백오프로 자동 재접속."""
    global _ws_app, _ws_connected, _ws_symbols
    import websocket
    backoff = 2.0
    while True:
        if not _api_key:
            time.sleep(5)
            continue

        def _on_open(ws):
            global _ws_connected, _ws_symbols
            _ws_connected = True
            _ws_symbols = set()
            _ws_sync_symbols()

        def _on_close(_ws, *_a):
            global _ws_connected
            _ws_connected = False

        def _on_error(_ws, err):
            global _last_error, _last_error_ts
            _last_error = f"ws: {err}"[:200]
            _last_error_ts = time.time()

        try:
            _ws_app = websocket.WebSocketApp(
                f"wss://ws.finnhub.io?token={_api_key}",
                on_message=_ws_on_message, on_open=_on_open,
                on_close=_on_close, on_error=_on_error)
            _ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            pass
        _ws_connected = False
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)   # 재연결 성공하면 아래에서 리셋
        if _ws_last_msg and time.time() - _ws_last_msg < 120:
            backoff = 2.0


def _ensure_ws():
    """API 키가 있으면 ws 스레드 기동 (없으면 폴링만으로 동작)."""
    global _ws_thread
    if not _api_key:
        return
    try:
        import websocket  # noqa: F401  (websocket-client)
    except Exception:
        return
    if _ws_thread is None or not _ws_thread.is_alive():
        _ws_thread = threading.Thread(target=_ws_loop, daemon=True,
                                      name="rt-feed-ws")
        _ws_thread.start()


# ── 내부 구현: REST/yfinance 폴링 ────────────────────────────────────────────

def _fetch_finnhub(ticker: str) -> dict | None:
    """Finnhub /quote 호출 → quote dict."""
    if not _api_key:
        return None
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": _api_key},
            timeout=3,
        )
        d = r.json()
        c = d.get("c", 0)
        pc = d.get("pc", 0)
        if not c:
            return None
        return {
            "price":      float(c),
            "prev":       float(pc),
            "open":       float(d.get("o", c)),
            "high":       float(d.get("h", c)),
            "low":        float(d.get("l", c)),
            "change":     float(c - pc),
            "change_pct": float((c - pc) / pc) if pc else 0.0,
            "source":     "finnhub",
            "ts":         time.time(),
        }
    except Exception:
        return None


def _fetch_yf(ticker: str) -> dict | None:
    """yfinance fast_info 폴백."""
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        price = float(getattr(fi, "last_price",       0) or 0)
        prev  = float(getattr(fi, "previous_close",   price) or price)
        high  = float(getattr(fi, "day_high",         price) or price)
        low   = float(getattr(fi, "day_low",          price) or price)
        open_ = float(getattr(fi, "open",             price) or price)
        if not price:
            return None
        return {
            "price":      price,
            "prev":       prev,
            "open":       open_,
            "high":       high,
            "low":        low,
            "change":     price - prev,
            "change_pct": (price - prev) / prev if prev else 0.0,
            "source":     "yfinance",
            "ts":         time.time(),
        }
    except Exception:
        return None


def _poll_one(ticker: str) -> bool:
    """단일 종목 갱신. 성공 True."""
    data = _fetch_finnhub(ticker) or _fetch_yf(ticker)
    if data:
        old = _cache.get(ticker, {})
        # 가격 변동 이력 기록
        if old.get("price") and abs(data["price"] - old["price"]) > 0.001:
            _change_log.append({
                "ticker": ticker,
                "from":   old["price"],
                "to":     data["price"],
                "ts":     data["ts"],
            })
        _cache[ticker] = data
        return True
    return False


def _batch_yf(tickers: list[str]) -> int:
    """yf.download 단일 호출로 전체 시세 배치 갱신. 갱신 종목 수 반환."""
    import yfinance as yf
    if not tickers:
        return 0
    n = 0
    try:
        raw = yf.download(" ".join(tickers), period="2d", interval="1d",
                          auto_adjust=True, progress=False, threads=True)
        if raw is None or raw.empty:
            return 0
        close  = raw["Close"]
        volume = raw.get("Volume")
        high   = raw.get("High")
        low    = raw.get("Low")
        single = not hasattr(close, "columns")

        def _col(df, tk):
            if df is None: return None
            if single: return df
            return df[tk] if tk in getattr(df, "columns", []) else None

        for tk in tickers:
            try:
                c = _col(close, tk)
                if c is None: continue
                c = c.dropna()
                if len(c) < 1: continue
                price = float(c.iloc[-1])
                prev  = float(c.iloc[-2]) if len(c) >= 2 else price
                h = _col(high, tk);  h = float(h.dropna().iloc[-1]) if h is not None and len(h.dropna()) else price
                l = _col(low, tk);   l = float(l.dropna().iloc[-1]) if l is not None and len(l.dropna()) else price
                old = _cache.get(tk, {})
                if old.get("price") and abs(price - old["price"]) > 0.001:
                    _change_log.append({"ticker": tk, "from": old["price"],
                                        "to": price, "ts": time.time()})
                _cache[tk] = {
                    "price": price, "prev": prev, "change": price-prev,
                    "change_pct": (price-prev)/prev if prev else 0,
                    "high": h, "low": l, "open": prev,
                    "source": "yfinance", "ts": time.time(),
                }
                n += 1
            except Exception:
                continue
    except Exception as e:
        global _last_error, _last_error_ts
        _last_error = f"yf.download 실패: {type(e).__name__}: {e}"[:200]
        _last_error_ts = time.time()
    return n


def _run_loop():
    """백그라운드 폴링 루프.
    - Finnhub 키 있으면: 종목당 REST (빠름, 분당 60건 제한 준수)
    - 없으면: yf.download 단일 배치 (수십 종목도 1~2초)
    """
    while True:
        with _lock:
            tickers = sorted(_subscribed, key=lambda t: -_subscribed[t])
        if not tickers:
            time.sleep(1)
            continue

        global _consecutive_fails, _last_success_ts
        if _api_key:
            # Finnhub: 분당 60건 → 종목당 ~1초 간격, 단 상위 우선순위부터
            if len(tickers) <= 50:
                got = 0
                for tk in tickers:
                    if _poll_one(tk): got += 1
                    time.sleep(min(1.0, 60.0 / max(len(tickers), 1)))
            else:
                got = _batch_yf(tickers)
        else:
            got = _batch_yf(tickers)      # 단일 배치 호출

        # 건강 상태 갱신
        if got > 0:
            _consecutive_fails = 0
            _last_success_ts = time.time()
        else:
            _consecutive_fails += 1

        # ws 스트림이 살아있으면 폴링은 보조(전일종가·고저 보강)로 격하 —
        # 호출량 절약. ws 가 죽으면 다시 _interval 로 촘촘히 폴링(폴백).
        if _ws_connected and _ws_last_msg and time.time() - _ws_last_msg < 30:
            time.sleep(max(_interval, 60))
        else:
            time.sleep(_interval)


def _focus_loop():
    """포커스 종목 전용 1초 폴링 루프 — 메인 루프의 다종목 순회와 무관하게 동작."""
    while True:
        now = time.time()
        with _lock:
            active = [t for t, ts in _focus.items() if now - ts <= _FOCUS_TTL]
            stale  = [t for t, ts in _focus.items() if now - ts > _FOCUS_TTL]
            for t in stale:
                _focus.pop(t, None)
        for tk in active:
            _poll_one(tk)
        time.sleep(1.0)


def _ensure_running():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_run_loop, daemon=True, name="rt-feed")
        _thread.start()


def _ensure_focus_running():
    global _focus_thread
    if _focus_thread is None or not _focus_thread.is_alive():
        _focus_thread = threading.Thread(target=_focus_loop, daemon=True, name="rt-focus")
        _focus_thread.start()


# ── 편의 함수: 앱에서 한 번에 초기화 ────────────────────────────────────────

def init_from_config(tickers: list[str], interval: float = 5.0):
    """config.py 의 FINNHUB_API_KEY 를 읽어 자동 초기화."""
    try:
        from config import FINNHUB_API_KEY
        subscribe(tickers, api_key=FINNHUB_API_KEY, interval=interval)
    except Exception:
        subscribe(tickers, interval=interval)
