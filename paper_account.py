"""
모의투자(페이퍼) 가상 현금 계좌 — Alpaca 키 없이 로컬에서 동작.

페이퍼 모드 매매는 브로커를 호출하지 않고 현재가로 즉시 체결하며,
체결 금액만큼 이 가상 현금에서 차감/증가시킨다.
포지션 자체는 PortfolioManager(paper=True) 가 state_paper.json 으로 관리한다.

저장 구조 (paper_account.json):
  {"cash": 현재 가상현금, "seed": 시작 자본}
seed 는 수익률 계산 기준선이며, reset 시 새 시작자본으로 갱신된다.
"""
from pathlib import Path
from safe_store import atomic_write_json, safe_read_json, trade_lock
from config import CAPITAL_TOTAL

PAPER_FILE = Path(__file__).parent / "paper_account.json"


def _load() -> dict:
    d = safe_read_json(PAPER_FILE, default={"cash": CAPITAL_TOTAL, "seed": CAPITAL_TOTAL})
    # 구버전 파일(seed 없음) 호환
    if "seed" not in d:
        d["seed"] = float(d.get("cash", CAPITAL_TOTAL))
    return d


def cash() -> float:
    return float(_load().get("cash", CAPITAL_TOTAL))


def seed() -> float:
    """시작 자본 (수익률 계산 기준선)."""
    return float(_load().get("seed", CAPITAL_TOTAL))


def adjust(delta: float) -> float:
    """현금을 delta만큼 변동시키고 새 잔고 반환 (매수 음수, 매도 양수).

    읽기→계산→쓰기 전체를 trade_lock 으로 감싼다. 데몬과 앱이 동시에
    현금을 건드려도 한쪽 변동이 유실되지 않는다(lost-update 방지)."""
    with trade_lock():
        d = _load()
        new = float(d.get("cash", CAPITAL_TOTAL)) + float(delta)
        d["cash"] = new
        atomic_write_json(PAPER_FILE, d)
        return new


def reset(amount: float | None = None):
    """모의 현금·시작자본을 초기화 (기본 = 설정 총자본)."""
    amt = float(amount) if amount else float(CAPITAL_TOTAL)
    atomic_write_json(PAPER_FILE, {"cash": amt, "seed": amt})
