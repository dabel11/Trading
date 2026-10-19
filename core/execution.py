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
  - 브로커 측 보호 주문(무인 운영 안전): 매수가 체결되면 그 종목 보유 전량에 GTC stop
    (설정 broker_stop = stop | trailing | off, 손절률 stop_loss)을 브로커 서버에 건다.
    앱 측 매도는 그 stop 을 먼저 취소하고 취소가 확인된 뒤에만 낸다(묶인 수량 때문에
    거부되거나 stop 과 이중 매도되지 않게). 매도 뒤 남은 보유에는 다시 건다. 대조는
    보유 종목마다 보호 주문이 있는지 보고 없거나 수량·가격이 어긋나면 다시 건다.
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
    "broker_stop": "stop",       # 브로커 측 보호 주문: stop(고정 손절가) | trailing | off
    "stop_loss": 0.07,           # 보호 stop 손절률(설정의 손절 값과 같은 키)
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
      이 앱의 보호 stop 은 '열린 매도'에서 빼고 protect_qty 로 따로 돌려준다 — 호출부가
      매도 직전에 취소(_drop_protect)하므로 그 수량은 매도 가능분이다. 취소가 확인되지
      않으면 호출부가 매도를 보류한다(stop 과 이중 매도 방지).
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
    protect_qty = 0.0
    if side != "buy":
        own = _it.protect_coids(journal)
        try:
            held = float((broker.get_positions(strict=True).get(ticker) or {})
                         .get("shares") or 0)
            open_sell = 0.0
            for o in broker.get_open_orders():
                if o["symbol"] != ticker.upper() or o["side"] != "sell":
                    continue
                rest = max(0.0, o["qty"] - o["filled_qty"])
                if o.get("coid") in own:
                    protect_qty += rest
                else:
                    open_sell += rest
        except Exception as e:
            return no(f"브로커 보유 조회 실패 — 매도 보류(숏 방지): {e}")
        avail = int(held - open_sell + 1e-9)
        if avail < 1:
            return no(f"브로커 매도 가능 수량 없음(보유 {held:g} · 열린 매도 "
                      f"{open_sell:g}) — 숏 방지")
        qty = min(qty, avail)
    return {"ok": True, "reason": "", "qty": qty, "est_price": est,
            "protect_qty": protect_qty}


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


# ── 브로커 측 보호 주문 ──────────────────────────────────────────────────

def _protect_mode(lim: dict) -> Optional[tuple]:
    """(종류 "stop"|"trailing", 손절률) 또는 None(끔)."""
    mode = str(lim.get("broker_stop") or "off").strip().lower()
    try:
        sl = float(lim.get("stop_loss") or 0)
    except (TypeError, ValueError):
        sl = 0.0
    if mode not in ("stop", "trailing") or not 0 < sl < 1:
        return None
    return mode, sl


def _open_child_qty(rec: dict) -> float:
    return sum(max(0.0, float(c.get("qty") or 0) - float(c.get("filled_qty") or 0))
               for c in rec["orders"]
               if str(c.get("status") or "").lower() not in _it.CHILD_FINAL)


def _drop_protect(broker, j: dict, ticker: str, pm, L) -> tuple:
    """앱 매도 직전 — 이 종목의 보호 stop 을 취소하고 최종 상태를 확인한다.

    취소 직전에 stop 이 (일부) 체결됐으면 그 체결을 바로 장부에 반영한다(브로커 손절).
    반환 (취소 확인됨, [(수량, 가격, 손익률)…]). 확인되지 않으면 호출부는 매도를
    내지 않는다 — 살아 있는 stop 과 앱 매도가 둘 다 체결되면 이중 매도(숏)다.
    조회 오류는 예외로 올린다(호출부가 매도 보류)."""
    ok, fills = True, []
    for rec in _it.unresolved(j, ticker, protect=True):
        for c in rec["orders"]:
            if str(c.get("status") or "").lower() in _it.CHILD_FINAL:
                continue
            oid = c.get("id")
            if not oid:
                got = broker.get_order_status(client_order_id=c.get("coid"))
                if got is None:
                    c["status"] = "not_found"
                    continue
                oid = got["id"]
                c.update(id=oid, status=got["status"])
            r = broker.cancel_confirm(oid)
            c.update(status=r["status"],
                     filled_qty=max(int(r["filled_qty"] or 0), int(c.get("filled_qty") or 0)),
                     fill_price=r["fill_price"] or c.get("fill_price") or 0.0)
        _it.refresh(rec, final=True)
        if _it.has_open_child(rec):
            ok = False
        ent = pm.positions.get(ticker)
        q, px = _book(rec, pm)
        if q:
            pnl = ((px - ent.entry_price) / ent.entry_price
                   if ent and ent.entry_price else None)
            log_order(ticker, "sell", q, px, source="broker_stop", pnl_pct=pnl,
                      reason=rec.get("reason") or "stop_loss")
            L(f"브로커 보호 stop 체결 반영: {ticker} {q}주 @ ${px:.2f}")
            fills.append((q, px, pnl))
    _it.save(j)
    return ok, fills


