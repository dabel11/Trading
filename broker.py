"""
Broker layer: wraps Alpaca API for order execution and price fetching.
Paper trading by default — just change ALPACA_BASE_URL in config.py to go live.

안전장치:
  - client_order_id 로 중복 주문 차단 (멱등성) — 실행기는 주문 의도(core.intents)에서
    결정적으로 만든 coid 를 넘긴다. 같은 coid 재제출은 브로커의 기존 주문을 이어받는다.
  - 시장가 주문 체결 후 filled_avg_price 폴링 → 실제 체결가 반환
  - 취소 뒤에는 최종 상태(filled_qty)를 다시 읽어 체결분을 놓치지 않는다
  - 재시도는 '요청 − 누적 체결'만, 앞 주문의 종료가 확인된 뒤에만 낸다(과체결 방지)
  - 비대칭 실행: 매수는 추격 금지, 매도(청산)는 재가격 → 시장가로 끝까지 낸다
"""

import time
import yfinance as yf
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL


# 체결분이 붙어 있을 수 있는 주문 상태 (Alpaca OrderStatus 값)
_FILL_STATUSES = ("filled", "partially_filled", "canceled", "expired",
                  "done_for_day", "pending_cancel", "replaced")
# 더 이상 체결이 붙지 않는 상태
_FINAL_STATUSES = ("filled", "canceled", "expired", "rejected", "replaced",
                   "done_for_day")


def _status_of(o) -> str:
    return str(getattr(o, "status", "") or "").split(".")[-1].lower()


def _http_status(e: Exception):
    try:
        return getattr(e, "status_code", None)
    except Exception:
        return None


def _is_not_found(e: Exception) -> bool:
    return _http_status(e) == 404 or "not found" in str(e).lower()


def _is_duplicate(e: Exception) -> bool:
    m = str(e).lower()
    return "client_order_id" in m or "duplicate" in m


def _definite_reject(e: Exception) -> bool:
    """브로커가 주문을 '받지 않았다'고 확답한 오류(4xx). 네트워크·5xx 는 불명."""
    code = _http_status(e)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)


def validate_fill(result: dict, est_price: float, req_shares: int,
                  max_slippage: float = 0.08) -> dict:
    """체결 결과를 검증해 장부에 안전하게 기록할 수 있는지 판단.

    실거래 주문은 "submit 성공 = 체결 완료"가 아니다 — 미체결/거부 상태인데도
    추정가로 장부를 기록해버리면 실제 계좌와 어긋난다. 이 함수는:
      1) 체결 확인(체결이 붙을 수 있는 상태 — filled/partially_filled 뿐 아니라
         부분 체결 뒤 canceled/expired 포함, fill_price>0, filled_qty>0)
         되지 않으면 기록을 보류(ok=False)하도록 신호
      2) 부분 체결이면 실제 체결 수량만 반환 (요청 초과면 경고 + 브로커 수량)
      3) 추정가 대비 체결가 괴리(슬리피지)가 크면 경고 문구를 채워 넣음

    반환:
      ok          — True면 기록해도 안전 (체결이 확인된 상태)
      fill_price  — 기록에 쓸 체결가 (ok=False면 0.0)
      filled_qty  — 실제 체결 수량 (부분 체결 시 요청보다 작을 수 있음)
      warning     — 사용자에게 보여줄 경고 메시지 (없으면 "")
    """
    status     = str(result.get("status") or "").lower()
    fill_price = float(result.get("fill_price") or 0)
    filled_qty = int(float(result.get("filled_qty") or 0))

    # canceled/expired 여도 그 전에 붙은 체결은 실제 계좌에 있다 — 반드시 기록한다.
    confirmed = (status in _FILL_STATUSES
                 and fill_price > 0 and filled_qty > 0)
    if not confirmed:
        return {"ok": False, "fill_price": 0.0, "filled_qty": 0,
                "warning": f"체결 미확인(상태: {status or '알 수 없음'}) — "
                           f"장부 기록을 보류했습니다. 다음 사이클에서 재확인하세요."}

    # 장부는 브로커 체결 수량을 그대로 따른다. 요청보다 적게 잘라 기록하면 실제
    # 보유분 일부가 장부 밖(손절 관리 밖)에 남는다.
    qty = filled_qty
    warn = ""
    if req_shares and qty < req_shares:
        warn = f"부분 체결: {qty}/{req_shares}주만 체결됨"
    elif req_shares and qty > req_shares:
        warn = f"⚠️ 요청 초과 체결: {qty}/{req_shares}주 — 브로커 수량으로 기록"
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


