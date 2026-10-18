"""EV(기대값) 판단 — 매 사이클 '매수 / 대기 / 보유 / 매도'를 기대수익으로 결정한다.

왜 점수만으론 부족한가: 점수 61과 90은 둘 다 '매수 문턱 60 이상'이지만, 손절·익절·
트레일링·보유기간이라는 실제 청산 규칙 아래에서 기대할 수 있는 수익은 전혀 다르다.
EV 는 "지금 사서 이 규칙대로 들고 있으면 평균적으로 몇 % 남는가(비용 차감)"를 직접 계산한다.

모델 (종목별)
  σ  = 최근 6개월 일간 수익률 표준편차
  μ  = 모멘텀 드리프트(6개월 평균 일간수익 × 축소계수, 이상치 윈저라이즈)
     + 신호 드리프트(IR_MAX × (점수-50)/50 × 확신도 × σ)
  → 일간 로그수익 (μ - σ²/2) + σ·t(4) 로 경로 시뮬레이션(안티테틱, 날짜·종목 고정 시드)
  → 실제 청산 규칙을 그대로 적용: 손절(진입가 -SL) · 익절 도달 후 고점 대비 TRAIL 되돌림 ·
    보유기간 만료(점수≥75 → hold_strong, 아니면 hold_medium)
  → EV = 평균 청산수익 - 왕복비용, 승률 = 비용 넘는 경로 비율

판단
  신규(점수 ≥ 매수 문턱인 후보만): EV ≥ ev_min 이고 승률 ≥ p_min → BUY, 아니면 WAIT
  보유: '계속 보유'의 EV < ev_exit → SELL, 아니면 HOLD
  드리프트가 0(우위 없음)이면 가격이 마팅게일이라 EV≈0 → 비용 때문에 자연히 WAIT.

안전 방향: EV 는 기존 주문 로직의 점수만 조정한다(gate_scores) — 매수를 '막거나'
매도를 '추가'만 하고, 새 매수를 만들거나 기존 손절·트레일링을 끄지 않는다.
라이브(core.cycle)와 백테스트(backtester.run(ev_filter=True))가 같은 함수를 쓴다.
"""
from __future__ import annotations

import time
import zlib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import numpy as np

EV_FILE: Optional[Path] = None   # None = 설정 파일과 같은 폴더(격리 모드면 격리 폴더)


def _ev_file() -> Path:
    if EV_FILE is not None:
        return EV_FILE
    from core import control
    return control.CONFIG_FILE.parent / "ev_decisions.json"

IR_MAX = 0.05          # 최강 신호(점수100·확신1)의 일간 정보비율 상한 (σ2% 기준 월 +2%)
MOM_SHRINK = 0.15      # 과거 평균수익을 미래 드리프트로 쓸 때의 축소계수
MU_CAP = 0.003         # 일간 드리프트 절대값 상한(0.3%)
MIN_BARS = 40          # 이보다 일봉이 적으면 '데이터 부족'


@dataclass
class EVParams:
    sl: float = 0.07             # 손절
    tp: float = 0.15             # 익절(도달 시 트레일링 시작)
    trail: float = 0.07          # 고점 대비 되돌림 청산
    hold_strong: int = 60        # 점수 ≥ 75 보유기간(일)
    hold_medium: int = 30
    cost_side: float = 0.0005    # 편도 비용(수수료+슬리피지)
    ev_min: float = 0.0          # 신규 매수 최소 EV(비용 차감 후 0% — 음의 EV만 대기)
    ev_exit: float = -0.005      # 보유 EV 가 이보다 낮으면 매도(-0.5%)
    p_min: float = 0.40          # 최소 승률(추세추종은 승률<50%여도 EV+ 가능)
    min_score: float = 60
    sell_score: float = 35
    n_paths: int = 4000

    @classmethod
    def from_cfg(cls, cfg: dict, **over) -> "EVParams":
        p = cls(
            sl=float(cfg.get("stop_loss", 0.07)), tp=float(cfg.get("take_profit", 0.15)),
            trail=float(cfg.get("trail", 0.07)),
            hold_strong=int(cfg.get("hold_strong", 60)), hold_medium=int(cfg.get("hold_medium", 30)),
            cost_side=float(cfg.get("ev_cost_bps", 5)) / 1e4,
            ev_min=float(cfg.get("ev_min", 0.0)), ev_exit=float(cfg.get("ev_exit", -0.005)),
            p_min=float(cfg.get("ev_p_min", 0.40)),
            min_score=float(cfg.get("min_score", 60)), sell_score=float(cfg.get("sell_score", 35)))
        for k, v in over.items():
            setattr(p, k, v)
        return p


