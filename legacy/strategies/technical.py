"""
차트 기반(기술적) 전략 모음.
- 골든크로스 (이동평균 정배열)
- 돌파 (저항선 돌파 + 거래량)
- MACD (추세 전환)
- 볼린저 스퀴즈 (변동성 수축 후 돌파)
"""
import numpy as np
import yfinance as yf
from .base import BaseStrategy, StrategyInfo


def _hist(ticker: str, period: str = "1y"):
    df = yf.download(ticker, period=period, interval="1d",
                     auto_adjust=True, progress=False)
    return df if df is not None and len(df) >= 60 else None


# ──────────────────────────────────────────────────────────────────────────────
class GoldenCrossStrategy(BaseStrategy):
    info = StrategyInfo(
        name="golden_cross", display_name="골든크로스",
        description="단기·중기·장기 이동평균이 정배열(20>60>120)된 강한 상승 추세 종목.",
        best_for="중장기 추세 추종",
    )
    def score(self, ticker: str) -> float:
        try:
            df = _hist(ticker)
            if df is None: return 0.0
            c = df["Close"].squeeze()
            ma20, ma60, ma120 = c.rolling(20).mean(), c.rolling(60).mean(), c.rolling(120).mean()
            cur = c.iloc[-1]
            m20, m60, m120 = ma20.iloc[-1], ma60.iloc[-1], ma120.iloc[-1]
            pts = 0.0
            # 정배열 (0~50)
            if cur > m20 > m60 > m120: pts += 50
            elif cur > m20 > m60:      pts += 35
            elif cur > m20:            pts += 18
            # 골든크로스 신규 발생 보너스 (0~25)
            cross = (ma20.iloc[-5] <= ma60.iloc[-5]) and (ma20.iloc[-1] > ma60.iloc[-1])
            if cross: pts += 25
            # 이격도 적정 (과열 아님) (0~25)
            gap = (cur - m20) / m20 * 100
            if 0 < gap < 8: pts += 25
            elif gap < 15:  pts += 12
            return round(min(100, pts), 1)
        except: return 0.0


# ──────────────────────────────────────────────────────────────────────────────
class BreakoutStrategy(BaseStrategy):
    info = StrategyInfo(
        name="breakout", display_name="돌파매매",
        description="최근 고점(저항선)을 거래량 동반 돌파하는 종목. 신고가 추격.",
        best_for="강세장 추격매수",
    )
    def score(self, ticker: str) -> float:
        try:
            df = _hist(ticker, "6mo")
            if df is None: return 0.0
            c = df["Close"].squeeze(); v = df["Volume"].squeeze()
            cur = c.iloc[-1]
            hi20 = c.iloc[-21:-1].max()    # 직전 20일 고점
            hi60 = c.iloc[-61:-1].max()
            pts = 0.0
            # 돌파 강도 (0~45)
            if cur > hi60:    pts += 45
            elif cur > hi20:  pts += 30
            elif cur > hi20 * 0.98: pts += 12
            # 거래량 급증 (0~35)
            vr = v.iloc[-3:].mean() / v.iloc[-20:].mean() if v.iloc[-20:].mean() > 0 else 1
            pts += min(35, max(0, (vr - 1) * 35))
            # 직전 변동성 수축 (눌림 후 돌파) (0~20)
            recent_range = (c.iloc[-10:].max() - c.iloc[-10:].min()) / c.iloc[-10:].mean()
            if recent_range < 0.05: pts += 20
            elif recent_range < 0.10: pts += 10
            return round(min(100, pts), 1)
        except: return 0.0


# ──────────────────────────────────────────────────────────────────────────────
class MACDStrategy(BaseStrategy):
    info = StrategyInfo(
        name="macd", display_name="MACD 추세",
        description="MACD 골든크로스 + 0선 상향 돌파로 추세 전환 초입을 포착.",
        best_for="추세 전환 초기 진입",
    )
    def score(self, ticker: str) -> float:
        try:
            df = _hist(ticker)
            if df is None: return 0.0
            c = df["Close"].squeeze()
            ema12 = c.ewm(span=12).mean()
            ema26 = c.ewm(span=26).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9).mean()
            hist = macd - signal
            pts = 0.0
            # MACD > Signal (0~35)
            if macd.iloc[-1] > signal.iloc[-1]: pts += 35
            # 골든크로스 신규 (0~30)
            if hist.iloc[-2] <= 0 and hist.iloc[-1] > 0: pts += 30
            # MACD 0선 위 (상승추세) (0~20)
            if macd.iloc[-1] > 0: pts += 20
            # 히스토그램 확대 (모멘텀 가속) (0~15)
            if hist.iloc[-1] > hist.iloc[-2] > hist.iloc[-3]: pts += 15
            return round(min(100, pts), 1)
        except: return 0.0


# ──────────────────────────────────────────────────────────────────────────────
class BollingerSqueezeStrategy(BaseStrategy):
    info = StrategyInfo(
        name="bollinger", display_name="볼린저 스퀴즈",
        description="밴드폭이 좁아진 변동성 수축 구간에서 상단 돌파하는 종목.",
        best_for="변동성 확장 초입",
    )
    def score(self, ticker: str) -> float:
        try:
            df = _hist(ticker, "6mo")
            if df is None: return 0.0
            c = df["Close"].squeeze()
            ma = c.rolling(20).mean()
            sd = c.rolling(20).std()
            upper = ma + 2*sd; lower = ma - 2*sd
            width = (upper - lower) / ma
            cur = c.iloc[-1]
            pts = 0.0
            # 밴드 수축 (현재 밴드폭이 최근 6개월 하위 25%) (0~40)
            w_now = width.iloc[-1]
            w_pct = (width.iloc[-120:] < w_now).mean() if len(width) >= 120 else 0.5
            if w_pct < 0.25: pts += 40
            elif w_pct < 0.5: pts += 20
            # 상단 돌파 (0~40)
            bb_pos = (cur - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1]) if (upper.iloc[-1]-lower.iloc[-1])>0 else 0.5
            if bb_pos > 0.95: pts += 40
            elif bb_pos > 0.8: pts += 22
            # 상승 방향 (0~20)
            if cur > ma.iloc[-1]: pts += 20
            return round(min(100, pts), 1)
        except: return 0.0
