"""단일 주문 실행기 — 모든 매매(데몬 자동·앱 수동·텔레그램)가 이 깔때기를 지난다.

여기 한 곳에서: 자금 게이팅 → (실거래) 브로커 체결·검증 → 장부 기록 →
모의 현금 조정 → orders_log 기록 → 알림. 경로별로 실행 코드가 중복돼
동작이 어긋나던 문제(데몬 거래가 주문 내역에 안 남던 것 등)를 없앤다.

실거래(paper=False) 경로의 멱등성·안전:
  - 주문 의도 저널(core.intents): 주문 '전에' PENDING 으로 기록하고, client_order_id 는
    의도(ET 거래일·사이클·종목·방향)에서 결정적으로 만든다.
  - 미해결 의도가 있는 종목·방향은 새 주문을 내지 않는다.
  - 장부 반영은 의도의 '누적 체결 − 이미 반영' 만큼만 (부분 체결·취소 후 체결 포함).
  - 사전 리스크 게이트(pre_trade_check): 주문당 최대 금액 · 일 최대 주문 수 ·
    가격 신선도 · 매도 ≤ 브로커 보유 − 열린 매도 주문.
  - 사이클 시작 대조(reconcile_live): 브로커가 진실 — 미해결 의도를 브로커 상태로
    확정·반영하고, 허용치를 넘는 장부↔브로커 불일치면 매수 중단 + 경보
    (손절·청산 매도는 브로커 보유 한도 안에서 계속).
"""
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import market_hours as _mh
from core import intents as _it
from safe_store import atomic_write_json, safe_read_json, trade_lock

DIR = Path(__file__).resolve().parent.parent
ORDERS_FILE = DIR / "orders_log.json"
_ORDERS_MAX = 2000          # 무한 증가 방지: 최근 N건만 유지

# 거래 직렬화 — 같은 장부를 동시에 쓰는 레이스 방지.
# 프로세스 내부(전략 사이클 메인 스레드 ↔ 틱 가드 워커 스레드)뿐 아니라
# 프로세스 사이(데몬 ↔ 앱)까지 trade_lock 으로 직렬화한다. 자금 게이팅부터
# 브로커 체결·장부 기록·현금 조정까지의 전 구간을 한 락에 묶어 TOCTOU 를 막는다.
# (구버전 호환 별칭 — 외부에서 참조하던 이름 유지)
TRADE_LOCK = threading.RLock()


def _locked(fn):
    def wrapper(*a, **kw):
        with trade_lock():
            return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def log_order(ticker, side, shares, price, source="manual", *,
              score=None, pnl_pct=None, reason="", notify=True,
              balance=None):
    """개별 체결 이벤트 기록(차트 마커·최근 주문 내역) + 알림 발송.

    모든 거래의 단일 길목 — 알림이 경로마다 빠지거나 중복되지 않는다.
    side: 'buy'|'sell'. balance: 알림에 표기할 잔액(없으면 모의는 자동 계산).
    """
    data = safe_read_json(ORDERS_FILE, default={"orders": []})
    orders = data.setdefault("orders", [])
    orders.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": _mh.trading_day().isoformat(),
        "ticker": ticker, "side": side,
        "shares": float(shares), "price": float(price),
        "source": source,
    })
    if len(orders) > _ORDERS_MAX:
        data["orders"] = orders[-_ORDERS_MAX:]
    atomic_write_json(ORDERS_FILE, data)

    if not notify:
        return

    # 잔액 조회는 알림과 분리 — 알림 실패가 잔액 계산 예외까지 함께 삼켜
    # "왜 잔액이 틀리지/알림이 안 오지"를 추적 불가능하게 만들던 것을 막는다.
    bal = balance
    if side == "buy" and bal is None and source == "paper":
        try:
            import paper_account as _pa
            bal = _pa.cash()
        except Exception as e:
            print(f"[execution] 잔액 조회 실패: {e}", flush=True)

    try:
        import notifier
        slbl = {"auto": "자동", "paper": "모의", "manual": "수동"}.get(source, "")
        if side == "buy":
            notifier.notify_buy(ticker, int(shares), float(price), score,
                                float(shares) * float(price), source=slbl,
                                balance=bal)
        else:
            notifier.notify_sell(ticker, int(shares), float(price), pnl_pct,
                                 reason or "manual", source=slbl)
    except Exception as e:
        print(f"[execution] 알림 발송 실패: {e}", flush=True)


