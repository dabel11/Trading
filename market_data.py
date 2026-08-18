"""
실시간 시장 데이터: 공포탐욕지수, 섹터 히트맵, 뉴스, 상승/하락 종목
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


# ── 공포탐욕지수 (VIX + 모멘텀 + 폭으로 자체 계산) ──────────────────────────
def fear_greed_index() -> dict:
    """0=극공포, 50=중립, 100=극탐욕"""
    try:
        tickers = {"vix": "^VIX", "spy": "SPY", "junk": "HYG"}
        raw = yf.download(list(tickers.values()), period="30d", interval="1d",
                          auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]

        scores = []

        # 1. VIX (공포 반전 지표)
        vix = cl["^VIX"].dropna()
        if not vix.empty:
            v = float(vix.iloc[-1])
            vix_score = max(0, min(100, 100 - (v - 10) * 3))
            scores.append(vix_score)

        # 2. SPY 모멘텀 (125일 MA 대비)
        spy = cl["SPY"].dropna()
        if len(spy) >= 25:
            ma25 = spy.rolling(25).mean().iloc[-1]
            cur = spy.iloc[-1]
            mom = (cur - ma25) / ma25 * 100
            mom_score = max(0, min(100, 50 + mom * 4))
            scores.append(mom_score)

        # 3. HYG (정크본드 스프레드: 올라가면 탐욕)
        hyg = cl["HYG"].dropna()
        if len(hyg) >= 20:
            hyg_ma = hyg.rolling(20).mean().iloc[-1]
            hyg_cur = hyg.iloc[-1]
            hyg_score = max(0, min(100, 50 + (hyg_cur - hyg_ma) / hyg_ma * 500))
            scores.append(hyg_score)

        score = round(np.mean(scores)) if scores else 50
        if score >= 75:   label, color = "극도의 탐욕", "#F04452"
        elif score >= 60: label, color = "탐욕",        "#FF9500"
        elif score >= 45: label, color = "중립",        "#8B95A1"
        elif score >= 25: label, color = "공포",        "#2F80ED"
        else:             label, color = "극도의 공포",  "#A855F7"

        return dict(score=score, label=label, color=color,
                    vix=float(vix.iloc[-1]) if not vix.empty else 0)
    except Exception as e:
        return dict(score=50, label="중립", color="#8B95A1", vix=0)


# ── 섹터 성과 ──────────────────────────────────────────────────────────────
SECTOR_ETFS = {
    "XLK":"테크","XLF":"금융","XLE":"에너지","XLV":"헬스","XLY":"소비재",
    "XLI":"산업재","XLP":"필수소비","XLB":"소재","XLU":"유틸리티","XLRE":"리츠",
}

def sector_performance(period: str = "5d") -> list[dict]:
    try:
        raw = yf.download(list(SECTOR_ETFS.keys()), period=period, interval="1d",
                          auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        result = []
        for etf, name in SECTOR_ETFS.items():
            try:
                s = cl[etf].dropna()
                ret = (s.iloc[-1] / s.iloc[0] - 1) * 100
                result.append(dict(etf=etf, name=name, ret=round(float(ret), 2)))
            except: pass
        return sorted(result, key=lambda x: x["ret"], reverse=True)
    except:
        return []


# ── 상승/하락 상위 종목 ────────────────────────────────────────────────────
UNIVERSE = [
    "NVDA","MSFT","AAPL","META","GOOGL","AMZN","TSLA","AMD","AVGO","CRM",
    "NFLX","ORCL","ADBE","SHOP","PLTR","SMCI","ARM","COIN","SNOW","UBER",
    "JPM","GS","BAC","LLY","UNH","XOM","CVX",
]

def top_movers(n: int = 5) -> dict:
    try:
        raw = yf.download(UNIVERSE, period="2d", interval="1d",
                          auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        changes = {}
        for t in UNIVERSE:
            try:
                s = cl[t].dropna()
                if len(s) >= 2:
                    changes[t] = (s.iloc[-1] / s.iloc[-2] - 1) * 100
            except: pass
        sorted_c = sorted(changes.items(), key=lambda x: x[1], reverse=True)
        gainers = [dict(ticker=t, chg=round(c,2)) for t,c in sorted_c[:n]]
        losers  = [dict(ticker=t, chg=round(c,2)) for t,c in sorted_c[-n:]]
        return dict(gainers=gainers, losers=losers)
    except:
        return dict(gainers=[], losers=[])


# ── Finnhub 뉴스 헤드라인 ─────────────────────────────────────────────────
def market_news(limit: int = 6) -> list[dict]:
    try:
        import finnhub, os
        key = os.environ.get("FINNHUB_API_KEY","")
        if not key or "your_" in key:
            return []
        client = finnhub.Client(api_key=key)
        news = client.general_news("general", min_id=0)
        return [dict(
            headline=n.get("headline","")[:80],
            source=n.get("source",""),
            url=n.get("url",""),
            sentiment=n.get("sentiment",""),
        ) for n in news[:limit]]
    except:
        return []


# ── 주요 종목 인트라데이 (쇼케이스용) ─────────────────────────────────────
SHOWCASE = ["NVDA","AAPL","MSFT","TSLA","META","GOOGL","AMZN","AMD"]

def showcase_data() -> list[dict]:
    try:
        raw = yf.download(SHOWCASE, period="1d", interval="5m",
                          auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        result = []
        for t in SHOWCASE:
            try:
                s = cl[t].dropna()
                if len(s) < 2: continue
                open_ = float(s.iloc[0])
                cur   = float(s.iloc[-1])
                chg   = (cur - open_) / open_ * 100
                result.append(dict(ticker=t, open=open_, current=cur,
                                   chg=round(chg,2), series=s.tolist()[-30:],
                                   times=s.index.tolist()[-30:]))
            except: pass
        return result
    except:
        return []
