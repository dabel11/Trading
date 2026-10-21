"""
Backtester: 과거 데이터로 전략 성과를 시뮬레이션합니다.

방식:
  - yfinance로 과거 OHLCV + 펀더멘털 스냅샷 로드
  - 매 리밸런싱 주기마다 시그널을 재계산 (섹터 로테이션 + 기술적 시그널)
  - 포지션 진입/청산 규칙 동일하게 적용
  - 최종 결과: 수익률, MDD, 샤프, 승률 등 리포트

주의: 백테스트에서는 Finnhub 실시간 뉴스/어닝 API 대신
      과거 데이터로 proxy 지표를 사용합니다.
"""

import json
import math
import os
import time
from pathlib import Path
from statistics import NormalDist

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import date
from dataclasses import dataclass

import portfolio
from portfolio import PortfolioManager
from scorer import StockScore
from config import (
    TRADEABLE_UNIVERSE, SECTOR_ETFS,
    MAX_POSITIONS, MAX_POSITION_PCT,
    MIN_SCORE_TO_BUY, SELL_SCORE_THRESHOLD,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    HOLD_DAYS_STRONG, HOLD_DAYS_MEDIUM,
    VOLUME_LOOKBACK_DAYS,
)

REBALANCE_DAYS = 7   # 매 N일마다 포트폴리오 재검토


# ──────────────────────────────────────────────────────────────────────────────
# 시그널 (백테스트용 — API 없이 과거 데이터만 사용)
# ──────────────────────────────────────────────────────────────────────────────

def _inst_score_bt(close: pd.Series, high: pd.Series, low: pd.Series,
                   volume: pd.Series, idx: int) -> float:
    """기관 매수 시그널 (과거 데이터 슬라이스 기준)."""
    if idx < VOLUME_LOOKBACK_DAYS + 5:
        return 0.0
    window = slice(idx - VOLUME_LOOKBACK_DAYS - 5, idx)
    recent = slice(idx - 5, idx)

    avg_vol = volume.iloc[window].mean()
    vol_ratio = volume.iloc[recent].mean() / avg_vol if avg_vol > 0 else 1.0
    vol_score = min(40, max(0, (vol_ratio - 1.0) * 25))

    rng = (high - low).iloc[recent]
    cth = ((close - low) / rng.where(rng > 0, other=np.nan)).iloc[recent].mean()
    cth_score = float(cth * 30) if not np.isnan(cth) else 15.0

    obv = (np.sign(close.diff()) * volume).fillna(0).cumsum()
    obv_recent = obv.iloc[idx - 5: idx].mean()
    obv_prev   = obv.iloc[idx - 20: idx - 5].mean()
    obv_change = (obv_recent - obv_prev) / (abs(obv_prev) + 1)
    obv_score  = min(30, max(0, obv_change * 60))

    return min(100.0, vol_score + cth_score + obv_score)


def _sector_scores_bt(etf_data: dict[str, pd.DataFrame], idx: int,
                       lookback: int = 30) -> dict[str, float]:
    """섹터 ETF 모멘텀 순위 (백테스트용)."""
    if idx < lookback:
        return {}
    returns = {}
    for etf, df in etf_data.items():
        if idx >= len(df):
            continue
        start_price = df["Close"].iloc[max(0, idx - lookback)]
        end_price   = df["Close"].iloc[idx]
        if start_price > 0:
            returns[etf] = (end_price - start_price) / start_price

    if not returns:
        return {}
    sorted_etfs = sorted(returns, key=returns.get)
    n = len(sorted_etfs)
    return {etf: round((rank / max(n - 1, 1)) * 100, 1)
            for rank, etf in enumerate(sorted_etfs)}


TICKER_SECTOR_MAP = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK","AVGO":"XLK",
    "CRM":"XLK","ORCL":"XLK","ADBE":"XLK","GOOGL":"XLK","META":"XLK",
    "NFLX":"XLK","SHOP":"XLK",
    "JPM":"XLF","GS":"XLF","MS":"XLF","BAC":"XLF",
    "XOM":"XLE","CVX":"XLE",
    "LLY":"XLV","UNH":"XLV","JNJ":"XLV",
    "TSLA":"XLY","AMZN":"XLY",
}

SIGNAL_W = {"institutional": 0.35, "sector": 0.35, "momentum": 0.30}


def _composite_score_bt(ticker: str, stock_data: dict, etf_data: dict,
                         idx: int) -> float:
    df = stock_data.get(ticker)
    if df is None or idx < 30:
        return 0.0

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    inst   = _inst_score_bt(close, high, low, volume, idx)

    sector_ranks = _sector_scores_bt(etf_data, idx)
    etf = TICKER_SECTOR_MAP.get(ticker)
    sect = sector_ranks.get(etf, 50.0) if etf else 50.0

    # 모멘텀: 1개월 + 3개월 리턴 조합
    if idx >= 63:
        ret_1m  = (close.iloc[idx] / close.iloc[idx - 21] - 1) * 100
        ret_3m  = (close.iloc[idx] / close.iloc[idx - 63] - 1) * 100
        momentum = min(100, max(0, 50 + ret_1m * 1.5 + ret_3m * 0.5))
    else:
        momentum = 50.0

    total = (SIGNAL_W["institutional"] * inst
             + SIGNAL_W["sector"] * sect
             + SIGNAL_W["momentum"] * momentum)
    return round(total, 1)


