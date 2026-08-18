"""
토스증권 오픈 API 어댑터 (기반 골격).

상태: 토스 오픈 API는 2026-06 기준 **사전 신청 단계**로 정식 엔드포인트 스펙이
      아직 공개되지 않았다. 이 파일은 정식 출시 후 키만 받으면 바로 채워 넣을 수
      있도록 broker.Broker 와 **동일한 인터페이스**로 골격만 잡아둔 것이다.

설계 메모 (토스 발표 기준):
  - REST + WebSocket, 국내·해외 주식 **통합 API** (하나의 인터페이스로 둘 다)
  - 토스증권 PC 웹에서 발급한 API 키/시크릿 사용 → OAuth 액세스 토큰 방식 추정
    (대부분의 국내 증권 오픈API와 동일 패턴: app key/secret → access_token)
  - 미국 주식 야간 자동매매(분할매매) 지원

정식 스펙 공개 시 채울 곳은 모두 `# TODO(toss):` 로 표시했다.
공식: https://corp.tossinvest.com/ko/open-api

인터페이스 계약 (broker.Broker 와 동일해야 멀티브로커 팩토리가 갈아끼울 수 있음):
  get_prices(tickers)  -> {ticker: last_price}
  place_buy(t, shares) -> {id, ticker, shares, side, duplicate, fill_price, filled_qty, status}
  place_sell(t, shares)-> 위와 동일
  get_account()        -> {equity, cash, buying_power, last_equity}
  get_positions()      -> {ticker: {shares, avg_price, market_value, unrealized_pl}}
"""

import time
from datetime import datetime

from config import (
    TOSS_APP_KEY,
    TOSS_APP_SECRET,
    TOSS_BASE_URL,
)

# 정식 출시 전까지 주문류 호출 시 던지는 명확한 안내 메시지
_NOT_READY = (
    "토스증권 오픈 API는 아직 정식 출시 전(사전 신청 단계)이라 주문 엔드포인트가 "
    "공개되지 않았습니다. 정식 오픈 후 toss_broker.py 의 TODO(toss) 부분을 채우면 "
    "동작합니다. 그 전까지는 페이퍼/Alpaca 를 사용하세요."
)


class TossBroker:
    """토스증권 오픈 API 어댑터 (골격). broker.Broker 와 동일 인터페이스."""

    def __init__(self, paper: bool = True):
        self.paper = paper
        self._token = None
        self._token_exp = 0.0
        # 세션은 정식 연동 시 requests.Session() 등으로 교체
        self._session = None

    # ----------------------------------------------------------- 설정/인증

    @staticmethod
    def configured() -> bool:
        """API 키가 입력돼 있는지 (플레이스홀더가 아닌 실제 값)."""
        return bool(
            TOSS_APP_KEY and TOSS_APP_SECRET
            and not TOSS_APP_KEY.startswith("여기에")
            and not TOSS_APP_KEY.startswith("your_")
        )

    def _ensure_token(self) -> str:
        """OAuth 액세스 토큰 발급/캐시.

        대부분의 국내 증권 오픈API 패턴(app key/secret → access_token, 만료 시 갱신)을
        그대로 따를 것으로 보고 골격만 둔다.
        """
        if not self.configured():
            raise RuntimeError("토스 API 키가 설정되지 않았습니다 (.env 의 TOSS_APP_KEY/SECRET).")
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        # TODO(toss): 정식 토큰 발급 엔드포인트로 교체
        #   resp = POST {TOSS_BASE_URL}/oauth2/token
        #          {"grant_type":"client_credentials",
        #           "appkey":TOSS_APP_KEY, "appsecret":TOSS_APP_SECRET}
        #   self._token = resp["access_token"]; self._token_exp = time.time()+resp["expires_in"]
        raise NotImplementedError(_NOT_READY)

    # ----------------------------------------------------------- 종목 라우팅

    @staticmethod
    def _is_domestic(ticker: str) -> bool:
        """국내 종목 여부. '005930' 6자리 숫자 또는 .KS/.KQ → 국내, 그 외 → 해외."""
        t = (ticker or "").upper()
        if t.endswith((".KS", ".KQ")):
            return True
        core = t.split(".")[0]
        return core.isdigit() and len(core) == 6

    # ----------------------------------------------------------- 멱등 주문 ID

    def _coid(self, ticker: str, side: str, shares: int) -> str:
        """결정적 client_order_id (분 단위) — Alpaca 어댑터와 동일한 중복 차단 전략."""
        stamp = datetime.now().strftime("%Y%m%d%H%M")
        return f"ait-{side}-{ticker}-{shares}-{stamp}"

    # ----------------------------------------------------------- 시세

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """{ticker: last_price}. 정식 연동 전에는 빈 dict (앱이 yfinance 폴백)."""
        if not self.configured():
            return {}
        # TODO(toss): 국내/해외 현재가 조회 엔드포인트 연동
        #   국내: GET {TOSS_BASE_URL}/domestic/quotations/price?code=...
        #   해외: GET {TOSS_BASE_URL}/overseas/quotations/price?symbol=...
        #   (실시간은 WebSocket 채널 구독)
        return {}

    # ----------------------------------------------------------- 주문

    def _submit(self, ticker: str, shares: int, side_str: str) -> dict:
        self._ensure_token()  # 미구현 시 여기서 명확히 예외
        # TODO(toss): 국내/해외 분기하여 주문 전송
        #   coid = self._coid(ticker, side_str, shares)
        #   국내: POST {TOSS_BASE_URL}/domestic/trading/order   {code, qty, side, ord_type, coid}
        #   해외: POST {TOSS_BASE_URL}/overseas/trading/order   {symbol, qty, side, ord_type, coid}
        #   체결가는 응답 또는 체결 조회/WebSocket 으로 폴링
        raise NotImplementedError(_NOT_READY)

    def place_buy(self, ticker: str, shares: int) -> dict:
        return self._submit(ticker, shares, "buy")

    def place_sell(self, ticker: str, shares: int) -> dict:
        return self._submit(ticker, shares, "sell")

    # ----------------------------------------------------------- 계좌/잔고

    def get_account(self) -> dict:
        """{equity, cash, buying_power, last_equity}. 정식 연동 전에는 0/None."""
        if not self.configured():
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "last_equity": None}
        # TODO(toss): 계좌 잔고/예수금 조회 엔드포인트 연동
        #   GET {TOSS_BASE_URL}/account/balance  → 원화/외화 통합 평가
        return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "last_equity": None}

    def get_positions(self) -> dict[str, dict]:
        """{ticker: {shares, avg_price, market_value, unrealized_pl}}. 전에는 빈 dict."""
        if not self.configured():
            return {}
        # TODO(toss): 보유 종목(국내+해외) 조회 엔드포인트 연동
        return {}
