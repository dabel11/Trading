"""자동매매 사이클 1회 — 유일한 자동매매 결정·실행 흐름.

데몬(autotrader.py)과 앱의 '지금 1회 실행'이 같은 함수를 부른다.
순서: 파라미터 주입 → 발굴 → 채점 → EV 판단(매수/대기/보유/매도) → 주문 결정
      → 일일손실/약세장 방어
      → core.execution.execute_orders → 텔레그램 상태 스냅샷.
"""
from datetime import datetime
from typing import Callable, Optional

from core import scoring
from core.execution import execute_orders


def _ai_snapshot() -> Optional[dict]:
    """AI 인플루언서 엔진의 최신 판단 요약 (텔레그램/UI용). 실패 시 None."""
    try:
        from influence import engine as _eng
        return _eng.snapshot(top=12)
    except Exception:
        return None


def _ev_summary(decisions: list) -> Optional[dict]:
    try:
        from core import ev as _ev
        return _ev.summary(decisions, top=10)
    except Exception:
        return None


def _market_open() -> bool:
    """텔레그램 상태용 실제 장 상태(예전엔 True 고정 — 수동 실행·장외에도 '개장' 표시)."""
    try:
        import market_hours as mh
        return bool(mh.is_market_open())
    except Exception:
        return False


def _mode_label(paper: bool, broker=None) -> str:
    """'실거래' 모드라도 PK(페이퍼) 키면 주문은 Alpaca 페이퍼 계좌로 간다(broker._paper_endpoint)."""
    if paper:
        return "모의"
    return "Alpaca 페이퍼" if getattr(broker, "paper", False) else "🔴 실거래"


def mark_to_market_equity(pm, prices: dict, paper: bool) -> float:
    """킬스위치용 시가평가 자산 = 현금 + Σ(보유수량 × 현재가).

    원가(cost basis)로 합치면 평가손실이 안 보여 '실제 -15%인데 손실 0%'가 된다.
    현재가가 없는 종목만 진입가로 대체한다. (실거래는 호출부가 브로커 equity 로 덮어씀)"""
    import paper_account as _pa
    mv = sum(p.shares * float(prices.get(t) or p.entry_price)
             for t, p in pm.positions.items())
    cash = _pa.cash() if paper else pm.available_capital()
    return cash + mv


def run_cycle(cfg: dict, log: Optional[Callable[[str], None]] = None) -> dict:
    """설정(cfg = autotrader_config.json 스키마)대로 사이클 1회 실행.

    반환: execute_orders 결과 + {"regime", "top"}.
    성공/실패는 monitor 에 기록된다(장중 멈춤 경보·일일 리포트의 근거).
    """
    try:
        result = _run_cycle(cfg, log)
    except Exception as e:
        _record(False, str(e))
        raise
    _record(True)
    return result


def _record(ok: bool, error: str = ""):
    try:
        import monitor
        monitor.record_cycle(ok, error)
    except Exception:
        pass