@dataclass
class EVDecision:
    ticker: str
    action: str                  # BUY | WAIT | HOLD | SELL
    held: bool
    score: float
    conviction: Optional[float] = None
    price: float = 0.0
    ev: float = 0.0              # 기대수익(신규=왕복비용 차감, 보유=지금 팔 때 대비)
    p_win: float = 0.0
    exp_days: float = 0.0
    kelly: float = 0.0           # 하프 켈리 비중(참고용 — 주문 크기엔 쓰지 않음)
    cvar5: float = 0.0           # 최악 5% 경로 평균수익
    mu: float = 0.0
    sigma: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── 추정 ─────────────────────────────────────────────────────────────────
def estimate_drift(closes, score: float, conviction: Optional[float] = None):
    """(mu, sigma) 일간 산술 드리프트·변동성. 데이터 부족이면 None."""
    c = np.asarray([float(x) for x in (closes or []) if x and float(x) > 0], float)
    if len(c) < MIN_BARS:
        return None
    r = np.diff(c) / c[:-1]
    r = r[-126:]
    sigma = float(np.std(r, ddof=1))
    sigma = min(max(sigma, 0.004), 0.08)
    r_w = np.clip(r, -3 * sigma, 3 * sigma)          # 갭 하루가 드리프트를 지배하지 않게
    mu_mom = MOM_SHRINK * float(np.mean(r_w))
    s = max(-1.0, min(1.0, (float(score) - 50.0) / 50.0))
    conv = 0.5 if conviction is None else max(0.0, min(1.0, float(conviction)))
    mu_sig = IR_MAX * s * conv * sigma
    mu = max(-MU_CAP, min(MU_CAP, mu_mom + mu_sig))
    return mu, sigma


def p_hit_up_first(a: float, b: float, mu: float, sigma: float) -> float:
    """드리프트 μ·변동성 σ 브라운 운동이 +a 를 -b 보다 먼저 칠 확률(해석해 — 시뮬 검증용)."""
    if sigma <= 0:
        return 1.0 if mu > 0 else 0.0
    k = 2.0 * mu / (sigma * sigma)
    if abs(k) * max(a, b) < 1e-9:
        return b / (a + b)
    k = max(-50.0 / max(a, b), min(50.0 / max(a, b), k))
    return (1.0 - np.exp(k * b)) / (np.exp(-k * a) - np.exp(k * b))