def _paper_endpoint(paper: bool) -> bool:
    """실제로 호출할 Alpaca 엔드포인트가 페이퍼인지.

    호출부의 Broker(paper=False)는 '앱 내부 모의장부 대신 Alpaca 로 주문'이라는 뜻이고,
    어느 계좌로 갈지는 키가 정한다(PK=페이퍼, AK=실계좌 — save_alpaca_keys 와 같은 규칙).
    PK 키를 실계좌 엔드포인트로 보내면 항상 401 이므로 페이퍼로 보정한다.
    실계좌 엔드포인트는 'paper=False + 실계좌 키'일 때만 — 보정은 돈이 안 오가는 쪽으로만."""
    if paper:
        return True
    k = str(ALPACA_API_KEY or "")
    if k.startswith("PK"):
        return True
    if k.startswith("AK"):
        return False
    return "paper" in str(ALPACA_BASE_URL)


class Broker:
    def __init__(self, paper: bool = True):
        self.paper = _paper_endpoint(paper)
        self._trading = TradingClient(
            ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=self.paper
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
        """레거시 client_order_id (분 단위) — coid 를 안 넘긴 직접 호출용.
        실행기(core.execution)는 주문 의도에서 만든 결정적 coid 를 넘긴다."""
        stamp = datetime.now().strftime("%Y%m%d%H%M")
        return f"ait-{side}-{ticker}-{shares}-{stamp}"

    def _submit(self, ticker: str, shares: int, side: OrderSide,
                side_str: str, limit_price: float | None = None,
                coid_suffix: str = "", client_order_id: str | None = None,
                poll: float = 3.0) -> dict:
        """주문 제출. limit_price 지정 시 지정가(marketable limit)로 보낸다 —
        급변 장에서 시장가의 무제한 슬리피지를 방지하는 실무 표준.
        client_order_id 가 이미 브로커에 있으면 duplicate=True (예외 아님).
        그 밖의 제출 예외는 그대로 올린다(호출부가 거부/불명을 가른다)."""
        coid = client_order_id or (self._coid(ticker, side_str, shares) + coid_suffix)
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
            if _is_duplicate(e):
                return {"id": None, "ticker": ticker, "shares": shares,
                        "side": side_str, "duplicate": True, "coid": coid,
                        "fill_price": 0.0, "filled_qty": 0,
                        "error": "중복 주문 차단됨"}
            raise

        # 체결가 폴링 (시장가는 보통 즉시 체결, 최대 ~3초 대기)
        fill_price, filled_qty, status = self._poll_fill(str(order.id), timeout=poll)
        return {
            "id": str(order.id),
            "ticker": ticker,
            "shares": shares,
            "side": side_str,
            "duplicate": False,
            "coid": coid,
            "fill_price": fill_price,
            "filled_qty": filled_qty,
            "status": status,
        }

    def _poll_fill(self, order_id: str, timeout: float = 3.0):
        """주문 체결가·체결수량 폴링. (fill_price, filled_qty, status).
        체결 완료뿐 아니라 취소·만료·거부 같은 최종 상태에서도 멈춘다."""
        deadline = time.time() + timeout
        last_price, last_qty, last_status = 0.0, 0, "unknown"
        while True:
            try:
                o = self._trading.get_order_by_id(order_id)
                last_status = _status_of(o)
                fap = getattr(o, "filled_avg_price", None)
                fq  = getattr(o, "filled_qty", None)
                if fap:
                    last_price = float(fap)
                if fq:
                    last_qty = int(float(fq))
                if last_status == "filled" and last_price > 0:
                    break
                if last_status in _FINAL_STATUSES and last_status != "filled":
                    break
            except Exception:
                pass
            if time.time() >= deadline:
                break
            time.sleep(0.4)
        return last_price, last_qty, last_status

    def _cancel_confirm(self, order_id: str, timeout: float = 3.0):
        """취소 요청 후 최종 상태를 다시 읽는다 — (fill_price, filled_qty, status).
        취소 직전에 체결된 수량(부분 체결 → canceled 포함)을 여기서 확정한다.
        timeout 안에 최종 상태가 안 되면 마지막으로 본 상태(예: pending_cancel)를 준다."""
        try:
            self._trading.cancel_order_by_id(order_id)
        except Exception:
            pass                       # 이미 체결/종료된 주문은 취소가 거부된다
        return self._poll_fill(order_id, timeout=timeout)

    def place_buy(self, ticker: str, shares: int,
                  client_order_id: str | None = None) -> dict:
        return self._submit(ticker, shares, OrderSide.BUY, "buy",
                            client_order_id=client_order_id)

    def place_sell(self, ticker: str, shares: int,
                   client_order_id: str | None = None) -> dict:
        return self._submit(ticker, shares, OrderSide.SELL, "sell",
                            client_order_id=client_order_id)

    def execute_order(self, ticker: str, shares: int, side_str: str,
                      est_price: float, max_retries: int = 1,
                      slip_buffer: float = 0.005,
                      max_requote: float = 0.02, *,
                      coid_base: str | None = None,
                      chase: bool | None = None,
                      market: bool = False,
                      keep_open: bool | None = None,
                      on_event=None,
                      fill_timeout: float = 5.0,
                      cancel_timeout: float = 3.0) -> dict:
        """미체결 재시도가 있는 안전 체결 (실무 방식).

        1) 예상가 ±slip_buffer(0.5%)의 '체결 가능한 지정가'로 주문
           → 급변 장에서 시장가의 무제한 슬리피지를 구조적으로 차단
        2) fill_timeout 안에 다 안 채워지면 잔여 취소 → 최종 체결 수량 재조회
           (취소 확인이 안 되면 재주문하지 않고 멈춘다 — 앞 주문이 마저 체결되면 과체결)
        3) 재주문 수량은 항상 '요청 − 누적 체결'
           · 매수(위험 증가): 새 가격이 예상가 대비 max_requote(2%) 이내일 때만
             재주문, 더 튀면 추격하지 않고 포기 — 추격 매수 방지
           · 매도(청산, chase): 가격이 얼마나 움직였든 새 호가로 재가격하고,
             그래도 남으면 시장가로 에스컬레이션. 마지막 시장가는 취소하지 않고
             열어 둔다(open=True — 주문 의도 저널·대조가 추적)
        market=True: 처음부터 시장가 1건(수동 주문). 타임아웃이면 취소(keep_open=False 기본).
        coid_base  : 의도에서 만든 client_order_id. n번째 재주문은 f"{coid_base}-r{n}".
        on_event(ev, child): 자식 주문 상태가 바뀔 때마다 호출 — ev="pre" 는 제출 '직전'
                     (write-ahead: 크래시가 나도 coid 로 브로커에 다시 물어볼 수 있게).
        반환 dict 은 place_buy/sell 과 같은 키 + filled_qty(누적)·fill_price(VWAP)·
        status(종합)·open(브로커에 열린 주문 남음)·orders(자식 내역)·note.
        """
        side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
        chase = (side_str == "sell") if chase is None else bool(chase)
        if keep_open is None:
            keep_open = chase and not market
        base = coid_base or self._coid(ticker, side_str, shares)
        req = int(shares)
        est = float(est_price or 0)
        emit = on_event or (lambda ev, child: None)
        notes = []
        children: list[dict] = []
        if market:
            last_step = 0
        else:
            last_step = max_retries + (1 if chase else 0)   # 매도는 끝에 시장가 1단계

        def cum():
            q = sum(int(c["filled_qty"] or 0) for c in children)
            n = sum(int(c["filled_qty"] or 0) * float(c["fill_price"] or 0) for c in children)
            return q, (n / q if q else 0.0)

        n = 0
        while True:
            remaining = req - cum()[0]
            if remaining <= 0:
                break
            is_mkt = market or (chase and n == last_step and n > 0)
            limit = None
            if not is_mkt:
                mult = (1 + slip_buffer) if side_str == "buy" else (1 - slip_buffer)
                limit = est * mult if est else None
                if limit is None:                       # 가격을 모르면 지정가 불가
                    if not chase:
                        notes.append("가격 미상 — 주문 안 함")
                        break
                    is_mkt = True
            child = {"coid": base if n == 0 else f"{base}-r{n}", "id": None,
                     "qty": remaining, "kind": "market" if is_mkt else "limit",
                     "limit": round(limit, 2) if limit else None,
                     "status": "pending_submit", "filled_qty": 0, "fill_price": 0.0}
            children.append(child)
            emit("pre", child)
            try:
                r = self._submit(ticker, remaining, side, side_str,
                                 limit_price=limit, client_order_id=child["coid"])
            except Exception as e:
                child["error"] = str(e)[:200]
                if _definite_reject(e):
                    child["status"] = "rejected"
                    notes.append(f"주문 거부: {str(e)[:80]}")
                else:
                    child["status"] = "unknown"     # 브로커가 받았는지 모름 → 대조가 확정
                    notes.append(f"제출 결과 불명({str(e)[:60]}) — 대조로 확정")
                emit("update", child)
                break
            if r.get("duplicate"):
                # 같은 의도의 주문이 이미 브로커에 있다(재기동 후 재생 등) — 그 주문을 이어받는다
                try:
                    got = self.get_order_status(client_order_id=child["coid"])
                except Exception:
                    got = None
                if not got:
                    child["status"] = "unknown"
                    notes.append("중복 주문 — 기존 주문 조회 실패, 대조로 확정")
                    emit("update", child)
                    break
                child.update(id=got["id"], status=got["status"],
                             filled_qty=got["filled_qty"], fill_price=got["fill_price"])
                notes.append("기존 주문 이어받음")
            else:
                child.update(id=r["id"], status=r.get("status") or "unknown",
                             filled_qty=r.get("filled_qty") or 0,
                             fill_price=r.get("fill_price") or 0.0)
            emit("update", child)

            # 지정가는 즉시 체결 안 될 수 있음 → 추가 폴링
            if child["status"] not in _FINAL_STATUSES and child["id"]:
                fp, fq, stt = self._poll_fill(child["id"], timeout=fill_timeout)
                child.update(fill_price=fp or child["fill_price"],
                             filled_qty=max(fq, int(child["filled_qty"] or 0)), status=stt)
                emit("update", child)
            if child["status"] not in _FINAL_STATUSES:
                if keep_open and is_mkt:
                    notes.append("시장가 미체결 — 주문 유지(대조로 확정)")
                    break
                if child["id"]:
                    fp, fq, stt = self._cancel_confirm(child["id"], timeout=cancel_timeout)
                    child.update(fill_price=fp or child["fill_price"],
                                 filled_qty=max(fq, int(child["filled_qty"] or 0)),
                                 status=stt)
                    emit("update", child)
                if child["status"] not in _FINAL_STATUSES:
                    notes.append(f"취소 미확정({child['status']}) — 재주문 보류(대조로 확정)")
                    break
            if child["status"] == "rejected":
                notes.append("주문 거부됨")
                break
            if req - cum()[0] <= 0 or n >= last_step:
                break

            # 남은 수량 재검토
            if chase and n + 1 >= last_step:
                notes.append("매도 미체결 → 시장가")
                n += 1
                continue
            new_px = self.get_prices([ticker]).get(ticker) or 0.0
            if chase:
                if new_px:
                    notes.append(f"매도 미체결 → 재가격 ${est:.2f}→${new_px:.2f}")
                    est = new_px
                else:
                    notes.append("재조회 실패 — 시장가로")
                    n = last_step
                    continue
            else:
                if not new_px:
                    notes.append("재조회 실패")
                    break
                drift = abs(new_px - est) / est if est else 1.0
                if drift > max_requote:
                    notes.append(f"가격 급변 {drift:+.1%} (${est:.2f}→${new_px:.2f}) — 추격 포기")
                    break
                notes.append(f"미체결 → 재검토 ${est:.2f}→${new_px:.2f}")
                est = new_px
            n += 1

        fq, vwap = cum()
        open_ = any(c["status"] not in _FINAL_STATUSES for c in children)
        if fq >= req:
            status = "filled"
        elif children:
            status = children[-1]["status"]
            if not open_ and fq > 0 and status == "rejected":
                status = "canceled"
        else:
            status = "unfilled"
        if fq >= req:
            notes.append(f"체결 @${vwap:.2f}" + (f" (재시도 {len(children) - 1}회)"
                                                 if len(children) > 1 else ""))
        elif fq > 0:
            notes.append(f"부분 체결 {fq}/{req}주")
        elif not open_ and not notes:
            notes.append("미체결 — 주문 취소됨")
        last = children[-1] if children else {}
        return {"id": last.get("id"), "ticker": ticker, "shares": req,
                "side": side_str, "duplicate": False,
                "fill_price": vwap, "filled_qty": fq, "status": status,
                "open": open_, "orders": children, "note": " → ".join(notes)}

    def get_order_status(self, client_order_id: str | None = None,
                         order_id: str | None = None) -> dict | None:
        """주문 1건의 현재 상태. 브로커에 없으면 None, 그 밖의 오류는 예외."""
        try:
            if order_id:
                o = self._trading.get_order_by_id(order_id)
            else:
                o = self._trading.get_order_by_client_id(client_order_id)
        except Exception as e:
            if _is_not_found(e):
                return None
            raise
        return {
            "id": str(getattr(o, "id", "") or ""),
            "coid": getattr(o, "client_order_id", client_order_id),
            "status": _status_of(o),
            "filled_qty": int(float(getattr(o, "filled_qty", 0) or 0)),
            "fill_price": float(getattr(o, "filled_avg_price", 0) or 0),
        }

    def get_open_orders(self) -> list[dict]:
        """브로커에 열려 있는 주문 전부(앱 밖에서 낸 주문 포함). 실패는 예외."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = self._trading.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        out = []
        for o in orders or []:
            out.append({
                "id": str(getattr(o, "id", "") or ""),
                "coid": str(getattr(o, "client_order_id", "") or ""),
                "symbol": str(getattr(o, "symbol", "") or "").upper(),
                "side": str(getattr(o, "side", "") or "").split(".")[-1].lower(),
                "qty": float(getattr(o, "qty", 0) or 0),
                "filled_qty": float(getattr(o, "filled_qty", 0) or 0),
                "status": _status_of(o),
            })
        return out

    def get_account(self) -> dict:
        acct = self._trading.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            # 전일 종가 기준 자산 — 킬스위치 당일 손실률의 정확한 앵커
            "last_equity": float(getattr(acct, "last_equity", 0) or 0) or None,
        }

    def get_positions(self, strict: bool = False) -> dict[str, dict]:
        """브로커 실제 보유 포지션. {ticker: {shares, avg_price, market_value}}
        strict=True 면 조회 실패를 예외로 올린다 — '보유 0'과 '조회 실패'를 구분해야
        하는 곳(매도 게이트·대조)에서 쓴다."""
        try:
            poss = self._trading.get_all_positions()
        except Exception:
            if strict:
                raise
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
