"""
Portfolio manager: decides what to buy/sell based on scores,
manages capital rotation within the fixed pool.

Capital rotation logic:
  - Rank all scored stocks
  - Fill available slots with top scorers above MIN_SCORE_TO_BUY
  - Exit positions whose score dropped below SELL_SCORE_THRESHOLD
    OR hit stop-loss / take-profit / max hold days
  - Freed capital immediately re-deploys into next best opportunity
"""

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from config import (
    CAPITAL_TOTAL,
    MAX_POSITIONS,
    MAX_POSITION_PCT,
    MIN_SCORE_TO_BUY,
    SELL_SCORE_THRESHOLD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    HOLD_DAYS_STRONG,
    HOLD_DAYS_MEDIUM,
)
from scorer import StockScore
from safe_store import atomic_write_json, safe_read_json, trade_lock

STATE_FILE = Path(__file__).parent / "state.json"
# 모의(페이퍼) 전용 장부 — 실거래 state.json 과 절대 섞이지 않도록 분리.
PAPER_STATE_FILE = Path(__file__).parent / "state_paper.json"

# 트레일링 스탑: 목표(TAKE_PROFIT_PCT) 도달 후, 고점 대비 이만큼 되돌리면 청산.
# 하드 익절이 승자를 가두는 문제를 줄이고 추세를 끝까지 끌고 간다.
# app.py 의 투자기간(horizon) 설정이 이 값을 동적으로 덮어쓸 수 있다.
TRAIL_GIVEBACK_PCT = 0.08

# 유동형(동적 배분) 모드 파라미터
# REBAL_BAND : 보유와 목표의 차이가 목표의 이 비율을 넘을 때만 매수/매도(잦은 거래 억제)
# ROTATE_MARGIN : 슬롯이 꽉 찼을 때, 신규 후보가 최약 보유보다 이 점수 이상 높아야 교체
REBAL_BAND = 0.25
ROTATE_MARGIN = 10.0
# MIN_ROTATE_DAYS : 이 일수 미만 보유는 회전(목표 이탈)으로 팔지 않는다. 손절·트레일링·점수 급락·
#   보유만료 같은 위험 청산은 그대로. 재시작 직후 전체시장 스캔이 진행되며 순위가 바뀌어
#   5분 전에 산 종목을 갈아타던 낭비를 막는다(2026-10-19 AAPL·GOOGL→GOOG 등, 감사 #18 회전율).
MIN_ROTATE_DAYS = 1
# 같은 발행사의 다른 주식 종류 — 한 종목만 보유(GOOGL 팔고 GOOG 사는 식의 무의미한 교체 방지)
SAME_ISSUER = {"GOOG": "GOOGL", "BRK.A": "BRK.B", "FOX": "FOXA", "NWS": "NWSA",
               "UA": "UAA", "LEN.B": "LEN", "HEI.A": "HEI", "BF.A": "BF.B"}


def _issuer(ticker: str) -> str:
    return SAME_ISSUER.get(ticker, ticker)

# 매수 가격대 제한 — 사용자가 전략 선택에서 설정 (0 = 무제한).
# 예: 하한 $20(동전주 배제), 상한 $500(고가주 배제 → 적은 시드로 분산 확보)
BUY_PRICE_MIN = 0.0
BUY_PRICE_MAX = 0.0

# 손절 후 재진입 쿨다운(일) — 손절당한 종목을 바로 다시 사는 휩쏘(whipsaw) 방지.
# 실무 추세추종 시스템의 표준 안전장치.
REENTRY_COOLDOWN_DAYS = 3

# 재진입 가격 히스테리시스 — 판 가격에서 이만큼(1%) 움직이기 전엔 같은 종목을
# 되사지 않는다. 주가 변화가 거의 없는데 매도↔매수만 반복하는 낭비 거래의
# 근본 차단. 가격이 실제로 움직이면(신호가 유효해지면) 즉시 재진입 가능.
MIN_REENTRY_MOVE_PCT = 0.01

# 섹터 집중 상한 — 같은 섹터 최대 보유 종목 수 (5종목 중 3개까지).
# 점수 상위가 한 섹터(예: 테크)에 몰려 포트폴리오가 사실상 단일 베팅이 되는 것 방지.
MAX_PER_SECTOR = 3

