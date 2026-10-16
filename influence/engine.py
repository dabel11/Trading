"""AI 인플루언서 엔진 — 공신력 소스 수집 → AI/LLM 판단 → 0~100 점수.

자동매매 사이클은 이 엔진의 `score_universe()` 가 돌려주는 점수를 기존 전략
스코어 자리에 그대로 사용한다(portfolio.generate_orders). 판단(Verdict)은
스냅샷(`LAST`)에 저장돼 UI·텔레그램이 "AI가 왜 이렇게 판단했는지"를 보여준다.
"""
from __future__ import annotations

import concurrent.futures
import time

import ai_judge
from influence import providers

# 판단 스냅샷 — UI/텔레그램이 읽는 최신 결과
LAST: dict = {"ts": 0.0, "verdicts": {}, "by": "rule", "regime": "neutral"}
_TTL = 180.0   # 판단 캐시(초) — scoring 번들 TTL 과 맞춤


def _ctx(ticker: str, held: set, regime: str) -> dict:
    return {"held": ticker in held, "regime": regime}


def score_universe(tickers: list, held: set | None = None, regime: str = "neutral",
                   buy_th: float = 60.0, sell_th: float = 35.0,
                   use_llm: bool = True, ttl: float = _TTL,
                   log=None) -> tuple[dict, dict]:
    """tickers 전체를 AI로 판단.

    반환: (scores{ticker: 0~100}, verdicts{ticker: Verdict})
    """
    L = log or (lambda m: None)
    held = held or set()
    now = time.time()

    # 캐시 적중: TTL 내면 재판단 생략
    if LAST["verdicts"] and now - LAST["ts"] < ttl and \
            set(LAST["verdicts"].keys()) >= set(tickers):
        v = LAST["verdicts"]
        return ({t: v[t].score for t in tickers if t in v},
                {t: v[t] for t in tickers if t in v})

    # 1) 증거 수집 (IO 바운드 → 제한 병렬). Finnhub 무료 한도 보호 위해 보수적.
    #    전체 타임아웃을 둬, 특정 소스가 멈춰도 엔진이 영원히 매달리지 않게 한다
    #    (남은 종목은 빈 증거로 진행 → 최소 '중립' 판단은 보장).
    evidence_map: dict = {t: [] for t in tickers}
    deadline = 30.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(providers.gather, t): t for t in tickers}
        try:
            for f in concurrent.futures.as_completed(futs, timeout=deadline):
                try:
                    evidence_map[futs[f]] = f.result() or []
                except Exception:
                    evidence_map[futs[f]] = []
        except concurrent.futures.TimeoutError:
            L(f"일부 소스 응답 지연 — {deadline:.0f}s 초과분은 건너뜀")

    # 2) AI 판단 (LLM 1콜 배치 or 규칙)
    items = [(t, evidence_map.get(t, []), _ctx(t, held, regime)) for t in tickers]
    verdicts_list = ai_judge.judge_batch(items, buy_th=buy_th, sell_th=sell_th,
                                         use_llm=use_llm)
    verdicts = {v.ticker: v for v in verdicts_list}
    by = verdicts_list[0].by if verdicts_list else "rule"

    n_buy = sum(1 for v in verdicts_list if v.action == "BUY")
    n_sell = sum(1 for v in verdicts_list if v.action == "SELL")
    L(f"AI 판단({'LLM' if by == 'llm' else '규칙'}) · {len(tickers)}종목 "
      f"매수의견 {n_buy} / 매도의견 {n_sell}")

    LAST.update(ts=now, verdicts=verdicts, by=by, regime=regime)
    return ({t: verdicts[t].score for t in tickers if t in verdicts}, verdicts)


def verdict_for(ticker: str, buy_th: float = 60.0, sell_th: float = 35.0) -> ai_judge.Verdict:
    """단일 종목 즉시 판단 (텔레그램 /ai 등). 캐시 우선."""
    ticker = ticker.upper().strip()
    hit = LAST["verdicts"].get(ticker)
    if hit and time.time() - LAST["ts"] < _TTL:
        return hit
    _, v = score_universe([ticker], buy_th=buy_th, sell_th=sell_th)
    return v.get(ticker) or ai_judge.Verdict(
        ticker, "HOLD", 50.0, 0.0, "데이터 없음", "rule", [])


def snapshot(top: int = 20) -> dict:
    """UI/텔레그램용 — 최신 판단 요약 (점수 내림차순)."""
    vs = sorted(LAST["verdicts"].values(), key=lambda v: -v.score)[:top]
    return {
        "ts": LAST["ts"], "by": LAST["by"], "regime": LAST["regime"],
        "llm_active": ai_judge.llm_active(),
        "verdicts": [v.to_dict() for v in vs],
    }
