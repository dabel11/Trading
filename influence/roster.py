"""공신력 있는 '시장 목소리' 로스터 — 단일 소스 오브 트루스.

이 시스템은 특정 전략을 고르는 대신, 아래 '공신력 있는 유명 인플루언서/권위
소스'들의 실시간 의견을 모아 AI가 매매를 판단한다. 각 소스는 정적 '공신력
(credibility)' 가중치를 갖는다 — 월가 애널리스트 컨센서스가 가장 높고, 기업
내부자·금융 미디어·SNS 인플루언서·시장 군중 순으로 낮아진다.

SNS(X·StockTwits) 소스는 전용 키가 있어야 활성화된다(키 없으면 자동 비활성).
키를 넣으면 로스터에 '살아있는' 소스로 합류한다.
"""
from __future__ import annotations
import os


def _has(*env_keys: str) -> bool:
    for k in env_keys:
        v = os.environ.get(k, "")
        if v and "your_" not in v and len(v) > 8:
            return True
    return False


# source_key → 메타. credibility: 0~1 (공신력). active 는 런타임(키 유무)으로 결정.
ROSTER = {
    "analyst": dict(
        label="월가 애널리스트 컨센서스",
        who="Goldman·Morgan Stanley·JPMorgan 등 추천 등급 집계",
        category="권위", credibility=0.92, emoji="🏦",
        note="30여 개 투자은행의 강력매수~강력매도 등급을 합산한 공신력 최상위 신호."),
    "insider": dict(
        label="기업 내부자 매매",
        who="등기임원·대주주의 실제 자사주 매수/매도 (SEC Form 4)",
        category="내부자", credibility=0.78, emoji="🕵️",
        note="내부 정보에 가장 가까운 당사자들의 돈이 실제로 어디로 움직이는가."),
    "news": dict(
        label="금융 미디어·뉴스",
        who="Bloomberg·Reuters·CNBC 등 헤드라인 긍/부정",
        category="미디어", credibility=0.55, emoji="📰",
        note="시장 서사를 만드는 주요 금융 매체의 논조."),
    "social": dict(
        label="SNS 인플루언서",
        who="X(트위터)·StockTwits 유명 트레이더·커뮤니티 심리",
        category="SNS", credibility=0.60, emoji="📣",
        note="전용 키(X_BEARER_TOKEN / STOCKTWITS_TOKEN) 연결 시 활성화."),
    "crowd": dict(
        label="시장 군중 모멘텀",
        who="가격·거래량에 드러난 집단 심리 프록시",
        category="군중", credibility=0.32, emoji="🌊",
        note="다른 근거가 없을 때만 낮은 비중으로 참고하는 최후 신호."),
}


def is_active(source_key: str) -> bool:
    """해당 소스가 현재 환경에서 실데이터를 낼 수 있는지."""
    if source_key == "social":
        return _has("X_BEARER_TOKEN", "STOCKTWITS_TOKEN")
    if source_key in ("analyst", "insider"):
        from config import FINNHUB_API_KEY as fk
        return bool(fk) and "your_" not in (fk or "")
    return True   # news(무료 폴백 포함), crowd 은 항상 가용


def active_sources() -> list[str]:
    return [k for k in ROSTER if is_active(k)]


def summary() -> list[dict]:
    """UI/텔레그램 표시용 — 로스터 + 활성 여부."""
    out = []
    for k, v in ROSTER.items():
        out.append({**v, "key": k, "active": is_active(k)})
    # 공신력 높은 순
    out.sort(key=lambda d: -d["credibility"])
    return out