# 소액 자본 단위 정책 (기본 정책 — 별도 모드 아님, lot_plan 참고)
# 자본이 적은데 MAX_POSITIONS 개로 나누면 종목당 1~2주짜리 자투리가 되고, 정수 주식
# 반올림 탓에 목표와 실제 비중이 크게 어긋나며(목표 $400 에 $250 1주 = 62%) 비싼 종목은 0주다.
# MIN_POSITION_VALUE : 한 종목에 최소 이만큼은 싣는다 → 슬롯 수 = 자본 ÷ 이 값 (1~MAX_POSITIONS)
# MIN_LOT_FILL       : 신규 매수는 정수 주식으로 최소 단위의 이 비율 이상을 살 수 있을 때만
# LOT_TRIM_SLACK     : 트림 기준 상한은 자본을 (1+이 값)으로 나눠 계산 — 자본이 슬롯 문턱
#                      ($1,000 등)을 오르내릴 때 '반 팔고 → 되사는' 왕복 방지
MIN_POSITION_VALUE = 500.0
MIN_LOT_FILL = 0.5
LOT_TRIM_SLACK = 0.5


# 백테스트가 '오늘'을 시뮬레이션 날짜로 갈아끼울 수 있게 하는 인디렉션.
# 평소엔 실제 오늘(ET 거래일)을 그대로 쓰고, 백테스터만 set_clock()으로
# 과거 날짜를 주입한다. 이렇게 해야 보유기간·재진입 쿨다운 등 '날짜에 의존하는'
# 라이브 로직을 백테스트에서 그대로(같은 코드로) 재현할 수 있다.
_CLOCK: Optional[date] = None


def _today() -> date:
    # ET 기준 날짜 — 진입일·보유일·쿨다운이 KST 자정(미국 장중)에 넘어가지 않게.
    if _CLOCK is not None:
        return _CLOCK
    import market_hours as _mh
    return _mh.trading_day()


def set_clock(d: Optional[date]):
    """백테스트용 가상 '오늘' 설정. None이면 실제 시계로 복귀."""
    global _CLOCK
    _CLOCK = d


def _sector_of(ticker: str) -> str:
    """종목 → 섹터 ETF 키 (모르면 '' → 상한 미적용)."""
    try:
        from signals.sector import TICKER_SECTOR
        return TICKER_SECTOR.get(ticker, "")
    except Exception:
        return ""


def _price_in_band(price: float) -> bool:
    """매수 가격대(상·하한) 통과 여부. 0이면 해당 제한 없음."""
    if BUY_PRICE_MIN and price < BUY_PRICE_MIN:
        return False
    if BUY_PRICE_MAX and price > BUY_PRICE_MAX:
        return False
    return True