# ── 실거래 사전 리스크 게이트 ─────────────────────────────────────────────

# 설정(autotrader_config.json)에 같은 키가 있으면 그 값을 쓴다.
RISK_DEFAULTS = {
    "max_order_usd": 0,          # 매수 1건 최대 금액. 0 = CAPITAL_TOTAL
    "max_orders_per_day": 40,    # ET 거래일당 낸 주문 의도 수 상한 (매수에만 적용)
    "max_price_age_sec": 120,    # 이보다 오래된 가격이면 브로커 호가로 재확인
    "max_requote": 0.02,         # 자동 매수: 재확인 가격이 결정 가격과 이만큼 다르면 거부
    "reconcile_tol_shares": 0,   # 대조 허용 오차(주)
    "reconcile_ignore": [],      # 대조에서 뺄 종목(앱 밖에서 직접 관리하는 보유)
}


def risk_limits(cfg: Optional[dict] = None) -> dict:
    """리스크 한도. cfg 를 안 주면 설정 파일이 있을 때만 읽는다(없는 파일을 만들지 않음)."""
    if cfg is None:
        try:
            from core import control
            cfg = control.load_config() if control.CONFIG_FILE.exists() else {}
        except Exception:
            cfg = {}
    lim = {k: cfg.get(k, v) for k, v in RISK_DEFAULTS.items()}
    if not lim["max_order_usd"]:
        from config import CAPITAL_TOTAL
        lim["max_order_usd"] = float(CAPITAL_TOTAL)
    return lim


def _price_age(ticker: str, price_ts: Optional[dict]) -> float:
    """결정에 쓴 가격의 나이(초). price_ts 가 없으면 실시간 피드 갱신 시각으로 본다."""
    ts = None
    if price_ts is not None:
        ts = price_ts.get(ticker)
    else:
        try:
            import realtime_feed as _rtf
            ts = _rtf.last_updated(ticker)
        except Exception:
            ts = None
    return (time.time() - float(ts)) if ts else float("inf")


