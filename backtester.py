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

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import date, timedelta
from dataclasses import dataclass, field

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
            "=" * 55,
        ]
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
    """
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

        for idx, today in enumerate(date_index):
            # --- 일일 평가(mark-to-market) ---
            pv = cash
            for t, pos in pmbt.positions.items():
                df = stock_data.get(t)
                if df is not None and idx < len(df):
                    pv += float(df["Close"].iloc[idx]) * pos.shares
            equity_curve.append(pv)

            if idx % REBAL != 0:
                continue

            # 가상 '오늘' 주입 — 보유기간/재진입 쿨다운이 시뮬 날짜 기준으로 계산되게
            portfolio.set_clock(today.date())

            # 현재가 + 점수 (점수는 라이브 자동매매와 동일한 _strategy_score_bt)
            prices: dict[str, float] = {}
            scores: list[StockScore] = []
            for t, df in stock_data.items():
                if idx >= len(df):
                    continue
                prices[t] = float(df["Close"].iloc[idx])
                try:
                    sc = _strategy_score_bt(strategy, t, stock_data, etf_data, idx)
                except Exception:
                    sc = 0.0
                scores.append(StockScore(t, float(sc), 0, 0, 0, 0))
            scores.sort(key=lambda s: -s.total)

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

            # --- 체결: 매도 먼저(자본 회수) → 매수, 수수료·슬리피지 현금 반영 ---
            for o in orders.get("sell", []):
                t = o["ticker"]
                pos = pmbt.positions.get(t)
                if pos is None:
                    continue
                qty = int(o["shares"])
                if qty < 1:
                    continue
                fill = float(o["est_price"]) * (1 - slippage)
                proceeds = fill * qty
                cash += proceeds - proceeds * commission
                pmbt.record_sell(t, exit_price=fill, reason=o["reason"], shares=qty)

            for o in orders.get("buy", []):
                t = o["ticker"]
                qty = int(o["shares"])
                if qty < 1:
                    continue
                fill = float(o["est_price"]) * (1 + slippage)
                cost = fill * qty
                if cost + cost * commission > cash + 1e-6:
                    # 슬리피지·수수료로 예산 초과 → 가능한 수량으로 축소
                    qty = int(cash / (fill * (1 + commission)))
                    if qty < 1:
                        continue
                    cost = fill * qty
                cash -= cost + cost * commission
                pmbt.record_buy(t, qty, fill, o.get("score", 0))

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

    equity_series = pd.Series(equity_curve, index=date_index)
    return BacktestResult(trades=trades, equity_curve=equity_series, start_capital=capital)