def simulate(price: float, entry: float, peak: float, mu: float, sigma: float,
             sl: float, tp: float, trail: float, horizon: int,
             n: int = 4000, seed: int = 0):
    """청산 규칙(손절·익절후 트레일링·기간만료)을 적용한 경로별 (현재가 대비 청산수익, 청산일)."""
    horizon = max(1, int(horizon))
    half = max(1, n // 2)
    rng = np.random.default_rng(seed)
    z = rng.standard_t(4, size=(half, horizon)) / np.sqrt(2.0)   # 단위분산 t(4): 두꺼운 꼬리
    z = np.vstack([z, -z])
    steps = (mu - 0.5 * sigma * sigma) + sigma * z
    px = price * np.exp(np.cumsum(steps, axis=1))
    m = px.shape[0]
    stop = entry * (1.0 - sl)
    tp_px = entry * (1.0 + tp)
    pk = np.full(m, max(peak, price), float)
    alive = np.ones(m, bool)
    exit_px = px[:, -1].copy()
    exit_day = np.full(m, float(horizon))
    for d in range(horizon):
        p = px[:, d]
        pk = np.maximum(pk, p)
        hit = (p <= stop) | ((pk >= tp_px) & (p <= pk * (1.0 - trail)))
        ex = alive & hit
        if ex.any():
            exit_px[ex] = p[ex]
            exit_day[ex] = d + 1
            alive &= ~ex
        if not alive.any():
            break
    return exit_px / price - 1.0, exit_day


def _seed(ticker: str, today) -> int:
    return zlib.crc32(f"{ticker}|{today}".encode()) & 0x7FFFFFFF


# ── 판단 ─────────────────────────────────────────────────────────────────
def evaluate(ticker: str, score: float, closes, price: float, params: EVParams,
             conviction: Optional[float] = None, position=None, today=None) -> EVDecision:
    """한 종목의 EV 판단. position = portfolio.Position(보유 중이면)."""
    held = position is not None
    today = today or date.today()
    d = EVDecision(ticker, "HOLD" if held else "WAIT", held, float(score), conviction, float(price or 0))
    if not price or price <= 0:
        d.reason = "가격 없음"
        return d
    est = estimate_drift(closes, score, conviction)
    if est is None:
        d.reason = "일봉 데이터 부족 — 판단 보류" if not held else "일봉 데이터 부족 — 기존 규칙 유지"
        return d
    mu, sigma = est
    d.mu, d.sigma = mu, sigma

    if held:
        entry, peak = float(position.entry_price), float(getattr(position, "peak_price", 0) or 0)
        try:
            left = int(position.max_hold_days()) - int(position.days_held())
        except Exception:
            left = params.hold_medium
        horizon = max(3, left)
        if price <= entry * (1 - params.sl) or (
                peak >= entry * (1 + params.tp) and price <= peak * (1 - params.trail)):
            d.action, d.reason = "SELL", "청산 규칙 도달(손절/트레일링)"
            return d
        cost = 0.0                    # 지금 팔든 나중에 팔든 편도 비용은 같다
    else:
        entry, peak = float(price), float(price)
        horizon = params.hold_strong if score >= 75 else params.hold_medium
        cost = 2.0 * params.cost_side

    ret, days = simulate(price, entry, peak, mu, sigma, params.sl, params.tp, params.trail,
                         horizon, n=params.n_paths, seed=_seed(ticker, today))
    net = ret - cost
    d.ev = float(np.mean(net))
    d.p_win = float(np.mean(net > 0))
    d.exp_days = float(np.mean(days))
    m2 = float(np.mean(net * net))
    d.kelly = float(max(0.0, min(1.0, 0.5 * d.ev / m2))) if m2 > 0 else 0.0
    k = max(1, int(len(net) * 0.05))
    d.cvar5 = float(np.mean(np.sort(net)[:k]))

    if held:
        if d.ev < params.ev_exit:
            d.action = "SELL"
            d.reason = f"보유 EV {d.ev:+.1%} < 매도 기준 {params.ev_exit:+.1%}"
        else:
            d.action = "HOLD"
            d.reason = f"보유 EV {d.ev:+.1%} · 예상 {d.exp_days:.0f}일"
    else:
        if d.ev < params.ev_min:
            d.action = "WAIT"
            d.reason = f"EV {d.ev:+.1%} < 매수 기준 {params.ev_min:+.1%}"
        elif d.p_win < params.p_min:
            d.action = "WAIT"
            d.reason = f"승률 {d.p_win:.0%} < {params.p_min:.0%}"
            if sigma * np.sqrt(10) > params.sl:     # 2주 변동폭이 손절폭보다 큼 → 잦은 손절
                d.reason += " · 변동성 대비 손절폭 좁음"
        else:
            d.action = "BUY"
            d.reason = f"EV {d.ev:+.1%} · 승률 {d.p_win:.0%} · 예상 {d.exp_days:.0f}일"
    return d


def decide(scores, prices: dict, positions: dict, params: EVParams,
           closes_fn: Callable[[str], list], conviction_fn: Optional[Callable] = None,
           today=None) -> list:
    """scores: [(ticker, score)]. 후보 = 보유 + 점수 ≥ 매수 문턱. closes_fn(t) → 일봉 종가."""
    out = []
    for t, sc in scores:
        held = t in positions
        if not held and sc < params.min_score:
            continue
        try:
            conv = conviction_fn(t) if conviction_fn else None
        except Exception:
            conv = None
        try:
            closes = closes_fn(t)
        except Exception:
            closes = []
        out.append(evaluate(t, sc, closes, prices.get(t, 0.0), params, conv,
                            positions.get(t), today))
    return out


def gate_scores(score_map: dict, decisions: list, params: EVParams) -> dict:
    """EV 판단을 주문 로직이 읽는 점수에 반영(새 dict). WAIT → 매수 문턱 아래, SELL → 매도 문턱 아래.
    매수를 새로 만들거나 기존 청산을 막는 방향으로는 절대 바꾸지 않는다."""
    out = dict(score_map)
    for d in decisions:
        s = out.get(d.ticker)
        if s is None:
            continue
        if d.action == "WAIT" and not d.held:
            out[d.ticker] = min(s, params.min_score - 1)
        elif d.action == "SELL" and d.held:
            out[d.ticker] = min(s, params.sell_score - 1)
    return out


_ORDER = {"BUY": 0, "SELL": 1, "HOLD": 2, "WAIT": 3}


def summary(decisions: list, top: int = 15) -> dict:
    cnt = {a: 0 for a in _ORDER}
    for d in decisions:
        cnt[d.action] = cnt.get(d.action, 0) + 1
    rows = sorted(decisions, key=lambda d: (_ORDER.get(d.action, 9), -d.ev))[:top]
    return {"counts": cnt, "rows": [d.to_dict() for d in rows]}


def save(decisions: list, params: EVParams, path: Path | None = None):
    from safe_store import atomic_write_json
    data = {"ts": time.time(), "params": asdict(params), **summary(decisions, top=60)}
    atomic_write_json(path or _ev_file(), data)


def load(path: Path | None = None) -> dict:
    try:
        from safe_store import safe_read_json
        return safe_read_json(path or _ev_file(), {}) or {}
    except Exception:
        return {}


# ── 라이브 진입점 (core.cycle) ──────────────────────────────────────────
def decide_live(cfg: dict, scores_raw: list, prices: dict, positions: dict,
                log: Optional[Callable[[str], None]] = None, deadline: float = 25.0) -> list:
    """일봉은 stock_browser.chart_closes(Nasdaq, 15분 캐시)를 병렬로, 확신도는 AI 스캐너 판단에서."""
    from concurrent.futures import ThreadPoolExecutor, wait as _wait
    import stock_browser as sb
    L = log or (lambda m: None)
    params = EVParams.from_cfg(cfg)
    pairs = [(r["ticker"], float(r["score"])) for r in scores_raw]
    need = [t for t, s in pairs if t in positions or s >= params.min_score]
    closes: dict = {}
    if need:
        ex = ThreadPoolExecutor(max_workers=6)
        futs = {ex.submit(sb.chart_closes, t, "6mo", "1d", 160): t for t in need}
        done, _ = _wait(futs, timeout=deadline)
        for f in done:
            try:
                closes[futs[f]] = f.result() or []
            except Exception:
                closes[futs[f]] = []
        ex.shutdown(wait=False, cancel_futures=True)

    conv_fn = None
    if cfg.get("strategy") == "ai_influencer":
        try:
            from influence import scanner as _sc
            conv_fn = lambda t: getattr(_sc.FULL.get(t), "conviction", None)
        except Exception:
            conv_fn = None

    decisions = decide(pairs, prices, positions, params, lambda t: closes.get(t, []), conv_fn)
    try:
        save(decisions, params)
    except Exception:
        pass
    c = summary(decisions)["counts"]
    waits = [d for d in decisions if d.action == "WAIT"][:3]
    L(f"EV 판단 · 매수 {c['BUY']} · 대기 {c['WAIT']} · 보유 {c['HOLD']} · 매도 {c['SELL']}"
      + (" — 대기: " + ", ".join(f"{d.ticker}({d.reason})" for d in waits) if waits else ""))
    return decisions
