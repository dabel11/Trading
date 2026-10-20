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
_DV_CACHE: dict = {}   # TK -> (ts, 최근 평균 거래대금) — AI 경로 동점 처리용
DV_TTL = 6 * 3600      # 20일 평균이라 천천히 변한다


def rank(scores: list, dollar_volume: dict) -> None:
    """점수 내림차순 정렬(제자리). 동점은 최근 평균 거래대금 큰 순, 그것도 같으면 티커순.

    예전엔 안정 정렬이라 동점이면 입력(워치리스트) 순서대로 샀다 — 이산 점수 전략은
    동점이 흔해 앞쪽 종목만 사는 편향이 생긴다. 백테스트 tie_order("dollar_volume") 와 같은 기준."""
    scores.sort(key=lambda r: (-r["score"], -float(dollar_volume.get(r["ticker"]) or 0.0),
                               r["ticker"]))


def _tied(scores: list) -> list:
    """다른 종목과 점수가 같은 티커들."""
    from collections import Counter
    n = Counter(r["score"] for r in scores)
    return [r["ticker"] for r in scores if n[r["score"]] > 1]


def _dv_from_bundle(sd: dict, tickers: list) -> dict:
    """번들 일봉 → 최근 TIE_DV_WINDOW 거래일 평균 거래대금(종가×거래량)."""
    from backtester import TIE_DV_WINDOW
    out = {}
    for t in tickers:
        try:
            df = sd[t]
            v = float((df["Close"] * df["Volume"]).tail(TIE_DV_WINDOW).mean())
            out[t] = v if v == v else 0.0
        except Exception:
            out[t] = 0.0
    return out


def _dv_live(tickers: list) -> dict:
    """번들이 없는 AI 경로 — Nasdaq 일봉으로 평균 거래대금(캐시 DV_TTL)."""
    import stock_browser as sb
    from concurrent.futures import ThreadPoolExecutor
    from backtester import TIE_DV_WINDOW
    now = time.time()
    out, need = {}, []
    for t in tickers:
        hit = _DV_CACHE.get(t)
        if hit and now - hit[0] < DV_TTL:
            out[t] = hit[1]
        else:
            need.append(t)
    if need:
        with ThreadPoolExecutor(max_workers=8) as ex:
            vals = list(ex.map(lambda t: sb.nasdaq_dollar_volume(t, TIE_DV_WINDOW), need))
        for t, v in zip(need, vals):
            out[t] = v
            if v > 0:
                _DV_CACHE[t] = (time.time(), v)
    return out


def score(strategy: str, tickers: list, ttl: int = BUNDLE_TTL, log=None):
    """전략 점수 + 현재가 + 시장 국면. 반환: (scores, prices, regime)

    scores: [{"ticker", "score"}] 내림차순 · prices: {ticker: 현재가}
    regime: "bull"|"bear"|"neutral" (SPY vs 200일선 ±2%)
    """
    if strategy == "ai_influencer":
        return _score_ai(tickers, log=log)
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
        smap, _verdicts = _eng.score_universe(valid, regime=regime, log=log,
                                              prices=prices)
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

    try:
        dv = _dv_from_bundle(sd, _tied(scores))
    except Exception:
        dv = {}
    rank(scores, dv)
    return scores, prices, regime


def _score_ai(tickers: list, log=None):
    """AI 인플루언서 채점 — 2년치 Yahoo 번들(429 위험) 없이 동작.

    가격: 실시간 피드 → Nasdaq 공개 시세. 국면: SPY vs 200일선(Nasdaq 일봉).
    대상이 수십 종목으로 늘어도(전체 시장 스캔 상위 편입) 레이트리밋에 막히지 않는다.
    """
    import stock_browser as sb
    from influence import engine as _eng
    tickers = list(dict.fromkeys(t.upper() for t in tickers if t))
    prices: dict = {}
    try:
        import realtime_feed as rtf
        for t in tickers:
            d = rtf.get_price(t)
            if d and d.get("price"):
                prices[t] = float(d["price"])
    except Exception:
        pass
    missing = [t for t in tickers if t not in prices]
    if missing:
        try:
            prices.update(sb.quote_prices(missing))
        except Exception:
            pass
    regime = sb.market_regime()
    valid = [t for t in tickers if prices.get(t, 0) > 0]
    smap, _v = _eng.score_universe(valid, regime=regime, log=log, prices=prices)
    scores = [{"ticker": t, "score": round(float(smap.get(t, 50.0)), 1)} for t in valid]
    dv: dict = {}
    tied = _tied(scores)
    if tied:
        try:
            dv = _dv_live(tied)
        except Exception as e:
            if log:
                log(f"동점 거래대금 조회 실패 — 티커순으로 정렬: {e}")
    rank(scores, dv)
    return scores, prices, regime
