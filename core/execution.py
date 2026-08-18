"""단일 주문 실행기 — 모든 매매(데몬 자동·앱 수동·텔레그램)가 이 깔때기를 지난다.

여기 한 곳에서: 자금 게이팅 → (실거래) 브로커 체결·검증 → 장부 기록 →
모의 현금 조정 → orders_log 기록 → 알림. 경로별로 실행 코드가 중복돼
동작이 어긋나던 문제(데몬 거래가 주문 내역에 안 남던 것 등)를 없앤다.
"""
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

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
        "date": date.today().isoformat(),
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


@_locked
def execute_orders(orders: dict, prices: dict, paper: bool, pm,
                   broker=None, source: Optional[str] = None,
                   log: Optional[Callable[[str], None]] = None,
                   gap_sec: float = 0.0) -> dict:
    """generate_orders 결과({"sell": [...], "buy": [...]})를 실행한다.

    paper=True  → 브로커 없이 현재가 로컬 체결 + 가상 현금 조정
    paper=False → broker.execute_order + validate_fill 통과분만 장부 기록
    log: 진행 메시지 콜백 (데몬은 파일 로그, 앱은 라이브 로그)
    반환: {"sold": [..], "bought": [..], "skipped": [..]} (체결 내역)
    """
    import paper_account as _pa
    L = log or (lambda m: None)
    src = source or ("paper" if paper else "auto")
    out = {"sold": [], "bought": [], "skipped": []}

    # 락 안에서 장부를 최신화 — 결정(generate_orders)과 실행 사이 다른 프로세스가
    # 거래했을 수 있다. 가용자금 게이트가 묵은 스냅샷을 보고 과매수하는 것 방지.
    try:
        pm._load_state()
    except Exception:
        pass

    if not paper and broker is None:
        from broker import Broker
        broker = Broker(paper=False)

    # ── 매도 먼저 (자본 회수 → 매수 여력 확보) ───────────────────────────
    for o in orders.get("sell", []):
        try:
            pos = pm.positions.get(o["ticker"])
            fp = prices.get(o["ticker"], o["est_price"])
            qty = int(o["shares"])
            if not paper:
                from broker import validate_fill
                res = broker.execute_order(o["ticker"], qty, "sell", o["est_price"])
                if res.get("note"):
                    L(f"{o['ticker']}: {res['note']}")
                if res.get("duplicate"):
                    L(f"매도 중복 차단: {o['ticker']}")
                    out["skipped"].append(o["ticker"]); continue
                v = validate_fill(res, est_price=o["est_price"], req_shares=qty)
                if not v["ok"]:
                    L(f"매도 보류 {o['ticker']}: {v['warning']}")
                    out["skipped"].append(o["ticker"]); continue
                if v["warning"]:
                    L(f"{o['ticker']}: {v['warning']}")
                fp, qty = v["fill_price"], v["filled_qty"]
            pnl = ((fp - pos.entry_price) / pos.entry_price
                   if pos and pos.entry_price else 0.0)
            pm.record_sell(o["ticker"], exit_price=fp, reason=o["reason"], shares=qty)
            if paper:
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

    # ── 매수 (자금 게이팅: 모의=가상 현금, 실거래=장부 가용자금) ──────────
    for o in orders.get("buy", []):
        try:
            price = prices.get(o["ticker"], o["est_price"])
            qty = int(o["shares"])
            need = o.get("est_cost") or qty * price
            avail = _pa.cash() if paper else pm.available_capital()
            if need > avail + 1e-6:
                L(f"매수 보류(자금부족) {o['ticker']} 필요 ${need:,.0f}/가용 ${avail:,.0f}")
                out["skipped"].append(o["ticker"]); continue
            if not paper:
                from broker import validate_fill
                res = broker.execute_order(o["ticker"], qty, "buy", o["est_price"])
                if res.get("note"):
                    L(f"{o['ticker']}: {res['note']}")
                if res.get("duplicate"):
                    L(f"매수 중복 차단: {o['ticker']}")
                    out["skipped"].append(o["ticker"]); continue
                v = validate_fill(res, est_price=o["est_price"], req_shares=qty)
                if not v["ok"]:
                    L(f"매수 보류 {o['ticker']}: {v['warning']}")
                    out["skipped"].append(o["ticker"]); continue
                if v["warning"]:
                    L(f"{o['ticker']}: {v['warning']}")
                price, qty = v["fill_price"], v["filled_qty"]
            pm.record_buy(o["ticker"], qty, price, o.get("score", 0))
            if paper:
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


@_locked
def execute_manual(ticker: str, shares: float, side: str, paper: bool,
                   est_price: float, pm=None, source: Optional[str] = None,
                   reason: str = "manual") -> dict:
    """수동 주문 1건 실행 (앱 빠른거래·직접주문·텔레그램 공용).

    성공 시 {"price", "shares", "pnl_pct"(매도)} 반환, 실패는 RuntimeError.
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
        from broker import Broker, validate_fill
        res = Broker(paper=False).place_buy(ticker, qty)
        if res.get("duplicate"):
            raise RuntimeError("중복 주문이 차단되었습니다")
        v = validate_fill(res, est_price=est_price, req_shares=qty)
        if not v["ok"]:
            raise RuntimeError(v["warning"])
        pm.record_buy(ticker, v["filled_qty"], v["fill_price"], 0)
        log_order(ticker, "buy", v["filled_qty"], v["fill_price"], source=src)
        return {"price": v["fill_price"], "shares": v["filled_qty"],
                "warning": v.get("warning")}

    # ── sell ──
    pos = pm.positions.get(ticker)
    if not pos:
        raise RuntimeError(f"{ticker} — 보유하지 않은 종목입니다")
    qty = min(qty, int(pos.shares)) if qty >= 1 else int(pos.shares)
    if paper:
        pnl = ((est_price - pos.entry_price) / pos.entry_price
               if pos.entry_price else 0.0)
        pm.record_sell(ticker, exit_price=est_price, reason=reason, shares=qty)
        _pa.adjust(qty * est_price)
        log_order(ticker, "sell", qty, est_price, source=src,
                  pnl_pct=pnl, reason=reason)
        return {"price": est_price, "shares": qty, "pnl_pct": pnl}
    from broker import Broker, validate_fill
    res = Broker(paper=False).place_sell(ticker, qty)
    if res.get("duplicate"):
        raise RuntimeError("중복 주문이 차단되었습니다")
    v = validate_fill(res, est_price=est_price, req_shares=qty)
    if not v["ok"]:
        raise RuntimeError(v["warning"])
    fp, fq = v["fill_price"], v["filled_qty"]
    pnl = (fp - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
    pm.record_sell(ticker, exit_price=fp, reason=reason, shares=fq)
    log_order(ticker, "sell", fq, fp, source=src, pnl_pct=pnl, reason=reason)
    return {"price": fp, "shares": fq, "pnl_pct": pnl,
            "warning": v.get("warning")}