def _strategy_score_bt(strategy, ticker, stock_data, etf_data, idx) -> float:
    """선택된 전략에 따라 과거 데이터 기반 스코어 산출.
    strategy: 전략 이름(str) 또는 .info.name 속성을 가진 인스턴스."""
    df = stock_data.get(ticker)
    if df is None or idx < 30:
        return 0.0
    if isinstance(strategy, str):
        name = strategy
    else:
        name = getattr(getattr(strategy, "info", None), "name", "composite") if strategy else "composite"
    close = df["Close"]; high = df["High"]; low = df["Low"]; volume = df["Volume"]

    if name == "momentum":
        # 강한 추세: 1·3·6개월 수익률 + 신고가 근접
        if idx < 126: return 0.0
        r1 = (close.iloc[idx]/close.iloc[idx-21]-1)*100
        r3 = (close.iloc[idx]/close.iloc[idx-63]-1)*100
        r6 = (close.iloc[idx]/close.iloc[idx-126]-1)*100
        hi = close.iloc[max(0,idx-252):idx+1].max()
        near = close.iloc[idx]/hi*100  # 신고가 근접도
        return round(min(100, max(0, r1*0.3 + r3*0.4 + r6*0.3 + (near-90)*2)), 1)

    if name == "mean_reversion":
        # 과매도 반등: RSI 낮을수록 높은 점수
        if idx < 20: return 50.0
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean().iloc[idx]
        loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[idx]
        rs = gain/loss if loss else 1
        rsi = 100 - 100/(1+rs)
        ma20 = close.iloc[max(0,idx-20):idx+1].mean()
        dev = (close.iloc[idx]-ma20)/ma20*100
        return round(min(100, max(0, (50-rsi)*1.5 + max(0,-dev)*3)), 1)

    if name == "sector_rotation":
        ranks = _sector_scores_bt(etf_data, idx)
        etf = TICKER_SECTOR_MAP.get(ticker)
        sect = ranks.get(etf, 50.0) if etf else 50.0
        if idx >= 21:
            r1 = (close.iloc[idx]/close.iloc[idx-21]-1)*100
            return round(min(100, max(0, sect*0.6 + (50+r1*2)*0.4)), 1)
        return sect

    if name == "fundamental":
        # 백테스트에서는 펀더 데이터 없음 → 안정 성장 proxy (저변동 + 우상향)
        if idx < 63: return 0.0
        r3 = (close.iloc[idx]/close.iloc[idx-63]-1)*100
        vol = close.iloc[idx-63:idx].pct_change().std()*100
        return round(min(100, max(0, 50 + r3*0.8 - vol*2)), 1)

    # ── 차트(기술적) 전략 ──
    if name == "golden_cross":
        if idx < 120: return 0.0
        ma20 = close.iloc[idx-20:idx].mean()
        ma60 = close.iloc[idx-60:idx].mean()
        ma120 = close.iloc[idx-120:idx].mean()
        cur = close.iloc[idx]; pts = 0.0
        if cur > ma20 > ma60 > ma120: pts += 50
        elif cur > ma20 > ma60: pts += 35
        elif cur > ma20: pts += 18
        gap = (cur - ma20)/ma20*100
        if 0 < gap < 8: pts += 30
        elif gap < 15: pts += 15
        if idx >= 125:
            prev20 = close.iloc[idx-25:idx-5].mean()
            prev60 = close.iloc[idx-65:idx-5].mean()
            if prev20 <= prev60 and ma20 > ma60: pts += 20
        return round(min(100, pts), 1)

    if name == "breakout":
        if idx < 61: return 0.0
        cur = close.iloc[idx]
        hi20 = close.iloc[idx-20:idx].max()
        hi60 = close.iloc[idx-60:idx].max()
        pts = 0.0
        if cur > hi60: pts += 45
        elif cur > hi20: pts += 30
        elif cur > hi20*0.98: pts += 12
        vr = volume.iloc[idx-3:idx].mean() / (volume.iloc[idx-20:idx].mean() or 1)
        pts += min(35, max(0, (vr-1)*35))
        rng = (close.iloc[idx-10:idx].max()-close.iloc[idx-10:idx].min())/(close.iloc[idx-10:idx].mean() or 1)
        if rng < 0.05: pts += 20
        elif rng < 0.10: pts += 10
        return round(min(100, pts), 1)

    if name == "macd":
        if idx < 35: return 0.0
        c = close.iloc[:idx+1]
        ema12 = c.ewm(span=12).mean()
        ema26 = c.ewm(span=26).mean()
        macd = ema12 - ema26
        sig = macd.ewm(span=9).mean()
        h = macd - sig
        pts = 0.0
        if macd.iloc[-1] > sig.iloc[-1]: pts += 35
        if h.iloc[-2] <= 0 and h.iloc[-1] > 0: pts += 30
        if macd.iloc[-1] > 0: pts += 20
        if h.iloc[-1] > h.iloc[-2] > h.iloc[-3]: pts += 15
        return round(min(100, pts), 1)

    if name == "bollinger":
        if idx < 120: return 0.0
        c = close.iloc[:idx+1]
        ma = c.rolling(20).mean()
        sd = c.rolling(20).std()
        upper = ma + 2*sd; lower = ma - 2*sd
        width = (upper - lower)/ma
        cur = c.iloc[-1]; pts = 0.0
        w_now = width.iloc[-1]
        w_pct = (width.iloc[-120:] < w_now).mean()
        if w_pct < 0.25: pts += 40
        elif w_pct < 0.5: pts += 20
        denom = (upper.iloc[-1]-lower.iloc[-1]) or 1
        bb_pos = (cur - lower.iloc[-1])/denom
        if bb_pos > 0.95: pts += 40
        elif bb_pos > 0.8: pts += 22
        if cur > ma.iloc[-1]: pts += 20
        return round(min(100, pts), 1)

    # ═══ 추가 추세추종 ═══
    if name == "dual_momentum":
        # 절대(12개월 +) AND 상대(강한 수익률) 모멘텀
        if idx < 252: return 0.0
        abs_mom = (close.iloc[idx]/close.iloc[idx-252]-1)*100
        if abs_mom <= 0: return 0.0          # 절대 모멘텀 음수면 제외
        r6 = (close.iloc[idx]/close.iloc[idx-126]-1)*100
        r3 = (close.iloc[idx]/close.iloc[idx-63]-1)*100
        return round(min(100, max(0, 30 + r6*0.4 + r3*0.6)), 1)

    if name == "turtle":
        # 돈치안 20일 신고가 돌파
        if idx < 25: return 0.0
        cur = close.iloc[idx]
        hi20 = high.iloc[idx-20:idx].max() if "High" in df else close.iloc[idx-20:idx].max()
        pts = 0.0
        if cur >= hi20: pts += 60
        elif cur >= hi20*0.99: pts += 35
        # ATR 대비 돌파 강도
        if idx >= 35:
            atr = (high.iloc[idx-14:idx]-low.iloc[idx-14:idx]).mean()
            if atr > 0:
                breakout_str = (cur - hi20)/atr
                pts += min(40, max(0, breakout_str*40 + 20))
        return round(min(100, pts), 1)

    if name == "ma_ribbon":
        # 5~60일 다중 이평 정배열
        if idx < 60: return 0.0
        mas = [close.iloc[idx-p:idx].mean() for p in (5,10,20,40,60)]
        pts = 0.0
        aligned = all(mas[i] > mas[i+1] for i in range(len(mas)-1))
        if aligned: pts += 60
        else:
            ups = sum(mas[i] > mas[i+1] for i in range(len(mas)-1))
            pts += ups*12
        if close.iloc[idx] > mas[0]: pts += 20
        gap = (close.iloc[idx]-mas[2])/mas[2]*100
        if 0 < gap < 10: pts += 20
        return round(min(100, pts), 1)

    # ═══ 급등주 타기 ═══
    if name == "surge":
        # 지금 급등 중인 종목: 거래량 폭발 + 강한 단기 상승 + 가속, 단 과열 직전
        if idx < 30: return 0.0
        cur = close.iloc[idx]
        r5  = (cur/close.iloc[idx-5]-1)*100     # 5일 수익률
        r3  = (cur/close.iloc[idx-3]-1)*100     # 3일 수익률
        r20 = (cur/close.iloc[idx-20]-1)*100    # 20일 수익률
        vr  = volume.iloc[idx-3:idx+1].mean()/(volume.iloc[idx-20:idx].mean() or 1)
        pts = 0.0
        # 1) 단기 급등 강도 (0~40): 5일 +8%↑ 이상이면 만점
        pts += min(40, max(0, r5*4))
        # 2) 거래량 폭발 (0~30): 평소의 1.5배↑
        pts += min(30, max(0, (vr-1)*30))
        # 3) 가속 (3일 > 5일 평균속도) (0~15)
        if r3/3 > r5/5 and r3 > 0: pts += 15
        # 4) 과열 패널티: 20일새 +60% 넘게 폭등했으면 추격 위험 → 감점
        if r20 > 60: pts -= 25
        elif r20 > 40: pts -= 10
        # 5) 직전 5일 하락이면 급등 아님
        if r5 <= 0: return 0.0
        return round(min(100, max(0, pts)), 1)

    # ═══ 추가 돌파 ═══
    if name == "volatility_breakout":
        # 래리 윌리엄스: 당일 시가 + 전일 변동폭*K 돌파
        if idx < 5: return 0.0
        prev_range = high.iloc[idx-1] - low.iloc[idx-1]
        target = close.iloc[idx-1] + prev_range*0.5   # 전일종가+0.5*range proxy
        cur = close.iloc[idx]
        pts = 0.0
        if cur > target: pts += 55
        # 거래량 동반
        vr = volume.iloc[idx-2:idx+1].mean()/(volume.iloc[idx-20:idx].mean() or 1)
        pts += min(30, max(0,(vr-1)*30))
        if close.iloc[idx] > close.iloc[idx-1]: pts += 15
        return round(min(100, pts), 1)

    if name == "52w_high":
        if idx < 252: return 0.0
        cur = close.iloc[idx]
        hi52 = close.iloc[idx-252:idx+1].max()
        near = cur/hi52
        pts = 0.0
        if near >= 0.99: pts += 60
        elif near >= 0.95: pts += 40
        elif near >= 0.90: pts += 20
        r3 = (cur/close.iloc[idx-63]-1)*100
        pts += min(40, max(0, r3*1.5))
        return round(min(100, pts), 1)

    # ═══ 추가 평균회귀 ═══
    if name == "rsi2":
        # 코너스 RSI(2) 극단 과매도
        if idx < 10: return 0.0
        c = close.iloc[:idx+1]
        delta = c.diff()
        gain = delta.clip(lower=0).rolling(2).mean().iloc[-1]
        loss = (-delta.clip(upper=0)).rolling(2).mean().iloc[-1]
        rs = gain/loss if loss else 1
        rsi2 = 100 - 100/(1+rs)
        # 장기추세는 상승(200일 위)일 때만
        ma200 = c.iloc[-200:].mean() if len(c) >= 200 else c.mean()
        if c.iloc[-1] < ma200: return 0.0
        pts = 0.0
        if rsi2 < 5: pts += 70
        elif rsi2 < 10: pts += 55
        elif rsi2 < 20: pts += 30
        return round(min(100, pts), 1)

    if name == "zscore":
        if idx < 20: return 0.0
        window = close.iloc[idx-20:idx]
        mean = window.mean(); std = window.std()
        if std == 0: return 0.0
        z = (close.iloc[idx]-mean)/std
        pts = 0.0
        if z < -2: pts += 70
        elif z < -1.5: pts += 50
        elif z < -1: pts += 25
        return round(min(100, pts), 1)

    # ═══ 추가 오실레이터 ═══
    if name == "stochastic":
        if idx < 16: return 0.0
        lo14 = low.iloc[idx-14:idx+1].min()
        hi14 = high.iloc[idx-14:idx+1].max()
        if hi14 == lo14: return 50.0
        k = (close.iloc[idx]-lo14)/(hi14-lo14)*100
        # 이전 K
        lo14p = low.iloc[idx-15:idx].min(); hi14p = high.iloc[idx-15:idx].max()
        kp = (close.iloc[idx-1]-lo14p)/((hi14p-lo14p) or 1)*100
        pts = 0.0
        if k < 20: pts += 40       # 과매도
        if k > kp and kp < 30: pts += 40  # 과매도서 상향
        if k > 20 and kp <= 20: pts += 20 # 과매도 탈출
        return round(min(100, pts), 1)

    if name == "ichimoku":
        # 일목균형표: 전환선(9)/기준선(26)/선행스팬
        if idx < 52: return 0.0
        def midpt(p):
            return (high.iloc[idx-p:idx+1].max()+low.iloc[idx-p:idx+1].min())/2
        tenkan = midpt(9); kijun = midpt(26)
        span_a = (tenkan+kijun)/2
        span_b = (high.iloc[idx-52:idx+1].max()+low.iloc[idx-52:idx+1].min())/2
        cloud_top = max(span_a, span_b)
        cur = close.iloc[idx]; pts = 0.0
        if cur > cloud_top: pts += 45        # 구름 위
        if tenkan > kijun: pts += 30         # 전환>기준
        if cur > kijun: pts += 25
        return round(min(100, pts), 1)

    if name == "adx_trend":
        if idx < 30: return 0.0
        h = high.iloc[idx-28:idx+1]; l = low.iloc[idx-28:idx+1]; c = close.iloc[idx-29:idx+1]
        up = h.diff(); dn = -l.diff()
        plus_dm = ((up > dn) & (up > 0)) * up
        minus_dm = ((dn > up) & (dn > 0)) * dn
        tr = (h - l).clip(lower=0)
        atr = tr.rolling(14).mean().iloc[-1] or 1
        pdi = 100*plus_dm.rolling(14).mean().iloc[-1]/atr
        mdi = 100*minus_dm.rolling(14).mean().iloc[-1]/atr
        dx = 100*abs(pdi-mdi)/((pdi+mdi) or 1)
        pts = 0.0
        if dx > 25: pts += 40
        elif dx > 20: pts += 20
        if pdi > mdi: pts += 40
        if close.iloc[idx] > close.iloc[idx-14]: pts += 20
        return round(min(100, pts), 1)

    # ═══ 추가 팩터 ═══
    if name == "low_vol":
        # 저변동성 + 우상향
        if idx < 63: return 0.0
        vol = close.iloc[idx-63:idx].pct_change().std()*100
        r3 = (close.iloc[idx]/close.iloc[idx-63]-1)*100
        pts = max(0, 60 - vol*15)   # 변동성 낮을수록 고점
        if r3 > 0: pts += min(40, r3*1.5)
        return round(min(100, pts), 1)

    if name == "quality_trend":
        # 저변동 + 꾸준한 우상향 (낙폭 작은 추세)
        if idx < 126: return 0.0
        seg = close.iloc[idx-126:idx+1]
        r6 = (seg.iloc[-1]/seg.iloc[0]-1)*100
        vol = seg.pct_change().std()*100
        # 최대낙폭
        rm = seg.cummax(); mdd = ((seg-rm)/rm).min()*100
        pts = 0.0
        if r6 > 0: pts += min(50, r6*0.8)
        pts += max(0, 30 - vol*8)
        pts += max(0, 20 + mdd*1.0)   # mdd 작을수록(0에 가까울수록) 높음
        return round(min(100, max(0,pts)), 1)

    # ═══ 퀀트 팩터 ═══
    if name == "risk_adj_momentum":
        # 위험조정 모멘텀(샤프 모멘텀): 12-1개월 수익률 ÷ 변동성.
        # 최근 1개월을 건너뛰어(skip) 단기 반전 노이즈를 피하는 학계 표준(12-1).
        # 같은 수익률이라도 '덜 흔들리며 오른' 종목을 선호 → 위험 대비 추세 품질.
        if idx < 252: return 0.0
        p_skip = close.iloc[idx-21]      # 1개월 전 (최근월 스킵)
        p_base = close.iloc[idx-252]     # 12개월 전
        if p_base <= 0: return 0.0
        mom = (p_skip / p_base) - 1                      # 11개월 수익률
        rets = close.iloc[idx-252:idx].pct_change().dropna()
        vol = float(rets.std()) * (252 ** 0.5)           # 연율 변동성
        if vol <= 0: return 0.0
        ram = mom / vol                                  # 위험조정 모멘텀(≈샤프)
        if mom <= 0:
            return round(max(0.0, 25 + ram * 12), 1)     # 절대 모멘텀 음수 → 약하게
        return round(min(100, max(0, 50 + ram * 30)), 1)

    if name == "quant_multifactor":
        # 멀티팩터 결합: 위험조정 모멘텀 + 저변동성 + 추세품질(저MDD) + 과매수가드.
        # 단일 팩터의 노이즈를 분산해 더 견고한 점수를 만드는 AQR식 팩터 슬리브.
        if idx < 252: return 0.0
        seg = close.iloc[idx-252:idx+1]
        # 1) 위험조정 모멘텀 (0~40)
        if close.iloc[idx-252] <= 0: return 0.0
        mom = (close.iloc[idx-21] / close.iloc[idx-252]) - 1
        vol = float(close.iloc[idx-252:idx].pct_change().std()) * (252 ** 0.5)
        ram = (mom / vol) if vol > 0 else 0.0
        f_mom = min(40, max(0, 20 + ram * 20))
        # 2) 저변동성 (0~25): 변동성 낮을수록 가점 (Low-Vol 이상현상)
        f_lowvol = max(0, 25 - vol * 40)
        # 3) 추세 품질 (0~25): 최대낙폭(MDD) 작을수록 높음
        rm = seg.cummax(); mdd = float(((seg - rm) / rm).min())
        f_quality = max(0, 25 + mdd * 60)                # mdd=-0.2 → 13, mdd≈0 → 25
        # 4) 과매수 가드 (0~10): RSI 과열이면 감점
        delta = close.diff()
        g = delta.clip(lower=0).rolling(14).mean().iloc[idx]
        l = (-delta.clip(upper=0)).rolling(14).mean().iloc[idx]
        rsi = 100 - 100 / (1 + (g / l if l else 1))
        f_guard = 10 if rsi < 70 else (5 if rsi < 80 else 0)
        return round(min(100, max(0, f_mom + f_lowvol + f_quality + f_guard)), 1)

    if name == "momentum_max":
        # ── 최대수익 모멘텀 (연구 근거 기반 수익 극대화) ─────────────────────
        # 모멘텀은 1927~2024 미국 연 9.2%, 31개국에서 재현된 가장 강력한 수익
        # 프리미엄. 최고 성과 백테스트 레시피를 결합한다:
        #   ① 6개월 모멘텀(최근 1개월 스킵) 중심 + 3·12개월 블렌드(다차원 모멘텀)
        #      — 6개월 룩백이 raw 수익 최강(연 24.7%·샤프 1.27), 스킵으로 단기반전 회피
        #   ② 변동성 역비례 위험조정 (Barroso & Santa-Clara 2015) — 샤프 거의 2배,
        #      모멘텀 크래시 완화. '덜 흔들리며 오른' 종목 선호.
        #   ③ 절대모멘텀/추세 게이트 (듀얼 모멘텀) — 200일선 아래/음(-) 모멘텀은
        #      신규 매수 배제(하락추세에서 터지는 모멘텀 붕괴 회피)
        #   ④ 52주 신고가 근접 보너스 (George & Hwang 52주 신고가 모멘텀)
        if idx < 273:
            return 0.0
        px = float(close.iloc[idx])
        p21 = float(close.iloc[idx - 21])            # 최근 1개월 스킵 기준점

        def _ret(lb):
            base = float(close.iloc[idx - 21 - lb])
            return (p21 / base - 1.0) if base > 0 else 0.0
        r3, r6, r12 = _ret(63), _ret(126), _ret(252)
        blended = 0.5 * r6 + 0.25 * r3 + 0.25 * r12  # 6개월 중심 다차원 모멘텀(연구 최적 룩백)
        rets = close.iloc[idx - 126:idx].pct_change().dropna()
        vol = float(rets.std()) * (252 ** 0.5)       # 6개월 연율 변동성
        ram = (blended / vol) if vol > 0 else 0.0    # 위험조정(≈샤프) 모멘텀
        ma200 = float(close.iloc[idx - 200:idx].mean())
        # ③ 절대모멘텀/추세 게이트
        if px < ma200 or blended <= 0:
            return round(max(0.0, 18 + ram * 8), 1)  # 추세 이탈/음수 → 약하게(거의 미매수)
        # ④ 52주 신고가 근접도 (0.90~1.00 → 0~12점)
        hi = float(close.iloc[max(0, idx - 252):idx + 1].max())
        near_bonus = max(0.0, min(12.0, (px / hi - 0.90) * 120)) if hi > 0 else 0.0
        # 위험조정 모멘텀(Barroso·Santa-Clara: 위험조정이 수익·샤프 모두 개선, 크래시 완화)을
        # 핵심 랭킹으로. 추세 게이트가 하락장 붕괴를 막아 상승 추세에서 승자를 끝까지 끈다.
        score = 50.0 + ram * 28.0 + near_bonus
        return round(min(100.0, max(0.0, score)), 1)

    if name == "adaptive":
        # ── 국면 적응형 (Regime-Switching) ──────────────────────────────────
        # 시장은 만변하므로 한 가지 로직에 매이지 않는다. 종목의 현재 국면을
        # 200/50일선과 변동성으로 판정해, 그 국면에서 가장 검증된 하위 전략으로
        # 동적 위임한다. (관리형선물·AQR식 레짐 스위칭의 단순·견고 버전)
        #   • 강세 추세(가격>50일>200일)  → 샤프 모멘텀: 위험조정 추세 추종(승자 보유)
        #   • 약세(가격<200일)            → RSI(2) 과매도 반등만 선별 + 비중 축소(×0.7)
        #   • 횡보/전환(그 사이)          → 볼린저 스퀴즈: 변동성 수축 후 돌파 포착
        if idx < 252:
            return 0.0
        px    = float(close.iloc[idx])
        ma50  = float(close.iloc[idx - 50:idx].mean())
        ma200 = float(close.iloc[idx - 200:idx].mean())
        if px > ma50 > ma200:
            return _strategy_score_bt("risk_adj_momentum", ticker, stock_data, etf_data, idx)
        if px < ma200:
            base = _strategy_score_bt("rsi2", ticker, stock_data, etf_data, idx)
            return round(base * 0.7, 1)      # 약세장 추격 자제(과매도 반등만, 보수적)
        return _strategy_score_bt("bollinger", ticker, stock_data, etf_data, idx)

    # composite (기본)
    return _composite_score_bt(ticker, stock_data, etf_data, idx)


