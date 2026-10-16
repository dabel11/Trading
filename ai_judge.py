"""AI 판단 레이어 — 공신력 있는 인플루언서/권위 소스의 '증거'를 모아
매매 의견(BUY/SELL/HOLD + 0~100 확신 점수 + 근거)을 실시간으로 내린다.

이 모듈은 두 가지 판단기(judge)를 제공한다:

  1) LLM 판단기 (`_judge_llm`)  — OPENAI/ANTHROPIC/로컬 호환 키가 있으면 사용.
       여러 종목의 증거를 한 번에 요약해 LLM에게 JSON 배열 판단을 받는다.
  2) 규칙 판단기 (`_judge_rule`) — 키가 없을 때의 폴백. 신뢰도·확신도로 가중한
       stance 평균을 0~100 점수로 환산. LLM과 '동일한 출력 스키마'를 낸다.

핵심 설계: 판단 결과의 `score`(0~100)가 기존 자동매매 파이프라인
(`portfolio.generate_orders` 의 MIN_SCORE_TO_BUY 등)에 그대로 꽂힌다.
→ 전략 스코어러를 이 판단기로 갈아끼우면 주문·리스크·실행 로직은 무수정.

LLM 키가 없어도 규칙 판단기로 끝까지 동작하고, 키를 넣으면 자동 업그레이드된다
(코드베이스 전체 철학: "키 없어도 동작, 있으면 업그레이드").
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────
# 증거(Evidence)와 판단(Verdict) 스키마
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """한 소스가 한 종목에 대해 내놓은 의견 한 조각."""
    source_key: str          # "analyst" | "insider" | "news" | "social" | "crowd"
    source_label: str        # 사람이 읽는 이름
    category: str            # "권위" | "내부자" | "미디어" | "SNS" | "군중"
    credibility: float       # 0~1, 소스의 '공신력' 정적 가중치 (로스터에서)
    stance: float            # -1(강한 매도) ~ +1(강한 매수)
    confidence: float        # 0~1, 이 소스가 이번 판단에 얼마나 확신하는지(표본·신선도)
    detail: str = ""         # 근거 한 줄
    raw: dict = field(default_factory=dict)

    @property
    def weight(self) -> float:
        return max(0.0, self.credibility) * max(0.0, min(1.0, self.confidence))


@dataclass
class Verdict:
    """한 종목에 대한 최종 AI 판단."""
    ticker: str
    action: str              # "BUY" | "SELL" | "HOLD"
    score: float             # 0~100 (매수 확신). 50=중립
    conviction: float        # 0~1 (근거의 강도·일치도)
    rationale: str           # 한국어 근거 요약
    by: str                  # "llm" | "rule"
    evidence: list = field(default_factory=list)   # list[Evidence]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = [asdict(e) for e in self.evidence]
        return d


# ─────────────────────────────────────────────────────────────────────────
# 점수 환산 (규칙 판단기의 핵심 + LLM 결과 클램프에도 재사용)
# ─────────────────────────────────────────────────────────────────────────

def aggregate(evidence: list) -> tuple[float, float]:
    """증거들을 신뢰도·확신도로 가중 평균 → (weighted_stance[-1~1], total_weight)."""
    num = den = 0.0
    for e in evidence:
        w = e.weight
        num += w * max(-1.0, min(1.0, e.stance))
        den += w
    if den <= 0:
        return 0.0, 0.0
    return num / den, den


def stance_to_score(weighted_stance: float) -> float:
    """-1~+1 stance → 0~100 점수 (50=중립)."""
    return round(max(0.0, min(100.0, 50.0 + 50.0 * weighted_stance)), 1)


def _action(score: float, buy_th: float, sell_th: float) -> str:
    if score >= buy_th:
        return "BUY"
    if score <= sell_th:
        return "SELL"
    return "HOLD"


# ─────────────────────────────────────────────────────────────────────────
# 규칙 판단기 (LLM 폴백)
# ─────────────────────────────────────────────────────────────────────────

_WEIGHT_NORM = 1.6   # total_weight 가 이 값이면 '증거 충분' = 확신 상한 근접


def _judge_rule(ticker: str, evidence: list, buy_th: float, sell_th: float) -> Verdict:
    ws, tw = aggregate(evidence)
    score = stance_to_score(ws)
    coverage = min(1.0, tw / _WEIGHT_NORM)
    conviction = round(coverage * (0.45 + 0.55 * abs(ws)), 2)
    action = _action(score, buy_th, sell_th)

    # 근거 요약: 가중치 큰 소스 상위 3개
    top = sorted(evidence, key=lambda e: e.weight, reverse=True)[:3]
    bits = []
    for e in top:
        arrow = "▲" if e.stance > 0.1 else ("▼" if e.stance < -0.1 else "─")
        bits.append(f"{e.source_label} {arrow}{e.detail}")
    verb = {"BUY": "매수 우위", "SELL": "매도 우위", "HOLD": "관망"}[action]
    rationale = f"{verb}(확신 {int(conviction*100)}%) · " + " · ".join(bits) if bits \
        else f"{verb} · 신뢰할 근거 부족"
    return Verdict(ticker, action, score, conviction, rationale, "rule", list(evidence))


# ─────────────────────────────────────────────────────────────────────────
# LLM 판단기 (키 있을 때)
# ─────────────────────────────────────────────────────────────────────────

def _llm_config() -> Optional[dict]:
    """환경에서 LLM 설정을 읽는다. 키 없으면 None (→ 규칙 판단기)."""
    prov = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    anth = os.environ.get("ANTHROPIC_API_KEY", "")
    oai = os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("LLM_BASE_URL", "").strip()

    def _real(k: str) -> bool:
        return bool(k) and "your_" not in k and len(k) > 12

    if prov == "anthropic" or (not prov and _real(anth)):
        if not _real(anth):
            return None
        return {"provider": "anthropic", "key": anth,
                "model": os.environ.get("LLM_MODEL", "claude-opus-4-8"),
                "url": base or "https://api.anthropic.com/v1/messages"}
    if prov in ("openai", "local") or (not prov and _real(oai)):
        if prov != "local" and not _real(oai):
            return None
        return {"provider": "openai", "key": oai or "sk-local",
                "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
                "url": base or "https://api.openai.com/v1/chat/completions"}
    return None


_SYS_PROMPT = (
    "You are a disciplined trading analyst for an automated stock system. "
    "You are given, per ticker, evidence from CREDIBLE market voices: Wall-Street "
    "analyst consensus, corporate insiders, financial media, and social influencers. "
    "Decide BUY / SELL / HOLD and a 0-100 conviction score (50=neutral, >=buy_th=buy, "
    "<=sell_th=sell). Weigh higher-credibility sources more. Be decisive but risk-aware. "
    "Reply ONLY with a compact JSON array; one object per ticker: "
    '{"ticker","action","score","conviction","rationale"}. '
    "rationale must be ONE short Korean sentence citing the key sources."
)


def _llm_payload(items: list, buy_th: float, sell_th: float) -> str:
    lines = []
    for ticker, evidence, ctx in items:
        ev = [{"source": e.source_label, "credibility": round(e.credibility, 2),
               "stance": round(e.stance, 2), "confidence": round(e.confidence, 2),
               "note": e.detail} for e in evidence]
        lines.append({"ticker": ticker, "held": ctx.get("held", False),
                      "regime": ctx.get("regime", "neutral"), "evidence": ev})
    return json.dumps({"buy_th": buy_th, "sell_th": sell_th, "stocks": lines},
                      ensure_ascii=False)


def _http_json(url: str, headers: dict, body: dict, timeout: float = 30.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _judge_llm(items: list, cfg: dict, buy_th: float, sell_th: float) -> Optional[list]:
    """items: list[(ticker, [Evidence], ctx)] → list[Verdict] 또는 None(실패)."""
    user = _llm_payload(items, buy_th, sell_th)
    try:
        if cfg["provider"] == "anthropic":
            out = _http_json(cfg["url"], {
                "x-api-key": cfg["key"], "anthropic-version": "2023-06-01",
                "content-type": "application/json"}, {
                "model": cfg["model"], "max_tokens": 2000,
                "system": _SYS_PROMPT,
                "messages": [{"role": "user", "content": user}]})
            text = "".join(b.get("text", "") for b in out.get("content", []))
        else:  # openai 호환 (로컬 포함)
            out = _http_json(cfg["url"], {
                "Authorization": f"Bearer {cfg['key']}",
                "content-type": "application/json"}, {
                "model": cfg["model"], "temperature": 0.2,
                "messages": [{"role": "system", "content": _SYS_PROMPT},
                             {"role": "user", "content": user}]})
            text = out["choices"][0]["message"]["content"]
        arr = _extract_json_array(text)
        if not arr:
            return None
        by_ticker = {a.get("ticker"): a for a in arr if isinstance(a, dict)}
        verdicts = []
        for ticker, evidence, _ctx in items:
            a = by_ticker.get(ticker)
            if not a:
                verdicts.append(_judge_rule(ticker, evidence, buy_th, sell_th))
                continue
            score = float(a.get("score", 50))
            score = max(0.0, min(100.0, score))
            act = str(a.get("action", _action(score, buy_th, sell_th))).upper()
            if act not in ("BUY", "SELL", "HOLD"):
                act = _action(score, buy_th, sell_th)
            conv = float(a.get("conviction", abs(score - 50) / 50))
            verdicts.append(Verdict(
                ticker, act, round(score, 1), round(max(0, min(1, conv)), 2),
                str(a.get("rationale", ""))[:240], "llm", list(evidence)))
        return verdicts
    except Exception:
        return None


def _extract_json_array(text: str) -> Optional[list]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j < 0 or j < i:
        return None
    try:
        v = json.loads(text[i:j + 1])
        return v if isinstance(v, list) else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────────

def judge_batch(items: list, buy_th: float = 60.0, sell_th: float = 35.0,
                use_llm: bool = True) -> list:
    """items: list[(ticker, [Evidence], ctx)] → list[Verdict].

    LLM 키가 있으면 한 번의 호출로 전 종목을 판단, 실패/무키면 규칙 판단기.
    """
    if use_llm:
        cfg = _llm_config()
        if cfg:
            v = _judge_llm(items, cfg, buy_th, sell_th)
            if v is not None:
                return v
    return [_judge_rule(t, ev, buy_th, sell_th) for t, ev, _ in items]


def llm_active() -> bool:
    """LLM 판단기가 실제로 활성(키 유효)인지."""
    return _llm_config() is not None