def _ensure_protect(broker, j: dict, ticker: str, pm, lim: dict, L, *,
                    anchor: Optional[str] = None, bpos: Optional[dict] = None,
                    bopen: Optional[list] = None) -> dict:
    """이 종목 보유에 맞는 보호 stop 이 브로커에 걸려 있게 한다.

    목표 수량 = min(장부 보유, 브로커 보유 − 이 앱 보호 stop 외 열린 매도) — 장부가
    브로커보다 많아도(대조 불일치) 브로커에 있는 만큼만, 매도가 진행 중이면 그만큼 빼고.
    이미 맞는 stop 이 있으면 그대로, 수량·가격이 어긋나면 취소 확인 후 다시 건다.
    목표가 0 이면 남은 stop(고아)을 내린다.
    반환 {"action": ok|placed|replaced|canceled|skip|error, "note"}. 조회 오류는 예외."""
    T = ticker.upper()
    pmode = _protect_mode(lim)
    if pmode is None:
        return {"action": "skip", "note": "보호 주문 꺼짐"}
    mode, sl = pmode
    if bpos is None:
        bpos = broker.get_positions(strict=True)
    if bopen is None:
        bopen = broker.get_open_orders()
    own_coids = _it.protect_coids(j)
    bp = bpos.get(T) or {}
    held = float(bp.get("shares") or 0)
    px = float(bp.get("market_value") or 0) / held if held else 0.0
    other_sell = sum(max(0.0, o["qty"] - o["filled_qty"]) for o in bopen
                     if o["symbol"] == T and o["side"] == "sell"
                     and o.get("coid") not in own_coids)
    pos = pm.positions.get(T)
    led = float(pos.shares) if pos else 0.0
    want = int(min(led, held - other_sell) + 1e-9)
    own = [r for r in _it.unresolved(j, T, protect=True) if _it.has_open_child(r)]

    if want < 1:
        if not own:
            return {"action": "ok", "note": ""}
        ok, _ = _drop_protect(broker, j, T, pm, L)
        return {"action": "canceled" if ok else "error",
                "note": "보유 없음 — 보호 stop 내림" if ok else "보호 stop 취소 미확정"}

    stop_px = round(float(pos.entry_price) * (1 - sl), 2) if mode == "stop" else None
    trail = round(sl * 100, 2) if mode == "trailing" else None
    if mode == "stop" and not (stop_px and stop_px > 0):
        return {"action": "error", "note": "평단가를 몰라 손절가 계산 불가"}
    if own:
        cur = own[-1]["orders"][-1]
        same = (len(own) == 1 and abs(_open_child_qty(own[0]) - want) < 1e-9
                and (abs(float(cur.get("stop_price") or 0) - stop_px) < 0.01
                     if stop_px else abs(float(cur.get("trail_percent") or 0) - trail) < 1e-6))
        if same:
            return {"action": "ok", "note": ""}
        ok, fills = _drop_protect(broker, j, T, pm, L)
        if not ok:
            return {"action": "error", "note": "기존 보호 stop 취소 미확정 — 재설정 보류"}
        if fills:                      # 그 사이 stop 이 체결됨 — 다음 대조에서 다시 맞춘다
            return {"action": "skip", "note": "기존 보호 stop 체결 — 다음 대조에서 재설정"}
    if stop_px and px and px <= stop_px:
        return {"action": "skip",
                "note": f"현재가 ${px:.2f} ≤ 손절선 ${stop_px:.2f} — 앱 손절에 맡김"}

    if anchor is None:
        anchor = f"{_mh.trading_day().strftime('%Y%m%d')}-{_it.default_cycle_id('reconcile')}-{T}"
    iid = _it.protect_intent_id(j, anchor)
    rec = _it.open_intent(j, iid, ticker=T, side="sell", qty=want,
                          est_price=stop_px or px, source="broker_stop",
                          reason="stop_loss (브로커 보호 stop)", kind=_it.PROTECT)
    child = {"coid": _it.coid_base(iid), "id": None, "qty": want,
             "kind": "stop" if stop_px else "trailing_stop",
             "stop_price": stop_px, "trail_percent": trail,
             "status": "pending_submit", "filled_qty": 0, "fill_price": 0.0}
    _it.upsert_child(rec, child)
    _it.save(j)                                   # write-ahead — 제출보다 먼저 디스크에
    try:
        r = broker.place_protective_stop(T, want, client_order_id=child["coid"],
                                         stop_price=stop_px, trail_percent=trail)
    except Exception as e:
        from broker import _definite_reject
        child.update(status="rejected" if _definite_reject(e) else "unknown",
                     error=str(e)[:200])
        _it.upsert_child(rec, child)
        _it.refresh(rec, final=True)              # unknown 은 열린 채 → 대조가 coid 로 확정
        _it.save(j)
        return {"action": "error", "note": f"보호 stop 제출 실패: {str(e)[:80]}"}
    child.update(id=r.get("id"), status=r.get("status") or "new",
                 filled_qty=r.get("filled_qty") or 0, fill_price=r.get("fill_price") or 0.0)
    _it.upsert_child(rec, child)
    _it.refresh(rec, final=True)
    _it.save(j)
    what = f"${stop_px:.2f} stop" if stop_px else f"trailing {trail:g}%"
    return {"action": "replaced" if own else "placed",
            "note": f"보호 {what} {want}주 (coid {child['coid']})"}


