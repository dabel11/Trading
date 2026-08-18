"""
뉴스·심리 신호 (0-100). 무료 데이터만 사용.

이전 구현은 Finnhub 의 news_sentiment(프리미엄) 엔드포인트를 호출했는데,
무료 플랜에서는 항상 에러 → 50(중립) 으로 떨어져 사실상 죽은 신호였고,
종목당 API를 2번(company_news + news_sentiment) 호출해 분당 한도까지 낭비했다.

개선:
  1순위) Finnhub 무료 company_news 헤드라인을 소형 긍/부정 어휘사전으로 채점 (1회 호출)
  2순위) 키 없음/뉴스 없음 → yfinance 가격·거래량 모멘텀을 심리 프록시로 사용
모두 실패해야 50(중립). 결과가 종목마다 실제로 달라져 신호가 살아난다.
"""

import time
from datetime import datetime, timedelta
from config import FINNHUB_API_KEY, SENTIMENT_LOOKBACK_DAYS

_POS = {
    "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies",
    "beat", "beats", "upgrade", "upgraded", "record", "strong", "gains", "gain",
    "profit", "growth", "bullish", "outperform", "raise", "raised", "top", "tops",
    "win", "wins", "rise", "rises", "boost", "boosts", "buy", "breakout",
    "rebound", "expand", "expands", "approval", "approved", "soaring",
}
_NEG = {
    "miss", "misses", "fall", "falls", "drop", "drops", "plunge", "plunges",
    "downgrade", "downgraded", "cut", "cuts", "weak", "loss", "losses", "lawsuit",
    "probe", "decline", "declines", "slump", "bearish", "warn", "warns", "slash",
    "layoff", "layoffs", "recall", "fraud", "sink", "sinks", "tumble", "tumbles",
    "downbeat", "halt", "delay", "delays", "investigation", "bankruptcy",
}

_client = None


def _get_client():
    global _client
    if _client is None:
        import finnhub
        _client = finnhub.Client(api_key=FINNHUB_API_KEY)
    return _client


def _lexicon_score(headlines: list[str]) -> float | None:
    """헤드라인 리스트를 긍/부정 어휘로 채점 → 0-100. 단어 미검출 시 None."""
    pos = neg = 0
    for h in headlines:
        for w in h.lower().replace(".", " ").replace(",", " ").split():
            if w in _POS: pos += 1
            elif w in _NEG: neg += 1
    if pos + neg == 0:
        return None
    bull = pos / (pos + neg)              # 0~1
    # 기사 수(buzz)로 신뢰도 가중: 많을수록 50에서 멀어지게
    buzz = min(1.0, len(headlines) / 20.0)
    centered = (bull - 0.5) * buzz + 0.5
    return round(min(100.0, max(0.0, centered * 100)), 1)


def _price_proxy(ticker: str) -> float:
    """가격·거래량 모멘텀 기반 심리 프록시 (뉴스 불가 시 폴백)."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="1mo", interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or len(df) < 6:
            return 50.0
        c = df["Close"].squeeze()
        cur = float(c.iloc[-1])
        r5 = (cur / float(c.iloc[-6]) - 1) if len(c) > 6 else 0.0
        r20 = (cur / float(c.iloc[0]) - 1)
        mom = r5 * 0.6 + r20 * 0.4
        # ±10% 모멘텀을 0~100 으로 매핑 (50 = 보합)
        return round(min(100.0, max(0.0, 50 + mom * 250)), 1)
    except Exception:
        return 50.0


def score(ticker: str) -> float:
    # 1) Finnhub 무료 뉴스 + 어휘 채점 (1회 호출, 프리미엄 미사용)
    try:
        key = FINNHUB_API_KEY or ""
        if key and "your_" not in key:
            client = _get_client()
            end = datetime.now()
            start = end - timedelta(days=SENTIMENT_LOOKBACK_DAYS)
            news = client.company_news(
                ticker, _from=start.strftime("%Y-%m-%d"),
                to=end.strftime("%Y-%m-%d"))
            time.sleep(0.05)
            if news:
                heads = [f"{n.get('headline','')} {n.get('summary','')}" for n in news[:40]]
                lex = _lexicon_score(heads)
                if lex is not None:
                    return lex
    except Exception:
        pass
    # 2) 가격 모멘텀 프록시
    return _price_proxy(ticker)