def pre_trade_check(side: str, ticker: str, qty: int, est_price: float, *,
                    broker, journal: dict, limits: dict,
                    price_ts: Optional[dict] = None, manual: bool = False) -> dict:
    """실거래 주문 1건의 사전 검사. 반환 {"ok", "reason", "qty", "est_price"}.

    - 매매 중단(대조 불일치) 중: 매수 거부. 매도(자동·수동)는 아래 브로커 보유 검사로 허용
    - 같은 종목·방향에 미해결 의도가 있으면 거부
    - 가격 신선도: 오래된 가격이면 브로커 호가로 재확인. 매수는 재확인 실패 시 거부,
      자동 매수는 결정 가격과 max_requote 넘게 다르면 거부(수동은 금액 계산만 보수적으로).
      매도는 거부하지 않고 새 호가로 지정가를 다시 잡는다(위험을 줄이는 주문).
    - 매수: 주문당 최대 금액 · 일 최대 주문 수
    - 매도: 브로커 보유 − 열린 매도 주문으로 수량을 자른다(숏 방지). 조회 실패면 거부.
    """
    est = float(est_price or 0)
    qty = int(qty)

    def no(reason):
        return {"ok": False, "reason": reason, "qty": 0, "est_price": est}

    h = _it.halted(journal)
    if h and side == "buy":
        return no(f"매매 중단 중(매수 금지) — {h.get('reason')}")
    if _it.unresolved(journal, ticker, side):
        return no("미해결 주문 의도 있음 — 브로커 대조로 확정될 때까지 새 주문 안 함")

    ref = est
    age = _price_age(ticker, price_ts)
    if age > float(limits["max_price_age_sec"]):
        try:
            fresh = float((broker.get_prices([ticker]) or {}).get(ticker) or 0)
        except Exception:
            fresh = 0.0
        if side == "buy":
            if not fresh:
                return no("가격 신선도 미달(재확인 실패)")
            if manual:
                ref = max(est, fresh)
            else:
                if not est or abs(fresh - est) / est > float(limits["max_requote"]):
                    return no(f"결정 가격이 낡음(${est:.2f} → 현재 ${fresh:.2f})")
        elif fresh:
            est = ref = fresh

    if side == "buy":
        notional = qty * ref
        if notional > float(limits["max_order_usd"]) + 1e-6:
            return no(f"주문당 최대 금액 초과 (${notional:,.0f} > "
                      f"${float(limits['max_order_usd']):,.0f})")
        n = _it.count_today(journal)
        if n >= int(limits["max_orders_per_day"]):
            return no(f"일 최대 주문 수 도달({n}/{int(limits['max_orders_per_day'])})")
    else:
        try:
            held = float((broker.get_positions(strict=True).get(ticker) or {})
                         .get("shares") or 0)
            open_sell = sum(max(0.0, o["qty"] - o["filled_qty"])
                            for o in broker.get_open_orders()
                            if o["symbol"] == ticker.upper() and o["side"] == "sell")
        except Exception as e:
            return no(f"브로커 보유 조회 실패 — 매도 보류(숏 방지): {e}")
        avail = int(held - open_sell + 1e-9)
        if avail < 1:
            return no(f"브로커 매도 가능 수량 없음(보유 {held:g} · 열린 매도 "
                      f"{open_sell:g}) — 숏 방지")
        qty = min(qty, avail)
    return {"ok": True, "reason": "", "qty": qty, "est_price": est}


def _num(q: float):
    return int(q) if float(q).is_integer() else float(q)


def _live_order(broker, j: dict, *, ticker: str, side: str, qty: int,
                est_price: float, cycle_id: str, source: str, reason: str = "",
                score: float = 0.0, market: bool = False):
    """의도 1건을 저널에 먼저 남기고 브로커로 실행. (res, rec) — 같은 의도가 이미
    있으면 (None, 기존 rec) (같은 사이클의 재생 = 재주문 안 함)."""
    iid = _it.make_intent_id(cycle_id, ticker, side)
    if iid in j["intents"]:
        return None, j["intents"][iid]
    rec = _it.open_intent(j, iid, ticker=ticker, side=side, qty=qty,
                          est_price=est_price, source=source, reason=reason,
                          score=score, cycle_id=cycle_id)
    _it.save(j)                                   # write-ahead — 주문보다 먼저 디스크에

    def on_event(ev, child):
        _it.upsert_child(rec, child)
        _it.refresh(rec, final=False)
        _it.save(j)

    try:
        res = broker.execute_order(ticker, qty, side, est_price,
                                   coid_base=_it.coid_base(iid), market=market,
                                   on_event=on_event)
    finally:
        _it.save(j)
    for c in res.get("orders") or []:
        _it.upsert_child(rec, c)
    _it.refresh(rec, final=True)                  # 열린 자식이 남았으면 진행 상태 유지
    _it.save(j)
    return res, rec


def _book(rec: dict, pm) -> tuple:
    """의도의 '누적 체결 − 이미 반영' 을 장부에 반영. (반영 수량, 평균가)."""
    q, px = _it.unbooked(rec)
    if q <= 0:
        return 0, 0.0
    if rec["side"] == "buy":
        pm.record_buy(rec["ticker"], _num(q), px, rec.get("score", 0))
    else:
        pm.record_sell(rec["ticker"], exit_price=px,
                       reason=rec.get("reason") or "manual", shares=_num(q))
    _it.mark_booked(rec, q, px)
    return _num(q), px


def _load_journal(L) -> Optional[dict]:
    try:
        return _it.load()
    except _it.JournalError as e:
        L(f"{e} — 실거래 주문 중단")
        return None