# ──────────────────────────────────────────────────────────────────────────────
# 백테스트 엔진
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    ticker:     str
    entry_date: date
    exit_date:  date
    entry_price: float
    exit_price:  float
    shares:      float
    reason:      str

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price

    @property
    def pnl_usd(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares


@dataclass
class BacktestResult:
    trades:       list[Trade]
    equity_curve: pd.Series
    start_capital: float
    benchmark:    dict | None = None   # 같은 구간 SPY(수정주가) 성과 — benchmark_stats()

    # ── 핵심 지표 ──────────────────────────────
    @property
    def total_return(self) -> float:
        return (self.equity_curve.iloc[-1] / self.start_capital) - 1

    @property
    def cagr(self) -> float:
        years = len(self.equity_curve) / 252
        if years <= 0:
            return 0.0
        return (self.equity_curve.iloc[-1] / self.start_capital) ** (1 / years) - 1

    @property
    def mdd(self) -> float:          # Maximum Drawdown
        roll_max = self.equity_curve.cummax()
        dd = (self.equity_curve - roll_max) / roll_max
        return float(dd.min())

    @property
    def sharpe(self) -> float:
        rets = self.equity_curve.pct_change().dropna()
        if rets.std() == 0:
            return 0.0
        return float(rets.mean() / rets.std() * np.sqrt(252))

    @property
    def daily_returns(self) -> pd.Series:
        return self.equity_curve.pct_change().dropna()

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_pct > 0)
        return wins / len(self.trades)

    @property
    def avg_hold_days(self) -> float:
        if not self.trades:
            return 0.0
        return np.mean([(t.exit_date - t.entry_date).days for t in self.trades])

    def report(self) -> str:
        lines = [
            "=" * 55,
            "  📊  백테스트 결과",
            "=" * 55,
            f"  총 수익률    : {self.total_return:+.1%}",
            f"  CAGR        : {self.cagr:+.1%}",
            f"  최대 낙폭    : {self.mdd:.1%}",
            f"  샤프 지수    : {self.sharpe:.2f}",
            f"  승률         : {self.win_rate:.1%}  ({len(self.trades)}건)",
            f"  평균 보유기간: {self.avg_hold_days:.0f}일",
            f"  최종 자산    : ${self.equity_curve.iloc[-1]:,.0f}  "
            f"(시작 ${self.start_capital:,.0f})",
        ]
        b = self.benchmark
        if b:
            lines.append(f"  SPY 같은 구간: {b['ret']:+.1%} · CAGR {b['cagr']:+.1%} · "
                         f"MDD {b['mdd']:.1%} · 샤프 {b['sharpe']:.2f}")
        lines.append("=" * 55)
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 메인 백테스트 함수
# ──────────────────────────────────────────────────────────────────────────────

