"""증거 수집기(providers) — 종목별로 공신력 소스에서 의견 한 조각(Evidence)을 뽑는다.

각 provider(ticker) → Evidence | None.
  None = 이 소스가 지금 이 종목에 대해 쓸 만한 데이터를 못 냄(무키·무응답·표본부족).
모두 '무료/보유 키'만 사용하고, 실패하면 조용히 None 을 반환해 다른 소스로 판단이 이어진다.

credibility 는 로스터(정적)에서 가져오고, confidence(표본·신선도)는 소스가 계산한다.
"""
from __future__ import annotations

import time
import urllib.request
import json
from datetime import datetime, timedelta

from ai_judge import Evidence
from influence.roster import ROSTER

# 소스별 호출 캐시 (TTL) — Finnhub 무료 60req/min 보호. 느리게 변하는 신호는 길게.
_CACHE: dict = {}
_TTL = {"analyst": 6 * 3600, "insider": 12 * 3600, "news": 3600, "social": 1800}

_fh_client = None


def _finnhub():
    global _fh_client
    if _fh_client is None:
        import finnhub
        from config import FINNHUB_API_KEY
        _fh_client = finnhub.Client(api_key=FINNHUB_API_KEY)
    return _fh_client


def _finnhub_ready() -> bool:
    try:
        from config import FINNHUB_API_KEY as fk
        return bool(fk) and "your_" not in (fk or "")
    except Exception:
        return False


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# ─────────────────────────────────────────────────────────────────────────
# 1) 월가 애널리스트 컨센서스 (Finnhub recommendation) — 공신력 최상위
# ─────────────────────────────────────────────────────────────────────────

def analyst(ticker: str) -> Evidence | None:
    if not _finnhub_ready():
        return None
    meta = ROSTER["analyst"]

    def _fetch():
        try:
            data = _finnhub().recommendation_trends(ticker)
            time.sleep(0.04)
            return data or []
        except Exception:
            return []

    rows = _cached(f"analyst:{ticker}", _TTL["analyst"], _fetch)
    if not rows:
        return None
    r = rows[0]  # 최신 기간
    sb, b, h, s, ss = (r.get("strongBuy", 0), r.get("buy", 0), r.get("hold", 0),
                       r.get("sell", 0), r.get("strongSell", 0))
    total = sb + b + h + s + ss
    if total <= 0:
        return None
    stance = (sb * 1.0 + b * 0.5 + h * 0.0 + s * -0.5 + ss * -1.0) / total
    conf = min(1.0, total / 25.0)
    bull = sb + b
    detail = f"{total}곳 중 매수 {bull}·보유 {h}·매도 {s + ss}"
    return Evidence("analyst", meta["label"], meta["category"], meta["credibility"],
                    round(stance, 3), round(conf, 3), detail,
                    {"strongBuy": sb, "buy": b, "hold": h, "sell": s, "strongSell": ss,
                     "period": r.get("period")})


# ─────────────────────────────────────────────────────────────────────────
# 2) 기업 내부자 매매 심리 (Finnhub insider-sentiment, MSPR -100~100)
# ─────────────────────────────────────────────────────────────────────────