@_locked
def execute_orders(orders: dict, prices: dict, paper: bool, pm,
                   broker=None, source: Optional[str] = None,
                   log: Optional[Callable[[str], None]] = None,
                   gap_sec: float = 0.0, *, cycle_id: Optional[str] = None,
                   price_ts: Optional[dict] = None,
                   cfg: Optional[dict] = None) -> dict:
    """generate_orders 결과({"sell": [...], "buy": [...]})를 실행한다.

    paper=True  → 브로커 없이 현재가 로컬 체결 + 가상 현금 조정
    paper=False → 주문 의도 기록 → 사전 리스크 게이트 → broker.execute_order →
                  의도의 실제 누적 체결만 장부 기록 (매도는 미체결이면 추격)
    log: 진행 메시지 콜백 (데몬은 파일 로그, 앱은 라이브 로그)
    cycle_id: 의도 ID 에 들어갈 사이클 식별자(없으면 출처+ET 시각)
    price_ts: {ticker: 가격 epoch} — 가격 신선도 판단용(없으면 실시간 피드 시각)
    cfg: 리스크 한도를 읽을 설정(없으면 설정 파일)
    반환: {"sold": [..], "bought": [..], "skipped": [..], "pending": [..]} (체결 내역,
          pending = 브로커에 열린 주문이 남아 다음 대조에서 확정될 종목)
    """
    import paper_account as _pa
    L = log or (lambda m: None)
    src = source or ("paper" if paper else "auto")
    out = {"sold": [], "bought": [], "skipped": [], "pending": []}

    # 락 안에서 장부를 최신화 — 결정(generate_orders)과 실행 사이 다른 프로세스가
    # 거래했을 수 있다. 가용자금 게이트가 묵은 스냅샷을 보고 과매수하는 것 방지.
    try:
        pm._load_state()
    except Exception:
        pass

    j = lim = None
    cyc = cycle_id
    if not paper:
        j = _load_journal(L)
        if j is None:
            out["halted"] = True
            return out
        h = _it.halted(j)
        if h:
            # 대조 불일치 중: 위험을 늘리는 매수만 막는다. 손절·청산 매도는 계속
            # (pre_trade_check 가 브로커 보유 − 열린 매도로 수량을 잘라 숏은 막는다).
            out["halted"] = True
            if orders.get("buy"):
                L(f"매매 중단 중 — 매수 {len(orders['buy'])}건 보류"
                  f"(손절·청산 매도는 진행) · {h.get('reason')}")
                orders = {**orders, "buy": []}
        lim = risk_limits(cfg)
        cyc = cycle_id or _it.default_cycle_id(src)
        if broker is None:
            from broker import Broker
            broker = Broker(paper=False)

    # ── 매도 먼저 (자본 회수 → 매수 여력 확보) ───────────────────────────
    for o in orders.get("sell", []):
        try:
            # 결정(generate_orders)은 락 밖의 묵은 스냅샷 기준이다. 그 사이 틱 가드·
            # 수동·텔레그램 매도가 먼저 팔았을 수 있으므로, 락 안에서 다시 읽은 장부로
            # 재검증한다: 미보유면 건너뛰고(이중 매도 → 모의 유령 현금 / 실거래 공매도
            # 방지), 수량은 현재 보유량으로 자른다.
            pos = pm.positions.get(o["ticker"])
            if pos is None or int(pos.shares) < 1:
                L(f"매도 건너뜀(이미 미보유) {o['ticker']}")
                out["skipped"].append(o["ticker"]); continue
            fp = prices.get(o["ticker"], o["est_price"])
            qty = min(int(o["shares"]), int(pos.shares))
            if qty < 1:
                out["skipped"].append(o["ticker"]); continue
            if not paper:
                from broker import validate_fill
                g = pre_trade_check("sell", o["ticker"], qty, o["est_price"],
                                    broker=broker, journal=j, limits=lim,
                                    price_ts=price_ts)
                if not g["ok"]:
                    L(f"매도 보류 {o['ticker']}: {g['reason']}")
                    out["skipped"].append(o["ticker"]); continue
                res, rec = _live_order(broker, j, ticker=o["ticker"], side="sell",
                                       qty=g["qty"], est_price=g["est_price"],
                                       cycle_id=cyc, source=src, reason=o["reason"])
                if res is None:
                    L(f"매도 중복 차단(같은 의도 이미 실행): {o['ticker']}")
                    out["skipped"].append(o["ticker"]); continue
                if res.get("note"):
                    L(f"{o['ticker']}: {res['note']}")
                v = validate_fill(res, est_price=g["est_price"], req_shares=g["qty"])
                pnl_px = res.get("fill_price") or fp
                pnl = ((pnl_px - pos.entry_price) / pos.entry_price
                       if pos.entry_price else 0.0)
                qty, fp = _book(rec, pm)
                _it.save(j)
                if _it.is_unresolved(rec):
                    out["pending"].append(o["ticker"])
                    L(f"{o['ticker']}: 열린 매도 주문 추적 중 — 다음 대조에서 확정")
                if qty <= 0:
                    L(f"매도 보류 {o['ticker']}: {v['warning'] or '체결 없음'}")
                    out["skipped"].append(o["ticker"]); continue
                if v["warning"]:
                    L(f"{o['ticker']}: {v['warning']}")
            else:
                pnl = ((fp - pos.entry_price) / pos.entry_price
                       if pos and pos.entry_price else 0.0)
                sold = pm.record_sell(o["ticker"], exit_price=fp, reason=o["reason"],
                                      shares=qty)
                qty = int(sold or 0)
                if qty < 1:
                    out["skipped"].append(o["ticker"]); continue
                _pa.adjust(qty * fp)
            log_order(o["ticker"], "sell", qty, fp, source=src,
                      pnl_pct=pnl, reason=o["reason"])
            L(f"매도 체결: {o['ticker']} {qty}주 @ ${fp:.2f} · {o['reason']}")
            out["sold"].append({"ticker": o["ticker"], "shares": qty,
                                "price": fp, "pnl_pct": pnl})
        except Exception as e:
            L(f"매도 실패 {o['ticker']}: {e}")

    if gap_sec and orders.get("sell") and orders.get("buy"):
        time.sleep(gap_sec)

    # ── 매수 (자금 게이팅: 모의=가상 현금, 실거래=장부 가용자금 ∧ 브로커 buying_power) ──
    bp = None
    if not paper and orders.get("buy"):
        try:
            bp = float(broker.get_account().get("buying_power"))
        except Exception:
            bp = None
    for o in orders.get("buy", []):
        try:
            price = prices.get(o["ticker"], o["est_price"])
            qty = int(o["shares"])
            need = o.get("est_cost") or qty * price
            avail = _pa.cash() if paper else pm.available_capital()
            if bp is not None:
                avail = min(avail, bp)
            if need > avail + 1e-6:
                L(f"매수 보류(자금부족) {o['ticker']} 필요 ${need:,.0f}/가용 ${avail:,.0f}")
                out["skipped"].append(o["ticker"]); continue
            if not paper:
                from broker import validate_fill
                g = pre_trade_check("buy", o["ticker"], qty, o["est_price"],
                                    broker=broker, journal=j, limits=lim,
                                    price_ts=price_ts)
                if not g["ok"]:
                    L(f"매수 보류 {o['ticker']}: {g['reason']}")
                    out["skipped"].append(o["ticker"]); continue
                res, rec = _live_order(broker, j, ticker=o["ticker"], side="buy",
                                       qty=qty, est_price=o["est_price"],
                                       cycle_id=cyc, source=src,
                                       score=o.get("score", 0))
                if res is None:
                    L(f"매수 중복 차단(같은 의도 이미 실행): {o['ticker']}")
                    out["skipped"].append(o["ticker"]); continue
                if res.get("note"):
                    L(f"{o['ticker']}: {res['note']}")
                v = validate_fill(res, est_price=o["est_price"], req_shares=qty)
                qty, price = _book(rec, pm)
                _it.save(j)
                if _it.is_unresolved(rec):
                    out["pending"].append(o["ticker"])
                    L(f"{o['ticker']}: 미확정 매수 주문 — 다음 대조에서 확정")
                if qty <= 0:
                    L(f"매수 보류 {o['ticker']}: {v['warning'] or '체결 없음'}")
                    out["skipped"].append(o["ticker"]); continue
                if v["warning"]:
                    L(f"{o['ticker']}: {v['warning']}")
                if bp is not None:
                    bp -= qty * price
            else:
                pm.record_buy(o["ticker"], qty, price, o.get("score", 0))
                _pa.adjust(-qty * price)
            bal = _pa.cash() if paper else None
            log_order(o["ticker"], "buy", qty, price, source=src,
                      score=o.get("score"), balance=bal)
            L(f"매수 체결: {o['ticker']} {qty}주 @ ${price:.2f}"
              f" (스코어 {o.get('score', 0):.0f})"
              + (f" · 잔액 ${bal:,.0f}" if bal is not None else ""))
            out["bought"].append({"ticker": o["ticker"], "shares": qty,
                                  "price": price})
        except Exception as e:
            L(f"매수 실패 {o['ticker']}: {e}")

    return out