def load_market_data(universe: list[str], start: str, end: str) -> dict:
    """
    백테스트용 시세 데이터를 한 번에 다운로드.
    여러 전략을 같은 데이터로 돌릴 때 재사용 (전체 전략 테스트).
    """
    all_tickers = universe + list(SECTOR_ETFS)
    raw = yf.download(all_tickers, start=start, end=end,
                      auto_adjust=True, progress=False)

    def _df(t: str):
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw.xs(t, axis=1, level=1).dropna(how="all")
            else:
                df = raw.dropna(how="all")
            return df if len(df) > 30 else None
        except Exception:
            return None

    stock_data = {t: _df(t) for t in universe}
    stock_data = {t: df for t, df in stock_data.items() if df is not None}
    etf_data   = {t: _df(t) for t in SECTOR_ETFS if _df(t) is not None}

    if not stock_data:
        raise ValueError("다운로드된 종목 데이터가 없습니다")

    date_index = next(iter(stock_data.values())).index

    spy_ma200 = spy_ma50 = spy_close = None
    try:
        _warm = (pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        spy_raw = yf.download("SPY", start=_warm, end=end,
                              auto_adjust=True, progress=False)
        spy_full = spy_raw["Close"].squeeze()
        spy_close = spy_full.reindex(date_index, method="ffill")
        spy_ma200 = spy_full.rolling(200, min_periods=100).mean().reindex(date_index, method="ffill")
        spy_ma50  = spy_full.rolling(50,  min_periods=25).mean().reindex(date_index, method="ffill")
    except Exception:
        pass

    return {"stock_data": stock_data, "etf_data": etf_data,
            "date_index": date_index, "spy_close": spy_close,
            "spy_ma200": spy_ma200, "spy_ma50": spy_ma50}


class _BacktestPortfolio(PortfolioManager):
    """백테스트 전용 인메모리 포트폴리오.

    핵심: 라이브와 '같은 코드'로 결정·기록한다. 매수/매도/손절/트레일링/회전/
    섹터상한/재진입 쿨다운·히스테리시스 판정은 모두 부모(PortfolioManager)의
    generate_orders·should_sell·record_buy·record_sell 를 그대로 쓴다. 다만
    실거래 장부(state.json·trades.json)는 절대 건드리지 않고 메모리에만 둔다.
    → 백테스트 수익률이 라이브 결정 로직을 그대로 반영한다(분기 제거).
    """

    def __init__(self):
        self.paper = True
        self.state_file = None        # 디스크 미사용 (아래 _load/_save no-op)
        self.trade_file = None
        self.positions = {}
        self._trades: list[dict] = []  # 인메모리 청산 내역 (재진입 필터·집계 시드)

    # 디스크 I/O 차단 — 메모리가 단일 진실원이라 리로드/세이브가 필요 없다.
    def _load_state(self):
        pass

    def _save_state(self):
        pass

    def _read_trades(self) -> list:
        return self._trades

    def _store_trade(self, rec: dict):
        self._trades.append(rec)


def run(
    start: str = "2022-01-01",
    end:   str  = date.today().isoformat(),
    capital: float = 10_000.0,
    universe: list[str] | None = None,
    strategy=None,          # strategies.BaseStrategy 인스턴스 (None이면 기본 복합)
    # ── 파라미터 직접 전달 (None이면 config 기본값) ──
    max_positions: int = None,
    max_position_pct: float = None,
    min_score: float = None,
    sell_score: float = None,
    stop_loss: float = None,
    take_profit: float = None,
    hold_strong: int = None,
    hold_medium: int = None,
    rebalance_days: int = None,
    trailing_stop: float = 0.15,   # 고점 대비 이만큼 되돌리면 청산 → portfolio.TRAIL_GIVEBACK_PCT
    use_trend_filter: bool = True, # 중립장에서 SPY<200MA면 신규 매수 보류(백테스트 추가 보수)
    adaptive_regime: bool = True,  # 약세장(SPY<200MA-2%) 신규 매수 보류 — 라이브와 동일
    commission: float = 0.001,     # 거래 수수료 (0.1% = Alpaca 무료, 실제 0.1~0.5%)
    slippage: float = 0.0005,      # 슬리피지 (0.05% = 체결 가격 불리해지는 정도)
    prefetched: dict = None,       # load_market_data() 결과 재사용 (전체전략 테스트용)
    risk_sizing: bool = False,     # (구) 변동성 타겟 사이징 — 현재는 라이브 엔진 사이징이 우선(무시)
    vol_target: float = 0.25,      # (구) risk_sizing 기준선 (현재 미사용)
    dynamic: bool = True,          # 라이브 기본값과 동일 — 유동형(_dynamic_orders) 배분
    ev_filter: bool = False,       # 라이브와 같은 EV 판단(core.ev)으로 매수 대기·보유 매도
    ev_min: float = 0.0,
    ev_exit: float = -0.005,
    execution: str = "close",      # "close": 결정한 날 종가 체결(예전) · "next_open": 다음 날 시가 체결
    daily_stops: bool = False,     # 매 거래일 시가·고가·저가로 손절·트레일링 체결(갭은 시가)
) -> BacktestResult:
    """과거 데이터로 전략을 시뮬레이션한다.

    결정·실행 로직은 라이브와 '동일'하다(PortfolioManager.generate_orders/
    should_sell 를 그대로 호출). 점수도 라이브 자동매매와 같은 _strategy_score_bt
    를 쓴다(core.scoring 와 공유). 따라서 백테스트 결과가 라이브 동작을 예측한다.

    백테스트 고유 요소(라이브엔 없음):
      • commission/slippage 현금 반영(체결 현실성)
      • use_trend_filter: 중립장 추가 보수 필터(끄면 라이브와 동일)
    리밸런싱은 rebalance_days 주기. 손절/트레일링도 그 주기에 평가된다
    (일봉 백테스트의 한계 — 라이브 틱 가드만큼 즉각적이지 않음).

    체결 현실성 옵션(기본값 = 예전 동작, 연구·신뢰성 점검용):
      • execution="next_open": 결정은 그날 종가까지의 정보로, 체결은 다음 거래일 시가.
        기본 "close" 는 결정에 쓴 바로 그 종가에 체결해 약한 미래 참조가 있다.
      • daily_stops=True: 리밸런싱 날이 아니어도 매일 손절·트레일링을 일봉 고가·저가로
        확인한다(증권사 측 stop 주문과 같은 의미). 시가가 이미 선 아래면 시가에 체결(갭),
        장중에 닿으면 선 가격에 체결. 고점(peak)은 그날 판정 뒤 고가로 갱신 — 같은 날
        고가·저가의 선후를 모르므로 보수적으로.
    """
    if execution not in ("close", "next_open"):
        raise ValueError(f"execution 은 'close' 또는 'next_open': {execution!r}")
    if universe is None:
        universe = TRADEABLE_UNIVERSE

    # 파라미터 바인딩 (전달값 우선, 없으면 config 기본)
    MAX_POS   = max_positions    if max_positions    is not None else MAX_POSITIONS
    MAX_PCT   = max_position_pct if max_position_pct is not None else MAX_POSITION_PCT
    MIN_SC    = min_score        if min_score        is not None else MIN_SCORE_TO_BUY
    SELL_SC   = sell_score       if sell_score       is not None else SELL_SCORE_THRESHOLD
    SL        = stop_loss        if stop_loss        is not None else STOP_LOSS_PCT
    TP        = take_profit      if take_profit      is not None else TAKE_PROFIT_PCT
    HOLD_S    = hold_strong      if hold_strong      is not None else HOLD_DAYS_STRONG
    HOLD_M    = hold_medium      if hold_medium      is not None else HOLD_DAYS_MEDIUM
    REBAL     = rebalance_days   if rebalance_days   is not None else REBALANCE_DAYS

    if prefetched is not None:
        stock_data = prefetched["stock_data"]
        etf_data   = prefetched["etf_data"]
        date_index = prefetched["date_index"]
        spy_close  = prefetched["spy_close"]
        spy_ma200  = prefetched["spy_ma200"]
        spy_ma50   = prefetched["spy_ma50"]
    else:
        _b = load_market_data(universe, start, end)
        stock_data = _b["stock_data"]; etf_data = _b["etf_data"]
        date_index = _b["date_index"]; spy_close = _b["spy_close"]
        spy_ma200  = _b["spy_ma200"];  spy_ma50  = _b["spy_ma50"]

    # 연구용 번들(load_research_bundle) 전용 키 — 없으면 기존 동작 그대로.
    #   eval_start_idx : 워밍업 구간(지표용 과거 데이터)은 매매·평가에서 제외
    #   members        : 시점별 지수 구성 — 그날 구성종목이 아니면 신규 매수 후보 제외
    #   n_valid/min_history : 상장 직후(이력 부족) 종목은 신규 매수 후보 제외
    #   sector_map     : 섹터 신호용 티커→섹터 ETF 보강(이 런 동안만, 기존 키는 불변)
    #   tie_break/tie_seed : 같은 점수(동점) 순서 — tie_order() 참고. 없으면 입력 순서
    _px = prefetched or {}
    eval_start = int(_px.get("eval_start_idx") or 0)
    members    = _px.get("members")
    n_valid    = _px.get("n_valid")
    min_hist   = int(_px.get("min_history") or 0)
    _sec_added = [k for k in (_px.get("sector_map") or {}) if k not in TICKER_SECTOR_MAP]
    _tie = tie_order(_px.get("tie_break"), stock_data, _px.get("tie_seed"))

    def _rank(scores: list, i: int) -> None:
        """점수 내림차순. 동점은 tie_order 보조 키(없으면 안정 정렬 = 입력 순서)."""
        if _tie is None:
            scores.sort(key=lambda s: -s.total)
        else:
            tk = _tie(i)
            scores.sort(key=lambda s: (-s.total, tk[s.ticker]))

    def _live_regime(i: int) -> str:
        """시장 국면 — 라이브(core.scoring.score)와 동일: SPY vs 200일선 ±2%."""
        if spy_close is None or spy_ma200 is None:
            return "neutral"
        try:
            pv = float(spy_close.iloc[i]); mv = float(spy_ma200.iloc[i])
            if np.isnan(mv):
                return "neutral"
            if pv < mv * 0.98:
                return "bear"
            if pv > mv * 1.02:
                return "bull"
            return "neutral"
        except Exception:
            return "neutral"

    def _spy_below_200(i: int) -> bool:
        if spy_close is None or spy_ma200 is None:
            return False
        try:
            mv = float(spy_ma200.iloc[i])
            return (not np.isnan(mv)) and float(spy_close.iloc[i]) < mv
        except Exception:
            return False

    # ── 라이브 결정 로직 주입 ──────────────────────────────────────────────────
    # generate_orders/should_sell 가 참조하는 portfolio 모듈 전역을 이번 런 동안
    # 백테스트 파라미터로 덮어쓴다. core.cycle.run_cycle 이 라이브에서 하는 것과
    # 똑같은 주입 — 같은 코드가 같은 설정을 보게 해 결과 일관성을 보장한다.
    _SAVE_KEYS = ("CAPITAL_TOTAL", "MAX_POSITIONS", "MAX_POSITION_PCT",
                  "MIN_SCORE_TO_BUY", "SELL_SCORE_THRESHOLD",
                  "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "TRAIL_GIVEBACK_PCT",
                  "HOLD_DAYS_STRONG", "HOLD_DAYS_MEDIUM",
                  "BUY_PRICE_MIN", "BUY_PRICE_MAX")
    _saved = {k: getattr(portfolio, k) for k in _SAVE_KEYS}

    cash = capital
    equity_curve = []
    pmbt = _BacktestPortfolio()

    try:
        portfolio.CAPITAL_TOTAL       = float(capital)
        portfolio.MAX_POSITIONS       = int(MAX_POS)
        portfolio.MAX_POSITION_PCT    = float(MAX_PCT)
        portfolio.MIN_SCORE_TO_BUY    = float(MIN_SC)
        portfolio.SELL_SCORE_THRESHOLD = float(SELL_SC)
        portfolio.STOP_LOSS_PCT       = float(SL)
        portfolio.TAKE_PROFIT_PCT     = float(TP)
        portfolio.TRAIL_GIVEBACK_PCT  = float(trailing_stop)
        portfolio.HOLD_DAYS_STRONG    = int(HOLD_S)
        portfolio.HOLD_DAYS_MEDIUM    = int(HOLD_M)
        portfolio.BUY_PRICE_MIN       = 0.0
        portfolio.BUY_PRICE_MAX       = 0.0
        for k in _sec_added:
            TICKER_SECTOR_MAP[k] = _px["sector_map"][k]

        ev_stats: dict = {}
        if ev_filter:
            from core import ev as _ev
            _evp = _ev.EVParams(
                sl=float(SL), tp=float(TP), trail=float(trailing_stop),
                hold_strong=int(HOLD_S), hold_medium=int(HOLD_M),
                cost_side=float(commission + slippage), ev_min=float(ev_min),
                ev_exit=float(ev_exit), min_score=float(MIN_SC), sell_score=float(SELL_SC),
                n_paths=1000)
        def _fill(orders: dict, i: int, at: str, cash: float) -> float:
            """주문 체결: 매도 먼저(자본 회수) → 매수, 수수료·슬리피지 현금 반영.
            at="close": 주문의 est_price(결정일 종가) · at="open": i 일 시가(없으면 건너뜀)."""
            def _px(o: dict) -> float:
                if at == "close":
                    return float(o["est_price"])
                df = stock_data.get(o["ticker"])
                if df is None or i >= len(df) or "Open" not in df.columns:
                    return 0.0
                v = float(df["Open"].iloc[i])
                return v if v == v and v > 0 else 0.0
            for o in orders.get("sell", []):
                t = o["ticker"]
                pos = pmbt.positions.get(t)
                if pos is None:
                    continue
                qty = min(int(o["shares"]), int(pos.shares))
                px = _px(o)
                if qty < 1 or not px > 0:
                    continue
                fill = px * (1 - slippage)
                proceeds = fill * qty
                cash += proceeds - proceeds * commission
                pmbt.record_sell(t, exit_price=fill, reason=o["reason"], shares=qty)
            for o in orders.get("buy", []):
                t = o["ticker"]
                qty = int(o["shares"])
                px = _px(o)
                if qty < 1 or not px > 0:
                    continue
                fill = px * (1 + slippage)
                cost = fill * qty
                if cost + cost * commission > cash + 1e-6:
                    # 슬리피지·수수료(또는 시가 갭)로 예산 초과 → 가능한 수량으로 축소
                    qty = int(cash / (fill * (1 + commission)))
                    if qty < 1:
                        continue
                    cost = fill * qty
                cash -= cost + cost * commission
                pmbt.record_buy(t, qty, fill, o.get("score", 0))
            return cash

        def _intraday_stops(i: int, cash: float) -> float:
            """일봉 OHLC 로 손절·트레일링 판정·체결(증권사 stop 주문 근사)."""
            for t in list(pmbt.positions):
                df = stock_data.get(t)
                if df is None or i >= len(df) or "Low" not in df.columns:
                    continue
                o_, h_, l_ = (float(df[c].iloc[i]) for c in ("Open", "High", "Low"))
                if not (o_ > 0 and h_ > 0 and l_ > 0):
                    continue
                pos = pmbt.positions[t]
                levels = [(pos.entry_price * (1 - portfolio.STOP_LOSS_PCT), "stop_loss")]
                if pos.entry_price and (pos.peak_price - pos.entry_price) / pos.entry_price \
                        >= portfolio.TAKE_PROFIT_PCT:
                    levels.append((pos.peak_price * (1 - portfolio.TRAIL_GIVEBACK_PCT),
                                   "trailing_stop"))
                level, why = max(levels)        # 떨어질 때 먼저 닿는 선
                hit = o_ if o_ <= level else (level if l_ <= level else None)
                if hit is None:
                    pos.update_peak(h_)
                    continue
                fill = hit * (1 - slippage)
                qty = pos.shares
                proceeds = fill * qty
                cash += proceeds - proceeds * commission
                pmbt.record_sell(t, exit_price=fill, reason=f"{why} (일중)", shares=qty)
            return cash

        pending = None            # execution="next_open": 다음 거래일 시가에 체결할 주문
        for idx, today in enumerate(date_index):
            if idx < eval_start:
                continue          # 워밍업 — 지표 계산용 과거 데이터일 뿐 매매·평가 없음
            if pending is not None or (daily_stops and pmbt.positions):
                portfolio.set_clock(today.date())
                if pending is not None:
                    cash = _fill(pending, idx, "open", cash)
                    pending = None
                if daily_stops and pmbt.positions:
                    cash = _intraday_stops(idx, cash)
            # --- 일일 평가(mark-to-market) ---
            pv = cash
            for t, pos in pmbt.positions.items():
                df = stock_data.get(t)
                if df is not None and idx < len(df):
                    pv += float(df["Close"].iloc[idx]) * pos.shares
            equity_curve.append(pv)

            if (idx - eval_start) % REBAL != 0:
                continue

            # 가상 '오늘' 주입 — 보유기간/재진입 쿨다운이 시뮬 날짜 기준으로 계산되게
            portfolio.set_clock(today.date())

            # 현재가 + 점수 (점수는 라이브 자동매매와 동일한 _strategy_score_bt)
            prices: dict[str, float] = {}
            scores: list[StockScore] = []
            for t, df in stock_data.items():
                if idx >= len(df):
                    continue
                if t not in pmbt.positions:
                    if members is not None and not (t in members and members[t][idx]):
                        continue      # 그날 지수 구성종목 아님 → 신규 후보 아님
                    if n_valid is not None and t in n_valid and n_valid[t][idx] < min_hist:
                        continue      # 상장 직후 — 전략 룩백을 채울 이력 부족
                px = float(df["Close"].iloc[idx])
                if not px > 0:
                    continue          # 상장 전(NaN) 구간
                prices[t] = px
                try:
                    sc = _strategy_score_bt(strategy, t, stock_data, etf_data, idx)
                except Exception:
                    sc = 0.0
                if sc != sc:          # NaN(결측 룩백) → 신호 없음
                    sc = 0.0
                scores.append(StockScore(t, float(sc), 0, 0, 0, 0))
            _rank(scores, idx)

            # EV 판단 — 라이브 run_cycle 과 같은 core.ev (당일까지의 종가만 사용, 미래 참조 없음)
            if ev_filter:
                _decs = _ev.decide(
                    [(s.ticker, s.total) for s in scores], prices, pmbt.positions, _evp,
                    lambda t: stock_data[t]["Close"].iloc[max(0, idx - 160):idx + 1].tolist(),
                    today=today.date())
                _g = _ev.gate_scores({s.ticker: s.total for s in scores}, _decs, _evp)
                scores = [StockScore(s.ticker, _g[s.ticker], 0, 0, 0, 0) for s in scores]
                _rank(scores, idx)
                for _d in _decs:
                    ev_stats[_d.action] = ev_stats.get(_d.action, 0) + 1

            regime = _live_regime(idx) if adaptive_regime else "neutral"

            # ★ 라이브와 '같은 코드'로 매수/매도 결정 (손절·트레일링·회전·섹터·쿨다운 포함)
            orders = pmbt.generate_orders(
                scores, prices, dynamic=dynamic,
                buy_mode="전량", sell_mode="전량",
                available_override=cash)

            # 신규 매수 보류 — 라이브 run_cycle 과 동일(약세장). use_trend_filter 는
            # 중립장 추가 보수 옵션(라이브엔 없음 — 끄면 라이브와 동일).
            if adaptive_regime and regime == "bear":
                orders["buy"] = []
            elif use_trend_filter and regime == "neutral" and _spy_below_200(idx):
                orders["buy"] = []

            # --- 체결 ---
            if execution == "next_open":
                pending = orders          # 다음 거래일 시가(마지막 날 주문은 체결 없음)
            else:
                cash = _fill(orders, idx, "close", cash)

        # --- 남은 포지션 강제 청산 (마지막 날 종가) ---
        last_idx = len(date_index) - 1
        portfolio.set_clock(date_index[last_idx].date())
        for t in list(pmbt.positions.keys()):
            df = stock_data.get(t)
            if df is None:
                continue
            pos = pmbt.positions[t]
            price = float(df["Close"].iloc[min(last_idx, len(df) - 1)])
            fill = price * (1 - slippage)
            qty = pos.shares
            proceeds = fill * qty
            cash += proceeds - proceeds * commission
            pmbt.record_sell(t, exit_price=fill, reason="end_of_backtest", shares=qty)
    finally:
        # 전역·시계 원복 — 라이브 경로가 백테스트 설정에 오염되지 않게
        portfolio.set_clock(None)
        for k, v in _saved.items():
            setattr(portfolio, k, v)
        for k in _sec_added:
            TICKER_SECTOR_MAP.pop(k, None)

    # 인메모리 청산 내역 → Trade 객체 (리포트·지표용)
    trades: list[Trade] = []
    for r in pmbt._trades:
        try:
            trades.append(Trade(
                ticker=r["ticker"],
                entry_date=date.fromisoformat(r["entry_date"]),
                exit_date=date.fromisoformat(r["exit_date"]),
                entry_price=float(r["entry_price"]),
                exit_price=float(r["exit_price"]),
                shares=float(r["shares"]),
                reason=r["reason"],
            ))
        except Exception:
            continue

    equity_series = pd.Series(equity_curve, index=date_index[eval_start:])
    res = BacktestResult(trades=trades, equity_curve=equity_series, start_capital=capital,
                         benchmark=benchmark_stats(spy_close, equity_series.index))
    res.ev_stats = ev_stats          # EV 판단 집계(ev_filter=True 일 때)
    return res


def compare_strategies(keys: list[str], universe: list[str], start: str, end: str,
                       capital: float, min_score: float, stop_loss: float,
                       take_profit: float, ev_filter: bool = False) -> list[dict]:
    """여러 전략을 같은 데이터 번들로 백테스트해 총수익률 내림차순 지표 목록 반환.

    앱의 '최고 수익 전략 찾기'가 core.isolated 로 별도 프로세스에서 부른다
    (모듈 최상위 함수여야 자식 프로세스가 import 할 수 있다).

    각 행에는 기존 키(ret·cagr·mdd·sharpe·win·eq)에 더해 정직한 평가 지표가 붙는다:
      spy_ret·spy_cagr·spy_mdd·spy_sharpe : 같은 구간 SPY(수정주가)
      dsr·sr_deflated·sr0                 : 이번 비교의 시도 수 N=len(keys) 로 운 보정한 샤프
      family·corr                         : 일간 수익률 상관 + 계층 군집으로 묶은 '계열'
    """
    bundle = load_market_data(universe, start, end)
    results: dict = {}
    for k in keys:
        try:
            results[k] = run(strategy=k, start=start, end=end, capital=capital,
                             universe=universe, min_score=min_score, stop_loss=stop_loss,
                             take_profit=take_profit, prefetched=bundle, ev_filter=ev_filter)
        except Exception:
            pass
    rows = summarize_results(results, n_trials=len(keys))["rows"]
    rows.sort(key=lambda x: -x["ret"])
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# 정직한 평가 기준 (연구용) — SPY 벤치마크 · 봉인 구간 · 운 보정 샤프 · 전략 계열
# ──────────────────────────────────────────────────────────────────────────────
# 손으로 고른 종목·같은 기간에서 27개 중 1등을 고르면 운(데이터 스누핑)을 실력으로
# 착각한다. 그래서 (1) 같은 구간 SPY 를 항상 같이 보고, (2) 2025-01-01 이후는 봉인해
# 최종 확인에 1회만 쓰고, (3) 시도한 전략 수만큼 샤프를 깎고(DSR), (4) 사실상 같은
# 전략(높은 상관)끼리 '계열'로 묶어 계열별 대표만 비교한다.

DEV_START = "2020-01-01"          # 개발 구간 (포함)
DEV_END = "2024-12-31"            # 개발 구간 (포함)
HOLDOUT_START = "2025-01-01"      # 봉인 구간 시작 — holdout=True 일 때만 연다
WARMUP_CAL_DAYS = 450             # 지표 워밍업(최장 룩백 momentum_max 273봉 + 여유)
MIN_HISTORY_BARS = 252            # 이보다 이력이 짧은(상장 직후) 종목은 신규 후보 제외
FAMILY_CORR = 0.7                 # 계열 기준: 평균 상관 ≥ 이 값이면 같은 계열
CACHE_DIR = Path(os.environ.get("TRADE_BT_CACHE",
                                str(Path(__file__).parent / ".bt_cache")))

# GICS 섹터 → 섹터 ETF (커뮤니케이션은 기존 TICKER_SECTOR_MAP 관례대로 XLK)
GICS_TO_ETF = {
    "Information Technology": "XLK", "Communication Services": "XLK",
    "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV",
    "Industrials": "XLI", "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP", "Materials": "XLB", "Utilities": "XLU",
    "Real Estate": "XLRE",
}

# 티커만 바뀐 같은 회사 — 위키 이력의 옛 티커를 yfinance 의 현재 티커 시세로 잇는다
# (옛 티커로는 이력 전체가 사라져 생존 편향에 잘못 섞이던 것들)
YF_ALIASES = {"WLTW": "WTW", "GPS": "GAP", "FBHS": "FBIN", "HFC": "DINO",
              "ADS": "BFH", "ARNC": "HWM"}

_EULER_GAMMA = 0.5772156649015329
_ND = NormalDist()


def check_window(start: str, end: str, holdout: bool = False) -> tuple[str, str]:
    """평가 구간 가드. 봉인 구간(HOLDOUT_START~)에 닿으면 holdout=True 를 요구한다.

    end 는 포함(inclusive). 반환: 정규화된 (start, end) 'YYYY-MM-DD'."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if e < s:
        raise ValueError(f"평가 구간이 거꾸로입니다: {start} ~ {end}")
    if e >= pd.Timestamp(HOLDOUT_START) and not holdout:
        raise ValueError(f"봉인 구간({HOLDOUT_START}~)은 holdout=True 를 명시해야 엽니다 "
                         f"(요청: {start} ~ {end}). 개발은 {DEV_START}~{DEV_END} 에서.")
    return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")


def live_equivalent_kwargs(cfg: dict) -> dict:
    """라이브 설정(autotrader_config) → 라이브와 같은 과정으로 도는 run() 인자.

    라이브는 채점은 완성된 일봉(전일까지)으로, 체결은 장중 실시간 가격으로, 손절·트레일링은
    사이클마다(5분) 확인하고, 판단은 매일(실제로는 사이클마다) 다시 한다. 일봉으로 이 과정에
    가장 가까운 것: 다음 날 시가 체결 + 매일 일봉 고가·저가 손절 + 매일 결정.
    파라미터도 라이브 값 그대로 — 토너먼트 RUN_KW(트레일링 15%·EV 끔)와 다를 수 있다.
    ai_influencer 처럼 과거 시점을 재현할 수 없는 전략은 받지 않는다."""
    if cfg.get("strategy") == "ai_influencer":
        raise ValueError("ai_influencer 는 과거 판단을 재현할 수 없어 백테스트 대상이 아닙니다 "
                         "(influence_log + research/ai_forward_eval.py 로 forward 평가)")
    g = lambda k, d: cfg.get(k) if cfg.get(k) is not None else d   # noqa: E731
    return dict(
        min_score=float(g("min_score", 60)), sell_score=float(g("sell_score", 35)),
        stop_loss=float(g("stop_loss", 0.07)), take_profit=float(g("take_profit", 0.15)),
        trailing_stop=float(g("trail", 0.07)),
        hold_strong=int(g("hold_strong", 60)), hold_medium=int(g("hold_medium", 30)),
        dynamic=bool(g("dynamic", True)),
        ev_filter=bool(g("ev_enabled", True)),     # 라이브 core/cycle.py 기본값 = 켜짐
        use_trend_filter=False, adaptive_regime=True,
        execution="next_open", daily_stops=True, rebalance_days=1)


TIE_BREAKS = ("random", "dollar_volume")
TIE_DV_WINDOW = 20                # dollar_volume: 최근 N거래일 평균 거래대금


def tie_order(mode, stock_data: dict, seed=None):
    """동점 처리(연구용 번들 키 tie_break) → idx 를 받아 {티커: 보조 키(작을수록 앞)} 를
    돌려주는 함수. mode 가 None 이면 None — run() 이 예전처럼 입력 순서(안정 정렬)로 가른다.

    이산 점수 전략은 S&P 500 에서 수십~백수십 종목이 같은 점수라, 입력 순서(번들은
    알파벳순)로 가르면 'A' 로 시작하는 종목만 산다. 라이브는 이 함수를 쓰지 않는다 —
    core.scoring.rank 가 "random" 과 같은 의미(티커별 해시 키, ET 주마다 새 순서)로 가른다.
      "random"        : 리밸런싱 날마다 (seed, idx) 로 정한 무작위 순서. 같은 seed 면 재현되고,
                        종목 이름순으로 섞으므로 번들의 입력 순서와도 무관하다.
      "dollar_volume" : 최근 TIE_DV_WINDOW 거래일 평균 거래대금(종가×거래량) 큰 순 — 당일까지만
                        본다. 동점이면 대형·유동성주를 사는 셈이라 전략에 대형주 기울기가 생긴다."""
    if mode is None:
        return None
    names = sorted(stock_data)
    if mode == "random":
        base = int(seed or 0)

        def _random(idx: int) -> dict:
            perm = np.random.default_rng([base, int(idx)]).permutation(len(names))
            return dict(zip(names, perm.tolist()))
        return _random
    if mode == "dollar_volume":
        dv = {}
        for t in names:
            df = stock_data[t]
            if "Volume" in df.columns:
                v = (df["Close"] * df["Volume"]).rolling(TIE_DV_WINDOW, min_periods=1).mean()
                dv[t] = np.nan_to_num(v.to_numpy(dtype=float), nan=0.0)
            else:
                dv[t] = np.zeros(len(df))
        pos = {t: i for i, t in enumerate(names)}   # 거래대금까지 같으면 이름순(사실상 없음)

        def _dollar_volume(idx: int) -> dict:
            return {t: (-(a[idx] if idx < len(a) else 0.0), pos[t]) for t, a in dv.items()}
        return _dollar_volume
    raise ValueError(f"알 수 없는 tie_break: {mode!r} (가능: None, {', '.join(TIE_BREAKS)})")


def perf_stats(curve: pd.Series) -> dict:
    """자산(또는 가격) 곡선 → 총수익률·CAGR·MDD·샤프 (BacktestResult 와 같은 정의)."""
    c = pd.Series(curve, dtype=float).dropna()
    if len(c) < 2 or c.iloc[0] <= 0:
        return {"ret": 0.0, "cagr": 0.0, "mdd": 0.0, "sharpe": 0.0}
    growth = float(c.iloc[-1] / c.iloc[0])
    years = len(c) / 252
    rets = c.pct_change().dropna()
    sd = float(rets.std())
    return {"ret": growth - 1,
            "cagr": growth ** (1 / years) - 1 if growth > 0 else -1.0,
            "mdd": float(((c - c.cummax()) / c.cummax()).min()),
            "sharpe": float(rets.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0}


def benchmark_stats(spy_close, index) -> dict | None:
    """같은 날짜들(index)의 SPY 성과. spy_close 는 auto_adjust 수정주가(배당 재투자 근사)."""
    if spy_close is None or index is None or len(index) < 2:
        return None
    try:
        s = pd.Series(spy_close).reindex(index).ffill().dropna()
    except Exception:
        return None
    if len(s) < 2:
        return None
    out = perf_stats(s)
    out["start"] = str(pd.Timestamp(s.index[0]).date())
    out["end"] = str(pd.Timestamp(s.index[-1]).date())
    return out


def sharpe_moments(returns) -> dict:
    """일간 수익률 → 비연율 샤프·관측 수·왜도·첨도(정규=3). DSR 입력."""
    r = pd.Series(returns, dtype=float).dropna()
    T = len(r)
    sd = float(r.std()) if T > 1 else 0.0
    if T < 2 or sd == 0:
        return {"sr": 0.0, "T": T, "skew": 0.0, "kurt": 3.0}
    z = (r - r.mean()) / r.std(ddof=0)
    return {"sr": float(r.mean() / sd), "T": T,
            "skew": float((z ** 3).mean()), "kurt": float((z ** 4).mean())}


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """N 개 시도에서 '운만으로' 기대되는 최대 샤프 SR0 (Bailey & López de Prado 2014).

    SR0 = √V[SR] · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))),  γ = 오일러-마스케로니 상수.
    sr_var 는 시도들 샤프(비연율)의 분산. N ≤ 1 이면 고른 게 없으니 0."""
    n = int(n_trials or 0)
    if n <= 1 or not sr_var or sr_var <= 0:
        return 0.0
    return math.sqrt(sr_var) * ((1 - _EULER_GAMMA) * _ND.inv_cdf(1 - 1 / n)
                                + _EULER_GAMMA * _ND.inv_cdf(1 - 1 / (n * math.e)))


def probabilistic_sharpe(sr: float, sr_star: float, T: int, skew: float, kurt: float) -> float:
    """PSR: 참 샤프가 sr_star 보다 클 확률 (비연율 샤프, 왜도·첨도(정규=3) 반영)."""
    if T < 2:
        return 0.0
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if den <= 0:
        return 0.0
    return _ND.cdf((sr - sr_star) * math.sqrt(T - 1) / math.sqrt(den))


def deflated_sharpe(sr: float, T: int, skew: float, kurt: float,
                    n_trials: int, sr_var: float) -> dict:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    입력은 비연율(일간) 샤프. 반환:
      dsr         : PSR(SR0) — 'N 번 시도 중 운으로 나온 최대치'를 넘는 실력일 확률 (0~1)
      sr0         : 운으로 기대되는 최대 샤프 (연율)
      sr_deflated : 연율 샤프 − SR0. > 0 ⇔ dsr > 0.5 (운 보정 후에도 양(+)의 실력)"""
    sr0 = expected_max_sharpe(n_trials, sr_var)
    return {"dsr": probabilistic_sharpe(sr, sr0, T, skew, kurt),
            "sr0": sr0 * math.sqrt(252),
            "sr_deflated": (sr - sr0) * math.sqrt(252)}


