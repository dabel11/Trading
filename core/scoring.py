"""자동매매 채점 단일 진입점.

대량 다운로드 1회 + 백테스터 전략 스코어러로 채점한다 — 모든 자동 경로
(데몬·앱 1회 실행)가 같은 점수를 보도록 보장. scorer.score_universe
(실시간 개별 신호)는 수동 스캔 전용으로 분리됐다.
"""
import time
from datetime import date, timedelta

_BUNDLE_CACHE: dict = {}
BUNDLE_TTL = 180     # 3분 — 점수는 천천히 변하므로 재다운로드 최소화


def score(strategy: str, tickers: list, ttl: int = BUNDLE_TTL):
    """전략 점수 + 현재가 + 시장 국면. 반환: (scores, prices, regime)

    scores: [{"ticker", "score"}] 내림차순 · prices: {ticker: 현재가}
    regime: "bull"|"bear"|"neutral" (SPY vs 200일선 ±2%)
    """
    import backtester as bt
    key = tuple(sorted(tickers))
    now = time.time()
    c = _BUNDLE_CACHE.get(key)
    if c and now - c[0] < ttl:
        bundle = c[1]
    else:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=730)).isoformat()
        bundle = bt.load_market_data(list(tickers), start, end)
        _BUNDLE_CACHE.clear()
        _BUNDLE_CACHE[key] = (now, bundle)
    sd, ed = bundle["stock_data"], bundle["etf_data"]
    scores, prices = [], {}
    for t in tickers:
        df = sd.get(t)
        if df is None or len(df) < 60:
            continue
        try:
            s = bt._strategy_score_bt(strategy, t, sd, ed, len(df) - 1)
        except Exception:
            s = 0.0
        scores.append({"ticker": t, "score": round(float(s), 1)})
        prices[t] = float(df["Close"].iloc[-1])
    scores.sort(key=lambda x: -x["score"])

    regime = "neutral"
    try:
        spy = bundle.get("spy_close"); ma = bundle.get("spy_ma200")
        if spy is not None and ma is not None:
            pv = float(spy.iloc[-1]); mv = float(ma.iloc[-1])
            if pv < mv * 0.98:
                regime = "bear"
            elif pv > mv * 1.02:
                regime = "bull"
    except Exception:
        pass
    return scores, prices, regime