def _manual_live(ticker: str, qty: int, side: str, est_price: float, pm,
                 src: str, reason: str, entry_price: float = 0.0) -> dict:
    """수동 실거래 1건 — 시장가, 타임아웃이면 취소 후 최종 체결을 다시 읽어 반영.
    취소가 확인되지 않으면 의도를 미해결로 남겨 다음 대조가 확정한다."""
    from broker import Broker, validate_fill
    try:
        j = _it.load()
    except _it.JournalError as e:
        raise RuntimeError(f"{e} — 실거래 주문 중단")
    broker = Broker(paper=False)
    g = pre_trade_check(side, ticker, qty, est_price, broker=broker, journal=j,
                        limits=risk_limits(None), manual=True)
    if not g["ok"]:
        raise RuntimeError(g["reason"])
    res, rec = _live_order(broker, j, ticker=ticker, side=side, qty=g["qty"],
                           est_price=g["est_price"],
                           cycle_id=_it.default_cycle_id(src), source=src,
                           reason=reason, market=True)
    if res is None:
        raise RuntimeError("중복 주문이 차단되었습니다")
    v = validate_fill(res, est_price=est_price, req_shares=g["qty"])
    q, fp = _book(rec, pm)
    _it.save(j)
    tracked = _it.is_unresolved(rec)
    if q <= 0:
        if tracked:
            raise RuntimeError("체결 미확인 — 주문 취소가 확인되지 않아 추적 중입니다"
                               "(다음 사이클 브로커 대조에서 확정)")
        raise RuntimeError(f"미체결 — 주문 취소됨 (상태: {res.get('status')})")
    warn = v.get("warning") or ""
    if tracked:
        warn = (warn + " · " if warn else "") + "잔량 주문 추적 중(다음 대조에서 확정)"
    out = {"price": fp, "shares": q, "warning": warn}
    if side == "sell":
        out["pnl_pct"] = (fp - entry_price) / entry_price if entry_price else 0.0
        log_order(ticker, "sell", q, fp, source=src, pnl_pct=out["pnl_pct"],
                  reason=reason)
    else:
        log_order(ticker, "buy", q, fp, source=src)
    return out