def cluster_by_correlation(corr: pd.DataFrame, threshold: float = FAMILY_CORR) -> dict:
    """상관행렬 → 평균 연결 계층 군집(거리 1−ρ). 두 군집 간 평균 상관이 threshold
    이상인 동안 합친다. 반환: {키: 계열 번호(1부터, 큰 계열 먼저)}."""
    keys = list(corr.columns)
    c = corr.reindex(index=keys, columns=keys).astype(float).fillna(0.0)
    for k in keys:
        c.loc[k, k] = 1.0
    clusters = [[k] for k in keys]
    while len(clusters) > 1:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = float((1.0 - c.loc[clusters[i], clusters[j]].to_numpy()).mean())
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best[0] > 1.0 - threshold + 1e-12:
            break
        _, i, j = best
        clusters[i] = clusters[i] + clusters[j]
        del clusters[j]
    clusters.sort(key=lambda g: (-len(g), keys.index(g[0])))
    return {k: n for n, g in enumerate(clusters, 1) for k in g}


def strategy_families(returns: pd.DataFrame, threshold: float = FAMILY_CORR):
    """전략별 일간 수익률(열=전략) → (상관행렬, {전략: 계열 번호}).
    거래가 없어 수익률이 상수인 전략은 상관이 정의되지 않아 단독 계열이 된다."""
    corr = returns.corr()
    return corr, cluster_by_correlation(corr, threshold)


