"""전체 시장 AI 스캔 — S&P 500 + 나스닥 100 전 종목을 백그라운드로 AI 판단.

워치리스트(소수)만 보던 한계를 없앤다. Finnhub 무료 한도(분당 60회)를 지키려고
저우선 속도 제한으로 천천히 돌며(첫 회 약 30분, 이후 캐시로 10분 내외), 판단 결과는
종목별로 즉시 FULL 에 반영된다. 자동매매는 이 결과의 상위 매수 종목을 대상에 편입한다.
배치마다 판단을 시점 그대로 influence_log 에 기록한다(src="scanner", 스키마는 engine).
"""
from __future__ import annotations

import threading
import time

import ai_judge
from influence import providers

FULL: dict = {}        # TK -> Verdict (최신)
STATE: dict = {"running": False, "total": 0, "done": 0, "started": 0.0,
               "finished": 0.0, "passes": 0, "error": ""}
_LOCK = threading.Lock()
_STARTED = {"on": False}
_KICK = threading.Event()


def market_universe() -> list:
    """스캔 대상: S&P 500 + 나스닥 100 (실패 시 종목 DB의 미국 종목)."""
    tickers: list = []
    try:
        import universe as uni
        tickers = list(uni.get_combined(["S&P 500", "나스닥 100"]))
    except Exception:
        tickers = []
    if len(tickers) < 100:
        try:
            import stock_browser as sb
            db, _ = sb._get_db()
            tickers = [t for t, _n, _e, sec in db if sec not in ("국내", "ETF", "")]
        except Exception:
            pass
    out = []
    for t in dict.fromkeys(str(x).upper().strip() for x in tickers):
        if t and t.replace("-", "").replace(".", "").isalnum() and len(t) <= 6:
            out.append(t)
    return out


def _thresholds():
    try:
        from core.control import load_config
        c = load_config()
        return float(c.get("min_score", 60)), float(c.get("sell_score", 35))
    except Exception:
        return 60.0, 35.0


def scan_once(batch: int = 8):
    tickers = market_universe()
    with _LOCK:
        STATE.update(running=True, total=len(tickers), done=0,
                     started=time.time(), error="")
    providers.set_low_priority(True)
    buy_th, sell_th = _thresholds()
    try:
        for i in range(0, len(tickers), batch):
            chunk = tickers[i:i + batch]
            t0 = time.time()
            items = []
            for t in chunk:
                try:
                    ev = providers.gather(t)
                except Exception:
                    ev = []
                items.append((t, ev, {"held": False, "regime": "neutral"}))
            try:
                verdicts = ai_judge.judge_batch(items, buy_th=buy_th, sell_th=sell_th)
            except Exception:
                verdicts = []
            with _LOCK:
                for v in verdicts:
                    FULL[v.ticker] = v
                STATE["done"] = min(len(tickers), i + len(chunk))
            try:
                from influence import engine as _eng
                _eng.log_judgment(t0, set(), "neutral", buy_th, sell_th, verdicts,
                                  src="scanner")
            except Exception as e:
                print(f"[scanner] AI 판단 기록 실패(스캔 영향 없음): "
                      f"{type(e).__name__}: {e}", flush=True)
    except Exception as e:
        STATE["error"] = str(e)[:120]
    finally:
        with _LOCK:
            STATE.update(running=False, finished=time.time(),
                         passes=STATE.get("passes", 0) + 1)


def _loop(interval_hours: float):
    while True:
        try:
            scan_once()
        except Exception as e:
            STATE["error"] = str(e)[:120]
        _KICK.wait(timeout=max(600.0, interval_hours * 3600.0))
        _KICK.clear()


def start(interval_hours: float = 3.0) -> bool:
    """백그라운드 전체 스캔 시작(멱등)."""
    if _STARTED["on"]:
        return False
    _STARTED["on"] = True
    threading.Thread(target=_loop, args=(interval_hours,), daemon=True).start()
    return True


def rescan_now():
    """대기 중이면 즉시 다음 스캔을 시작."""
    if not STATE.get("running"):
        _KICK.set()


def merge(verdicts: dict):
    """다른 경로(자동매매 사이클 등)의 최신 판단도 FULL 에 합친다."""
    with _LOCK:
        for t, v in (verdicts or {}).items():
            cur = FULL.get(t)
            # 근거 없는(시간초과·무응답) 판단이 근거 있는 기존 판단을 덮지 않게
            if cur is not None and cur.evidence and not v.evidence:
                continue
            FULL[t] = v


def ranked(limit: int = 50) -> list:
    with _LOCK:
        vs = list(FULL.values())
    vs.sort(key=lambda v: -v.score)
    return vs[:limit]


def top_buys(k: int, buy_th: float) -> list:
    return [v.ticker for v in ranked(10_000) if v.score >= buy_th][:k]


def summary(buy_th: float, sell_th: float) -> dict:
    with _LOCK:
        vs = list(FULL.values())
        st = dict(STATE)
    n_buy = sum(1 for v in vs if v.score >= buy_th)
    n_sell = sum(1 for v in vs if v.score <= sell_th)
    return {"judged": len(vs), "buy": n_buy, "sell": n_sell,
            "hold": len(vs) - n_buy - n_sell, **st}