def insider(ticker: str) -> Evidence | None:
    if not _finnhub_ready():
        return None
    meta = ROSTER["insider"]

    def _fetch():
        try:
            end = datetime.now()
            start = end - timedelta(days=120)
            d = _finnhub().stock_insider_sentiment(
                ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            time.sleep(0.04)
            return (d or {}).get("data") or []
        except Exception:
            return []

    data = _cached(f"insider:{ticker}", _TTL["insider"], _fetch)
    if not data:
        return None
    data = sorted(data, key=lambda x: (x.get("year", 0), x.get("month", 0)))
    recent = data[-3:]
    mspr = sum(x.get("mspr", 0) for x in recent) / len(recent)   # -100~100
    stance = max(-1.0, min(1.0, mspr / 100.0))
    conf = min(1.0, len(data) / 4.0) * (0.4 + 0.6 * min(1.0, abs(stance) * 2))
    word = "순매수" if stance > 0.05 else ("순매도" if stance < -0.05 else "중립")
    detail = f"최근 내부자 {word}(MSPR {mspr:+.0f})"
    return Evidence("insider", meta["label"], meta["category"], meta["credibility"],
                    round(stance, 3), round(conf, 3), detail, {"mspr": round(mspr, 1)})


# ─────────────────────────────────────────────────────────────────────────
# 3) 금융 미디어·뉴스 (Finnhub company_news + 긍/부정 어휘) — 기존 로직 재사용
# ─────────────────────────────────────────────────────────────────────────

def news(ticker: str) -> Evidence | None:
    if not _finnhub_ready():
        return None
    meta = ROSTER["news"]

    def _fetch():
        try:
            from signals.sentiment import _lexicon_score
            end = datetime.now()
            start = end - timedelta(days=7)
            arr = _finnhub().company_news(
                ticker, _from=start.strftime("%Y-%m-%d"), to=end.strftime("%Y-%m-%d"))
            time.sleep(0.04)
            if not arr:
                return None
            heads = [f"{n.get('headline', '')} {n.get('summary', '')}" for n in arr[:40]]
            lex = _lexicon_score(heads)   # 0~100 또는 None
            return (lex, len(arr)) if lex is not None else None
        except Exception:
            return None

    res = _cached(f"news:{ticker}", _TTL["news"], _fetch)
    if not res:
        return None
    lex, n = res
    stance = max(-1.0, min(1.0, (lex - 50.0) / 50.0))
    conf = min(1.0, n / 20.0) * (0.5 + 0.5 * min(1.0, abs(stance) * 1.5))
    tone = "긍정" if stance > 0.08 else ("부정" if stance < -0.08 else "혼조")
    detail = f"최근 {n}건 헤드라인 {tone}"
    return Evidence("news", meta["label"], meta["category"], meta["credibility"],
                    round(stance, 3), round(conf, 3), detail, {"lexicon": lex, "n": n})


# ─────────────────────────────────────────────────────────────────────────
# 4) SNS 인플루언서 (X·StockTwits) — 전용 키 있을 때만 활성
# ─────────────────────────────────────────────────────────────────────────

def _http(url: str, headers: dict, timeout: float = 8.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def social(ticker: str) -> Evidence | None:
    import os
    meta = ROSTER["social"]
    st_token = os.environ.get("STOCKTWITS_TOKEN", "")
    x_token = os.environ.get("X_BEARER_TOKEN", "")

    def _fetch():
        # StockTwits (인증 토큰) 우선
        if st_token and "your_" not in st_token:
            d = _http(
                f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
                f"?access_token={st_token}",
                {"User-Agent": "Mozilla/5.0"})
            msgs = (d or {}).get("messages") or []
            if msgs:
                bull = bear = 0
                for m in msgs:
                    b = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                    if b == "Bullish":
                        bull += 1
                    elif b == "Bearish":
                        bear += 1
                if bull + bear > 0:
                    return ("stocktwits", bull, bear, len(msgs))
        # X(트위터) v2 — 최근 cashtag 트윗 (간이 어휘 채점)
        if x_token and "your_" not in x_token:
            from signals.sentiment import _lexicon_score
            d = _http(
                "https://api.twitter.com/2/tweets/search/recent"
                f"?query=%24{ticker}%20-is%3Aretweet%20lang%3Aen&max_results=50",
                {"Authorization": f"Bearer {x_token}"})
            tw = (d or {}).get("data") or []
            if tw:
                lex = _lexicon_score([t.get("text", "") for t in tw])
                if lex is not None:
                    return ("x", lex, None, len(tw))
        return None

    res = _cached(f"social:{ticker}", _TTL["social"], _fetch)
    if not res:
        return None
    if res[0] == "stocktwits":
        _, bull, bear, n = res
        stance = (bull - bear) / max(1, bull + bear)
        conf = min(1.0, (bull + bear) / 20.0)
        detail = f"StockTwits 강세 {bull}·약세 {bear}"
        raw = {"bull": bull, "bear": bear, "n": n}
    else:
        _, lex, _n2, n = res
        stance = max(-1.0, min(1.0, (lex - 50.0) / 50.0))
        conf = min(1.0, n / 30.0)
        detail = f"X 트윗 {n}건 심리 {int(lex)}/100"
        raw = {"lexicon": lex, "n": n}
    return Evidence("social", meta["label"], meta["category"], meta["credibility"],
                    round(stance, 3), round(conf, 3), detail, raw)


# ─────────────────────────────────────────────────────────────────────────
# 5) 시장 군중 모멘텀 (가격·거래량 프록시) — 항상 가용, 낮은 공신력
# ─────────────────────────────────────────────────────────────────────────

def crowd(ticker: str, price_hint: float | None = None) -> Evidence | None:
    meta = ROSTER["crowd"]

    def _fetch():
        try:
            from signals.sentiment import _price_proxy
            return _price_proxy(ticker)   # 0~100
        except Exception:
            return None

    val = _cached(f"crowd:{ticker}", 1800, _fetch)
    if val is None:
        return None
    stance = max(-1.0, min(1.0, (val - 50.0) / 50.0))
    conf = 0.35 + 0.35 * min(1.0, abs(stance))   # 군중은 확신도 자체를 낮게
    detail = f"가격·거래량 모멘텀 {int(val)}/100"
    return Evidence("crowd", meta["label"], meta["category"], meta["credibility"],
                    round(stance, 3), round(conf, 3), detail, {"proxy": val})


# provider 디스패치 — 순서 = 공신력 내림차순
ALL = [analyst, insider, news, social, crowd]


def gather(ticker: str) -> list:
    """한 종목에 대한 모든 소스의 Evidence 수집 (None 제외)."""
    out = []
    for fn in ALL:
        try:
            e = fn(ticker)
        except Exception:
            e = None
        if e is not None:
            out.append(e)
    return out