def lot_plan(equity: float) -> tuple[int, float, float]:
    """소액 자본 단위 정책 → (최대 종목 수, 종목당 상한 비율, 신규 매수 최소 금액).

    자본이 충분하면 설정 그대로이고, 적을수록 종목 수를 줄이고 한 종목에 더 싣는다.
    (기본값 MIN_POSITION_VALUE $500 · MAX_POSITIONS 5 · MAX_POSITION_PCT 25%)
      $2,500 이상 → 5종목 · 25%  · 최소 $250   ← 설정 그대로
      $1,500      → 3종목 · 33%  · 최소 $250
      $1,000      → 2종목 · 50%  · 최소 $250
      $800        → 1종목 · 100% · 최소 $250
      $400        → 1종목 · 100% · 최소 $200
    """
    eq = max(float(equity), 0.0)
    n = max(1, min(int(MAX_POSITIONS), int(eq // MIN_POSITION_VALUE)))
    cap = max(float(MAX_POSITION_PCT), 1.0 / n)
    return n, cap, MIN_LOT_FILL * min(MIN_POSITION_VALUE, eq)


def _lot_value(amount: float, price: float) -> float:
    """amount 로 살 수 있는 정수 주식의 금액."""
    return int(amount / price) * price if price > 0 else 0.0


class Position:
    def __init__(self, ticker: str, entry_price: float, shares: float,
                 entry_date: str, score_at_entry: float,
                 peak_price: float | None = None):
        self.ticker = ticker
        self.entry_price = entry_price
        self.shares = shares
        self.entry_date = entry_date          # ISO date string
        self.score_at_entry = score_at_entry
        # 트레일링 스탑용 고점(최고가) 추적. 구버전 state엔 없으므로 진입가로 초기화.
        self.peak_price = float(peak_price) if peak_price else float(entry_price)

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares

    def max_hold_days(self) -> int:
        if self.score_at_entry >= 75:
            return HOLD_DAYS_STRONG
        return HOLD_DAYS_MEDIUM

    def days_held(self) -> int:
        entry = date.fromisoformat(self.entry_date)
        return (_today() - entry).days

    def update_peak(self, price: float) -> bool:
        """현재가가 신고가면 peak 갱신. 갱신됐으면 True."""
        if price and price > self.peak_price:
            self.peak_price = float(price)
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "entry_price": self.entry_price,
            "shares": self.shares,
            "entry_date": self.entry_date,
            "score_at_entry": self.score_at_entry,
            "peak_price": self.peak_price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)


class PortfolioManager:
    def __init__(self, paper: bool = False):
        """paper=True → 모의 장부(state_paper.json/trades_paper.json),
        paper=False → 실거래 장부(state.json/trades.json).

        파일 경로는 인스턴스 단위로 잡아, 같은 프로세스에서 모의·실거래
        매니저를 동시에 다뤄도 서로 간섭하지 않는다. 경로를 모듈 전역
        STATE_FILE 기준으로 파생하므로 테스트에서 STATE_FILE 만 임시경로로
        바꿔치기하면 모의/실거래 양쪽이 함께 격리된다."""
        self.paper = paper
        if paper:
            # 테스트 격리( STATE_FILE 패치) 시에도 동일 디렉터리에 페이퍼 장부 생성
            self.state_file = STATE_FILE.parent / "state_paper.json"
            self.trade_file = STATE_FILE.parent / "trades_paper.json"
        else:
            self.state_file = STATE_FILE
            self.trade_file = STATE_FILE.parent / "trades.json"
        self.positions: dict[str, Position] = {}
        self._load_state()

    # ------------------------------------------------------------------ state

    def _load_state(self):
        data = safe_read_json(self.state_file, default={"positions": {}})
        self.positions = {
            t: Position.from_dict(p) for t, p in data.get("positions", {}).items()
        }

    def _save_state(self):
        data = {"positions": {t: p.to_dict() for t, p in self.positions.items()}}
        atomic_write_json(self.state_file, data)

    # --------------------------------------------------------------- capital

    def invested_capital(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def available_capital(self) -> float:
        return CAPITAL_TOTAL - self.invested_capital()

    def max_new_position_size(self) -> float:
        return min(
            self.available_capital(),
            CAPITAL_TOTAL * MAX_POSITION_PCT,
        )

    # --------------------------------------------------------- buy filters

    def _read_trades(self) -> list:
        """청산 거래 내역(재진입 필터·집계용). 디스크 trades.json 이 기본.

        백테스트 서브클래스는 이 시드만 인메모리로 갈아끼워 재진입 쿨다운·
        히스테리시스 같은 날짜 의존 필터를 라이브와 동일 코드로 재현한다."""
        try:
            return safe_read_json(self.trade_file,
                                  default={"trades": []}).get("trades", [])
        except Exception:
            return []

    def _recently_stopped(self) -> set:
        """최근 REENTRY_COOLDOWN_DAYS 내 손절 청산된 종목 — 재진입 금지(휩쏘 방지)."""
        trades = self._read_trades()
        cutoff = _today() - timedelta(days=REENTRY_COOLDOWN_DAYS)
        out = set()
        for t in trades[-200:]:
            if not str(t.get("reason", "")).startswith("stop_loss"):
                continue
            try:
                if date.fromisoformat(t.get("exit_date", "")) >= cutoff:
                    out.add(t.get("ticker"))
            except Exception:
                pass
        return out

    def _recent_exit_prices(self, days: int = 2) -> dict:
        """최근 청산가 (종목 → 마지막 exit_price).

        재진입 히스테리시스용: 판 가격에서 MIN_REENTRY_MOVE_PCT 이상
        움직이기 전엔 되사지 않는다. 가격 변화 없이 매도↔매수만 반복하는
        낭비 거래 방지 — 가격이 실제로 움직이면 즉시 재진입 가능."""
        trades = self._read_trades()
        cutoff = (_today() - timedelta(days=days)).isoformat()
        out = {}
        for t in trades[-300:]:                      # 시간순 → 마지막 값이 최신
            if (t.get("exit_date") or "") >= cutoff and t.get("exit_price"):
                out[t.get("ticker")] = float(t["exit_price"])
        return out

    @staticmethod
    def _reentry_blocked(price: float, last_exit: float | None) -> bool:
        """직전 청산가 대비 가격 변화가 미미하면 재매수 차단."""
        if not last_exit or not price:
            return False
        return abs(price - last_exit) / last_exit < MIN_REENTRY_MOVE_PCT

    # ------------------------------------------------------- sell decisions

    def should_sell(self, pos: Position, current_price: float, current_score: float) -> Optional[str]:
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price

        # 1) 하드 손절 — 항상 최우선 (리스크 관리)
        if pnl_pct <= -STOP_LOSS_PCT:
            return f"stop_loss ({pnl_pct:.1%})"

        # 2) 트레일링 익절 — 목표 수익률 도달(고점 기준)했다면, 하드 청산 대신
        #    고점 대비 TRAIL_GIVEBACK_PCT 만큼 되돌릴 때만 청산해 추세를 끝까지 끈다.
        peak_gain = (pos.peak_price - pos.entry_price) / pos.entry_price
        if peak_gain >= TAKE_PROFIT_PCT:
            giveback = (pos.peak_price - current_price) / pos.peak_price if pos.peak_price else 0.0
            if giveback >= TRAIL_GIVEBACK_PCT:
                return f"trailing_stop (고점 +{peak_gain:.0%}→현재 +{pnl_pct:.1%})"
            # 아직 고점 근처 → 보유 유지(승자를 끌고 감)
            return None

        # 3) 점수 악화 / 보유기간 만료
        if current_score < SELL_SCORE_THRESHOLD:
            return f"score_drop ({current_score:.0f})"
        if pos.days_held() >= pos.max_hold_days():
            return f"max_hold ({pos.days_held()}d)"
        return None

    # --------------------------------------------------- generate trade plan

    def generate_orders(
        self,
        scores: list[StockScore],
        current_prices: dict[str, float],
        buy_mode: str = "전량",      # "전량" | "분할"
        sell_mode: str = "전량",     # "전량" | "분할"
        buy_pct: float = 1.0,        # 분할 매수 시 목표 비중의 비율 (0.01~1.0 = 1%~100%)
        sell_pct: float = 1.0,       # 분할 매도 시 보유 수량의 비율 (0.01~1.0 = 1%~100%)
        split_n: int = 3,            # (구버전 호환용, 미사용)
        available_override: float | None = None,  # 가용 현금 직접 지정(모의=페이퍼 현금)
        dynamic: bool = False,       # True면 유동형(동적 배분) — 고정 분할(%) 대신 시드 내 비중 리밸런싱
    ) -> dict[str, list[dict]]:
        """
        Returns {"sell": [...], "buy": [...]} order lists.
        Does NOT execute trades — execution is broker.py's job.

        dynamic=True (유동형):
          고정 비율(분할 %)이 아니라, 제한된 시드(총자본) 안에서 신호 강도에
          비례해 목표 비중을 잡고 '보유 ↔ 목표'의 차이만큼만 유동적으로 매수/매도.
          → _dynamic_orders 참고.

        dynamic=False (고정형, buy_mode/sell_mode):
          - "전량": 한 번에 목표 비중 전액 매수 / 보유 전량 매도
          - "분할": 목표 비중의 buy_pct / 보유 수량의 sell_pct 만큼 (1%~100% 자유)
        손절(stop_loss)은 비율과 무관하게 항상 전량 매도(리스크 관리).
        """
        score_map = {s.ticker: s for s in scores}
        if dynamic:
            return self._dynamic_orders(scores, score_map, current_prices,
                                        available_override)
        sells = []
        buys = []
        _bp = min(max(float(buy_pct), 0.01), 1.0)
        _sp = min(max(float(sell_pct), 0.01), 1.0)

        # --- exits ---
        _peak_changed = False
        for ticker, pos in list(self.positions.items()):
            price = current_prices.get(ticker)
            if price is None:
                continue
            # 트레일링 스탑 기준점인 고점을 먼저 갱신
            if pos.update_peak(price):
                _peak_changed = True
            sc = score_map.get(ticker)
            current_score = sc.total if sc else 0.0
            reason = self.should_sell(pos, price, current_score)
            if reason:
                # 손절·트레일링 청산은 항상 전량(리스크/추세 종료), 그 외엔 모드 적용
                _full_exit = reason.startswith(("stop_loss", "trailing_stop"))
                if sell_mode == "분할" and not _full_exit:
                    qty = max(1, min(int(round(pos.shares * _sp)), int(pos.shares)))
                else:
                    qty = pos.shares
                sells.append({
                    "ticker": ticker,
                    "shares": qty,
                    "reason": reason,
                    "est_price": price,
                })
        # 고점 갱신분을 디스크에 반영 (다음 사이클의 트레일링 기준 유지)
        if _peak_changed:
            self._save_state()

        # Estimate post-sell available capital (실제 매도 수량 기준)
        freed = sum(o["shares"] * o["est_price"] for o in sells)
        _base_avail = (float(available_override) if available_override is not None
                       else self.available_capital())
        projected_available = _base_avail + freed
        # 완전 청산된 종목만 슬롯 반환 (분할 매도는 보유 유지)
        fully_sold = {o["ticker"] for o in sells
                      if o["ticker"] in self.positions
                      and o["shares"] >= self.positions[o["ticker"]].shares}

        # --- entries ---
        open_after_sells = {t for t in self.positions if t not in fully_sold}
        # 소액 자본 단위 정책 — 시가 기준 자본으로 슬롯 수·종목당 상한·신규 최소 금액
        _sold_qty = {o["ticker"]: o["shares"] for o in sells}
        equity = projected_available + sum(
            (self.positions[t].shares - _sold_qty.get(t, 0))
            * current_prices.get(t, self.positions[t].entry_price)
            for t in open_after_sells)
        n_slots, cap, min_lot = lot_plan(equity)
        self.last_lot_plan = {"equity": equity, "slots": n_slots,
                              "cap": cap, "min_lot": min_lot}
        slots_free = n_slots - len(open_after_sells)
        full_target = min(CAPITAL_TOTAL, equity) * cap   # 한 종목 목표 비중 (전액)
        _cooldown_fixed = self._recently_stopped()       # 손절 쿨다운(신규 진입 차단)
        _exit_px = self._recent_exit_prices()            # 재진입 가격 히스테리시스

        for s in scores:
            if s.total < MIN_SCORE_TO_BUY:
                break  # 내림차순 정렬 → 이하 전부 미달
            price = current_prices.get(s.ticker)
            if not price or price <= 0:
                continue

            held = s.ticker in open_after_sells
            if held:
                # 분할 매수 모드에서만 기존 포지션을 목표 비중까지 추가 매수(물타기).
                # 전량 모드는 한 번에 목표를 채우므로 추가 진입할 게 없다.
                if buy_mode != "분할":
                    continue
                pos = self.positions.get(s.ticker)
                if pos is None or pos.cost_basis >= full_target * 0.99:
                    continue
                target = min(full_target * _bp, full_target - pos.cost_basis)
            else:
                if slots_free <= 0:
                    continue  # 신규 슬롯 없음 — held 추가분은 계속 살펴야 하므로 break 금지
                # 신규 진입만 가격대·쿨다운·재진입 히스테리시스 필터 (보유 추가매수는 예외)
                if not _price_in_band(price) or s.ticker in _cooldown_fixed:
                    continue
                if self._reentry_blocked(price, _exit_px.get(s.ticker)):
                    continue  # 판 가격 그대로 → 되사지 않음 (가격이 움직이면 허용)
                target = full_target * _bp if buy_mode == "분할" else full_target
                target = max(target, min_lot)   # 신규는 최소 단위 이상으로 연다(분할 첫 매수도)

            size = min(projected_available, target)
            shares = int(size / price)
            if shares < 1:
                continue
            cost = shares * price
            if cost > projected_available:
                continue
            if not held and cost < min_lot:
                continue  # 1주가 너무 비싸거나 현금이 모자라 자투리로만 살 수 있음 → 건너뜀
            buys.append({
                "ticker": s.ticker,
                "shares": shares,
                "score": s.total,
                "est_price": price,
                "est_cost": cost,
            })
            projected_available -= cost
            if not held:
                slots_free -= 1

        return {"sell": sells, "buy": buys}

    # ----------------------------------------------- 유동형(동적 배분) 주문

    def _dynamic_orders(self, scores, score_map, current_prices,
                        available_override=None) -> dict[str, list[dict]]:
        """제한된 시드(총자본) 안에서 신호 강도에 비례해 비중을 동적 배분하고
        목표로 리밸런싱한다. 고정 비율(분할 %)이 아니라, 각 매수/매도 금액이
        '현재 보유액 ↔ 목표 비중액'의 차이로 유동적으로 결정된다.

        절차:
          1) 위험 청산(손절/트레일링/점수붕괴/보유만료) 먼저 — 항상 전량, 자본 확보
          2) 점수 ≥ 매수문턱 상위 후보로 목표 포트폴리오 구성
             - 확신(=점수)에 비례해 비중 배분, 종목 수·종목당 상한은 lot_plan
               (자본이 적으면 적은 종목에 크게, 적당한 단위로 못 사는 종목은 제외)
          3) 목표에서 빠진 보유는 회전 청산(단, 트레일링이 관리 중인 승자는 유지)
          4) 보유 ↔ 목표 차이가 리밸런스 밴드를 넘을 때만 그 차액만큼 매수/매도
          5) 시드가 하드 캡 — 가용 현금 한도 내에서만 매수(점수 높은 순 우선)
        """
        sells, buys = [], []

        # ── 1) 위험 청산 (하드 룰) — 항상 전량 ──────────────────────────────
        _peak_changed = False
        hard_exit = set()
        for ticker, pos in list(self.positions.items()):
            price = current_prices.get(ticker)
            if price is None:
                continue
            if pos.update_peak(price):
                _peak_changed = True
            sc = score_map.get(ticker)
            reason = self.should_sell(pos, price, sc.total if sc else 0.0)
            if reason:
                sells.append({"ticker": ticker, "shares": pos.shares,
                              "reason": reason, "est_price": price})
                hard_exit.add(ticker)
        if _peak_changed:
            self._save_state()

        # ── 자본 상태 (시가 기준) ──────────────────────────────────────────
        base_cash = (float(available_override) if available_override is not None
                     else self.available_capital())
        freed = sum(o["shares"] * o["est_price"] for o in sells)
        cash = base_cash + freed
        held_now = {t: p for t, p in self.positions.items() if t not in hard_exit}
        held_value = sum(p.shares * current_prices.get(t, p.entry_price)
                         for t, p in held_now.items())
        total_equity = cash + held_value          # 운용 총자본(시드)
        if total_equity <= 0:
            return {"sell": sells, "buy": buys}
        # 소액 자본 단위 정책 — 자본이 적으면 종목 수를 줄이고 종목당 상한을 늘린다
        n_slots, cap, min_lot = lot_plan(total_equity)
        self.last_lot_plan = {"equity": total_equity, "slots": n_slots,
                              "cap": cap, "min_lot": min_lot}

        # ── 2) 목표 포트폴리오 (확신=점수 비례, 상한 cap = lot_plan) ──────────
        # 신규 후보는 매수 가격대(상·하한) + 손절 쿨다운 필터 적용.
        # 이미 보유 중인 종목은 필터와 무관하게 목표에 남긴다(강제 청산 방지).
        _cooldown = self._recently_stopped()
        _exit_px = self._recent_exit_prices()
        _pre = [s for s in scores
                if s.total >= MIN_SCORE_TO_BUY
                and s.ticker not in hard_exit
                and current_prices.get(s.ticker, 0) > 0
                and (s.ticker in held_now
                     or (_price_in_band(current_prices[s.ticker])
                         and s.ticker not in _cooldown
                         and not self._reentry_blocked(
                             current_prices[s.ticker], _exit_px.get(s.ticker))))]
        # 섹터 집중 상한: 같은 섹터 신규 진입은 MAX_PER_SECTOR 까지만
        # (보유 종목은 항상 통과 — 강제 청산 방지, 섹터 미상('')은 상한 미적용)
        _sec_cnt: dict = {}
        for _t in held_now:
            _sec = _sector_of(_t)
            if _sec:
                _sec_cnt[_sec] = _sec_cnt.get(_sec, 0) + 1
        cands = []
        _iss = {_issuer(t) for t in held_now}       # 같은 발행사 중복 진입 방지
        for s in _pre:
            if len(cands) >= n_slots:
                break
            if s.ticker in held_now:
                cands.append(s); continue
            if _issuer(s.ticker) in _iss:
                continue          # 같은 회사의 다른 주식 종류를 이미 보유/선택
            if _lot_value(cap * total_equity, current_prices[s.ticker]) < min_lot:
                continue          # 1주가 너무 비싸 종목당 상한 안에서 적당한 단위로 못 삼 → 다음 후보
            _sec = _sector_of(s.ticker)
            if _sec and _sec_cnt.get(_sec, 0) >= MAX_PER_SECTOR:
                continue          # 섹터 포화 → 다음 점수 후보로
            cands.append(s)
            _iss.add(_issuer(s.ticker))
            if _sec:
                _sec_cnt[_sec] = _sec_cnt.get(_sec, 0) + 1
        base = MIN_SCORE_TO_BUY - 10.0            # 문턱 부근은 작게, 강한 신호는 크게

        def _weights(cs):
            raw = {s.ticker: max(1.0, s.total - base) for s in cs}
            tot_raw = sum(raw.values()) or 1.0
            return {t: r / tot_raw for t, r in raw.items()}

        weight = _weights(cands)
        target_val = {t: min(w, cap) * total_equity for t, w in weight.items()}
        # 점수 비례 몫이 최소 단위에 못 미치는 신규 후보는 약한 것부터 빼고 다시 나눈다
        # — 자투리 포지션 여러 개 대신 적은 종목에 제대로 싣는다
        while True:
            thin = [s for s in cands if s.ticker not in held_now
                    and _lot_value(target_val[s.ticker], current_prices[s.ticker]) < min_lot]
            if not thin:
                break
            cands.remove(min(thin, key=lambda s: s.total))
            weight = _weights(cands)
            target_val = {t: min(w, cap) * total_equity for t, w in weight.items()}

        # ── 3) 목표 이탈 보유 → 회전 청산 (트레일링 관리 승자는 예외) ────────
        # 회전은 교체 이득이 분명할 때만: 신규 후보의 점수가 보유 점수보다
        # ROTATE_MARGIN 이상 높아야 한다. 점수가 문턱 부근에서 미세하게
        # 출렁이는 것만으로 멀쩡한 보유를 팔고 비슷한 걸 사는 낭비 회전 방지.
        _best_new = max((s.total for s in cands if s.ticker not in held_now),
                        default=None)
        rotated = set()
        for t, p in held_now.items():
            if t in target_val:
                continue
            _held_sc = score_map[t].total if t in score_map else 0.0
            if _best_new is None or _best_new - _held_sc < ROTATE_MARGIN:
                continue        # 교체 이득 불충분 → 회전하지 않음
            if p.days_held() < MIN_ROTATE_DAYS:
                continue        # 당일 매수 → 회전 안 함(위험 청산은 1단계에서 이미 처리)
            price = current_prices.get(t, p.entry_price)
            peak_gain = ((p.peak_price - p.entry_price) / p.entry_price
                         if p.entry_price else 0.0)
            pnl = (price - p.entry_price) / p.entry_price if p.entry_price else 0.0
            if peak_gain >= TAKE_PROFIT_PCT and pnl > 0:
                continue        # 목표 도달 승자 → 트레일링 스탑이 관리, 회전 제외
            sells.append({"ticker": t, "shares": p.shares,
                          "reason": "rotate (목표 이탈)", "est_price": price})
            cash += p.shares * price
            rotated.add(t)

        # ── 4) 트림(과비중) 먼저 → 현금 확보 ───────────────────────────────
        # 트림 상한은 한 단계 넉넉히(LOT_TRIM_SLACK) — 자본이 충분하면 cap 과 같다
        trim_cap = lot_plan(total_equity / (1.0 + LOT_TRIM_SLACK))[1]
        for t in target_val:
            p = held_now.get(t)
            if p is None or t in rotated:
                continue
            price = current_prices.get(t, p.entry_price)
            cur_val = p.shares * price
            trim_to = min(weight[t], trim_cap) * total_equity
            gap = trim_to - cur_val
            if gap < -REBAL_BAND * trim_to:                # 과비중
                qty = min(int(round((-gap) / price)), int(p.shares))
                if qty >= 1:
                    sells.append({"ticker": t, "shares": qty,
                                  "reason": "trim (비중 축소)", "est_price": price})
                    cash += qty * price

        # ── 5) 매수/추가(저비중) — 점수 높은 순, 가용 현금 한도 내 ──────────
        # 하드캡: 회전 매도로 비워지지 않은 보유 종목 수 + 신규 매수가
        # n_slots(소액이면 MAX_POSITIONS 보다 적음)를 넘지 않게 한다. 보유는 유지
        # (히스테리시스)하되 신규 진입은 남은 슬롯까지만 — 유동형이 한도를 초과하지 않도록.
        retained = set(held_now.keys()) - rotated
        new_bought: set = set()
        for s in cands:
            t = s.ticker
            price = current_prices.get(t, 0)
            if price <= 0 or t in rotated or cash <= 0:
                continue
            p = held_now.get(t)
            if p is None and (len(retained) + len(new_bought)) >= n_slots:
                continue          # 하드캡: 신규 매수로 보유 종목 수 초과 금지
            cur_val = (p.shares * price) if p else 0.0
            gap = target_val[t] - cur_val
            if gap > REBAL_BAND * target_val[t]:           # 저비중/신규
                spend = min(gap, cash)
                qty = int(spend / price)
                if p is None and qty * price < min_lot:
                    continue      # 현금이 모자라 자투리로만 열 수 있음 → 신규 진입 안 함
                if qty >= 1:
                    cost = qty * price
                    buys.append({"ticker": t, "shares": qty, "score": s.total,
                                 "est_price": price, "est_cost": cost})
                    cash -= cost
                    if p is None:
                        new_bought.add(t)

        return {"sell": sells, "buy": buys}

    # ------------------------------------------------- record executed trades

    def record_buy(self, ticker: str, shares: float, price: float, score: float):
        # 디스크의 최신 장부를 락 안에서 다시 읽고 수정한다. 메모리 스냅샷
        # 기준으로 통째로 덮어쓰면, 그 사이 다른 프로세스(데몬↔앱)가 기록한
        # 거래가 사라진다(lost-update). reload→mutate→save 를 한 락에 묶는다.
        with trade_lock():
            self._load_state()
            existing = self.positions.get(ticker)
            if existing:
                # 추가 매수(물타기) → 가중평균 평단가로 누적.
                # 진입일·진입점수는 최초 진입 기준을 유지(보유기간/등급 판정 일관성).
                total_shares = existing.shares + shares
                existing.entry_price = (
                    (existing.cost_basis + shares * price) / total_shares
                ) if total_shares else price
                existing.shares = total_shares
            else:
                self.positions[ticker] = Position(
                    ticker=ticker,
                    entry_price=price,
                    shares=shares,
                    entry_date=_today().isoformat(),
                    score_at_entry=score,
                )
            self._save_state()

    def record_sell(self, ticker: str, exit_price: float = 0.0, reason: str = "",
                    shares: Optional[float] = None):
        """매도 기록.

        shares=None → 전량 매도(포지션 삭제).
        shares 지정 → 그만큼만 차감(분할 매도). 보유량 이상이면 전량 처리.
        부분 매도 시 평단가·진입일·진입점수는 그대로 유지된다.
        반환: 실제로 장부에서 차감한 수량(미보유면 0) — 현금 반영은 이 값 기준.
        """
        # record_buy 와 동일 — 락 안에서 디스크 최신 장부를 다시 읽고 수정해
        # 다른 프로세스의 동시 기록이 유실되지 않게 한다.
        with trade_lock():
            self._load_state()
            pos = self.positions.get(ticker)
            if pos is None:
                return 0.0
            sold = pos.shares if shares is None else min(float(shares), pos.shares)
            if sold <= 0:
                return 0.0
            if sold >= pos.shares:
                self.positions.pop(ticker, None)
            else:
                pos.shares -= sold
            self._save_state()
            self._append_trade(pos, exit_price, reason, shares=sold)
            return sold

    def _append_trade(self, pos: "Position", exit_price: float, reason: str,
                      shares: Optional[float] = None):
        """거래 내역 1건을 누적 저장.

        shares를 명시하면 (분할 매도) 실제 매도 수량을 기록한다.
        실제 저장은 _store_trade 가 담당한다(디스크 ↔ 인메모리 분기점).
        """
        self._store_trade({
            "ticker":      pos.ticker,
            "entry_date":  pos.entry_date,
            "exit_date":   _today().isoformat(),
            "entry_price": pos.entry_price,
            "exit_price":  exit_price,
            "shares":      pos.shares if shares is None else shares,
            "reason":      reason.split(" ")[0],  # "stop_loss (…)" → "stop_loss"
        })

    def _store_trade(self, rec: dict):
        """거래 1건을 trades.json 에 원자적으로 추가.

        백테스트 서브클래스는 이걸 인메모리 append 로 오버라이드해
        실거래 장부를 건드리지 않는다."""
        data = safe_read_json(self.trade_file, default={"trades": []})
        data["trades"].append(rec)
        atomic_write_json(self.trade_file, data)

    # ----------------------------------------------------------------- debug

    def summary(self, current_prices: dict[str, float]) -> str:
        lines = [f"{'─'*60}",
                 f"  Portfolio  |  Capital: ${CAPITAL_TOTAL:,.0f}  |  "
                 f"Invested: ${self.invested_capital():,.0f}  |  "
                 f"Free: ${self.available_capital():,.0f}",
                 f"{'─'*60}"]
        for t, pos in self.positions.items():
            price = current_prices.get(t, pos.entry_price)
            pnl = (price - pos.entry_price) / pos.entry_price
            lines.append(
                f"  {t:6s}  entry=${pos.entry_price:.2f}  now=${price:.2f}  "
                f"pnl={pnl:+.1%}  held={pos.days_held()}d"
            )
        if not self.positions:
            lines.append("  (no open positions)")
        lines.append(f"{'─'*60}")
        return "\n".join(lines)
