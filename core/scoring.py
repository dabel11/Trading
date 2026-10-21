"""자동매매 채점 단일 진입점.

대량 다운로드 1회 + (전략 스코어러 | AI 인플루언서 엔진)으로 채점한다 — 모든
자동 경로(데몬·앱 1회 실행)가 같은 점수를 보도록 보장. scorer.score_universe
(실시간 개별 신호)는 수동 스캔 전용으로 분리됐다.

strategy == "ai_influencer" 이면 전략 공식 대신 influence.engine 이 공신력
소스(애널리스트·내부자·미디어·SNS·군중)를 모아 AI/LLM 로 판단한 0~100 점수를
쓴다. 가격·시장국면은 기존 번들에서 그대로 얻으므로 하위 파이프라인은 무수정.
"""
import hashlib
import time
from datetime import date, timedelta

_BUNDLE_CACHE: dict = {}
BUNDLE_TTL = 180     # 3분 — 점수는 천천히 변하므로 재다운로드 최소화
TIE_SEED = 0         # 동점 순서 seed — 바꾸면 모든 날의 순서가 다른 표본으로 바뀐다


def _tie_day() -> str:
    """동점 순서를 고정하는 기준일 = 미국 동부(ET) 날짜.

    로컬(KST) date.today() 는 ET 오전 10~11시, 장중에 바뀌어 같은 세션 안에서 순서가 뒤집힌다."""
    import market_hours as mh
    return mh.now_et().date().isoformat()


def tie_key(ticker: str, day: str, seed: int = TIE_SEED) -> int:
    """동점 보조 키(작을수록 앞) — blake2b(seed|day|ticker) 64비트.

    티커 하나만 보므로 입력 순서·유니버스 구성과 무관하다. 내장 hash() 는 PYTHONHASHSEED 로
    프로세스마다 달라져(재시작하면 순서가 바뀜) 쓰면 안 된다."""
    h = hashlib.blake2b(f"{seed}|{day}|{ticker}".encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")


def rank(scores: list, day: str | None = None, seed: int = TIE_SEED) -> None:
    """점수 내림차순 정렬(제자리). 동점은 그날(ET)의 무작위 순서 tie_key, 키까지 같으면 티커순.

    예전엔 안정 정렬이라 동점이면 입력(워치리스트) 순서대로 샀다 — 이산 점수 전략은 동점이
    흔해 앞쪽 종목만 사는 편향이 생긴다. 300초마다 다시 채점해도 같은 날엔 순서가 같아야
    빈 슬롯을 채우는 종목이 사이클마다 바뀌지 않는다. 날이 바뀌면 새 순서.

    백테스트 tie_order("random") 와의 관계 — 분포는 같고 실현값은 다르다:
      · 백테스트: 결정일(idx)마다 default_rng([seed, idx]) 로 유니버스 전체(이름순)의 균등
        무작위 순열. 균등 순열을 동점 부분집합으로 좁혀도 균등 순서다.
      · 라이브: 티커마다 독립 균등 64비트 키로 정렬 = 동점 집합의 균등 무작위 순서
        (키 충돌 확률 ≈ k²/2⁶⁵, 무시 가능 — 충돌하면 티커순).
      → 둘 다 '결정일마다 새 무작위 순서, 동점 종목 간 선호 없음'.
    다른 점:
      1) 같은 seed 라도 백테스트 순서를 재현하지 않는다(판정은 seed 20개 평균이라 불필요).
         라이브는 seed 하나 = 그 분포의 한 표본 — 성과는 평균이 아니라 seed 간 퍼짐(±sd)과 비교.
      2) 백테스트 순열은 유니버스 구성에 의존한다(이름 하나가 늘면 전원 순서가 바뀜). 라이브는
         구성과 무관 — 장중에 워치리스트가 바뀌어도 남은 종목끼리의 순서는 그대로다.
      3) 순서가 바뀌는 주기: 백테스트는 결정일마다(기본 REBALANCE_DAYS=7거래일), 라이브는 ET
         거래일마다. 라이브가 더 자주 섞인다. 순서만 바뀌어서는 보유를 팔지 않지만(ROTATE_MARGIN),
         보유보다 마진 이상 높은 신규 후보가 하나라도 있으면 그날 순서로 목표에서 밀린 동점 보유가
         전부 회전된다 — test_live_tie_break 의 회전 테스트 참고."""
    d = day or _tie_day()
    scores.sort(key=lambda r: (-r["score"], tie_key(r["ticker"], d, seed), r["ticker"]))


def score(strategy: str, tickers: list, ttl: int = BUNDLE_TTL, log=None):
    """전략 점수 + 현재가 + 시장 국면. 반환: (scores, prices, regime)

    scores: [{"ticker", "score"}] 내림차순(동점은 rank — 그날의 무작위 순서) · prices: {ticker: 현재가}
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

    rank(scores)
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
    rank(scores)
    return scores, prices, regime
