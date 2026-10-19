"""장부 손상 fail-closed (감사 #13) — 손상된 장부로 조용히 매매를 이어가지 않는다.

safe_read_json 은 손상 파일을 <이름>.corrupt 로 옮기고 기본값(빈 보유·시작 현금)을 돌려준다.
그대로 두면 모의에선 보유를 잊은 채 새로 사고 현금이 시작 자본으로 되살아나며, 실거래에선
보유가 손절 관리 밖으로 떨어진다.

check() 는 사이클 시작에 핵심 장부(보유·현금)의 손상 흔적을 찾으면 주문 저널에 매매 중단
(by="ledger")을 건다. 중단은 사용자가 장부를 백업에서 되살린 뒤 /resume(앱 버튼)으로 푼다.
  · 모의: 자동 주문 전부 보류 — 브로커라는 진실의 원천이 없어 매도도 근거가 없다
  · 실거래: 매수만 보류 — execute_orders 가 저널 halt 를 보고 처리하고, 손절·청산 매도는
    브로커 보유 한도 안에서 계속한다(사이클 첫 단계 대조가 브로커 보유를 기준으로 삼는다)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

CRITICAL = {True: ("state_paper.json", "paper_account.json"),   # 모의: 보유 + 가상 현금
            False: ("state.json",)}                              # 실거래: 보유(현금은 브로커)


def _dir() -> Path:
    import portfolio
    return Path(portfolio.STATE_FILE).parent


def scan(paper: bool) -> list:
    """현재 모드의 핵심 장부 손상 흔적: 읽히지 않는 파일, 처리 안 된 .corrupt 백업."""
    d = _dir()
    found = []
    for name in CRITICAL[bool(paper)]:
        p = d / name
        if p.exists():
            try:
                ok = isinstance(json.loads(p.read_text(encoding="utf-8")), dict)
            except Exception:
                ok = False
            if not ok:
                found.append({"file": name, "kind": "읽기 실패"})
        if (d / (name + ".corrupt")).exists():
            found.append({"file": name, "kind": "손상 백업(.corrupt) 발견"})
    return found


def _archive(found: list):
    """감지한 .corrupt 백업에 시각을 붙여 보관 — 사용자가 해제한 뒤 같은 흔적으로 다시 멈추지 않게."""
    d = _dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in found:
        c = d / (f["file"] + ".corrupt")
        if c.exists():
            try:
                c.replace(d / (f["file"] + f".corrupt-{stamp}"))
            except OSError:
                pass


def check(paper: bool, log: Optional[Callable[[str], None]] = None,
          alert: Optional[Callable[[str], object]] = None) -> dict:
    """반환 {"halted", "block_all", "reason"}. block_all=True 면 이번 사이클 주문을 전부 보류."""
    from core import intents as _it
    from safe_store import trade_lock
    L = log or (lambda m: None)
    found = scan(paper)
    with trade_lock():
        try:
            j = _it.load()
        except _it.JournalError as e:
            L(f"주문 저널 손상 — 자동 주문 전부 보류: {e}")
            return {"halted": True, "block_all": True, "reason": str(e)}
        if found:
            files = sorted({f["file"] for f in found})
            reason = ("장부 손상 — " + ", ".join(f"{f['file']} {f['kind']}" for f in found)
                      + " · backups/ 의 백업으로 장부를 되살린 뒤 /resume 으로 해제")
            new = _it.set_halt(j, reason, sig="ledger:" + ",".join(files), by="ledger")
            _it.save(j)
            _archive(found)
            if new and alert:
                try:
                    alert(f"🛑 자동매매 중단 — 장부 파일 손상 감지\n{reason}\n"
                          + ("모의: 자동 주문을 전부 보류합니다." if paper else
                             "실거래: 매수를 보류하고, 손절·청산 매도는 브로커 보유 한도 안에서 계속합니다."))
                except Exception:
                    pass
        h = _it.halted(j)
    if h and h.get("by") == "ledger":
        L(f"장부 손상으로 매매 중단 중 — {h.get('reason')}")
        return {"halted": True, "block_all": bool(paper), "reason": h.get("reason", "")}
    return {"halted": False, "block_all": False, "reason": ""}
