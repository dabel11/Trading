"""
일일 손실 한도 (kill switch).

당일 시작 자산 대비 손실률이 한도(기본 -5%)를 넘으면
신규 매수를 전면 차단해 연쇄 손실을 끊는다.

day_state.json:
  {
    "date": "2026-06-04",
    "start_equity": 10000.0,
    "halted": false,
    "halt_reason": ""
  }
"""

from datetime import date
from pathlib import Path
from safe_store import atomic_write_json, safe_read_json

DAY_FILE = Path(__file__).parent / "day_state.json"

# 기본 일일 손실 한도 (-5%)
DEFAULT_DAILY_LOSS_LIMIT = 0.05


def _load() -> dict:
    return safe_read_json(DAY_FILE, default={})


def _save(d: dict):
    atomic_write_json(DAY_FILE, d)


def start_of_day(current_equity: float, start_equity: float | None = None) -> dict:
    """당일 첫 호출 시 시작 자산을 기록 (날짜가 바뀌면 리셋).

    start_equity가 주어지면(예: 브로커의 전일 종가 자산 last_equity) 그것을
    당일 기준 앵커로 쓴다. 없으면 첫 호출 시점의 current_equity를 쓴다 —
    앱을 장중에 늦게 켜면 이미 하락한 값이 기준이 돼 손실률이 과소평가되므로,
    가능하면 전일 종가 기준 앵커를 넘기는 것이 정확하다.
    """
    d = _load()
    today = date.today().isoformat()
    if d.get("date") != today:
        anchor = float(start_equity) if start_equity else float(current_equity)
        d = {
            "date": today,
            "start_equity": anchor,
            "halted": False,
            "halt_reason": "",
        }
        _save(d)
    return d


def check(current_equity: float, loss_limit: float = DEFAULT_DAILY_LOSS_LIMIT,
          start_equity: float | None = None) -> dict:
    """
    현재 자산으로 당일 손실률을 평가.
    반환: {halted, daily_pnl_pct, start_equity, halt_reason}
    """
    d = start_of_day(current_equity, start_equity=start_equity)
    start = d.get("start_equity", current_equity) or current_equity
    pnl_pct = (current_equity - start) / start if start else 0.0

    if not d.get("halted") and pnl_pct <= -abs(loss_limit):
        d["halted"] = True
        d["halt_reason"] = f"일일 손실 한도 도달 ({pnl_pct:.1%})"
        _save(d)

    return {
        "halted":        d.get("halted", False),
        "daily_pnl_pct": pnl_pct,
        "start_equity":  start,
        "halt_reason":   d.get("halt_reason", ""),
        "limit":         loss_limit,
    }


def is_halted() -> bool:
    d = _load()
    if d.get("date") != date.today().isoformat():
        return False        # 날짜 바뀌면 자동 해제
    return d.get("halted", False)


def reset():
    """수동 해제 (사용자가 명시적으로 거래 재개)."""
    d = _load()
    d["halted"] = False
    d["halt_reason"] = ""
    _save(d)