@_locked
def execute_manual(ticker: str, shares: float, side: str, paper: bool,
                   est_price: float, pm=None, source: Optional[str] = None,
                   reason: str = "manual") -> dict:
    """수동 주문 1건 실행 (앱 빠른거래·직접주문·텔레그램 공용).

    성공 시 {"price", "shares", "pnl_pct"(매도)} 반환, 실패는 RuntimeError.
    실거래는 시장가 — 타임아웃이면 주문을 취소하고 최종 체결분만 반영한다.
    """
    import paper_account as _pa
    from portfolio import PortfolioManager
    pm = pm or PortfolioManager(paper=paper)
    src = source or ("paper" if paper else "manual")
    qty = int(shares)

    if side == "buy":
        if paper:
            cost = qty * est_price
            if cost > _pa.cash() + 1e-6:
                raise RuntimeError(
                    f"모의 현금 부족 (필요 ${cost:,.0f} / 가용 ${_pa.cash():,.0f})")
            pm.record_buy(ticker, qty, est_price, 0)
            _pa.adjust(-cost)
            log_order(ticker, "buy", qty, est_price, source=src)
            return {"price": est_price, "shares": qty}
        return _manual_live(ticker, qty, "buy", est_price, pm, src, reason)

    # ── sell ──
    # 호출부가 넘긴 pm 은 묵은 스냅샷일 수 있다(UI 보관본 등) — 락 안에서 최신화.
    pm._load_state()
    pos = pm.positions.get(ticker)
    if not pos or int(pos.shares) < 1:
        raise RuntimeError(f"{ticker} — 보유하지 않은 종목입니다")
    qty = min(qty, int(pos.shares)) if qty >= 1 else int(pos.shares)
    if paper:
        pnl = ((est_price - pos.entry_price) / pos.entry_price
               if pos.entry_price else 0.0)
        qty = int(pm.record_sell(ticker, exit_price=est_price, reason=reason,
                                 shares=qty) or 0)
        if qty < 1:
            raise RuntimeError(f"{ticker} — 보유하지 않은 종목입니다")
        _pa.adjust(qty * est_price)
        log_order(ticker, "sell", qty, est_price, source=src,
                  pnl_pct=pnl, reason=reason)
        return {"price": est_price, "shares": qty, "pnl_pct": pnl}
    return _manual_live(ticker, qty, "sell", est_price, pm, src, reason,
                        entry_price=pos.entry_price)