def _run_cycle(cfg: dict, log: Optional[Callable[[str], None]] = None) -> dict:
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
    # 장부 손상 fail-closed (감사 #13) — 손상 흔적이 있으면 매매 중단(by="ledger").
    # 모의는 자동 주문 전부 보류, 실거래는 아래 대조·execute_orders 가 매수만 막는다.
    if paper:
        try:
            _pa.cash()     # 손상 파일이면 이 읽기에서 .corrupt 로 옮겨진다 → 아래 검사가 잡는다
        except Exception:
            pass
    try:
        from core import ledger_guard
        from core.runner import alert as _alert
        lg = ledger_guard.check(bool(paper), log=L, alert=_alert)
    except Exception as e:
        L(f"장부 검사 실패 — {'자동 주문 보류' if paper else '대조 단계로 진행'}: {e}")
        lg = {"halted": True, "block_all": bool(paper), "reason": str(e)}
    if lg.get("block_all"):
        return {"bought": [], "sold": [], "skipped": [], "halted": True,
                "halt_reason": lg.get("reason", ""), "regime": None, "top": [], "prices": {}}
    broker = None
    blocked: set = set()
    halted = None
    import market_hours as _mh
    cycle_id = "c" + _mh.now_et().strftime("%H%M%S")
    if not paper:
        from broker import Broker
        from core.execution import reconcile_live
        broker = Broker(paper=False)
        # 브로커 대조 — 크래시로 남은 미해결 주문 확정·장부 반영, 불일치면 매매 중단.
        # 조회 실패는 예외로 올려 러너의 연속 실패 백오프·경보가 처리한다.
        rec = reconcile_live(broker, pm, cfg, log=L)
        if rec["halted"]:
            halted = rec
            L("브로커 대조 불일치 — 매수 중단, 손절·청산 매도만 진행(일치하면 자동 재개)")
        blocked = set(rec.get("blocked") or [])
        if blocked:
            L(f"열린 주문이 있는 종목은 이번 사이클 제외: {sorted(blocked)}")

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
    # AI 전략: 전체 시장 스캔(S&P500+나스닥100) 상위 매수 종목을 대상에 편입
    if sn == "ai_influencer" and cfg.get("ai_full_market", True):
        try:
            from influence import scanner as _sc
            k = int(cfg.get("ai_top_k", 30))
            extra = _sc.top_buys(k, float(cfg.get("min_score", 60)))
            add = [t for t in extra if t not in universe]
            if add:
                universe = list(universe) + add
                L(f"전체 시장 스캔 상위 {len(add)}종목 편입 → 대상 {len(universe)}종목")
        except Exception:
            pass
    universe = list(dict.fromkeys(list(universe) + list(pm.positions.keys())))
    scores_raw, prices, regime = scoring.score(sn, universe, log=L)
    # 가격별 시각(실시간 피드 오버레이분만). 없는 종목은 채점 번들 가격 = 신선도 미상 →
    # 실거래 게이트가 브로커 호가로 재확인한다.
    price_ts: dict = {}

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
                if d.get("ts"):
                    price_ts[t] = float(d["ts"])
                _fresh += 1
        if _fresh:
            L(f"실시간 가격 갱신 {_fresh}/{len(prices)}종목")
    except Exception:
        pass
    # EV(기대값) 판단 — 매수 후보·보유 종목별 '매수/대기/보유/매도'.
    # 주문 로직이 읽는 점수만 조정(대기 → 매수 문턱 아래, 매도 → 매도 문턱 아래)해
    # 기존 청산·하드캡·이중매도 방지를 그대로 거친다. 표시용 scores_raw 는 원점수 유지.
    ev_decisions: list = []
    gated = {r["ticker"]: r["score"] for r in scores_raw}
    if cfg.get("ev_enabled", True):
        try:
            from core import ev as _ev
            ev_decisions = _ev.decide_live(cfg, scores_raw, prices, pm.positions, log=L)
            gated = _ev.gate_scores(gated, ev_decisions, _ev.EVParams.from_cfg(cfg))
        except Exception as e:
            L(f"EV 판단 건너뜀(점수만으로 진행): {e}")
    ev_map = {d.ticker: d for d in ev_decisions}
    scores = [StockScore(r["ticker"], gated.get(r["ticker"], r["score"]), 0, 0, 0, 0)
              for r in scores_raw]

    avail = _pa.cash() if paper else None
    orders = pm.generate_orders(
        scores, prices, dynamic=dynamic,
        buy_mode=cfg.get("buy_mode", "전량"),
        sell_mode=cfg.get("sell_mode", "전량"),
        buy_pct=float(cfg.get("buy_pct", 100)) / 100.0,
        sell_pct=float(cfg.get("sell_pct", 100)) / 100.0,
        available_override=avail)

    # EV 매도는 점수 조정으로 기존 청산 경로를 탔으므로 장부·알림의 사유를 EV 로 바로잡는다
    for o in orders.get("sell", []):
        d = ev_map.get(o.get("ticker"))
        if d is not None and d.action == "SELL" and str(o.get("reason", "")).startswith("score_drop"):
            o["reason"] = f"ev_exit (보유 EV {d.ev:+.1%})"

    # 일일 손실 kill switch
    try:
        import risk_guard as rg
        cur_eq = mark_to_market_equity(pm, prices, paper)
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

    if blocked:
        orders = {k: [o for o in v if o["ticker"] not in blocked]
                  for k, v in orders.items()}
    if halted and orders["buy"]:
        L(f"대조 불일치 → 매수 {len(orders['buy'])}건 보류")
        orders["buy"] = []

    top = scores_raw[0] if scores_raw else None
    n_b, n_s = len(orders["buy"]), len(orders["sell"])
    if not n_b and not n_s:
        L(f"관망 · 국면 {regime} · 상위 "
          f"{top['ticker'] + ' ' + str(top['score']) if top else '-'} · "
          f"현금 ${(_pa.cash() if paper else pm.available_capital()):,.0f}")
    else:
        L(f"주문 · 국면 {regime} · 매수 {n_b} / 매도 {n_s}")

    result = execute_orders(orders, prices, paper, pm, broker=broker, log=L,
                            cycle_id=cycle_id, price_ts=price_ts, cfg=cfg)
    result["regime"] = regime
    if halted:
        result["halted"], result["reconcile"] = True, halted
    result["top"] = scores_raw[:10]
    result["prices"] = {r["ticker"]: prices.get(r["ticker"])
                        for r in scores_raw[:10]}

    # 체결 발생 시 텔레그램 능동 알림(소유자 등록돼 있으면)
    try:
        bought, sold = result.get("bought") or [], result.get("sold") or []
        if bought or sold:
            import telegram_bot as tgb
            mode = _mode_label(paper, broker)
            eng = "🧠 AI 인플루언서" if sn == "ai_influencer" else cfg.get("strategy_name", sn)
            lines = [f"🤖 자동매매 체결 · {mode}", f"{eng} · 국면 {regime}"]
            for b in bought:
                d = ev_map.get(b["ticker"])
                evs = f" · EV {d.ev:+.1%} 승률 {d.p_win:.0%}" if d and d.sigma else ""
                lines.append(f"🔴 매수 {b['ticker']} {b['shares']}주 @ ${b['price']:.2f}{evs}")
            for s in sold:
                lines.append(f"🔵 매도 {s['ticker']} {s['shares']}주 @ ${s['price']:.2f} "
                             f"({s.get('pnl_pct', 0):+.1%})")
            cash_now = _pa.cash() if paper else pm.available_capital()
            lines.append(f"💰 현금 ${cash_now:,.0f}")
            tgb.push("\n".join(lines))
    except Exception as e:
        L(f"알림 건너뜀: {e}")

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
            "mode": "모의(페이퍼)" if paper else _mode_label(paper, broker).replace("🔴 ", ""),
            "strategy": cfg.get("strategy_name", sn), "sn": sn,
            "horizon": cfg.get("horizon", "단기"),
            "alloc": "유동형" if dynamic else "고정형",
            "market_open": _market_open(), "last_run": datetime.now().strftime("%H:%M:%S"),
            "equity": round(cash_b + mv), "cash": round(cash_b),
            "invested": round(mv), "upnl": round(mv - cost),
            "upnl_pct": ((mv - cost) / cost if cost else 0),
            "n_positions": len(pm.positions), "positions": pos_b,
            "buy_th": int(cfg.get("min_score", 60)),
            "top_scores": scores_raw[:10],
            "engine": "ai_influencer" if sn == "ai_influencer" else "strategy",
            "ai": (_ai_snapshot() if sn == "ai_influencer" else None),
            "ev": (_ev_summary(ev_decisions) if ev_decisions else None),
        })
    except Exception as e:
        L(f"상태기록 건너뜀: {e}")

    return result