def summarize_results(results: dict, n_trials: int | None = None,
                      sr_var: float | None = None,
                      family_threshold: float = FAMILY_CORR) -> dict:
    """{전략: BacktestResult} → 지표 행(SPY·DSR·계열 포함)·상관행렬·계열.

    n_trials: DSR 의 시도 수 N (기본: 결과 수). 다른 유니버스·설정까지 합쳐 시도했다면
              그 전체 수를 넘겨라 — 시도를 숨길수록 DSR 이 부풀려진다.
    sr_var  : 시도들 샤프(비연율) 분산 (기본: 이 결과들의 분산)."""
    if not results:
        return {"rows": [], "corr": pd.DataFrame(), "families": {}, "benchmark": None}
    rets = pd.DataFrame({k: r.daily_returns for k, r in results.items()})
    mom = {k: sharpe_moments(rets[k]) for k in rets.columns}
    srs = [m["sr"] for m in mom.values()]
    if sr_var is None:
        sr_var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    n = int(n_trials or len(results))
    corr, fam = strategy_families(rets, family_threshold)
    bench = next((r.benchmark for r in results.values() if r.benchmark), None)
    rows = []
    for k, r in results.items():
        m = mom[k]
        d = deflated_sharpe(m["sr"], m["T"], m["skew"], m["kurt"], n, sr_var)
        b = r.benchmark or {}
        rows.append({
            "key": k, "ret": r.total_return, "cagr": r.cagr, "mdd": r.mdd,
            "sharpe": r.sharpe, "win": r.win_rate, "eq": float(r.equity_curve.iloc[-1]),
            "trades": len(r.trades),
            "spy_ret": b.get("ret"), "spy_cagr": b.get("cagr"),
            "spy_mdd": b.get("mdd"), "spy_sharpe": b.get("sharpe"),
            "skew": m["skew"], "kurt": m["kurt"], "n_obs": m["T"],
            "dsr": d["dsr"], "sr0": d["sr0"], "sr_deflated": d["sr_deflated"],
            "n_trials": n, "family": fam.get(k),
            "corr": {o: round(float(corr.loc[k, o]), 3) for o in corr.columns
                     if o != k and corr.loc[k, o] == corr.loc[k, o]},
        })
    return {"rows": rows, "corr": corr, "families": fam, "benchmark": bench,
            "returns": rets, "sr_var": sr_var, "n_trials": n}


