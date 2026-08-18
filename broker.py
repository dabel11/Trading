"""
Broker layer: wraps Alpaca API for order execution and price fetching.
Paper trading by default — just change ALPACA_BASE_URL in config.py to go live.

안전장치:
  - client_order_id 로 중복 주문 차단 (멱등성)
  - 시장가 주문 체결 후 filled_avg_price 폴링 → 실제 체결가 반환
"""

import time
import uuid
import yfinance as yf
from datetime import datetime, date
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL


def validate_fill(result: dict, est_price: float, req_shares: int,
                  max_slippage: float = 0.08) -> dict:
    """체결 결과를 검증해 장부에 안전하게 기록할 수 있는지 판단.

    실거래 주문은 "submit 성공 = 체결 완료"가 아니다 — 미체결/거부 상태인데도
    추정가로 장부를 기록해버리면 실제 계좌와 어긋난다. 이 함수는:
      1) 체결 확인(status=filled/partially_filled, fill_price>0, filled_qty>0)
         되지 않으면 기록을 보류(ok=False)하도록 신호
      2) 부분 체결이면 실제 체결 수량만 반환
      3) 추정가 대비 체결가 괴리(슬리피지)가 크면 경고 문구를 채워 넣음

    반환:
      ok          — True면 기록해도 안전 (체결이 확인된 상태)
      fill_price  — 기록에 쓸 체결가 (ok=False면 0.0)
      filled_qty  — 실제 체결 수량 (부분 체결 시 요청보다 작을 수 있음)
      warning     — 사용자에게 보여줄 경고 메시지 (없으면 "")
    """
    status     = str(result.get("status") or "").lower()
    fill_price = float(result.get("fill_price") or 0)
    filled_qty = int(result.get("filled_qty") or 0)

    confirmed = (status in ("filled", "partially_filled")
                 and fill_price > 0 and filled_qty > 0)
    if not confirmed:
        return {"ok": False, "fill_price": 0.0, "filled_qty": 0,
                "warning": f"체결 미확인(상태: {status or '알 수 없음'}) — "
                           f"장부 기록을 보류했습니다. 다음 사이클에서 재확인하세요."}

    qty = min(filled_qty, req_shares) if req_shares else filled_qty
    warn = ""
    if req_shares and qty < req_shares:
        warn = f"부분 체결: {qty}/{req_shares}주만 체결됨"
    if est_price and fill_price:
        slip = (fill_price - est_price) / est_price
        if abs(slip) > max_slippage:
            _w = f"⚠️ 슬리피지 큼: 예상 ${est_price:.2f} → 체결 ${fill_price:.2f} ({slip:+.1%})"
            warn = f"{warn} · {_w}" if warn else _w

    return {"ok": True, "fill_price": fill_price, "filled_qty": qty, "warning": warn}


def make_broker(paper: bool = True):
    """설정(config.BROKER)에 따라 브로커 어댑터를 생성하는 팩토리.

    멀티브로커 진입점 — 호출부는 Broker() 대신 make_broker() 를 쓰면
    Alpaca(미국)/Toss(국내+해외) 를 코드 변경 없이 전환할 수 있다.
    기본값은 "alpaca" 라 기존 동작은 그대로 유지된다.
    토스는 정식 출시 후 키를 넣고 BROKER=toss 로 바꾸면 활성화된다.
    """
    try:
        from config import BROKER as _sel
    except Exception:
        _sel = "alpaca"
    if _sel == "toss":
        from toss_broker import TossBroker
        return TossBroker(paper=paper)
    return Broker(paper=paper)


