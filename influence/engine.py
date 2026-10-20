"""AI 인플루언서 엔진 — 공신력 소스 수집 → AI/LLM 판단 → 0~100 점수.

자동매매 사이클은 이 엔진의 `score_universe()` 가 돌려주는 점수를 기존 전략
스코어 자리에 그대로 사용한다(portfolio.generate_orders). 판단(Verdict)은
스냅샷(`LAST`)에 저장돼 UI·텔레그램이 "AI가 왜 이렇게 판단했는지"를 보여준다.

판단 기록(forward 검증용): LLM 판단은 학습데이터 미래 누설로 백테스트가 불가능해
시점 그대로의 누적 기록이 유일한 성과 측정 수단이다. `score_universe()` 가 새로
판단할 때마다(캐시 적중 제외) 한 줄을 `influence_log-YYYYMM.jsonl`(UTC 월 회전,
설정 파일과 같은 폴더 — 격리 모드면 격리 폴더)에 붙인다. 기록 실패는 로그만 남기고
매매 사이클엔 영향이 없다. 한 줄 스키마(v1):

    {"v": 1, "ts": 판단 완료 epoch, "iso": 같은 시각 ISO8601(UTC), "t0": 수집 시작 epoch,
     "regime": "bull"|"bear"|"neutral", "by": "llm"|"rule",
     "provider": LLM 제공자|null, "model": LLM 모델명|null, "buy_th", "sell_th",
     "held": [보유 티커],
     "verdicts": [Verdict.to_dict() + {"price": 판단 시점 가격|null}, ...]}

    Verdict.to_dict = {ticker, action, score, conviction, rationale, by(종목별 — LLM 응답
    누락 종목은 rule), ts, evidence: [{source_key, source_label, category, credibility,
    stance, confidence, detail, raw(원천 값)}]}
"""
from __future__ import annotations

import concurrent.futures
import time
from datetime import datetime, timezone
from pathlib import Path

import ai_judge
from influence import providers

# 판단 스냅샷 — UI/텔레그램이 읽는 최신 결과
LAST: dict = {"ts": 0.0, "verdicts": {}, "by": "rule", "regime": "neutral"}
_TTL = 180.0   # 판단 캐시(초) — scoring 번들 TTL 과 맞춤
LOG_DIR: Path | None = None   # None = 설정 파일과 같은 폴더(격리 모드면 격리 폴더)


def _ctx(ticker: str, held: set, regime: str) -> dict:
    return {"held": ticker in held, "regime": regime}


def _log_file(ts: float) -> Path:
    base = LOG_DIR
    if base is None:
        from core import control
        base = control.CONFIG_FILE.parent
    return Path(base) / f"influence_log-{time.strftime('%Y%m', time.gmtime(ts))}.jsonl"


def _append_log(t0: float, held: set, regime: str, buy_th: float, sell_th: float,
                by: str, verdicts_list: list, prices: dict | None) -> None:
    """새 판단 1회를 시점 그대로 JSONL 한 줄로 누적 (스키마는 모듈 docstring)."""
    if not verdicts_list:
        return
    from safe_store import append_jsonl
    ts = time.time()
    cfg = ai_judge._llm_config() if by == "llm" else None   # 키는 기록하지 않는다
    prices = prices or {}
    rows = []
    for v in verdicts_list:
        d = v.to_dict()
        px = prices.get(v.ticker)
        d["price"] = float(px) if px else None
        rows.append(d)
    append_jsonl(_log_file(ts), {
        "v": 1, "ts": round(ts, 3),
        "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
        "t0": round(t0, 3), "regime": regime, "by": by,
        "provider": cfg["provider"] if cfg else None,
        "model": cfg["model"] if cfg else None,
        "buy_th": buy_th, "sell_th": sell_th, "held": sorted(held),
        "verdicts": rows,
    })


def score_universe(tickers: list, held: set | None = None, regime: str = "neutral",
                   buy_th: float = 60.0, sell_th: float = 35.0,
                   use_llm: bool = True, ttl: float = _TTL,
                   log=None, prices: dict | None = None) -> tuple[dict, dict]:
    """tickers 전체를 AI로 판단.

    prices{ticker: 현재가} 는 판단 기록에만 쓰인다(점수엔 영향 없음).
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
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    futs = {pool.submit(providers.gather, t): t for t in tickers}
    try:
        for f in concurrent.futures.as_completed(futs, timeout=deadline):
            try:
                evidence_map[futs[f]] = f.result() or []
            except Exception:
                evidence_map[futs[f]] = []
    except concurrent.futures.TimeoutError:
        L(f"일부 소스 응답 지연 — {deadline:.0f}s 초과분은 건너뜀(백그라운드로 계속 수집)")
    finally:
        # 마감을 실제로 지킨다: 남은 작업은 기다리지 않음(진행 중인 건 캐시를 채우며 끝남)
        pool.shutdown(wait=False, cancel_futures=True)

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
    try:
        from influence import scanner as _sc
        _sc.merge(verdicts)
    except Exception:
        pass
    # 3) 판단 기록 — 실패해도 사이클은 그대로 진행(로그만)
    try:
        _append_log(now, held, regime, buy_th, sell_th, by, verdicts_list, prices)
    except Exception as e:
        L(f"AI 판단 기록 실패(매매 영향 없음): {type(e).__name__}: {e}")
    return ({t: verdicts[t].score for t in tickers if t in verdicts}, verdicts)


def verdict_for(ticker: str, buy_th: float = 60.0, sell_th: float = 35.0) -> ai_judge.Verdict:
    """단일 종목 즉시 판단 (텔레그램 /ai 등). 캐시 우선."""
    ticker = ticker.upper().strip()
    hit = LAST["verdicts"].get(ticker)
    if hit and time.time() - LAST["ts"] < _TTL:
        return hit
    try:
        from influence import scanner as _sc
        fv = _sc.FULL.get(ticker)
        if fv is not None and time.time() - fv.ts < 3600:
            return fv
    except Exception:
        pass
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