def _reprotect(broker, j: dict, ticker: str, pm, lim: dict, L,
               anchor: Optional[str] = None):
    """매수 체결·매도 시도 뒤 — 보호 stop 을 보유에 맞춘다. 실패는 로그만 남기고
    다음 사이클 대조(reconcile_live)가 다시 시도·경보한다."""
    if _protect_mode(lim) is None:
        return
    try:
        pm._load_state()
        p = pm.positions.get(ticker)
        if (p is None or float(p.shares) < 1) and not _it.unresolved(j, ticker, protect=True):
            return
        r = _ensure_protect(broker, j, ticker, pm, lim, L, anchor=anchor)
    except Exception as e:
        L(f"{ticker}: 보호 stop 설정 실패 — 다음 대조에서 재시도 ({e})")
        return
    if r["action"] != "ok" and r["note"]:
        L(f"{ticker}: {r['note']}")


def _live_sell(broker, j: dict, lim: dict, pm, o: dict, qty: int, pos, cyc: str,
               src: str, price_ts, L, out: dict) -> Optional[tuple]:
    """자동 실거래 매도 1건: 보호 stop 취소 확인 → 사전 게이트 → 주문 → 장부 반영 →
    남은 보유에 보호 stop 재설정. 반환 (반영 수량, 가격, 손익률) 또는 None(건너뜀)."""
    from broker import validate_fill
    t = o["ticker"]
    try:
        try:
            dropped, sfills = _drop_protect(broker, j, t, pm, L)
        except Exception as e:
            L(f"매도 보류 {t}: 보호 stop 확인 실패 — {e}")
            out["skipped"].append(t)
            return None
        for q, px, pnl in sfills:
            out["sold"].append({"ticker": t, "shares": q, "price": px,
                                "pnl_pct": pnl, "reason": "broker_stop"})
        if not dropped:
            L(f"매도 보류 {t}: 보호 stop 취소 미확정 — stop 과 이중 매도 방지(다음 사이클 재시도)")
            out["skipped"].append(t)
            return None
        if sfills:
            pm._load_state()
            pos = pm.positions.get(t)
            if pos is None or int(pos.shares) < 1:
                L(f"{t}: 브로커 보호 stop 으로 이미 청산됨 — 앱 매도 생략")
                return None
            qty = min(qty, int(pos.shares))
        g = pre_trade_check("sell", t, qty, o["est_price"], broker=broker, journal=j,
                            limits=lim, price_ts=price_ts)
        if not g["ok"]:
            L(f"매도 보류 {t}: {g['reason']}")
            out["skipped"].append(t)
            return None
        res, rec = _live_order(broker, j, ticker=t, side="sell", qty=g["qty"],
                               est_price=g["est_price"], cycle_id=cyc, source=src,
                               reason=o["reason"])
        if res is None:
            L(f"매도 중복 차단(같은 의도 이미 실행): {t}")
            out["skipped"].append(t)
            return None
        if res.get("note"):
            L(f"{t}: {res['note']}")
        v = validate_fill(res, est_price=g["est_price"], req_shares=g["qty"])
        pnl_px = res.get("fill_price") or g["est_price"]
        pnl = (pnl_px - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
        q, fp = _book(rec, pm)
        _it.save(j)
        if _it.is_unresolved(rec):
            out["pending"].append(t)
            L(f"{t}: 열린 매도 주문 추적 중 — 다음 대조에서 확정")
        if q <= 0:
            L(f"매도 보류 {t}: {v['warning'] or '체결 없음'}")
            out["skipped"].append(t)
            return None
        if v["warning"]:
            L(f"{t}: {v['warning']}")
        return q, fp, pnl
    finally:
        _reprotect(broker, j, t, pm, lim, L)


def _clear_protect_for_buy(broker, j: dict, t: str, pm, L, out: Optional[dict]) -> bool:
    """추가 매수 전 — 열린 보호 stop 을 내린다. Alpaca 는 반대 방향 열린 주문이 있으면
    매수를 wash trade 로 거부할 수 있고, 어차피 매수 뒤 보유 전량으로 다시 건다.
    내릴 수 없거나(취소 미확정) 그 사이 stop 이 체결됐으면(방금 손절) False — 매수 보류."""
    if not _it.unresolved(j, t, protect=True):
        return True
    try:
        dropped, sfills = _drop_protect(broker, j, t, pm, L)
    except Exception as e:
        L(f"매수 보류 {t}: 보호 stop 확인 실패 — {e}")
        return False
    for q, px, pnl in sfills:
        if out is not None:
            out["sold"].append({"ticker": t, "shares": q, "price": px,
                                "pnl_pct": pnl, "reason": "broker_stop"})
    if not dropped:
        L(f"매수 보류 {t}: 보호 stop 취소 미확정")
        return False
    if sfills:
        L(f"매수 보류 {t}: 방금 브로커 보호 stop 으로 손절됨")
        return False
    return True


def _live_buy(broker, j: dict, lim: dict, pm, o: dict, qty: int, cyc: str,
              src: str, price_ts, L, out: dict) -> Optional[tuple]:
    """자동 실거래 매수 1건: 사전 게이트 → (보유 중이면 보호 stop 내림) → 주문 →
    장부 반영 → 보유 전량에 보호 stop. 반환 (반영 수량, 평균가) 또는 None(건너뜀)."""
    from broker import validate_fill
    t = o["ticker"]
    g = pre_trade_check("buy", t, qty, o["est_price"], broker=broker, journal=j,
                        limits=lim, price_ts=price_ts)
    if not g["ok"]:
        L(f"매수 보류 {t}: {g['reason']}")
        out["skipped"].append(t)
        return None
    iid = _it.make_intent_id(cyc, t, "buy")
    try:
        if not _clear_protect_for_buy(broker, j, t, pm, L, out):
            out["skipped"].append(t)
            return None
        res, rec = _live_order(broker, j, ticker=t, side="buy", qty=qty,
                               est_price=o["est_price"], cycle_id=cyc, source=src,
                               score=o.get("score", 0))
        if res is None:
            L(f"매수 중복 차단(같은 의도 이미 실행): {t}")
            out["skipped"].append(t)
            return None
        if res.get("note"):
            L(f"{t}: {res['note']}")
        v = validate_fill(res, est_price=o["est_price"], req_shares=qty)
        q, price = _book(rec, pm)
        _it.save(j)
        if _it.is_unresolved(rec):
            out["pending"].append(t)
            L(f"{t}: 미확정 매수 주문 — 다음 대조에서 확정")
        if q <= 0:
            L(f"매수 보류 {t}: {v['warning'] or '체결 없음'}")
            out["skipped"].append(t)
            return None
        if v["warning"]:
            L(f"{t}: {v['warning']}")
        return q, price
    finally:
        _reprotect(broker, j, t, pm, lim, L, anchor=iid)


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
                r = _live_sell(broker, j, lim, pm, o, qty, pos, cyc, src, price_ts, L, out)
                if r is None:
                    continue
                qty, fp, pnl = r
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
                r = _live_buy(broker, j, lim, pm, o, qty, cyc, src, price_ts, L, out)
                if r is None:
                    continue
                qty, price = r
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
    취소가 확인되지 않으면 의도를 미해결로 남겨 다음 대조가 확정한다.
    매도는 보호 stop 을 먼저 취소(확인)하고, 끝나면 남은 보유에 다시 건다."""
    from broker import Broker, validate_fill
    try:
        j = _it.load()
    except _it.JournalError as e:
        raise RuntimeError(f"{e} — 실거래 주문 중단")
    broker = Broker(paper=False)
    lim = risk_limits(None)
    cyc = _it.default_cycle_id(src)
    try:
        if side == "sell":
            try:
                dropped, sfills = _drop_protect(broker, j, ticker, pm, lambda m: None)
            except Exception as e:
                raise RuntimeError(f"보호 stop 확인 실패 — 매도 보류: {e}")
            if not dropped:
                raise RuntimeError("보호 stop 취소가 확인되지 않아 매도를 보류했습니다"
                                   "(stop 과 이중 매도 방지) — 잠시 후 다시 시도하세요")
            if sfills:
                pm._load_state()
                p = pm.positions.get(ticker)
                if p is None or int(p.shares) < 1:
                    q, px, pnl = sfills[-1]
                    return {"price": px, "shares": sum(f[0] for f in sfills),
                            "pnl_pct": pnl or 0.0,
                            "warning": "브로커 보호 stop 이 먼저 체결돼 이미 청산됐습니다"}
                qty = min(qty, int(p.shares))
        g = pre_trade_check(side, ticker, qty, est_price, broker=broker, journal=j,
                            limits=lim, manual=True)
        if not g["ok"]:
            raise RuntimeError(g["reason"])
        if side == "buy" and not _clear_protect_for_buy(broker, j, ticker, pm,
                                                        lambda m: None, None):
            raise RuntimeError("보유 종목의 보호 stop 을 정리하지 못해(취소 미확정 또는 방금 손절) "
                               "추가 매수를 보류했습니다")
        res, rec = _live_order(broker, j, ticker=ticker, side=side, qty=g["qty"],
                               est_price=g["est_price"],
                               cycle_id=cyc, source=src, reason=reason, market=True)
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
    finally:
        _reprotect(broker, j, ticker, pm, lim, lambda m: None,
                   anchor=_it.make_intent_id(cyc, ticker, side) if side == "buy" else None)


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
       (브로커 보호 stop 이 앱이 꺼진 사이 체결된 것도 여기서 매도로 반영된다)
    3) 전 종목 장부 ↔ 브로커 보유 비교. 열린 주문이 있는 종목은 다음 대조로 미룬다
       (이 앱의 보호 stop 은 늘 열려 있으므로 '열린 주문'에서 뺀다).
    4) 허용치(reconcile_tol_shares) 넘는 불일치 → 매매 중단(저널 halt: 매수 금지,
       손절·청산 매도는 브로커 보유 한도 안에서 계속) + 경보(사유가 바뀔 때만).
       다음 대조에서 일치하면 자동 해제(장부 손상·비상 정지 중단은 사용자 해제로만).
    5) 보유 종목마다 보호 stop 확인 — 없거나(재기동·제출 실패·만료) 수량·가격이
       어긋나면 다시 건다. 보유가 없어진 종목의 stop 은 내린다. 실패는 경보(사유 바뀔 때만).
    반환 {"ok", "halted", "mismatches", "blocked"(이번 사이클 제외할 종목),
          "resolved", "booked", "protect"}. 브로커 조회 자체가 실패하면 RuntimeError
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
           "resolved": [], "booked": [], "protect": []}

    try:
        for rec in _it.unresolved(j, protect=None):
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
        bfull = {t.upper(): v for t, v in broker.get_positions(strict=True).items()}
        bpos = {t: float(v.get("shares") or 0) for t, v in bfull.items()}
        bopen = broker.get_open_orders()
    except Exception as e:
        _it.save(j)
        raise RuntimeError(f"브로커 대조 실패(조회 오류): {e}") from e
    _adopt_orphan_protects(j, bopen, L)
    own_prot = _it.protect_coids(j)

    def led(t):
        p = pm.positions.get(t)
        return float(p.shares) if p else 0.0

    open_t = {r["ticker"] for r in _it.unresolved(j) if _it.has_open_child(r)}
    open_t |= {o["symbol"] for o in bopen if o.get("coid") not in own_prot}
    # 보호 stop 이 상태 조회(1단계) 뒤에 체결되기 시작했으면 보유 조회와 어긋날 수 있다 —
    # 열린 주문 목록(가장 나중에 읽음)에서 사라졌거나 체결이 붙은 종목은 다음 대조로 미룬다.
    bo = {o.get("coid"): o for o in bopen}
    for r in _it.unresolved(j, protect=True):
        for c in r["orders"]:
            if str(c.get("status") or "").lower() in _it.CHILD_FINAL:
                continue
            ob = bo.get(c.get("coid"))
            if ob is None or float(ob["filled_qty"]) > float(c.get("filled_qty") or 0):
                open_t.add(r["ticker"])

    by_t: dict = {}
    for r in _it.unresolved(j, protect=None):
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

    # 5) 보호 stop 점검 — 대조 불일치 중에도 건다(위험을 줄이는 주문, 수량은 브로커 한도)
    if _protect_mode(lim) is not None:
        fails = []
        tks = set(pm.positions) | {r["ticker"] for r in _it.unresolved(j, protect=True)}
        for t in sorted(tks):
            if t in ignore:
                continue
            try:
                r = _ensure_protect(broker, j, t, pm, lim, L, bpos=bfull, bopen=bopen)
            except Exception as e:
                r = {"action": "error", "note": f"조회·제출 오류: {str(e)[:80]}"}
            if r["action"] != "ok":
                out["protect"].append(f"{t} {r['action']}: {r['note']}")
                L(f"대조: {t} 보호 stop {r['action']} — {r['note']}")
            if r["action"] == "error":
                fails.append(f"{t} {r['note']}")
        sig = ";".join(fails)
        if sig != (j.get("protect_alert") or ""):
            j["protect_alert"] = sig
            if fails:
                alert("⚠️ 브로커 보호 stop 설정 실패 — 앱이 꺼지면 손절이 작동하지 않습니다\n"
                      + "\n".join(fails) + "\n다음 사이클 대조에서 다시 시도합니다.")

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
        if _it.clear_auto_halt(j):
            alert("✅ 브로커 대조 일치 — 자동매매 재개")
            L("대조 일치 — 매매 중단 해제")
        h = _it.halted(j)
        if h:
            out["halted"] = True
            L(f"매매 중단 유지({_it.HALT_LABELS.get(h.get('by'), h.get('by'))}) — "
              "사용자 해제(/resume · 설정 화면) 필요")
    _it.save(j)
    return out


def _adopt_orphan_protects(j: dict, bopen: list, L):
    """브로커에 열려 있는 이 앱의 보호 stop(coid 'ait-…-stop…') 중 저널에 없는 것을
    저널에 되살린다 — 저널이 지워져도 stop 이 '모르는 열린 매도'로 남아 매도를 막거나
    종목을 영영 대조에서 빼지 않게."""
    import re
    known = {c.get("coid") for r in j["intents"].values() for c in r["orders"]}
    for o in bopen:
        coid = o.get("coid") or ""
        if (coid in known or o.get("side") != "sell"
                or not re.match(r"^ait-.+-stop\d*$", coid)):
            continue
        iid = coid[len("ait-"):]
        rec = _it.open_intent(j, iid, ticker=o["symbol"], side="sell",
                              qty=int(o["qty"]), est_price=o.get("stop_price") or 0,
                              source="broker_stop", reason="stop_loss (브로커 보호 stop)",
                              kind=_it.PROTECT)
        _it.upsert_child(rec, {"coid": coid, "id": o["id"], "qty": o["qty"],
                               "kind": o.get("type") or "stop",
                               "stop_price": o.get("stop_price") or None,
                               "trail_percent": o.get("trail_percent") or None,
                               "status": o["status"], "filled_qty": o["filled_qty"],
                               "fill_price": 0.0})
        _it.refresh(rec, final=True)
        L(f"대조: 저널에 없던 보호 stop 복원 {o['symbol']} {coid}")
