"""자동매매 채점 단일 진입점.

대량 다운로드 1회 + (전략 스코어러 | AI 인플루언서 엔진)으로 채점한다 — 모든
자동 경로(데몬·앱 1회 실행)가 같은 점수를 보도록 보장. scorer.score_universe
(실시간 개별 신호)는 수동 스캔 전용으로 분리됐다.

strategy == "ai_influencer" 이면 전략 공식 대신 influence.engine 이 공신력
소스(애널리스트·내부자·미디어·SNS·군중)를 모아 AI/LLM 로 판단한 0~100 점수를
쓴다. 가격·시장국면은 기존 번들에서 그대로 얻으므로 하위 파이프라인은 무수정.
"""
import time
from datetime import date, timedelta

_BUNDLE_CACHE: dict = {}
BUNDLE_TTL = 180     # 3분 — 점수는 천천히 변하므로 재다운로드 최소화


def score(strategy: str, tickers: list, ttl: int = BUNDLE_TTL, log=None):
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

    # 1) 현재가 + 채점 대상(충분한 이력 보유) 선별
    prices, valid = {}, []
    for t in tickers:
        df = sd.get(t)
        if df is None or len(df) < 60:
            continue
        prices[t] = float(df["Close"].iloc[-1])
        valid.append(t)

    # 2) 시장 국면 (AI 판단에도 컨텍스트로 넘김)
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

    # 3) 채점 — AI 인플루언서 엔진 또는 전략 공식
    if strategy == "ai_influencer":
        from influence import engine as _eng
        smap, _verdicts = _eng.score_universe(valid, regime=regime, log=log)
        scores = [{"ticker": t, "score": round(float(smap.get(t, 50.0)), 1)}
                  for t in valid]
    else:
        scores = []
        for t in valid:
            try:
                s = bt._strategy_score_bt(strategy, t, sd, ed, len(sd[t]) - 1)
            except Exception:
                s = 0.0
            scores.append({"ticker": t, "score": round(float(s), 1)})

    scores.sort(key=lambda x: -x["score"])
    return scores, prices, regime