# ── 연구용 데이터 번들 (디스크 캐시) ────────────────────────────────────────────

def _save_df(df: pd.DataFrame, path: Path):
    try:
        df.to_parquet(path.with_suffix(".parquet"))
    except Exception:                       # pyarrow 없음 → pickle (자체 생성 캐시)
        df.to_pickle(path.with_suffix(".pkl"))


def _load_df(path: Path):
    for suf, fn in ((".parquet", pd.read_parquet), (".pkl", pd.read_pickle)):
        p = path.with_suffix(suf)
        if p.exists():
            try:
                return fn(p)
            except Exception:
                return None
    return None


def _extract(raw: pd.DataFrame, t: str, multi: bool):
    try:
        df = raw.xs(t, axis=1, level=1) if multi else raw
    except Exception:
        return None
    cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    if "Close" not in cols:
        return None
    df = df[cols].dropna(how="all")
    return df if df["Close"].notna().sum() > 30 else None


def download_prices(tickers: list[str], start: str, end_excl: str,
                    chunk: int = 80, retry_missing: bool = False) -> tuple[dict, list]:
    """일봉(auto_adjust 수정주가) 대량 다운로드 + 디스크 캐시.

    캐시: CACHE_DIR/prices/<start>_<end_excl>/<티커>.parquet — 구간이 키라서 개발 구간
    캐시에는 봉인 구간 데이터가 들어가지 않는다. 받지 못한 티커(상장폐지 등)는
    _missing.json 에 기록해 재시도하지 않는다(retry_missing=True 면 재시도).
    반환: ({티커: DataFrame}, [못 받은 티커])"""
    d = CACHE_DIR / "prices" / f"{start}_{end_excl}"
    d.mkdir(parents=True, exist_ok=True)
    miss_file = d / "_missing.json"
    known_missing = set()
    if miss_file.exists() and not retry_missing:
        try:
            known_missing = set(json.loads(miss_file.read_text()))
        except Exception:
            pass
    out: dict = {}
    todo = []
    for t in dict.fromkeys(tickers):
        df = _load_df(d / t)
        if df is not None:
            out[t] = df
        elif t not in known_missing:
            todo.append(t)

    def _keep(t, df):
        df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
        out[t] = df
        _save_df(df, d / t)

    # 1차: 대량 다운로드(빠름). yfinance 1.x 는 실패 원인을 돌려주지 않으므로
    for i in range(0, len(todo), chunk):
        batch = todo[i:i + chunk]
        try:
            raw = yf.download(batch, start=start, end=end_excl, auto_adjust=True,
                              progress=False, threads=True)
        except Exception:
            continue
        if raw is None or raw.empty:
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for t in batch:
            df = _extract(raw, t, multi)
            if df is not None:
                _keep(t, df)

    # 2차: 실패분만 종목별로 — '없는 종목'(상장폐지)과 일시 오류(레이트리밋)를 구분한다.
    # 없는 종목만 _missing.json 에 남기고, 일시 오류는 다음 실행에서 다시 시도한다.
    from yfinance import exceptions as _yfe
    confirmed = set()
    for t in [t for t in todo if t not in out]:
        for attempt in range(3):
            try:
                h = yf.Ticker(t).history(start=start, end=end_excl, auto_adjust=True,
                                         raise_errors=True)
                df = _extract(h, t, False) if h is not None else None
                if df is not None:
                    _keep(t, df)
                else:
                    confirmed.add(t)
                break
            except _yfe.YFTickerMissingError:
                confirmed.add(t)
                break
            except Exception:                 # 레이트리밋·네트워크 → 잠시 후 재시도
                time.sleep(3 * (attempt + 1))
    missing = sorted(t for t in dict.fromkeys(tickers) if t not in out)
    try:
        miss_file.write_text(json.dumps(sorted(known_missing | confirmed)))
    except Exception:
        pass
    return out, missing