# ── 사이클 시작 대조 (실거래) ────────────────────────────────────────────

def _default_alert(msg: str):
    try:
        from core.runner import alert
        alert(msg)
    except Exception:
        pass


@_locked
def reconcile_live(broker, pm, cfg: Optional[dict] = None, *,
                   alert_fn: Optional[Callable[[str], object]] = None,
                   log: Optional[Callable[[str], None]] = None) -> dict:
    """실거래 사이클 첫 단계 — 브로커가 진실의 원천.

    1) 미해결 의도의 자식 주문을 get_order_by_client_id(또는 id)로 다시 읽어 상태 확정
       (크래시로 응답을 못 받은 주문 포함, 브로커에 없으면 not_found)
    2) 장부에 아직 반영 안 된 체결은 브로커 보유로 판정해 반영한다:
         장부 + 미반영 == 브로커 → 반영 / 장부 == 브로커 → 이미 반영된 것(크래시가
         장부 기록 뒤였음) / 둘 다 아니면 불일치
    3) 전 종목 장부 ↔ 브로커 보유 비교. 열린 주문이 있는 종목은 다음 대조로 미룬다.
    4) 허용치(reconcile_tol_shares) 넘는 불일치 → 매매 중단(저널 halt: 매수 금지,
       손절·청산 매도는 브로커 보유 한도 안에서 계속) + 경보(사유가 바뀔 때만).
       다음 대조에서 일치하면 자동 해제.
    반환 {"ok", "halted", "mismatches", "blocked"(이번 사이클 제외할 종목),
          "resolved", "booked"}. 브로커 조회 자체가 실패하면 RuntimeError
    (러너의 연속 실패 백오프·경보가 처리).
    """
    L = log or (lambda m: None)
    alert = alert_fn or _default_alert
    lim = risk_limits(cfg)
    tol = float(lim["reconcile_tol_shares"] or 0) + 1e-6
    ignore = {str(t).upper() for t in (lim["reconcile_ignore"] or [])}
    j = _it.load()                      # 손상이면 JournalError → 사이클 실패
    pm._load_state()
    out = {"ok": True, "halted": False, "mismatches": [], "blocked": [],
           "resolved": [], "booked": []}

    try:
        for rec in _it.unresolved(j):
            for c in rec["orders"]:
                if str(c.get("status") or "").lower() in _it.CHILD_FINAL:
                    continue
                got = broker.get_order_status(client_order_id=c.get("coid"),
                                              order_id=c.get("id"))
                if got is None:
                    c["status"] = "not_found"
                else:
                    c.update(id=got["id"] or c.get("id"), status=got["status"],
                             filled_qty=got["filled_qty"],
                             fill_price=got["fill_price"] or c.get("fill_price"))
            before = rec["state"]
            _it.refresh(rec, final=True)
            if rec["state"] != before:
                out["resolved"].append(f"{rec['intent_id']} {before}→{rec['state']}")
        bpos = {t.upper(): float(v.get("shares") or 0)
                for t, v in broker.get_positions(strict=True).items()}
        bopen = broker.get_open_orders()
    except Exception as e:
        _it.save(j)
        raise RuntimeError(f"브로커 대조 실패(조회 오류): {e}") from e

    def led(t):
        p = pm.positions.get(t)
        return float(p.shares) if p else 0.0

    open_t = {r["ticker"] for r in _it.unresolved(j) if _it.has_open_child(r)}
    open_t |= {o["symbol"] for o in bopen}

    by_t: dict = {}
    for r in _it.unresolved(j):
        if _it.unbooked(r)[0] > 0:
            by_t.setdefault(r["ticker"], []).append(r)
    for t, recs in by_t.items():
        delta = sum((1 if r["side"] == "buy" else -1) * _it.unbooked(r)[0] for r in recs)
        b, l = bpos.get(t, 0.0), led(t)
        if abs(l + delta - b) <= tol:
            for r in recs:
                ent = pm.positions.get(t)
                q, px = _book(r, pm)
                if q:
                    pnl = None
                    if r["side"] == "sell" and ent and ent.entry_price:
                        pnl = (px - ent.entry_price) / ent.entry_price
                    log_order(t, r["side"], q, px, source=r.get("source") or "auto",
                              score=r.get("score"), pnl_pct=pnl,
                              reason=r.get("reason") or "")
                    out["booked"].append(f"{t} {r['side']} {q}주 @ ${px:.2f}")
                    L(f"대조: 미반영 체결 반영 {t} {r['side']} {q}주 @ ${px:.2f}")
        elif abs(l - b) <= tol:
            for r in recs:
                q, px = _it.unbooked(r)
                _it.mark_booked(r, q, px)
                L(f"대조: {t} {r['side']} {_num(q)}주는 이미 장부에 반영돼 있음")
        pm._load_state()

    for t in sorted(set(pm.positions) | set(bpos)):
        if t in ignore or t in open_t:
            continue
        b, l = bpos.get(t, 0.0), led(t)
        if abs(b - l) > tol:
            out["mismatches"].append({"ticker": t, "ledger": l, "broker": b})
    out["blocked"] = sorted(open_t)
    _it.prune(j)

    if out["mismatches"]:
        mm = out["mismatches"]
        sig = ";".join(f"{m['ticker']}:{m['ledger']:g}/{m['broker']:g}" for m in mm)
        reason = "장부≠브로커 " + ", ".join(
            f"{m['ticker']} 장부 {m['ledger']:g}주/브로커 {m['broker']:g}주" for m in mm)
        out["ok"], out["halted"] = False, True
        if _it.set_halt(j, reason, sig):
            alert(f"🛑 자동매매 매수 중단 — 브로커 대조 불일치\n{reason}\n"
                  "손절·청산 매도는 브로커 보유 한도 안에서 계속됩니다. "
                  "장부를 브로커와 맞추면 다음 사이클 대조에서 자동 재개됩니다.")
        L(f"대조 불일치 → 매수 중단(매도는 진행): {reason}")
    else:
        h = _it.halted(j)
        if h and h.get("by") == "reconcile":
            _it.clear_halt(j)
            alert("✅ 브로커 대조 일치 — 자동매매 재개")
            L("대조 일치 — 매매 중단 해제")
        elif h:
            out["halted"] = True
    _it.save(j)
    return out
