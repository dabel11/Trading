"""매매 모드 전환 안전장치 (감사 #21) — 앱·텔레그램 공통 규칙.

예전엔 텔레그램 /실거래 한 번, 앱 버튼 한 번으로 즉시 바뀌었다. 자동매매가 도는 중이거나
보유가 남아 있어도 허용돼, 보유가 손절 관리 밖으로 떨어지거나 의도치 않게 실제 돈으로
주문이 나갈 수 있었다. 이제:

  · 전제조건(check): 자동매매 정지 · 현재 모드 보유 0 · 미해결 실거래 주문 0
                     → 실거래로 갈 땐 Alpaca 키 유효 + 계좌 연결 확인
  · 실거래로 가는 전환은 2단계: request_live() 가 확인 코드(2분 유효)를 발급하고,
    confirm_live(code) 가 전제조건을 다시 확인한 뒤에만 전환한다.
  · 어느 계좌로 가는지 항상 표시: PK 키 = Alpaca 페이퍼(가상 돈), AK 키 = 실계좌(실제 돈).
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Optional

CODE_TTL = 120            # 확인 코드 유효 시간(초)

_PENDING: dict = {}       # who -> {"code", "expires"}
_LOCK = threading.Lock()


def target_label() -> tuple:
    """(라벨, 실제 돈 여부) — '실거래' 모드에서 주문이 실제로 가는 곳."""
    try:
        import broker
        real = not broker._paper_endpoint(False)
    except Exception:
        import config
        real = config.alpaca_key_kind() == "live"
    return ("실계좌 — 실제 돈" if real else "Alpaca 페이퍼 계좌 — 가상 돈(실주문 경로 검증)"), real


def _auto_running(cfg: dict) -> bool:
    try:
        from core import control
        alive = control.daemon_alive()
    except Exception:
        alive = False
    return bool(cfg.get("auto_running")) or alive


def _positions(paper: bool) -> list:
    try:
        import portfolio
        return sorted(portfolio.PortfolioManager(paper=paper).positions)
    except Exception:
        return []


def _unresolved_live() -> Optional[list]:
    """미해결 실거래 주문 의도(종목) — 저널 손상이면 None."""
    try:
        from core import intents as _it
        return sorted({r["ticker"] for r in _it.unresolved(_it.load())})
    except Exception:
        return None


def check(to_paper: bool, cfg: Optional[dict] = None, verify_broker: bool = True) -> dict:
    """전환 전제조건. 반환 {"ok", "blockers": [..], "notes": [..], "target", "real_money"}."""
    from core import control
    cfg = cfg if cfg is not None else control.load_config()
    cur_paper = bool(cfg.get("paper", True))
    blockers, notes = [], []
    target, real = target_label()

    if cur_paper == bool(to_paper):
        blockers.append(f"이미 {'모의' if to_paper else '실거래'} 모드입니다")
    if _auto_running(cfg):
        blockers.append("자동매매가 실행 중입니다 — 먼저 정지하세요")
    held = _positions(cur_paper)
    if held:
        blockers.append(f"현재 모드({'모의' if cur_paper else '실거래'})에 보유 {len(held)}종목이 남아 있습니다 "
                        f"({', '.join(held[:6])}) — 전환하면 손절 관리 밖으로 떨어집니다. 먼저 정리하세요")
    unresolved = _unresolved_live()
    if unresolved is None:
        blockers.append("실거래 주문 저널이 손상돼 열린 주문을 확인할 수 없습니다")
    elif unresolved and not cur_paper:
        blockers.append(f"미해결 실거래 주문이 있습니다({', '.join(unresolved)}) — 체결·취소 확정 후 전환하세요")

    if not to_paper:
        try:
            import config
            ready = config.alpaca_ready()
        except Exception:
            ready = False
        if not ready:
            blockers.append("Alpaca API 키가 없습니다 — 설정 → Alpaca 카드에서 저장하세요")
        elif verify_broker:
            try:
                import alpaca_api
                a = alpaca_api.account()
            except Exception as e:
                a = {"ok": False, "error": str(e)}
            if not a.get("ok"):
                blockers.append(f"Alpaca 계좌 연결 실패: {a.get('error', '알 수 없음')}")
            else:
                notes.append(f"계좌 {a.get('account', '')} · 자산 ${a.get('equity', 0):,.0f} · "
                             f"매수가능 ${a.get('buying_power', 0):,.0f} · 상태 {a.get('status', '')}")
        notes.insert(0, f"주문이 가는 곳: {target}")
    return {"ok": not blockers, "blockers": blockers, "notes": notes,
            "target": target, "real_money": real}


def _apply(to_paper: bool):
    from core import control
    cfg = control.load_config()
    cfg["paper"] = bool(to_paper)
    control.save_config(cfg)


def switch_to_paper(cfg: Optional[dict] = None) -> dict:
    """실거래 → 모의 (위험을 줄이는 방향이라 1단계. 전제조건은 동일하게 확인)."""
    r = check(True, cfg, verify_broker=False)
    if r["ok"]:
        _apply(True)
    return r


def request_live(who: str = "app", cfg: Optional[dict] = None) -> dict:
    """1단계: 전제조건 확인 → 통과하면 확인 코드 발급. 반환 check() + {"code", "ttl"}."""
    r = check(False, cfg)
    if r["ok"]:
        code = f"{secrets.randbelow(9000) + 1000}"
        with _LOCK:
            _PENDING[who] = {"code": code, "expires": time.time() + CODE_TTL}
        r.update(code=code, ttl=CODE_TTL)
    return r


def confirm_live(code: str, who: str = "app", cfg: Optional[dict] = None) -> dict:
    """2단계: 코드·만료 확인 → 전제조건 재확인 → 전환. 반환 check() + {"switched"}."""
    with _LOCK:
        p = _PENDING.get(who)
        valid = bool(p) and str(code).strip() == p["code"] and time.time() <= p["expires"]
        if valid:
            _PENDING.pop(who, None)
    if not valid:
        return {"ok": False, "switched": False,
                "blockers": ["확인 코드가 틀렸거나 만료됐습니다 — 처음부터 다시 요청하세요"],
                "notes": [], "target": target_label()[0], "real_money": target_label()[1]}
    r = check(False, cfg)
    r["switched"] = False
    if r["ok"]:
        _apply(False)
        r["switched"] = True
    return r


def cancel(who: str = "app"):
    with _LOCK:
        _PENDING.pop(who, None)
