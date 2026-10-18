"""실거래 주문 의도(intent) 저널 — 주문 전에 기록하고, 결과는 브로커 상태로 확정한다.

  intent_id       = f"{ET 거래일 YYYYMMDD}-{cycle_id}-{TICKER}-{side}"
  client_order_id = f"ait-{intent_id}"  (같은 의도의 n번째 재주문은 "-r{n}")

coid 가 의도에서 결정적으로 나오므로, 크래시 뒤 재기동해도 get_order_by_client_id 로
"그 의도가 브로커에서 어떻게 됐는지"를 다시 물어볼 수 있다(core.execution.reconcile_live).

상태: PENDING → SUBMITTED → PARTIAL → FILLED | CANCELED | REJECTED | EXPIRED
  - 자식 주문(재가격·재시도)이 하나라도 브로커에서 열려 있으면 SUBMITTED/PARTIAL
  - 최종 상태가 아니거나, 체결됐는데 장부에 아직 반영 안 된 수량(filled > booked)이
    있으면 '미해결' — 같은 종목·방향의 새 주문은 내지 않는다.

저장은 실거래 장부(portfolio.STATE_FILE)와 같은 디렉터리의 order_intents.json.
손상되면 fail-closed(JournalError) — 진행 중이던 주문을 잊은 채 매매하지 않는다.
모든 쓰기는 호출부가 trade_lock 안에서 한다(core.execution).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from safe_store import atomic_write_json

PENDING, SUBMITTED, PARTIAL = "PENDING", "SUBMITTED", "PARTIAL"
FILLED, CANCELED, REJECTED, EXPIRED = "FILLED", "CANCELED", "REJECTED", "EXPIRED"
FINAL = frozenset({FILLED, CANCELED, REJECTED, EXPIRED})
_NEXT = {
    PENDING:   {SUBMITTED, PARTIAL} | FINAL,
    SUBMITTED: {SUBMITTED, PARTIAL} | FINAL,
    PARTIAL:   {PARTIAL, FILLED, CANCELED, EXPIRED},
}

# 브로커(Alpaca) 주문 상태 중 더 이상 체결이 붙지 않는 것.
# not_found = 저널엔 있는데 브로커엔 없는 주문(제출 전에 죽음), unsent = 제출 안 함.
CHILD_FINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced",
                         "done_for_day", "not_found", "unsent"})

JOURNAL_FILE: Optional[Path] = None   # 테스트 등에서 직접 지정할 때만
_KEEP_DAYS = 7                        # 해결된 의도 보관 기간(달력일)


class JournalError(RuntimeError):
    """저널 파일 손상 — 실거래를 멈춰야 한다."""


def path() -> Path:
    if JOURNAL_FILE is not None:
        return Path(JOURNAL_FILE)
    import portfolio
    return Path(portfolio.STATE_FILE).parent / "order_intents.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load() -> dict:
    p = path()
    if not p.exists():
        return {"intents": {}, "halt": None}
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(j, dict) or not isinstance(j.get("intents", {}), dict):
            raise ValueError("형식 오류")
    except Exception as e:
        raise JournalError(f"주문 저널 손상({p.name}): {e}") from e
    j.setdefault("intents", {})
    j.setdefault("halt", None)
    return j


def save(j: dict):
    atomic_write_json(path(), j)


# ── 식별자 ────────────────────────────────────────────────────────────────

def default_cycle_id(source: str = "auto") -> str:
    """호출부가 cycle_id 를 안 줬을 때 — 출처 접두어 + ET 시각(초).
    같은 초에 같은 종목·방향을 두 번 보내면 같은 의도로 보고 한 번만 낸다."""
    import market_hours as mh
    return f"{(source or 'x')[:1]}{mh.now_et().strftime('%H%M%S')}"


def make_intent_id(cycle_id: str, ticker: str, side: str, day=None) -> str:
    import market_hours as mh
    d = day or mh.trading_day()
    return f"{d.strftime('%Y%m%d')}-{cycle_id}-{ticker.upper()}-{side}"


def coid_base(intent_id: str) -> str:
    return f"ait-{intent_id}"


def child_coid(intent_id: str, n: int) -> str:
    return coid_base(intent_id) + (f"-r{n}" if n else "")


# ── 의도 레코드 ──────────────────────────────────────────────────────────

def open_intent(j: dict, intent_id: str, *, ticker: str, side: str, qty: int,
                est_price: float, source: str, reason: str = "",
                score: float = 0.0, cycle_id: str = "") -> dict:
    """새 의도를 PENDING 으로 만든다(호출부가 save 로 먼저 디스크에 남긴 뒤 주문)."""
    import market_hours as mh
    rec = {
        "intent_id": intent_id, "trading_day": mh.trading_day().isoformat(),
        "cycle_id": cycle_id, "ticker": ticker.upper(), "side": side,
        "req_qty": int(qty), "est_price": float(est_price or 0),
        "source": source, "reason": reason, "score": float(score or 0),
        "state": PENDING, "orders": [],
        "filled_qty": 0.0, "filled_notional": 0.0,
        "booked_qty": 0.0, "booked_notional": 0.0,
        "created": _now(), "updated": _now(), "history": [[_now(), PENDING, ""]],
    }
    j["intents"][intent_id] = rec
    return rec


def transition(rec: dict, state: str, note: str = ""):
    cur = rec["state"]
    if state == cur and state in FINAL:
        return
    if cur in FINAL or state not in _NEXT.get(cur, ()):
        raise ValueError(f"잘못된 상태 전이 {rec['intent_id']}: {cur} → {state}")
    if state != cur or note:
        rec["history"].append([_now(), state, note])
    rec["state"] = state
    rec["updated"] = _now()


def upsert_child(rec: dict, child: dict):
    """브로커 실행기(Broker.execute_order)가 넘긴 자식 주문 상태를 반영."""
    keep = ("coid", "id", "qty", "kind", "limit", "status", "filled_qty",
            "fill_price", "error")
    c = {k: child.get(k) for k in keep if k in child}
    for i, old in enumerate(rec["orders"]):
        if old.get("coid") == c.get("coid"):
            rec["orders"][i] = {**old, **c}
            break
    else:
        rec["orders"].append(c)
    _recompute(rec)


def _recompute(rec: dict):
    fq = sum(float(c.get("filled_qty") or 0) for c in rec["orders"])
    fn = sum(float(c.get("filled_qty") or 0) * float(c.get("fill_price") or 0)
             for c in rec["orders"])
    rec["filled_qty"], rec["filled_notional"] = fq, fn
    rec["updated"] = _now()


def has_open_child(rec: dict) -> bool:
    return any(str(c.get("status") or "").lower() not in CHILD_FINAL
               for c in rec["orders"])


def refresh(rec: dict, final: bool):
    """자식 주문들로 상태를 다시 매긴다.

    final=False: 아직 실행기가 이 의도를 붙잡고 있다(재주문 예정일 수 있음) → 진행 상태만.
    final=True : 실행기가 손을 뗐다 — 열린 자식이 없으면 최종 상태로 확정."""
    if rec["state"] in FINAL:
        return
    _recompute(rec)
    fq, req = rec["filled_qty"], rec["req_qty"]
    if fq >= req > 0:
        transition(rec, FILLED)
        return
    if has_open_child(rec) or not final:
        if rec["orders"]:
            transition(rec, PARTIAL if fq > 0 else SUBMITTED)
        return
    sts = [str(c.get("status") or "").lower() for c in rec["orders"]]
    if fq <= 0 and sts and all(s == "rejected" for s in sts):
        transition(rec, REJECTED)
    elif sts and sts[-1] in ("expired", "done_for_day"):
        transition(rec, EXPIRED)
    else:
        transition(rec, CANCELED, "부분 체결 후 종료" if fq > 0 else "")


def unbooked(rec: dict) -> tuple:
    """(장부 미반영 수량, 그 평균 체결가)."""
    q = float(rec["filled_qty"]) - float(rec["booked_qty"])
    if q <= 1e-9:
        return 0.0, 0.0
    n = float(rec["filled_notional"]) - float(rec["booked_notional"])
    return q, (n / q if q else 0.0)


def mark_booked(rec: dict, qty: float, price: float):
    rec["booked_qty"] = float(rec["booked_qty"]) + float(qty)
    rec["booked_notional"] = float(rec["booked_notional"]) + float(qty) * float(price)
    rec["updated"] = _now()


def is_unresolved(rec: dict) -> bool:
    return rec["state"] not in FINAL or unbooked(rec)[0] > 0


def unresolved(j: dict, ticker: Optional[str] = None,
               side: Optional[str] = None) -> list:
    return [r for r in j["intents"].values()
            if is_unresolved(r)
            and (ticker is None or r["ticker"] == ticker.upper())
            and (side is None or r["side"] == side)]


def count_today(j: dict, side: Optional[str] = None) -> int:
    """오늘(ET 거래일) 브로커에 실제로 낸 의도 수."""
    import market_hours as mh
    d = mh.trading_day().isoformat()
    return sum(1 for r in j["intents"].values()
               if r.get("trading_day") == d and r["orders"]
               and (side is None or r["side"] == side))


def prune(j: dict, keep_days: int = _KEEP_DAYS):
    import market_hours as mh
    cut = (mh.trading_day() - timedelta(days=keep_days)).isoformat()
    for k in [k for k, r in j["intents"].items()
              if not is_unresolved(r) and str(r.get("trading_day", "")) < cut]:
        j["intents"].pop(k, None)


# ── 매매 중단(대조 불일치) ───────────────────────────────────────────────

def halted(j: dict) -> Optional[dict]:
    return j.get("halt") or None


def set_halt(j: dict, reason: str, sig: str = "", by: str = "reconcile") -> bool:
    """중단 기록. 사유(sig)가 새로 바뀌었으면 True — 호출부는 그때만 경보한다."""
    prev = j.get("halt") or {}
    j["halt"] = {"reason": reason, "sig": sig, "by": by,
                 "since": prev.get("since") or _now(), "updated": _now()}
    return prev.get("sig") != sig


def clear_halt(j: Optional[dict] = None):
    """중단 해제. j 를 안 주면 디스크의 저널을 직접 고친다(사용자 수동 해제용)."""
    if j is not None:
        j["halt"] = None
        return
    from safe_store import trade_lock
    with trade_lock():
        jj = load()
        jj["halt"] = None
        save(jj)
