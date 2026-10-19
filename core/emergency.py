"""매매 중단 해제와 비상 정지 — 앱 설정 화면과 텔레그램(/resume, /panic)이 같이 쓴다.

status() : 지금 매매가 멈춰 있는지와 그 사유(주문 저널 halt, 저널 손상).
resume() : 사용자 해제 — core.intents.clear_halt(). 대조 불일치가 남아 있으면 다음
           사이클 대조가 다시 멈춘다(그 사실을 결과 문구에 적는다).
panic()  : 비상 정지.
  · 실거래: 자동매매 정지 + 매매 중단(by="panic", 사용자 해제로만 풀림) 을 먼저 디스크에
    남기고 → 브로커 cancel_orders() + close_all_positions(cancel_orders=True) →
    청산 주문을 의도 저널에 kind="panic" 매도로 기록 → 잠시 체결을 기다린 뒤
    reconcile_live 로 장부에 반영(체결이 늦으면 다음 대조가 반영 — 장부↔브로커 일치).
  · 모의: 자동매매 정지 + 모의 장부 전 종목을 현재가로 청산.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from core import intents as _it
from core.execution import _locked


def _default_alert(msg: str):
    try:
        from core.runner import alert
        alert(msg)
    except Exception:
        pass


def status() -> dict:
    """{"halted", "by", "label", "reason", "since", "also", "journal_error"}."""
    try:
        j = _it.load()
    except _it.JournalError as e:
        return {"halted": True, "by": "journal", "label": "주문 저널 손상",
                "reason": str(e), "since": "", "also": {}, "journal_error": True}
    h = _it.halted(j)
    if not h:
        return {"halted": False, "by": "", "label": "", "reason": "", "since": "",
                "also": {}, "journal_error": False}
    by = h.get("by") or ""
    return {"halted": True, "by": by, "label": _it.HALT_LABELS.get(by, by or "수동"),
            "reason": h.get("reason") or "", "since": h.get("since") or "",
            "also": h.get("also") or {}, "journal_error": False}


def resume(by: str = "user") -> dict:
    """매매 중단 해제. {"ok", "was"(해제 전 상태), "message"}."""
    was = status()
    if was.get("journal_error"):
        return {"ok": False, "was": was,
                "message": "주문 저널 파일이 손상돼 해제할 수 없습니다 — 백업에서 "
                           "order_intents.json 을 되살리세요(실거래는 저널 없이 주문하지 않음)."}
    if not was["halted"]:
        return {"ok": True, "was": was, "message": "매매 중단 상태가 아닙니다."}
    _it.clear_halt()
    note = ""
    if was["by"] == "reconcile" or "reconcile" in (was.get("also") or {}):
        note = ("\n장부와 브로커가 아직 다르면 다음 사이클 대조에서 다시 멈춥니다 — "
                "장부를 브로커 보유에 맞추거나 reconcile_ignore 에 넣으세요.")
    elif was["by"] == "ledger":
        note = ("\nbackups/ 의 백업으로 장부를 되살렸는지 확인하세요 — 손상 흔적이 남아 "
                "있으면 다음 사이클에 다시 멈춥니다.")
    elif was["by"] == "panic":
        note = "\n자동매매는 꺼진 상태입니다. 준비되면 앱에서 다시 시작하세요."
    print(f"[emergency] 매매 중단 해제 by={by} (was {was['by']}: {was['reason']})", flush=True)
    return {"ok": True, "was": was,
            "message": f"✅ 매매 중단 해제({was['label']})" + note}


def _stop_auto():
    """자동매매 정지 + 재시작 시 자동 재개도 끔(러너는 enabled=false 를 보고 스스로 멈춘다)."""
    from core import control
    c = control.load_config()
    c["enabled"] = False
    c["auto_running"] = False
    control.save_config(c)


def _quote(t: str) -> float:
    try:
        import realtime_feed as rtf
        d = rtf.get_price(t)
        if d and d.get("price"):
            return float(d["price"])
    except Exception:
        pass
    try:
        import yfinance as yf
        return float(getattr(yf.Ticker(t).fast_info, "last_price", 0) or 0)
    except Exception:
        return 0.0


@_locked
def panic(paper: Optional[bool] = None, *, source: str = "app", broker=None, pm=None,
          alert_fn: Optional[Callable[[str], object]] = None,
          log: Optional[Callable[[str], None]] = None,
          quote_fn: Optional[Callable[[str], float]] = None,
          wait: float = 6.0) -> dict:
    """비상 정지. 반환 {"ok", "paper", "canceled", "closed": [...], "failed": [...],
    "pending": [...], "reconcile", "message"}. trade_lock 안에서 돈다 — 진행 중이던
    사이클 주문과 섞이지 않고, 끝난 뒤의 사이클은 매수 금지(halt)·enabled=false 를 본다."""
    from core import control
    L = log or (lambda m: print(f"[panic] {m}", flush=True))
    alert = alert_fn or _default_alert
    if paper is None:
        paper = bool(control.load_config().get("paper", True))
    try:
        _stop_auto()
    except Exception as e:
        L(f"자동매매 정지 기록 실패: {e}")
    if paper:
        return _panic_paper(pm, source, L, quote_fn or _quote)
    return _panic_live(broker, pm, source, L, alert, wait)


def _panic_paper(pm, source, L, quote_fn) -> dict:
    from core.execution import execute_orders
    from portfolio import PortfolioManager
    pm = pm or PortfolioManager(paper=True)
    pm._load_state()
    sells, prices, failed = [], {}, []
    for t, p in pm.positions.items():
        px = float(quote_fn(t) or 0)
        if px <= 0:
            failed.append({"symbol": t, "error": "시세 없음 — 청산 못함"})
            continue
        prices[t] = px
        sells.append({"ticker": t, "shares": p.shares, "reason": "panic (비상 청산)",
                      "est_price": px})
    res = execute_orders({"sell": sells, "buy": []}, prices, True, pm,
                         source="paper", log=L)
    closed = [{"symbol": s["ticker"], "qty": s["shares"], "price": s["price"]}
              for s in res.get("sold") or []]
    msg = (f"🛑 비상 정지(모의) — 자동매매 정지, {len(closed)}종목 청산"
           + (f", 실패 {len(failed)}: " + ", ".join(f["symbol"] for f in failed)
              if failed else ""))
    L(msg)
    return {"ok": not failed, "paper": True, "canceled": 0, "closed": closed,
            "failed": failed, "pending": [], "reconcile": None, "message": msg}


def _panic_live(broker, pm, source, L, alert, wait) -> dict:
    import market_hours as mh
    from broker import Broker
    from core.execution import reconcile_live
    from portfolio import PortfolioManager
    broker = broker or Broker(paper=False)
    pm = pm or PortfolioManager(paper=False)
    out = {"ok": False, "paper": False, "canceled": 0, "closed": [], "failed": [],
           "pending": [], "reconcile": None, "message": ""}

    # 1) 중단을 먼저 디스크에 — 이후 어디서 죽어도 재기동 사이클이 다시 사지 않는다
    try:
        j = _it.load()
    except _it.JournalError as e:
        j = None
        L(f"{e} — 저널 없이 청산만 진행(결과는 다음 대조가 브로커 기준으로 판정)")
    stamp = mh.now_et().strftime("%H%M%S")
    if j is not None:
        _it.set_halt(j, f"비상 정지({source}) — 열린 주문 취소 + 전 종목 청산",
                     sig=f"panic:{stamp}", by="panic")
        _it.save(j)

    # 2) 브로커: 열린 주문 전부 취소 → 전 종목 시장가 청산
    try:
        held = {t.upper(): float(v.get("shares") or 0)
                for t, v in broker.get_positions(strict=True).items()}
    except Exception as e:
        held = {}
        L(f"보유 조회 실패(청산은 계속): {e}")
    try:
        out["canceled"] = len(broker.cancel_all_orders())
    except Exception as e:
        L(f"주문 일괄 취소 실패(청산 요청이 cancel_orders=True 로 다시 취소): {e}")
    try:
        closes = broker.close_all_positions()
    except Exception as e:
        msg = f"🛑 비상 정지 — 전 종목 청산 요청 실패: {e}\n브로커 앱/웹에서 직접 청산하세요."
        L(msg)
        alert(msg)
        out["message"] = msg
        return out

    # 3) 청산 주문을 저널에 기록 — 체결은 reconcile_live 가 장부에 반영한다
    if j is not None:
        cid = f"p{stamp}"
        for c in closes:
            t = c["symbol"]
            iid = _it.make_intent_id(cid, t, "sell")
            if iid in j["intents"]:
                continue
            qty = int(held.get(t) or c.get("qty") or 0)
            rec = _it.open_intent(j, iid, ticker=t, side="sell", qty=qty, est_price=0.0,
                                  source="panic", reason="panic (비상 청산)",
                                  cycle_id=cid, kind=_it.PANIC)
            # 상태는 '제출됨'으로만 남긴다 — 체결 수량·가격은 대조가 주문 id 로 브로커에
            # 다시 물어 확정한다(청산 응답 본문의 체결 정보는 비어 있거나 이르다).
            child = {"coid": c.get("coid") or f"panic-{c.get('order_id') or t}",
                     "id": c.get("order_id") or None, "qty": qty, "kind": "market",
                     "status": "rejected" if c.get("error") else "submitted",
                     "filled_qty": 0, "fill_price": 0.0}
            if c.get("error"):
                child["error"] = c["error"]
            _it.upsert_child(rec, child)
            _it.refresh(rec, final=True)
        _it.save(j)
    for c in closes:
        (out["failed"] if c.get("error") else out["closed"]).append(c)

    # 4) 잠깐 체결을 기다렸다가 대조로 장부 반영(장 마감 중이면 개장 때 체결 → 다음 대조)
    deadline = time.time() + max(0.0, wait)
    while True:
        try:
            left = {t for t, v in broker.get_positions(strict=True).items()
                    if float(v.get("shares") or 0)}
        except Exception:
            left = None
        if left == set() or time.time() >= deadline:
            break
        time.sleep(0.5)
    try:
        out["reconcile"] = reconcile_live(broker, pm, None, alert_fn=lambda m: None, log=L)
    except Exception as e:
        L(f"청산 후 대조 실패 — 다음 사이클 대조가 반영: {e}")
    out["pending"] = sorted(left or [])
    out["ok"] = not out["failed"]
    msg = (f"🛑 비상 정지 — 자동매매 정지·매수 중단, 주문 {out['canceled']}건 취소, "
           f"{len(out['closed'])}종목 청산 요청")
    if out["failed"]:
        msg += "\n청산 실패: " + ", ".join(f"{c['symbol']}({c['error'][:40]})"
                                        for c in out["failed"])
    if out["pending"]:
        msg += ("\n아직 체결 대기: " + ", ".join(out["pending"])
                + " — 장이 닫혀 있으면 개장 때 체결, 다음 대조가 장부에 반영")
    msg += "\n해제는 /resume 또는 앱 설정의 '매매 재개'."
    L(msg)
    alert(msg)
    out["message"] = msg
    return out