def load_sp500_snapshot(max_age_days: int = 30) -> dict:
    """S&P 500 현재 구성·GICS 섹터·변경 이력 스냅샷(디스크 캐시, 기본 30일)."""
    import universe as _uni
    p = CACHE_DIR / "sp500_snapshot.json"
    if p.exists() and time.time() - p.stat().st_mtime < max_age_days * 86400:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    snap = _uni.fetch_sp500_snapshot()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False))
    return snap


def _align(df: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """공통 거래일 축에 맞춘다(위치 인덱스 정렬 보장). 상장 전은 NaN 그대로,
    상장 후 결측(거래정지·상장폐지 이후)은 직전 가격 유지·거래량 0."""
    a = df.reindex(index)
    first = a["Close"].first_valid_index()
    if first is None:
        return a
    after = a.index >= first
    px_cols = [c for c in ("Open", "High", "Low", "Close") if c in a.columns]
    a.loc[after, px_cols] = a.loc[after, px_cols].ffill()
    if "Volume" in a.columns:
        a.loc[after, "Volume"] = a.loc[after, "Volume"].fillna(0)
    return a


def load_research_bundle(universe="sp500", start: str = DEV_START, end: str = DEV_END,
                         holdout: bool = False, warmup_days: int = WARMUP_CAL_DAYS,
                         min_history: int = MIN_HISTORY_BARS) -> dict:
    """정직한 평가용 번들 — run(prefetched=...) / evaluate 에 그대로 넘긴다.

    universe: "sp500"     — universe.py 의 S&P 500 구성 + 변경 이력으로 시점별 편입 근사
              "watchlist" — config 의 손으로 고른 종목(TRADEABLE_UNIVERSE, 선택 편향 큼)
              list        — 임의 티커
    load_market_data 와 달리 (1) 봉인 구간 가드, (2) 워밍업 후 start 부터 평가,
    (3) 모든 종목을 SPY 거래일 축에 정렬(상장 시점이 달라도 위치 인덱스 일치),
    (4) 디스크 캐시. meta 에 생존 편향 정보(받지 못한 과거 구성종목)를 담는다."""
    s, e = check_window(start, end, holdout)
    end_excl = (pd.Timestamp(e) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    warm = (pd.Timestamp(s) - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")

    spy_px, _ = download_prices(["SPY"], warm, end_excl)
    if "SPY" not in spy_px:
        raise ValueError("SPY 데이터를 받지 못했습니다")
    spy_full = spy_px["SPY"]["Close"].astype(float)
    date_index = pd.DatetimeIndex(spy_full.index)
    eval_start_idx = int(date_index.searchsorted(pd.Timestamp(s)))
    if eval_start_idx >= len(date_index) - 1:
        raise ValueError("평가 구간에 거래일이 없습니다")

    meta: dict = {"universe": universe if isinstance(universe, str) else "custom",
                  "start": s, "end": e, "warmup_from": warm,
                  "eval_start": str(date_index[eval_start_idx].date()),
                  "eval_end": str(date_index[-1].date()), "holdout": bool(holdout)}
    members = None
    sector_map: dict = {}
    if universe == "sp500":
        snap = load_sp500_snapshot()
        import universe as _uni
        mem = _uni.sp500_membership(snap["members"], snap["changes"], date_index)
        ev = slice(eval_start_idx, None)
        tickers = [t for t, m in mem.items() if any(m[ev])]
        members = {t: np.array(mem[t], dtype=bool) for t in tickers}
        sector_map = {t: GICS_TO_ETF[g] for t, g in snap.get("sectors", {}).items()
                      if g in GICS_TO_ETF}
        n_changes = sum(1 for c in snap["changes"] if s <= c[0] <= e)
        meta.update(snapshot=snap.get("fetched"), changes_in_window=n_changes,
                    current_members=len(snap["members"]))
    elif universe == "watchlist":
        tickers = list(TRADEABLE_UNIVERSE)
    else:
        tickers = list(universe)

    src = {t: YF_ALIASES.get(t, t) for t in tickers}
    px, _ = download_prices(sorted(set(src.values()) | SECTOR_ETFS), warm, end_excl)
    stock_data, n_valid = {}, {}
    for t in tickers:
        if src[t] not in px:
            continue
        a = _align(px[src[t]], date_index)
        if a["Close"].iloc[eval_start_idx:].notna().sum() < 2:
            continue
        stock_data[t] = a
        n_valid[t] = a["Close"].notna().cumsum().to_numpy()
    etf_data = {t: _align(px[t], date_index) for t in SECTOR_ETFS if t in px}
    if not stock_data:
        raise ValueError("다운로드된 종목 데이터가 없습니다")

    miss_stocks = sorted(t for t in tickers if t not in stock_data)
    meta.update(requested=len(tickers), loaded=len(stock_data), missing=miss_stocks,
                aliases={t: a for t, a in src.items() if a != t})
    if members is not None:
        # 생존 편향 측정: 구성종목이던 날(평가 구간) 중 시세가 있는 날의 비율
        ev = slice(eval_start_idx, None)
        tot = cov = 0
        thin = []
        for t, m in members.items():
            mdays = int(m[ev].sum())
            have = (int((m[ev] & stock_data[t]["Close"].iloc[ev].notna().to_numpy()).sum())
                    if t in stock_data else 0)
            tot += mdays; cov += have
            if mdays and have / mdays < 0.5:
                thin.append(t)
        meta.update(member_day_coverage=cov / tot if tot else 1.0,
                    effectively_missing=sorted(thin))
        members = {t: m for t, m in members.items() if t in stock_data}
    return {"stock_data": stock_data, "etf_data": etf_data, "date_index": date_index,
            "spy_close": spy_full,
            "spy_ma200": spy_full.rolling(200, min_periods=100).mean(),
            "spy_ma50": spy_full.rolling(50, min_periods=25).mean(),
            "eval_start_idx": eval_start_idx, "members": members, "n_valid": n_valid,
            "min_history": int(min_history), "sector_map": sector_map, "meta": meta}