class Broker:
    def __init__(self, paper: bool = True):
        self.paper = paper
        self._trading = TradingClient(
            ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper
        )
        self._data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    # ----------------------------------------------------------- prices

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Returns {ticker: last_price}. Falls back to yfinance if Alpaca fails."""
        prices = {}
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=tickers)
            quotes = self._data.get_stock_latest_quote(req)
            for t, q in quotes.items():
                mid = (q.ask_price + q.bid_price) / 2 if q.ask_price and q.bid_price else q.ask_price or q.bid_price
                if mid:
                    prices[t] = float(mid)
        except Exception:
            pass

        # Fill missing with yfinance
        missing = [t for t in tickers if t not in prices]
        if missing:
            for t in missing:
                try:
                    info = yf.Ticker(t).fast_info
                    price = getattr(info, "last_price", None)
                    if price:
                        prices[t] = float(price)
                except Exception:
                    pass

        return prices

    # ----------------------------------------------------------- orders

    def _coid(self, ticker: str, side: str, shares: int) -> str:
        """결정적 client_order_id 생성.
        같은 종목·방향·수량을 같은 분(minute)에 두 번 보내면 Alpaca가 거부 → 중복 차단.
        분 단위 타임스탬프 포함으로 의도적 재주문은 허용."""
        stamp = datetime.now().strftime("%Y%m%d%H%M")
        return f"ait-{side}-{ticker}-{shares}-{stamp}"

    def _submit(self, ticker: str, shares: int, side: OrderSide,
                side_str: str, limit_price: float | None = None,
                coid_suffix: str = "") -> dict:
        """주문 제출. limit_price 지정 시 지정가(marketable limit)로 보낸다 —
        급변 장에서 시장가의 무제한 슬리피지를 방지하는 실무 표준."""
        coid = self._coid(ticker, side_str, shares) + coid_suffix
        if limit_price:
            req = LimitOrderRequest(
                symbol=ticker, qty=shares, side=side,
                time_in_force=TimeInForce.DAY,
                limit_price=round(float(limit_price), 2),
                client_order_id=coid,
            )
        else:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=side,
                time_in_force=TimeInForce.DAY,
                client_order_id=coid,
            )
        try:
            order = self._trading.submit_order(req)
        except Exception as e:
            # client_order_id 중복 → 이미 같은 주문이 들어감
            if "client_order_id" in str(e).lower() or "duplicate" in str(e).lower():
                return {"id": None, "ticker": ticker, "shares": shares,
                        "side": side_str, "duplicate": True,
                        "fill_price": 0.0, "filled_qty": 0,
                        "error": "중복 주문 차단됨"}
            raise

        # 체결가 폴링 (시장가는 보통 즉시 체결, 최대 ~3초 대기)
        fill_price, filled_qty, status = self._poll_fill(str(order.id))
        return {
            "id": str(order.id),
            "ticker": ticker,
            "shares": shares,
            "side": side_str,
            "duplicate": False,
            "fill_price": fill_price,
            "filled_qty": filled_qty,
            "status": status,
        }

    def _poll_fill(self, order_id: str, timeout: float = 3.0):
        """주문 체결가·체결수량 폴링. (fill_price, filled_qty, status)."""
        deadline = time.time() + timeout
        last_price, last_qty, last_status = 0.0, 0, "unknown"
        while time.time() < deadline:
            try:
                o = self._trading.get_order_by_id(order_id)
                last_status = str(getattr(o, "status", "")).split(".")[-1].lower()
                fap = getattr(o, "filled_avg_price", None)
                fq  = getattr(o, "filled_qty", None)
                if fap:
                    last_price = float(fap)
                if fq:
                    last_qty = int(float(fq))
                if last_status in ("filled",) and last_price > 0:
                    break
            except Exception:
                pass
            time.sleep(0.4)
        return last_price, last_qty, last_status

    def place_buy(self, ticker: str, shares: int) -> dict:
        return self._submit(ticker, shares, OrderSide.BUY, "buy")

    def place_sell(self, ticker: str, shares: int) -> dict:
        return self._submit(ticker, shares, OrderSide.SELL, "sell")

    def execute_order(self, ticker: str, shares: int, side_str: str,
                      est_price: float, max_retries: int = 1,
                      slip_buffer: float = 0.005,
                      max_requote: float = 0.02) -> dict:
        """미체결 재시도가 있는 안전 체결 (실무 방식).

        1) 예상가 ±slip_buffer(0.5%)의 '체결 가능한 지정가'로 주문
           → 급변 장에서 시장가의 무제한 슬리피지를 구조적으로 차단
        2) 5초 내 미체결이면 잔여 주문 취소 → 현재가 재조회(재검토)
        3) 새 가격이 예상가 대비 max_requote(2%) 이내면 그 가격으로 1회 재주문,
           그 이상 튀었으면 추격하지 않고 포기(reason 명시) — 추격 매수 방지
        반환 dict 은 place_buy/sell 과 동일 + "note"(시도 내역).
        """
        side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
        est = float(est_price)
        notes = []
        res = {"id": None, "ticker": ticker, "shares": shares, "side": side_str,
               "duplicate": False, "fill_price": 0.0, "filled_qty": 0,
               "status": "unfilled"}
        for attempt in range(max_retries + 1):
            mult = (1 + slip_buffer) if side_str == "buy" else (1 - slip_buffer)
            limit = est * mult
            res = self._submit(ticker, shares, side, side_str,
                               limit_price=limit,
                               coid_suffix=(f"-r{attempt}" if attempt else ""))
            if res.get("duplicate"):
                res["note"] = "중복 차단"
                return res
            # 지정가는 즉시 체결 안 될 수 있음 → 추가 폴링 (총 ~5초)
            if res.get("status") not in ("filled",) and res.get("id"):
                fp, fq, stt = self._poll_fill(res["id"], timeout=5.0)
                res.update(fill_price=fp, filled_qty=fq, status=stt)
            if res.get("status") == "filled" and res.get("fill_price", 0) > 0:
                res["note"] = " → ".join(notes + [f"체결 @${res['fill_price']:.2f}"
                                                  + (f" (재시도 {attempt}회)" if attempt else "")])
                return res
            # 미체결 → 잔여 취소 후 재검토
            try:
                if res.get("id"):
                    self._trading.cancel_order_by_id(res["id"])
            except Exception:
                pass
            if attempt >= max_retries:
                break
            new_px = self.get_prices([ticker]).get(ticker) or 0.0
            if not new_px:
                notes.append("재조회 실패")
                break
            drift = abs(new_px - est) / est if est else 1.0
            if drift > max_requote:
                notes.append(f"가격 급변 {drift:+.1%} (${est:.2f}→${new_px:.2f}) — 추격 포기")
                break
            notes.append(f"미체결 → 재검토 ${est:.2f}→${new_px:.2f}")
            est = new_px
        res["note"] = " → ".join(notes) if notes else "미체결 — 주문 취소됨"
        return res

    def get_account(self) -> dict:
        acct = self._trading.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            # 전일 종가 기준 자산 — 킬스위치 당일 손실률의 정확한 앵커
            "last_equity": float(getattr(acct, "last_equity", 0) or 0) or None,
        }

    def get_positions(self) -> dict[str, dict]:
        """브로커 실제 보유 포지션. {ticker: {shares, avg_price, market_value}}"""
        try:
            poss = self._trading.get_all_positions()
        except Exception:
            return {}
        out = {}
        for p in poss:
            try:
                out[p.symbol] = {
                    "shares":       float(p.qty),
                    "avg_price":    float(p.avg_entry_price),
                    "market_value": float(p.market_value),
                    "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                }
            except Exception:
                continue
        return out
