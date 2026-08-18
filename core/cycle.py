"""자동매매 사이클 1회 — 유일한 자동매매 결정·실행 흐름.

데몬(autotrader.py)과 앱의 '지금 1회 실행'이 같은 함수를 부른다.
순서: 파라미터 주입 → 발굴 → 채점 → 주문 결정 → 일일손실/약세장 방어
      → core.execution.execute_orders → 텔레그램 상태 스냅샷.
"""
from datetime import datetime
from typing import Callable, Optional

from core import scoring
from core.execution import execute_orders


def run_cycle(cfg: dict, log: Optional[Callable[[str], None]] = None) -> dict:
    """설정(cfg = autotrader_config.json 스키마)대로 사이클 1회 실행.

    반환: execute_orders 결과 + {"regime", "top"}.
    """
    import portfolio as _pf
    from portfolio import PortfolioManager
    from scorer import StockScore
    import paper_account as _pa
    import watchlist as wl

    L = log or (lambda m: None)
    paper = cfg.get("paper", True)
    sn = cfg.get("strategy", "composite")
    dynamic = cfg.get("dynamic", True)

    # 청산/매수 파라미터 주입 (투자기간에서 계산된 값)
    _pf.STOP_LOSS_PCT        = float(cfg.get("stop_loss", 0.07))
    _pf.TAKE_PROFIT_PCT      = float(cfg.get("take_profit", 0.15))
    _pf.TRAIL_GIVEBACK_PCT   = float(cfg.get("trail", 0.07))
    _pf.MIN_SCORE_TO_BUY     = int(cfg.get("min_score", 60))
    _pf.SELL_SCORE_THRESHOLD = int(cfg.get("sell_score", 35))
    _pf.HOLD_DAYS_STRONG     = int(cfg.get("hold_strong", 60))
    _pf.HOLD_DAYS_MEDIUM     = int(cfg.get("hold_medium", 30))
    _pf.BUY_PRICE_MIN        = float(cfg.get("buy_price_min", 0) or 0)
    _pf.BUY_PRICE_MAX        = float(cfg.get("buy_price_max", 0) or 0)

    pm = PortfolioManager(paper=paper)
    broker = None
    if not paper:
        from broker import Broker
        broker = Broker(paper=False)

    # 보유종목 워치리스트 동기화 (유니버스 밖 보유가 점수 0으로 강제매도 방지)
    try:
        wl.sync_holdings(list(pm.positions.keys()))
    except Exception:
        pass

    # 시장 자동 발굴 (시간 게이트)
    disc = cfg.get("discover") or {}
    if disc.get("enabled"):
        try:
            import screener
            r = screener.maybe_discover(
                held=list(pm.positions.keys()),
                interval_sec=disc.get("interval", 14400),
                universe_names=disc.get("universe", ["S&P 500"]),
                top_k=disc.get("top_k", 30), cap=disc.get("cap", 50))
            if r and r.get("scanned") and r.get("added"):
                L(f"시장 스캔: 신규 {len(r['added'])}종목 편입 {r['added'][:5]}")
        except Exception as e:
            L(f"발굴 건너뜀: {e}")

    universe = wl.load()
    scores_raw, prices, regime = scoring.score(sn, universe)

    # 실시간 가격 오버레이 — 채점 번들(일봉, 3분 캐시)의 종가 대신 실시간
    # 피드(5초 폴링) 가격으로 갱신. 짧은 사이클(10초~)에서 손절·트레일링이
    # 초 단위 가격 변화에 즉시 반응하게 한다. 피드 실패 시 번들 가격 유지.
    try:
        import realtime_feed as rtf
        try:
            from config import FINNHUB_API_KEY as _fk
        except Exception:
            _fk = ""
        # 보유 종목은 우선순위 2.0 — WebSocket 50종목 한도에서 항상 포함
        # (손절·트레일링이 가장 빠른 가격을 봐야 하는 종목들)
        rtf.subscribe(list(pm.positions.keys()), api_key=_fk,
                      interval=5.0, priority=2.0)
        rtf.subscribe(list(set(universe) - set(pm.positions.keys())),
                      api_key=_fk, interval=5.0)
        _fresh = 0
        for t in list(prices.keys()):
            d = rtf.get_price(t)
            if d and d.get("price"):
                prices[t] = float(d["price"])
                _fresh += 1
        if _fresh:
            L(f"실시간 가격 갱신 {_fresh}/{len(prices)}종목")
    except Exception:
        pass
    scores = [StockScore(r["ticker"], r["score"], 0, 0, 0, 0) for r in scores_raw]

    avail = _pa.cash() if paper else None
    orders = pm.generate_orders(
        scores, prices, dynamic=dynamic,
        buy_mode=cfg.get("buy_mode", "전량"),
        sell_mode=cfg.get("sell_mode", "전량"),
        buy_pct=float(cfg.get("buy_pct", 100)) / 100.0,
        sell_pct=float(cfg.get("sell_pct", 100)) / 100.0,
        available_override=avail)

    # 일일 손실 kill switch
    try:
        import risk_guard as rg
        cur_eq = ((_pa.cash() + pm.invested_capital()) if paper
                  else (pm.invested_capital() + pm.available_capital()))
        last_eq = None
        if broker is not None:
            try:
                acct = broker.get_account()
                if acct.get("equity"):
                    cur_eq = acct["equity"]
                last_eq = acct.get("last_equity")
            except Exception:
                pass
        rgs = rg.check(cur_eq, loss_limit=float(cfg.get("daily_loss_limit", 0.05)),
                       start_equity=last_eq)
        if rgs.get("halted"):
            if orders["buy"]:
                L(f"일일손실 한도 도달({rgs.get('daily_pnl_pct', 0):.1%}) → 매수 중단")
            orders["buy"] = []
    except Exception as e:
        L(f"리스크가드 오류: {e}")

    # 약세장 방어: 신규 매수 보류 (현금 보유)
    if regime == "bear" and orders["buy"]:
        L(f"약세장 → 매수 {len(orders['buy'])}건 보류")
        orders["buy"] = []

    top = scores_raw[0] if scores_raw else None
    n_b, n_s = len(orders["buy"]), len(orders["sell"])
    if not n_b and not n_s:
        L(f"관망 · 국면 {regime} · 상위 "
          f"{top['ticker'] + ' ' + str(top['score']) if top else '-'} · "
          f"현금 ${(_pa.cash() if paper else pm.available_capital()):,.0f}")
    else:
        L(f"주문 · 국면 {regime} · 매수 {n_b} / 매도 {n_s}")

    result = execute_orders(orders, prices, paper, pm, broker=broker, log=L)
    result["regime"] = regime
    result["top"] = scores_raw[:10]
    result["prices"] = {r["ticker"]: prices.get(r["ticker"])
                        for r in scores_raw[:10]}

    # 텔레그램 봇 상태 스냅샷
    try:
        import telegram_bot as tgb
        cash_b = _pa.cash() if paper else pm.available_capital()
        mv = sum(p.shares * prices.get(t, p.entry_price)
                 for t, p in pm.positions.items())
        cost = sum(p.cost_basis for p in pm.positions.values())
        pos_b = [{"ticker": t, "shares": p.shares,
                  "entry": round(p.entry_price, 2),
                  "cur": round(prices.get(t, p.entry_price), 2),
                  "pnl_pct": ((prices.get(t, p.entry_price) - p.entry_price)
                              / p.entry_price if p.entry_price else 0)}
                 for t, p in pm.positions.items()]
        tgb.write_status({
            "auto_on": bool(cfg.get("enabled", True)), "paper": paper,
            "mode": "모의(페이퍼)" if paper else "실거래",
            "strategy": cfg.get("strategy_name", sn), "sn": sn,
            "horizon": cfg.get("horizon", "단기"),
            "alloc": "유동형" if dynamic else "고정형",
            "market_open": True, "last_run": datetime.now().strftime("%H:%M:%S"),
            "equity": round(cash_b + mv), "cash": round(cash_b),
            "invested": round(mv), "upnl": round(mv - cost),
            "upnl_pct": ((mv - cost) / cost if cost else 0),
            "n_positions": len(pm.positions), "positions": pos_b,
            "buy_th": int(cfg.get("min_score", 60)),
            "top_scores": scores_raw[:10],
        })
    except Exception as e:
        L(f"상태기록 건너뜀: {e}")

    return result
