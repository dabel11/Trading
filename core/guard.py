"""틱 구동 리스크 가드 — WebSocket 체결가가 도착하는 즉시 손절/트레일링 평가.

전략 사이클(채점·매수·리밸런싱)은 주기 실행으로 충분하지만, 손절·트레일링은
초가 중요하다. 이 가드는 실시간 피드의 틱 콜백에 붙어 보유 종목 가격이
들어오는 순간 하드 리스크 룰만 평가하고, 발동 시 즉시 전량 매도한다.

설계:
  - ws 스레드 콜백은 큐에 넣기만 (즉시 반환) → 워커 스레드가 평가·체결
  - 점수 기반 매도(score_drop/max_hold)·매수·회전은 다루지 않는다 (사이클 몫)
  - 같은 종목 연속 발동 방지: 30초 쿨다운 + 체결은 TRADE_LOCK 직렬화
  - 설정(autotrader_config.json)은 5초 캐시로 재독 — 앱 변경 즉시 반영
"""
import queue
import threading
import time
from typing import Callable, Optional

from core import control
from core.execution import execute_orders

_q: "queue.Queue[tuple]" = queue.Queue(maxsize=10000)
_worker: threading.Thread | None = None
_started = False
_last_fire: dict = {}            # ticker → 마지막 발동 epoch (쿨다운)
_FIRE_COOLDOWN = 30.0

_cfg_cache: tuple = (0.0, None)  # (ts, cfg)
_CFG_TTL = 5.0

_stats = {"ticks": 0, "evals": 0, "fires": 0}


def _cfg() -> dict:
    global _cfg_cache
    ts, c = _cfg_cache
    now = time.time()
    if c is None or now - ts > _CFG_TTL:
        c = control.load_config()
        _cfg_cache = (now, c)
    return c


def _on_tick(ticker: str, price: float, ts: float):
    """ws 스레드에서 호출 — 큐에만 넣고 즉시 반환."""
    try:
        _q.put_nowait((ticker, price, ts))
    except queue.Full:
        pass


def _evaluate(ticker: str, price: float, log: Callable[[str], None]):
    """보유 종목이면 하드 리스크 룰(손절/트레일링)만 평가, 발동 시 즉시 매도."""
    cfg = _cfg()
    if not cfg.get("enabled"):
        return
    import market_hours as mh
    if not mh.is_market_open():
        return
    paper = cfg.get("paper", True)

    import portfolio as _pf
    from portfolio import PortfolioManager
    pm = PortfolioManager(paper=paper)          # 매 평가 재로드 — 사이클과 정합
    pos = pm.positions.get(ticker)
    if pos is None:
        return
    _stats["evals"] += 1

    # 사이클과 동일한 청산 파라미터 주입
    _pf.STOP_LOSS_PCT      = float(cfg.get("stop_loss", 0.07))
    _pf.TAKE_PROFIT_PCT    = float(cfg.get("take_profit", 0.15))
    _pf.TRAIL_GIVEBACK_PCT = float(cfg.get("trail", 0.07))

    if pos.update_peak(price):
        pm._save_state()                        # 트레일링 기준 고점 즉시 반영
    # 점수는 만점으로 — score_drop/max_hold 는 전략 사이클 몫, 여기선
    # 손절·트레일링(점수 무관 하드 룰)만 발동시킨다.
    reason = pm.should_sell(pos, price, current_score=100.0)
    if not reason or not reason.startswith(("stop_loss", "trailing_stop")):
        return

    now = time.time()
    if now - _last_fire.get(ticker, 0) < _FIRE_COOLDOWN:
        return
    _last_fire[ticker] = now
    _stats["fires"] += 1
    log(f"⚡ 틱 가드 발동: {ticker} @ ${price:.2f} · {reason}")
    execute_orders(
        {"sell": [{"ticker": ticker, "shares": pos.shares,
                   "reason": reason, "est_price": price}], "buy": []},
        {ticker: price}, paper, pm, log=log)


def _run(log: Callable[[str], None]):
    while True:
        try:
            ticker, price, _ts = _q.get()
            _stats["ticks"] += 1
            _evaluate(ticker, price, log)
        except Exception as e:
            try:
                log(f"틱 가드 오류: {e}")
            except Exception:
                pass
            time.sleep(1)


def start(log: Optional[Callable[[str], None]] = None):
    """가드 기동 — 피드 틱 콜백 등록 + 워커 스레드 시작 (중복 호출 안전)."""
    global _worker, _started
    L = log or (lambda m: None)
    if _started and _worker is not None and _worker.is_alive():
        return
    import realtime_feed as rtf
    rtf.on_tick(_on_tick)
    _worker = threading.Thread(target=_run, args=(L,), daemon=True,
                               name="tick-guard")
    _worker.start()
    _started = True
    L("틱 가드 시작 — 체결 틱 수신 즉시 손절/트레일링 평가")


def stats() -> dict:
    return dict(_stats)
