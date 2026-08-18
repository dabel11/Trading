"""
AI 트레이딩 대시보드 — 토스 스타일 클린 버전
streamlit run app.py
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import threading, time, json
from datetime import datetime, date
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import paper_account as _paper   # 모의투자 가상 현금 (Alpaca 키 불필요)

# 파일 디스크립터 한도 상향 — yfinance/실시간 피드가 소켓·세션을 많이 열어
# macOS 기본 소프트 한도(256)를 넘으면 "[Errno 24] Too many open files" 발생.
try:
    import resource as _res
    _soft, _hard = _res.getrlimit(_res.RLIMIT_NOFILE)
    _want = 8192 if _hard == _res.RLIM_INFINITY else min(_hard, 8192)
    if _soft < _want:
        _res.setrlimit(_res.RLIMIT_NOFILE, (_want, _hard))
except Exception:
    pass

try:
    from streamlit_autorefresh import st_autorefresh
    _AR = True
except ImportError:
    _AR = False

# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="AI 트레이딩", page_icon="📈",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

*, *::before, *::after { box-sizing: border-box; }

:root {
  --bg0: #0B0B0F;
  --bg1: #111116;
  --bg2: #16161C;
  --bg3: #1C1C23;
  --bg4: #24242D;
  --line: #20202A;
  --line2: #2A2A35;
  --t1: #F2F3F5;
  --t2: #9099A6;
  --t3: #565E6B;
  --up: #F0454F;
  --dn: #3B82F6;
  --blue: #3B82F6;
  --green: #0FB873;
}

html, body, [class*="css"] {
  font-family: 'Pretendard', -apple-system, sans-serif !important;
  -webkit-font-smoothing: antialiased;
  background: var(--bg0) !important;
  color: var(--t1) !important;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
  letter-spacing: -.01em;
}
/* 시세·수치는 고정폭 정렬 (프로 터미널 룩) */
.kpi-v, .iprice, .ichg, .sticker, .mono { font-variant-numeric: tabular-nums; }

.stApp { background: var(--bg0) !important; }
.main .block-container { padding: 0 14px 14px !important; max-width: 100% !important; }
header[data-testid="stHeader"] { background: var(--bg1) !important;
  border-bottom: 1px solid var(--line) !important; height: 48px !important; }
#MainMenu, footer, .stDeployButton { visibility: hidden !important; }

/* 사이드바 */
[data-testid="stSidebar"] {
  background: var(--bg1) !important;
  border-right: 1px solid var(--line) !important;
  width: 200px !important;
  min-width: 200px !important;
}
[data-testid="stSidebar"] .block-container { padding: 0 !important; }
[data-testid="stSidebar"] * { color: var(--t2) !important; }
[data-testid="stSidebarNav"] { display: none !important; }

/* 사이드바 라디오 → 메뉴 */
[data-testid="stSidebar"] [data-testid="stRadio"] > div {
  gap: 1px !important;
  padding: 8px 0 !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label {
  background: transparent !important;
  border: none !important;
  border-radius: 0 !important;
  padding: 6px 18px !important;
  font-size: .82rem !important;
  font-weight: 500 !important;
  color: var(--t3) !important;
  cursor: pointer !important;
  display: block !important;
  transition: all .1s !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
  background: var(--bg3) !important;
  color: var(--t2) !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {
  background: var(--bg3) !important;
  color: var(--t1) !important;
  border-left: 2px solid var(--blue) !important;
  padding-left: 18px !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label p {
  font-size: .86rem !important;
  font-weight: 500 !important;
}
/* 사이드바 라디오 동그라미 완전 제거 */
[data-testid="stSidebar"] [data-testid="stRadio"] label > div:first-child,
[data-testid="stSidebar"] [data-testid="stRadio"] label [data-baseweb="radio"] > div:first-child,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radio"] {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  border: none !important;
}

/* 사이드바 토글 */
[data-testid="stSidebar"] [data-testid="stToggle"] > label {
  font-size: .82rem !important; color: var(--t2) !important;
}
[data-testid="stToggle"] > label > div[data-checked="true"] {
  background: var(--blue) !important;
}

/* 카드 — 고밀도 */
.card {
  background: var(--bg2);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 12px 14px;
  margin-bottom: 7px;
  transition: border-color .12s ease;
}
.card:hover { border-color: var(--line2); }
.card-xs {
  background: var(--bg2);
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 8px 11px;
  margin-bottom: 5px;
  transition: border-color .12s ease;
}
.card-xs:hover { border-color: var(--line2); }

/* KPI — 고밀도 */
.kpi {
  background: var(--bg2);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px 12px;
  transition: border-color .12s ease;
}
.kpi:hover { border-color: var(--line2); }
.kpi-l { font-size: .61rem; color: var(--t3); font-weight: 600;
          letter-spacing: .05em; text-transform: uppercase; margin-bottom: 5px; }
.kpi-v { font-size: 1.22rem; font-weight: 800; letter-spacing: -.03em; line-height: 1; }
.kpi-s { font-size: .66rem; color: var(--t2); margin-top: 3px; }

/* 지수 바 — 고밀도 티커테이프 */
.ibar {
  display: flex; overflow-x: auto; border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: var(--bg1); scrollbar-width: none; margin-bottom: 12px;
}
.ibar::-webkit-scrollbar { display: none; }
.iitem {
  display: flex; flex-direction: column; gap: 1px;
  padding: 6px 14px; border-right: 1px solid var(--line);
  flex-shrink: 0; cursor: default;
}
.iitem:last-child { border-right: none; }
.iname { font-size: .58rem; color: var(--t3); font-weight: 600;
         letter-spacing: .03em; text-transform: uppercase; }
.iprice { font-size: .82rem; font-weight: 700; color: var(--t1); }
.ichg { font-size: .64rem; font-weight: 700; }

/* 종목 행 — 고밀도 */
.srow {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0; border-bottom: 1px solid var(--line);
}
.srow:last-child { border-bottom: none; }
.sticker { font-weight: 700; font-size: .84rem; }
.ssub { font-size: .68rem; color: var(--t3); margin-top: 1px; }

/* 섹션 제목 */
.stitle { font-size: .86rem; font-weight: 800; letter-spacing: -.02em;
          margin-bottom: 9px; color: var(--t1); }
.sdesc { font-size: .72rem; color: var(--t3); margin-bottom: 8px; }

/* 뱃지 — 샤프 */
.bu { background: rgba(240,68,82,.1); color: #F04452; border-radius: 3px;
      padding: 1px 6px; font-size: .63rem; font-weight: 700; display: inline-block; }
.bd { background: rgba(47,128,237,.1); color: #2F80ED; border-radius: 3px;
      padding: 1px 6px; font-size: .63rem; font-weight: 700; display: inline-block; }
.bg_ { background: rgba(5,192,114,.1); color: #05C072; border-radius: 3px;
       padding: 1px 6px; font-size: .63rem; font-weight: 700; display: inline-block; }
.bn { background: var(--bg4); color: var(--t2); border-radius: 3px;
      padding: 1px 6px; font-size: .63rem; font-weight: 700; display: inline-block; }
.bb_ { background: rgba(49,130,246,.1); color: #3182F6; border-radius: 3px;
       padding: 1px 6px; font-size: .63rem; font-weight: 700; display: inline-block; }

/* 버튼 — 모던 핀테크 */
.stButton > button {
  background: linear-gradient(180deg, #3F8CFF 0%, #2E6FE0 100%) !important;
  color: #fff !important;
  border: 1px solid rgba(255,255,255,.10) !important;
  border-radius: 9px !important;
  font-weight: 700 !important; font-size: .83rem !important;
  padding: 9px 17px !important; width: 100% !important;
  font-family: 'Pretendard', sans-serif !important;
  letter-spacing: -.01em !important;
  box-shadow: 0 1px 2px rgba(0,0,0,.30), 0 3px 10px rgba(46,111,224,.28) !important;
  transition: transform .08s ease, box-shadow .14s ease, filter .14s ease, background .14s ease !important;
}
.stButton > button:hover {
  filter: brightness(1.05) !important; transform: translateY(-1px) !important;
  box-shadow: 0 2px 4px rgba(0,0,0,.35), 0 6px 18px rgba(46,111,224,.42) !important;
}
.stButton > button:active { transform: translateY(0) !important; filter: brightness(.96) !important; }
.stButton > button:disabled {
  background: var(--bg3) !important; color: var(--t3) !important;
  border-color: var(--line) !important; box-shadow: none !important; transform: none !important;
}
/* secondary = 톤다운 (테두리형) */
.stButton > button[kind="secondary"] {
  background: var(--bg3) !important; color: var(--t1) !important;
  border: 1px solid var(--line2) !important;
  box-shadow: 0 1px 2px rgba(0,0,0,.22) !important;
}
.stButton > button[kind="secondary"]:hover {
  background: var(--bg4) !important; border-color: rgba(63,140,255,.55) !important;
  color: #fff !important;
  box-shadow: 0 2px 10px rgba(0,0,0,.30) !important; transform: translateY(-1px) !important;
}
/* tertiary = 평평한 링크형 (랭킹 행 클릭용) */
.stButton > button[kind="tertiary"] {
  background: transparent !important; color: var(--t1) !important;
  border: none !important; box-shadow: none !important;
  text-align: left !important; padding: 4px 2px !important;
  font-weight: 700 !important; font-size: .8rem !important; width: 100% !important;
}
.stButton > button[kind="tertiary"]:hover {
  background: transparent !important; color: var(--blue) !important; filter: none !important;
}

/* 탭 — 샤프 */
[data-testid="stTabs"] {
  background: var(--bg3); border-radius: 5px; padding: 2px; margin-bottom: 9px;
}
[data-testid="stTabs"] button {
  color: var(--t3) !important; font-weight: 600 !important; border: none !important;
  background: transparent !important; padding: 5px 12px !important;
  border-radius: 4px !important; font-size: .78rem !important;
}
[data-testid="stTabs"] button:hover { color: var(--t2) !important; }
[data-testid="stTabs"] button[aria-selected="true"] {
  color: var(--t1) !important; background: var(--bg2) !important;
  box-shadow: 0 1px 4px rgba(0,0,0,.4) !important;
}

/* 인풋 */
.stTextInput > div > div > input,
.stNumberInput > div > div > input {
  background: var(--bg3) !important; color: var(--t1) !important;
  border: 1px solid var(--line2) !important; border-radius: 5px !important;
  font-size: .82rem !important;
}
.stTextInput > div > div > input:focus {
  border-color: var(--blue) !important;
  box-shadow: 0 0 0 2px rgba(49,130,246,.14) !important;
}
.stSelectbox > div > div { background: var(--bg3) !important;
  border: 1px solid var(--line2) !important; border-radius: 5px !important; }
.stMultiSelect > div > div { background: var(--bg3) !important;
  border: 1px solid var(--line2) !important; border-radius: 5px !important; }
[data-testid="stSlider"] > div > div > div { background: var(--blue) !important; }
[data-testid="stRadio"] label {
  background: var(--bg3) !important; border: 1px solid var(--line) !important;
  border-radius: 5px !important; padding: 5px 10px !important;
}
[data-testid="stRadio"] label:has(input:checked) {
  border-color: var(--blue) !important; background: rgba(49,130,246,.07) !important;
}

/* 알림 — 컴팩트 */
.ok   { background: rgba(5,192,114,.07); border: 1px solid #05C072;
        border-radius: 5px; padding: 8px 11px; color: #05C072;
        font-size: .78rem; margin-bottom: 5px; }
.fail { background: rgba(240,68,82,.07); border: 1px solid #F04452;
        border-radius: 5px; padding: 8px 11px; color: #F04452;
        font-size: .78rem; margin-bottom: 5px; }

/* 로그 */
.logline { font-size: .74rem; padding: 2px 0; border-bottom: 1px solid var(--line);
           font-family: 'SF Mono', 'Consolas', monospace; }

/* 구분선 */
hr { border: none; border-top: 1px solid var(--line); margin: 6px 0; }

/* 스크롤바 */
::-webkit-scrollbar { width: 3px; height: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--line2); border-radius: 3px; }

/* 데이터프레임 */
.stDataFrame { background: var(--bg2) !important; }
[data-testid="stDataFrame"] * { color: var(--t1) !important; }

/* ── 고밀도: 위젯 간 간격 축소 (프로 터미널 룩) ───────────────────────── */
[data-testid="stVerticalBlock"]   { gap: .42rem !important; }
[data-testid="stHorizontalBlock"] { gap: .5rem  !important; }
[data-testid="stElementContainer"] { margin: 0 !important; }
[data-testid="stMetric"] { padding: 6px 10px !important; }
[data-testid="stExpander"] summary { padding: 6px 10px !important; font-size: .8rem !important; }
[data-testid="stExpander"] details { border-radius: 5px !important; }
[data-testid="stHeading"] { margin-bottom: .2rem !important; }
.stPlotlyChart { margin: 0 !important; }
[data-testid="stToggle"] label { font-size: .8rem !important; }
[data-testid="stCaptionContainer"] { font-size: .68rem !important; }
/* 스크롤바 좀 더 얇게 */
::-webkit-scrollbar { width: 2px; height: 2px; }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────────────────────
# 차트
# ──────────────────────────────────────────────────────────────────────────────
_CL = dict(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
           font=dict(family="Pretendard,sans-serif", color="#4A5260", size=11),
           hovermode="x unified", margin=dict(l=0,r=0,t=6,b=0),
           hoverlabel=dict(bgcolor="#141419", bordercolor="#1E1E27",
                           font=dict(color="#ECEEF1", size=11)))
_XA = dict(gridcolor="#141419", showgrid=True, zeroline=False, tickfont=dict(size=10))
_YA = dict(gridcolor="#141419", showgrid=True, zeroline=False, tickfont=dict(size=10))

def CL(**ov):
    b = {**_CL, "xaxis": _XA, "yaxis": _YA}
    b.update(ov); return b

# ──────────────────────────────────────────────────────────────────────────────
# 세션
# ──────────────────────────────────────────────────────────────────────────────
_DEF = dict(page="대시보드", scan_results=None, scan_running=False, scan_ts=None,
            scan_strategy="composite", notify_cfg=None, period="6M",
            market_info=None, live_log=[], live_running=False,
            bt_result=None, bt_cap=10000, bt_strategy="composite",
            active_strategy="composite", selected_stock=None,
            alpaca_acct=None, tf_sel="일", auto_enabled=False,
            buy_mode="분할", sell_mode="전량", split_n=3,
            buy_pct=50, sell_pct=50, horizon="단기",
            currency="USD", auto_last_run=0)
for k, v in _DEF.items():
    if k not in st.session_state: st.session_state[k] = v

if st.session_state["notify_cfg"] is None:
    import notifier
    st.session_state["notify_cfg"] = notifier.load_config()

# ── 매매 규칙 + 가중치 앱 시작 시 자동 로드 (한 번만) ────────────────────────
# ── 실시간 가격 피드 초기화 (앱 첫 로드 시 한 번) ───────────────────────────
if not st.session_state.get("_feed_started"):
    try:
        import realtime_feed as _rtf0, watchlist as _wl0, config as _cfg_rt
        from portfolio import PortfolioManager as _PM0
        _feed_tickers = list(set(
            _wl0.load() +
            list(_PM0().positions.keys()) +
            ["SPY", "QQQ", "^VIX"]
        ))
        _rtf0.init_from_config(_feed_tickers, interval=5.0)
    except Exception:
        pass
    st.session_state["_feed_started"] = True

if not st.session_state.get("_rules_applied"):
    _rf = Path(__file__).parent / "rules_config.json"
    if _rf.exists():
        try:
            import config as _cfg0
            _d0 = json.loads(_rf.read_text())
            _cfg0.CAPITAL_TOTAL    = _d0.get("capital_total",    _cfg0.CAPITAL_TOTAL)
            _cfg0.MAX_POSITIONS    = _d0.get("max_positions",    _cfg0.MAX_POSITIONS)
            _cfg0.MAX_POSITION_PCT = _d0.get("max_position_pct", _cfg0.MAX_POSITION_PCT)
            _cfg0.MIN_SCORE_TO_BUY = _d0.get("min_score_to_buy", _cfg0.MIN_SCORE_TO_BUY)
            _cfg0.STOP_LOSS_PCT    = _d0.get("stop_loss_pct",    _cfg0.STOP_LOSS_PCT)
            _cfg0.TAKE_PROFIT_PCT  = _d0.get("take_profit_pct",  _cfg0.TAKE_PROFIT_PCT)
            if "signal_weights" in _d0:
                _cfg0.SIGNAL_WEIGHTS = _d0["signal_weights"]
        except Exception:
            pass
    st.session_state["_rules_applied"] = True

from config import CAPITAL_TOTAL
import strategies as strat_mod

# ──────────────────────────────────────────────────────────────────────────────
# 투자 기간 모드 (horizon) — 보유기간·손절·익절·점수 프리셋
#   라이브(portfolio 모듈값) + 백테스트(run 파라미터) 둘 다 이 값을 따른다.
# ──────────────────────────────────────────────────────────────────────────────
HORIZONS = {
    "장기":   dict(label="1년 이상",   days=(250, 400), hold_strong=400, hold_medium=250,
                  stop_loss=0.18, take_profit=0.50, min_score=68, sell_score=30, rebalance_days=20, trail=0.12),
    "중장기": dict(label="6개월 이상", days=(120, 180), hold_strong=180, hold_medium=120,
                  stop_loss=0.12, take_profit=0.30, min_score=63, sell_score=33, rebalance_days=10, trail=0.10),
    "단기":   dict(label="2주~3개월",  days=(14, 63),   hold_strong=60,  hold_medium=30,
                  stop_loss=0.07, take_profit=0.15, min_score=60, sell_score=35, rebalance_days=5, trail=0.07),
    "단타":   dict(label="며칠 이내",  days=(1, 5),     hold_strong=5,   hold_medium=2,
                  stop_loss=0.03, take_profit=0.06, min_score=55, sell_score=40, rebalance_days=1, trail=0.04),
}
HORIZON_ORDER = ["장기", "중장기", "단기", "단타"]


def horizon_params(name: str) -> dict:
    return HORIZONS.get(name, HORIZONS["단기"])


def apply_horizon_to_live(name: str):
    """선택한 기간 모드를 라이브 매매(portfolio 모듈 전역값)에 반영."""
    import portfolio as _pf
    p = horizon_params(name)
    _pf.STOP_LOSS_PCT        = p["stop_loss"]
    _pf.TAKE_PROFIT_PCT      = p["take_profit"]
    _pf.HOLD_DAYS_STRONG     = p["hold_strong"]
    _pf.HOLD_DAYS_MEDIUM     = p["hold_medium"]
    _pf.MIN_SCORE_TO_BUY     = p["min_score"]
    _pf.SELL_SCORE_THRESHOLD = p["sell_score"]
    _pf.TRAIL_GIVEBACK_PCT   = p.get("trail", 0.08)
    # 매수 가격대 제한 (전략 선택에서 설정, 0=무제한)
    _pf.BUY_PRICE_MIN = float(st.session_state.get("buy_price_min", 0) or 0)
    _pf.BUY_PRICE_MAX = float(st.session_state.get("buy_price_max", 0) or 0)


def _horizon_cb(wkey: str):
    st.session_state["horizon"] = st.session_state[wkey]


def render_horizon_picker(wkey: str, label: str = "투자 기간"):
    """백테스트·라이브 창 내부에서 쓰는 기간 모드 선택기. 전역 horizon과 동기화.
    on_change 콜백이 위젯→horizon을 먼저 갱신하므로, 매 렌더 위젯을 horizon에 맞춰도
    사용자의 방금 선택을 덮지 않는다(페이지 간 동기화도 일관)."""
    _cur = st.session_state.get("horizon", "단기")
    st.session_state[wkey] = _cur if _cur in HORIZON_ORDER else "단기"
    st.selectbox(label, HORIZON_ORDER, key=wkey, on_change=_horizon_cb, args=(wkey,),
                 format_func=lambda x: f"{x} · {HORIZONS[x]['label']}")
    _h = st.session_state[wkey]
    apply_horizon_to_live(_h)
    _hp = horizon_params(_h)
    st.caption(f"보유 ~{_hp['hold_strong']}일 · 손절 {_hp['stop_loss']:.0%} · "
               f"익절 {_hp['take_profit']:.0%}(고점 대비 {_hp.get('trail',0.08):.0%} 되돌리면 청산)")
    return _h



# 전략 카탈로그 (단일 소스) 기반
import strategy_catalog as scat
STRAT = {k: (v["name"], v["color"]) for k, v in scat.CATALOG.items()}
STRAT_CAT = scat.keys_by_category()
REASON_KR = {"take_profit":"익절","stop_loss":"손절",
             "score_drop":"신호 약화","max_hold":"기간 만료","end_of_backtest":"종료"}

def tc(t):  # ticker 색상
    cs = ["#3182F6","#F04452","#05C072","#FF9500","#A855F7","#06B6D4","#F59E0B","#EC4899"]
    return cs[sum(ord(c) for c in t) % len(cs)]

# ──────────────────────────────────────────────────────────────────────────────
# 데이터 (성능 최적화)
# ──────────────────────────────────────────────────────────────────────────────
import realtime_feed as _rtf

@st.cache_data(ttl=15, show_spinner=False)
def _batch_prices_yf(tickers: tuple) -> dict:
    """캐시 미스 종목용 단일 배치 다운로드 (15초 캐시, 블로킹 1회)."""
    import yfinance as _yf
    if not tickers: return {}
    out = {}
    try:
        raw = _yf.download(" ".join(tickers), period="2d", interval="1d",
                           auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        for t in tickers:
            try:
                s = (cl[t] if hasattr(cl, "columns") else cl).dropna()
                if len(s):
                    out[t] = float(s.iloc[-1])
            except Exception:
                pass
    except Exception:
        pass
    return out

def fetch_prices(tickers: tuple) -> dict:
    """
    실시간 피드 캐시 우선 (메모리, 즉시) → 미스만 15초 캐시 배치 폴백.
    동기 per-ticker yfinance 호출을 제거해 매 리런 블로킹을 없앤다.
    """
    if not tickers: return {}
    prices = {}
    missing = []
    for t in tickers:
        d = _rtf.get_price(t)
        if d and d.get("price"):
            prices[t] = d["price"]
        else:
            missing.append(t)

    if missing:
        # 백그라운드 피드에 구독 등록 → 다음 사이클부터 자동 갱신
        _rtf.subscribe(missing, interval=5.0)
        # 즉시 표시용: 15초 캐시 배치 (첫 호출만 네트워크, 이후 캐시)
        prices.update(_batch_prices_yf(tuple(sorted(missing))))

    return prices

_INDEX_SYMS = [("SPY","S&P 500"), ("QQQ","나스닥 100"), ("^DJI","다우존스"),
               ("^VIX","VIX"), ("^TNX","미국채 10Y"), ("IWM","러셀 2000"),
               ("HYG","하이일드"), ("DX-Y.NYB","달러인덱스"),
               ("^KS11","코스피"), ("^KQ11","코스닥"), ("KRW=X","원/달러")]

@st.cache_data(ttl=20, show_spinner=False)
def _index_fallback(syms: tuple) -> dict:
    """피드 미스 지수의 배치 폴백 (20초 캐시)."""
    import yfinance as _yf2
    if not syms: return {}
    out = {}
    try:
        raw = _yf2.download(list(syms), period="5d", interval="1d",
                            auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        for sym in syms:
            try:
                s = (cl[sym] if hasattr(cl,"columns") else cl).dropna()
                price = float(s.iloc[-1])
                prev  = float(s.iloc[-2]) if len(s) >= 2 else price
                out[sym] = dict(price=price, chg=(price-prev)/prev if prev else 0)
            except Exception:
                pass
    except Exception:
        pass
    return out

def fetch_index_bar():
    """지수 현재가 + 전일 종가 — 실시간 피드 우선(즉시), 미스만 캐시 폴백."""
    _rtf.subscribe([s for s,_ in _INDEX_SYMS], interval=5.0)
    result, missing = [], []
    for sym, lbl in _INDEX_SYMS:
        d = _rtf.get_price(sym)
        if d and d.get("price"):
            result.append(dict(sym=sym, lbl=lbl, price=d["price"],
                               chg=d["change_pct"], high=d.get("high",0),
                               low=d.get("low",0)))
        else:
            result.append(None); missing.append((sym, lbl))
    if missing:
        fb = _index_fallback(tuple(sorted(s for s,_ in missing)))
        mi = 0
        for i, item in enumerate(result):
            if item is None:
                sym, lbl = missing[mi]; mi += 1
                m = fb.get(sym, {})
                result[i] = dict(sym=sym, lbl=lbl,
                                 price=m.get("price",0), chg=m.get("chg",0))
    return result

@st.cache_data(ttl=900, show_spinner=False)
def _index_spark(symbols: tuple) -> dict:
    """지수별 최근 ~30거래일 종가 시리즈 (미니 스파크라인용, 1회 배치 다운로드)."""
    import yfinance as _yf3
    out = {}
    try:
        raw = _yf3.download(list(symbols), period="1mo", interval="1d",
                            auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        for sym in symbols:
            try:
                s = (cl[sym] if hasattr(cl, "columns") else cl).dropna().tolist()
                if len(s) >= 2:
                    out[sym] = [float(x) for x in s[-30:]]
            except Exception:
                pass
    except Exception:
        pass
    return out


def _spark_svg(vals, color_hex, w=128, h=30):
    """리스트 → 인라인 SVG 스파크라인(면적+라인). HTML 카드 안에 그대로 박는다."""
    if not vals or len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(
        f"{i/(n-1)*w:.1f},{h - (v-lo)/rng*(h-4) - 2:.1f}" for i, v in enumerate(vals))
    r, g, b = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
    return (f"<svg width='100%' height='{h}' viewBox='0 0 {w} {h}' "
            f"preserveAspectRatio='none' style='display:block'>"
            f"<polygon points='0,{h} {pts} {w},{h}' fill='rgba({r},{g},{b},.10)'/>"
            f"<polyline points='{pts}' fill='none' stroke='{color_hex}' stroke-width='1.5' "
            f"stroke-linejoin='round' stroke-linecap='round'/></svg>")


@st.cache_data(ttl=86400, show_spinner=False)
def _name_lookup() -> dict:
    """티커→회사명 매핑 (stock_db_cache.json 재사용, 네트워크 없음)."""
    out = {}
    try:
        d = json.loads((Path(__file__).parent / "stock_db_cache.json").read_text())
        for _key, rows in d.items():
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r, list) and len(r) >= 2 and r[0]:
                        out[str(r[0]).upper()] = r[1]
    except Exception:
        pass
    return out


def _nm(ticker: str) -> str:
    """티커의 회사명 (없으면 빈 문자열)."""
    return _name_lookup().get((ticker or "").upper(), "")


def _tk_label(ticker: str) -> str:
    """'TICKER · 회사명' 형태 라벨 (이름 없으면 티커만)."""
    nm = _nm(ticker)
    return f"{ticker} · {nm}" if nm else ticker


def _fmt_val(v: float) -> str:
    """거래대금($) 축약 표기."""
    if not v:
        return "—"
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


@st.dialog("종목 상세", width="large")
def _stock_detail_dialog(tk: str):
    """종목 클릭 시 뜨는 상세 창 — 차트 + 핵심 지표 + 바로 거래."""
    nm = _nm(tk)
    # 현재 보고 있는 종목 → 1초 간격 실시간 갱신 (Finnhub 키 있으면 실시간, 없으면 yfinance 폴백)
    _rtf.set_focus([tk])
    if _AR:
        st_autorefresh(interval=1000, key=f"dlg_ar_{tk}")
    _tf = st.segmented_control("봉 주기", ["당일", "일봉", "주봉", "월봉", "년봉"],
                               default="일봉", key=f"dlgtf_{tk}",
                               label_visibility="collapsed") or "일봉"
    _df = fetch_bars(tk, _tf)
    _cl = (_df["Close"].squeeze().dropna()
           if (_df is not None) and (not _df.empty) and ("Close" in _df) else None)
    if _cl is not None and len(_cl) >= 2:
        cur = float(_cl.iloc[-1]); base = float(_cl.iloc[0])
        chg = (cur - base) / base if base else 0
        _live = _rtf.get_price(tk)
        _live_badge = ""
        if _live and _live.get("price"):
            cur = float(_live["price"])
            _age = time.time() - _live.get("ts", 0)
            _src = "Finnhub 실시간" if _live.get("source") == "finnhub" else "yfinance"
            _live_badge = (f"<span style='font-size:.66rem;color:var(--t3);"
                           f"border:1px solid var(--line2);border-radius:5px;padding:1px 6px'>"
                           f"● {_src} · {_age:.0f}초 전</span>")
        lc = "#F0454F" if chg >= 0 else "#3B82F6"
        r2, g2, b2 = int(lc[1:3], 16), int(lc[3:5], 16), int(lc[5:7], 16)
        _win = {"당일": "1D", "일봉": "6M", "주봉": "2Y", "월봉": "10Y", "년봉": "Max"}.get(_tf, "")
        st.markdown(
            f"<div style='display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:2px'>"
            f"<span style='font-size:1.05rem;font-weight:800'>{tk}</span>"
            f"<span style='font-size:.78rem;color:var(--t3)'>{nm}</span></div>"
            f"<div style='display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px'>"
            f"<span style='font-size:1.6rem;font-weight:900'>${cur:,.2f}</span>"
            f"<span style='color:{lc};font-weight:700;font-size:.84rem'>"
            f"{'▲' if chg>=0 else '▼'} {abs(chg):.2%} ({_win})</span>{_live_badge}</div>",
            unsafe_allow_html=True)
        _xa = dict(**_XA, tickformat="%H:%M") if _tf == "당일" else _XA
        _f = go.Figure()
        _f.add_trace(go.Scatter(x=_cl.index, y=_cl, mode="lines",
            line=dict(color=lc, width=2), fill="tozeroy",
            fillcolor=f"rgba({r2},{g2},{b2},.05)"))
        _f.update_layout(**CL(height=260, xaxis=_xa, yaxis=dict(**_YA, tickprefix="$")))
        st.plotly_chart(_f, width="stretch", config={"displayModeBar": False},
                        key=f"dlg_chart_{tk}_{_tf}")
    else:
        st.caption(f"{tk} · {_tf} 데이터 없음 (장 마감/휴장 또는 시세 제한)")
    # ── 보유 중이면 내 포지션 손익 먼저 ──────────────────────────────────────
    try:
        from portfolio import PortfolioManager as _PMdlg
        _pos_d = _PMdlg(paper=_is_paper_mode()).positions.get(tk)
    except Exception:
        _pos_d = None
    if _pos_d:
        _lv = _rtf.get_price(tk)
        _cp_d = float(_lv["price"]) if (_lv and _lv.get("price")) else _pos_d.entry_price
        _pnl_d = (_cp_d - _pos_d.entry_price) / _pos_d.entry_price if _pos_d.entry_price else 0
        _pnl_usd_d = (_cp_d - _pos_d.entry_price) * _pos_d.shares
        _pc_d = "#F0454F" if _pnl_d >= 0 else "#2F80ED"
        st.markdown(
            f"<div style='background:rgba(15,184,115,.06);border:1px solid rgba(15,184,115,.3);"
            f"border-radius:9px;padding:8px 12px;margin:6px 0;display:flex;gap:14px;"
            f"flex-wrap:wrap;font-size:.76rem'>"
            f"<span style='font-weight:800;color:#0FB873'>보유 중</span>"
            f"<span>{_pos_d.shares:.0f}주 · 평단 ${_pos_d.entry_price:,.2f}</span>"
            f"<span>투자금 ${_pos_d.cost_basis:,.0f}</span>"
            f"<span style='color:{_pc_d};font-weight:700'>"
            f"손익 {_pnl_d:+.2%} (${_pnl_usd_d:+,.0f})</span></div>",
            unsafe_allow_html=True)

    # ── 펀더멘털 핵심 지표 (매매 판단용) ─────────────────────────────────────
    _fd = _fetch_fundamentals(tk)
    if _fd:
        def _fk(lbl, val, vc="var(--t1)"):
            return ("<div style='flex:1;min-width:96px'>"
                    f"<div style='font-size:.58rem;color:var(--t3)'>{lbl}</div>"
                    f"<div style='font-size:.8rem;font-weight:700;color:{vc}'>{val}</div></div>")
        def _bil(v):
            if not v: return "—"
            return f"${v/1e12:.2f}T" if v >= 1e12 else f"${v/1e9:.1f}B"
        _rows1 = (
            _fk("시가총액", _bil(_fd.get("market_cap")))
            + _fk("PER", f"{_fd['per']:.1f}" if _fd.get("per") else "—")
            + _fk("선행 PER", f"{_fd['fwd_per']:.1f}" if _fd.get("fwd_per") else "—")
            + _fk("EPS", f"${_fd['eps']:.2f}" if _fd.get("eps") else "—")
            + _fk("PBR", f"{_fd['pbr']:.1f}" if _fd.get("pbr") else "—")
            + _fk("배당률", f"{_fd['div']:.2%}" if _fd.get("div") else "—"))
        _rows2 = (
            _fk("매출(TTM)", _bil(_fd.get("revenue")))
            + _fk("순이익(TTM)", _bil(_fd.get("net_income")),
                  "#0FB873" if (_fd.get("net_income") or 0) > 0 else "#F0454F")
            + _fk("순이익률", f"{_fd['margin']:.1%}" if _fd.get("margin") is not None else "—",
                  "#0FB873" if (_fd.get("margin") or 0) > 0 else "#F0454F")
            + _fk("ROE", f"{_fd['roe']:.1%}" if _fd.get("roe") is not None else "—")
            + _fk("부채/자본", f"{_fd['de']:.0f}%" if _fd.get("de") is not None else "—")
            + _fk("베타", f"{_fd['beta']:.2f}" if _fd.get("beta") else "—"))
        _tgt = _fd.get("target")
        _tgt_txt = "—"
        _tgt_c = "var(--t1)"
        if _tgt and _fd.get("price"):
            _up_pct = (_tgt / _fd["price"] - 1)
            _tgt_c = "#0FB873" if _up_pct > 0 else "#F0454F"
            _tgt_txt = f"${_tgt:,.0f} ({_up_pct:+.0%})"
        _rows3 = (
            _fk("52주 최고", f"${_fd['h52']:,.2f}" if _fd.get("h52") else "—")
            + _fk("52주 최저", f"${_fd['l52']:,.2f}" if _fd.get("l52") else "—")
            + _fk("애널리스트 목표가", _tgt_txt, _tgt_c)
            + _fk("섹터", _fd.get("sector") or "—"))
        st.markdown(
            "<div style='background:var(--bg2);border:1px solid var(--line);border-radius:10px;"
            "padding:10px 12px;margin:6px 0'>"
            "<div style='font-size:.62rem;color:var(--t3);font-weight:800;margin-bottom:6px'>"
            "핵심 지표</div>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap'>{_rows1}</div>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:8px'>{_rows2}</div>"
            f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:8px'>{_rows3}</div>"
            "</div>", unsafe_allow_html=True)

    st.markdown("<div style='border-top:1px solid var(--line);margin:8px 0'></div>",
                unsafe_allow_html=True)
    st.markdown("<div style='font-size:.74rem;font-weight:700;margin-bottom:4px'>바로 거래</div>",
                unsafe_allow_html=True)
    quick_trade_panel(tk, key_prefix=f"dlg_{tk}", show_chart=False)


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_fundamentals(tk: str) -> dict:
    """매매 판단용 펀더멘털 (30분 캐시). 실패한 항목은 None — UI에서 '—' 처리."""
    import yfinance as yf
    try:
        info = yf.Ticker(tk).info or {}
    except Exception:
        return {}
    if not info:
        return {}
    _dy = info.get("dividendYield")
    # yfinance 버전에 따라 0.0055(비율) 또는 0.55(퍼센트)로 와서 정규화
    if _dy and _dy > 0.2:
        _dy = _dy / 100.0
    return {
        "price":      info.get("currentPrice") or info.get("regularMarketPrice"),
        "market_cap": info.get("marketCap"),
        "per":        info.get("trailingPE"),
        "fwd_per":    info.get("forwardPE"),
        "eps":        info.get("trailingEps"),
        "pbr":        info.get("priceToBook"),
        "div":        _dy,
        "revenue":    info.get("totalRevenue"),
        "net_income": info.get("netIncomeToCommon"),
        "margin":     info.get("profitMargins"),
        "roe":        info.get("returnOnEquity"),
        "de":         info.get("debtToEquity"),
        "beta":       info.get("beta"),
        "h52":        info.get("fiftyTwoWeekHigh"),
        "l52":        info.get("fiftyTwoWeekLow"),
        "target":     info.get("targetMeanPrice"),
        "sector":     info.get("sector"),
    }


def clickable_ticker(container, tk: str, *, key: str, label: str | None = None,
                     with_name: bool = False):
    """티커/종목명을 클릭 가능한 tertiary 버튼으로 렌더 → 클릭 시 상세 창.
    container = st 또는 st.columns()[i]. 종목 표기 모든 곳에서 공통 사용."""
    lbl = label or (f"{tk}  {_nm(tk)}" if with_name and _nm(tk) else tk)
    if container.button(lbl, key=key, type="tertiary"):
        _stock_detail_dialog(tk)


@st.cache_data(ttl=900, show_spinner=False)
def fetch_bars(ticker: str, tf: str):
    """봉 주기별 시세 DataFrame. tf: 당일·일봉·주봉·월봉·년봉."""
    import yfinance as yf
    if tf == "당일":
        return fetch_intraday(ticker)
    _cfg = {"일봉": ("6mo", "1d"), "주봉": ("2y", "1wk"),
            "월봉": ("10y", "1mo"), "년봉": ("max", "1mo")}
    period, interval = _cfg.get(tf, ("6mo", "1d"))
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        if df.empty:
            return df
        if tf == "년봉":
            df = df.resample("YE").last().dropna()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_intraday(ticker: str):
    import yfinance as yf
    try:
        df = yf.download(ticker, period="1d", interval="5m",
                         auto_adjust=True, progress=False)
        return df if not df.empty else pd.DataFrame()
    except: return pd.DataFrame()

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_history(ticker: str, period: str = "2y"):
    import yfinance as yf
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        return df if not df.empty else pd.DataFrame()
    except: return pd.DataFrame()


@st.cache_data(ttl=900, show_spinner=False)
def ai_technicals(ticker: str) -> dict | None:
    """가격 시계열에서 추세·모멘텀·변동성·지지/저항·52주 위치 등 기술적 지표 산출.
    외부 LLM 없이 순수 가격 데이터 기반(yfinance). None이면 데이터 부족."""
    df = fetch_history(ticker, "2y")
    if df is None or df.empty or "Close" not in df:
        return None
    cl = df["Close"].squeeze().dropna()
    if len(cl) < 30:
        return None
    px = float(cl.iloc[-1])

    def _ret(n):
        return (px / float(cl.iloc[-(n + 1)]) - 1) if len(cl) > n else None

    def _ma(n):
        return float(cl.iloc[-n:].mean()) if len(cl) >= n else None

    ma20, ma50, ma200 = _ma(20), _ma(50), _ma(200)

    # RSI(14)
    rsi = None
    if len(cl) > 15:
        diff = cl.diff().dropna()
        up = diff.clip(lower=0).rolling(14).mean()
        dn = (-diff.clip(upper=0)).rolling(14).mean()
        _rs = up / dn.replace(0, np.nan)
        _rsi_ser = (100 - 100 / (1 + _rs)).dropna()
        if len(_rsi_ser):
            rsi = float(_rsi_ser.iloc[-1])

    # 연율화 변동성 (최근 20일 일간수익률 표준편차)
    dr = cl.pct_change().dropna()
    vol = float(dr.iloc[-20:].std() * (252 ** 0.5)) if len(dr) >= 20 else None

    # 52주 고저 + 위치(0~1)
    win = cl.iloc[-252:] if len(cl) >= 252 else cl
    hi52, lo52 = float(win.max()), float(win.min())
    pos52 = (px - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5

    # 최근 60일 스윙 지지/저항
    recent = cl.iloc[-60:] if len(cl) >= 60 else cl
    support, resistance = float(recent.min()), float(recent.max())

    # 거래량 추세 (최근 5일 평균 vs 20일 평균)
    vol_ratio = None
    if "Volume" in df:
        vser = df["Volume"].squeeze().dropna()
        if len(vser) >= 20:
            v5, v20 = float(vser.iloc[-5:].mean()), float(vser.iloc[-20:].mean())
            vol_ratio = (v5 / v20) if v20 else None

    # 추세 분류 (이동평균 정배열/역배열)
    if ma20 and ma50 and ma200:
        if px > ma20 > ma50 > ma200:   trend = "강한 상승"
        elif px > ma50 > ma200:        trend = "상승"
        elif px < ma20 < ma50 < ma200: trend = "강한 하락"
        elif px < ma50 < ma200:        trend = "하락"
        else:                          trend = "횡보·혼조"
    elif ma20 and ma50:
        trend = "상승" if px > ma20 > ma50 else "하락" if px < ma20 < ma50 else "횡보·혼조"
    else:
        trend = "데이터 부족"

    return dict(px=px, ret1w=_ret(5), ret1m=_ret(21), ret3m=_ret(63), ret6m=_ret(126),
                ma20=ma20, ma50=ma50, ma200=ma200, rsi=rsi, vol=vol,
                hi52=hi52, lo52=lo52, pos52=pos52, support=support,
                resistance=resistance, vol_ratio=vol_ratio, trend=trend, n=len(cl))


@st.cache_data(ttl=1800, show_spinner=False)
def ai_relative_strength(ticker: str) -> float | None:
    """시장(SPY) 대비 3개월 상대강도. >0 이면 시장 상회. None이면 계산 불가."""
    try:
        tk = ai_technicals(ticker)
        spy = ai_technicals("SPY")
        if tk and spy and tk.get("ret3m") is not None and spy.get("ret3m") is not None:
            return tk["ret3m"] - spy["ret3m"]
    except Exception:
        pass
    return None

@st.cache_data(ttl=300, show_spinner=False)
def strategy_candidates(strategy_name: str, top_n: int = 6) -> list[dict]:
    """현재 전략이 매수 확률 높다고 본 종목 + 최근 주가 시계열."""
    import watchlist as wl, yfinance as yf
    try:
        scored = strat_mod.get(strategy_name).score_many(wl.load(), max_workers=4)
    except Exception:
        return []
    top = [s for s in scored if s["score"] > 0][:top_n]
    if not top:
        top = scored[:top_n]
    tickers = [s["ticker"] for s in top]
    out = []
    try:
        raw = yf.download(tickers, period="3mo", interval="1d",
                          auto_adjust=True, progress=False, threads=True)
        cl = raw["Close"]
        for s in top:
            t = s["ticker"]
            try:
                ser = (cl[t].dropna() if hasattr(cl, "columns") else cl.dropna())
                series = ser.tolist()[-40:]
                cur = float(ser.iloc[-1])
                chg = (cur/float(ser.iloc[-22])-1)*100 if len(ser) >= 22 else 0.0
                out.append(dict(ticker=t, score=s["score"], current=cur,
                                chg=round(chg,2), series=series))
            except Exception:
                out.append(dict(ticker=t, score=s["score"], current=0,
                                chg=0, series=[]))
    except Exception:
        for s in top:
            out.append(dict(ticker=s["ticker"], score=s["score"],
                            current=0, chg=0, series=[]))
    return out

# 차트 기간/간격 프리셋: 10분 / 일 / 주 / 월 / 년
TF_PRESETS = {
    "1분":  ("1d",  "1m"),    # 당일 1분봉
    "5분":  ("5d",  "5m"),    # 5일 5분봉
    "일":   ("3mo", "1d"),    # 3개월 일봉
    "주":   ("2y",  "1wk"),   # 2년 주봉
    "월":   ("5y",  "1mo"),   # 5년 월봉
    "년":   ("max", "3mo"),   # 전체 분기봉
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_ohlcv_tf(ticker: str, period: str, interval: str):
    import yfinance as yf
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        return df if not df.empty else pd.DataFrame()
    except: return pd.DataFrame()

def broker_connected() -> bool:
    """Alpaca API 키가 실제로 입력됐는지 (플레이스홀더 아님)."""
    import config as _cfg
    return ("your_" not in _cfg.ALPACA_API_KEY
            and "your_" not in _cfg.ALPACA_SECRET_KEY
            and len(_cfg.ALPACA_API_KEY) > 8)

@st.cache_data(ttl=60, show_spinner=False)
def get_account_cached() -> dict | None:
    """실제 브로커 계좌 조회 (60초 캐시). 미연동·실패 시 None."""
    if not broker_connected():
        return None
    try:
        import config as _cfg
        from broker import Broker
        return Broker(paper="paper" in _cfg.ALPACA_BASE_URL).get_account()
    except Exception:
        return None

@st.cache_data(ttl=600, show_spinner=False)
def usdkrw_rate() -> float:
    """원/달러 환율 (10분 캐시). 실패 시 0."""
    d = _rtf.get_price("KRW=X")
    if d and d.get("price"):
        return float(d["price"])
    try:
        import yfinance as _yf
        r = float(_yf.Ticker("KRW=X").fast_info.last_price or 0)
        return r
    except Exception:
        return 0.0

def _is_paper_mode() -> bool:
    """현재 전역 거래 모드가 모의(페이퍼)인지. 모든 장부 선택의 단일 기준."""
    return st.session_state.get("trade_mode", "페이퍼(모의)").startswith("페이퍼")


def load_portfolio(paper: bool | None = None):
    from portfolio import PortfolioManager
    from config import STOP_LOSS_PCT
    if paper is None:
        paper = _is_paper_mode()
    pm = PortfolioManager(paper=paper)
    tickers = tuple(pm.positions.keys())
    prices = fetch_prices(tickers) if tickers else {}
    positions = []
    for t, pos in pm.positions.items():
        price = prices.get(t, pos.entry_price)
        pp = (price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
        positions.append(dict(ticker=t, shares=pos.shares, entry=pos.entry_price,
            current=price, pnl_pct=pp, pnl_usd=(price-pos.entry_price)*pos.shares,
            held=pos.days_held(), score=pos.score_at_entry))
        # ── 손절 2% 이내 진입 시 macOS 알림 ──────────────────────────────
        sl_dist = pp + STOP_LOSS_PCT  # 남은 거리 (0에 가까울수록 위험)
        _warn_key = f"sl_warned_{t}"
        if sl_dist < 0.02 and not st.session_state.get(_warn_key):
            try:
                import notifier as _ntf
                _ntf._send_desktop({
                    "title": f"손절 임박: {t}",
                    "body":  f"{t} 손절까지 {sl_dist:.1%} — 현재 {pp:+.1%}"
                })
            except Exception:
                pass
            st.session_state[_warn_key] = True
        elif sl_dist >= 0.02:
            st.session_state[_warn_key] = False

    t_inv = sum(p["entry"] * p["shares"] for p in positions)
    t_cur = sum(p["current"] * p["shares"] for p in positions)

    # ── 모드별 계좌 구분 ─────────────────────────────────────────────────
    # 페이퍼: 항상 가상 현금(paper_account) 기준 — 브로커 미사용.
    # 실거래: Alpaca 계좌 조회. 연결 안 되면 미연동 표시.
    if paper:
        cash = _paper.cash()
        equity = cash + t_cur
        buying_power = cash
        connected = False
        paper_mode = True
    else:
        acct = get_account_cached()
        connected = acct is not None
        if connected:
            cash    = acct.get("cash", 0.0)
            equity  = acct.get("equity", 0.0)
            buying_power = acct.get("buying_power", 0.0)
        else:
            cash = equity = buying_power = 0.0
        paper_mode = False

    return dict(positions=positions, pm=pm, t_inv=t_inv, t_cur=t_cur,
                cash=cash, equity=equity, buying_power=buying_power,
                connected=connected, paper_mode=paper_mode)

from safe_store import atomic_write_json, safe_read_json

def load_trades(paper: bool | None = None):
    if paper is None:
        paper = _is_paper_mode()
    fname = "trades_paper.json" if paper else "trades.json"
    f = Path(__file__).parent / fname
    return safe_read_json(f, default={"trades": []}).get("trades", [])

ORDERS_FILE = Path(__file__).parent / "orders_log.json"

_ORDERS_MAX = 2000   # 무한 증가 방지: 최근 N건만 유지

def log_order(ticker, side, shares, price, source="manual", *,
              score=None, pnl_pct=None, reason="", notify=True):
    """개별 매수/매도 체결 이벤트를 기록(차트 마커용) + 알림 발송.

    모든 거래(자동·수동·모의)가 거치는 단일 길목이므로, 여기서 알림을 한 번만
    발송해 경로마다 빠지거나 중복되는 일이 없게 한다. side: 'buy'|'sell'.
    score(매수)·pnl_pct/reason(매도)는 알림 본문에 표기(없으면 생략).
    """
    # 실체는 core.execution.log_order — 앱은 실거래 잔액만 보강해 위임.
    from core.execution import log_order as _core_log
    _bal = None
    if notify and side == "buy" and source != "paper" and not _is_paper_mode():
        try:
            _acct = get_account_cached()
            _bal = _acct.get("cash") if _acct else None
        except Exception:
            _bal = None
    _core_log(ticker, side, shares, price, source, score=score,
              pnl_pct=pnl_pct, reason=reason, notify=notify, balance=_bal)

def load_orders(ticker=None):
    orders = safe_read_json(ORDERS_FILE, default={"orders": []}).get("orders", [])
    return [o for o in orders if (ticker is None or o["ticker"] == ticker)]

# ──────────────────────────────────────────────────────────────────────────────
# 백그라운드
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=900, show_spinner=False)
def _bulk_scan_scores(sn: str, tickers: tuple) -> list[dict]:
    """대량 다운로드 1회 + 백테스터 스코어러로 빠르게 채점.
    종목당 개별 yfinance 호출(수백 회)을 없애 수백 종목도 수십 초로 단축.
    점수 외에 가격·등락률·거래대금·섹터도 함께 반환(스캔 필터용).
    결과는 15분 캐시 — 같은 전략·유니버스 재스캔 시 즉시 표시."""
    import backtester as _bt
    from datetime import date as _d, timedelta as _td
    end = _d.today().isoformat()
    start = (_d.today() - _td(days=730)).isoformat()   # ~2년 (적응형/모멘텀 252봉 확보)
    bundle = _bt.load_market_data(list(tickers), start, end)
    sd, ed = bundle["stock_data"], bundle["etf_data"]
    # 섹터 맵 (로컬 DB, 네트워크 X)
    try:
        import stock_browser as _sb
        _db, _ = _sb._get_db()
        _sec_map = {r[0]: r[3] for r in _db}   # ticker → 섹터(한글)
    except Exception:
        _sec_map = {}
    out = []
    for t in tickers:
        df = sd.get(t)
        if df is None or len(df) < 60:
            continue
        try:
            s = _bt._strategy_score_bt(sn, t, sd, ed, len(df) - 1)
        except Exception:
            s = 0.0
        try:
            close = df["Close"]; vol = df["Volume"]
            price = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else price
            chg = (price / prev - 1) * 100 if prev else 0.0
            dvol = float((close.tail(20) * vol.tail(20)).mean())
        except Exception:
            price = chg = dvol = 0.0
        out.append({"ticker": t, "score": round(float(s), 1),
                    "price": round(price, 2), "change_pct": round(chg, 2),
                    "dollar_vol": dvol, "sector": _sec_map.get(t, "")})
    return sorted(out, key=lambda x: x["score"], reverse=True)


# ── 스캔 · 그리드 스윕 · 시장 분석 (UI 동기 실행 헬퍼) ─────────────────────
# 자동매매 사이클과 무관한 '조회/실험' 경로 — 주문을 내지 않는다.
def trigger_scan(sn="composite", universe=None):
    """전략 스코어링을 동기 실행 (스레드 X → session_state 안정적으로 반영).
    universe=None이면 워치리스트, 아니면 지정 종목 리스트.
    대량 다운로드 1회로 채점(빠름) — 개별 다운로드 폴백 제거."""
    import watchlist as wl
    if universe is None:
        universe = wl.load()
    sname = STRAT.get(sn, (sn,))[0]
    n = len(universe)
    with st.spinner(f"[{sname}] {n}개 종목 스캔 중… (대량 다운로드 1회, 보통 10~40초)"):
        try:
            results = _bulk_scan_scores(sn, tuple(universe))
        except Exception as e:
            results = []
            st.error(f"스캔 오류: {e}")
    st.session_state["scan_results"]  = results
    st.session_state["scan_strategy"] = sn
    st.session_state["scan_ts"]       = datetime.now().strftime("%H:%M:%S")
    st.session_state["scan_running"]  = False


# ── 커스텀 그리드 스윕 (여러 페이지 재사용) ──────────────────────────────────
_GRID_SL_OPTS = [0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15]
_GRID_TP_OPTS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
_GRID_MS_OPTS = [50, 55, 60, 62, 65, 70]


def render_grid_sweep(key_prefix: str, universe: list, start, end,
                      capital: float, default_strats: list):
    """손절×익절×진입점수 값을 직접 골라 조합 그리드를 만들고, 여러 전략에
    일괄 백테스트해 '어떤 파라미터 조합이 가장 좋은지' 검색한다. 재사용 컴포넌트.

    universe/start/end/capital 은 호출 페이지가 정해 넘긴다(전략선택·백테스트 공용).
    """
    import backtester as _gbtm
    _pf = lambda v: f"{v:.0%}"

    st.markdown("<div style='font-size:.74rem;color:var(--t3);margin-bottom:6px'>"
                "손절·익절·진입점수 값을 직접 선택 → 모든 조합 × 전략을 일괄 백테스트</div>",
                unsafe_allow_html=True)
    g1, g2, g3 = st.columns(3)
    _sl = g1.multiselect("손절 %", _GRID_SL_OPTS, default=[0.05, 0.08, 0.12],
                         format_func=_pf, key=f"{key_prefix}_sl")
    _tp = g2.multiselect("익절 %", _GRID_TP_OPTS, default=[0.15, 0.30, 0.50],
                         format_func=_pf, key=f"{key_prefix}_tp")
    _ms = g3.multiselect("진입점수", _GRID_MS_OPTS, default=[55, 62],
                         key=f"{key_prefix}_ms")
    _all = list(STRAT.keys())
    st.multiselect("대상 전략 (복수 — 많을수록 통계 신뢰도↑, 시간도 비례)",
                   _all, default=default_strats,
                   format_func=lambda k: STRAT[k][0], key=f"{key_prefix}_strats")
    _bc1, _bc2 = st.columns(2)
    if _bc1.button("전체 전략 선택", key=f"{key_prefix}_all"):
        st.session_state[f"{key_prefix}_strats"] = _all; st.rerun()
    if _bc2.button("선택 초기화", key=f"{key_prefix}_clr"):
        st.session_state[f"{key_prefix}_strats"] = list(default_strats); st.rerun()

    _strats = st.session_state.get(f"{key_prefix}_strats", default_strats)
    _slv = _sl or [0.08]; _tpv = _tp or [0.30]; _msv = _ms or [60]
    _combos = [(s, t, m) for s in _slv for t in _tpv for m in _msv]
    _nst = max(len(_strats), 1)
    _n_total = len(_combos) * _nst
    st.caption(f"조합 {len(_combos)}개 × 전략 {_nst}개 = 총 **{_n_total}회** 백테스트 "
               f"· 종목 {len(universe)}개 · {start}~{end}")
    _conf = True
    if _n_total > 150:
        _conf = st.checkbox(f"{_n_total}회는 많습니다 — 수십 분 이상 걸릴 수 있어요. 동의",
                            key=f"{key_prefix}_conf")

    if st.button("그리드 스윕 실행", key=f"{key_prefix}_run", type="primary",
                 disabled=(_n_total > 150 and not _conf)):
        if not _strats:
            st.warning("전략을 1개 이상 선택하세요.")
        elif not universe:
            st.warning("종목 유니버스가 비어 있습니다.")
        else:
            with st.spinner("시세 로딩…"):
                _bd = _gbtm.load_market_data(list(universe), str(start), str(end))
            _rows = []; _agg: dict = {}
            _pg = st.progress(0.0); _done = 0
            for _sk in _strats:
                for (s, t, m) in _combos:
                    _done += 1
                    _pg.progress(_done / _n_total, text=f"{STRAT[_sk][0]} · {_done}/{_n_total}")
                    try:
                        _r = _gbtm.run(start=str(start), end=str(end), capital=float(capital),
                                       universe=list(universe), strategy=_sk,
                                       stop_loss=s, take_profit=t, min_score=m,
                                       sell_score=max(m - 25, 10), prefetched=_bd)
                        _agg.setdefault((s, t, m), []).append(_r.total_return)
                        _rows.append({"전략": STRAT[_sk][0], "손절": f"{s:.0%}",
                                      "익절": f"{t:.0%}", "점수": m,
                                      "수익률": f"{_r.total_return:+.1%}",
                                      "MDD": f"{_r.mdd:.1%}", "샤프": f"{_r.sharpe:.2f}",
                                      "_ret": _r.total_return, "_key": _sk,
                                      "_sl": s, "_tp": t, "_ms": m})
                    except Exception:
                        pass
            _pg.empty()
            _rows.sort(key=lambda x: x["_ret"], reverse=True)
            _agg_rows = []
            for (s, t, m), _rets in _agg.items():
                _avg = sum(_rets) / len(_rets)
                _agg_rows.append({"손절": f"{s:.0%}", "익절": f"{t:.0%}", "점수": m,
                                  "평균 수익률": f"{_avg:+.1%}", "표본(전략)": len(_rets),
                                  "_avg": _avg})
            _agg_rows.sort(key=lambda x: x["_avg"], reverse=True)
            st.session_state[f"{key_prefix}_res"] = _rows
            st.session_state[f"{key_prefix}_agg"] = _agg_rows

    _agg = st.session_state.get(f"{key_prefix}_agg")
    _res = st.session_state.get(f"{key_prefix}_res")
    if _agg:
        _b = _agg[0]
        st.markdown(
            f"<div class='ok' style='margin-top:8px'>최적 조합 → 손절 <b>{_b['손절']}</b> · "
            f"익절 <b>{_b['익절']}</b> · 진입점수 <b>{_b['점수']}</b> · "
            f"평균 수익률 <b>{_b['평균 수익률']}</b> (전략 {_b['표본(전략)']}개 평균)</div>",
            unsafe_allow_html=True)
        st.markdown("**조합별 평균 (여러 전략 평균 — 노이즈 제거)**")
        st.dataframe(pd.DataFrame(_agg).drop(columns=["_avg"]),
                     hide_index=True, width="stretch")
    if _res:
        # ── 최고 성과 결과를 전략·기간으로 바로 적용 ──
        _best = _res[0]
        _bkey = _best.get("_key")
        if _bkey and _bkey in STRAT:
            _bsl = _best.get("_sl", 0.08)
            _bhz = ("단타" if _bsl <= 0.05 else "단기" if _bsl <= 0.08
                    else "중장기" if _bsl <= 0.11 else "장기")
            _ac1, _ac2 = st.columns([3, 2])
            _ac1.markdown(
                f"<div style='font-size:.78rem;color:var(--t2);padding-top:6px'>"
                f"최고: <b style='color:{STRAT[_bkey][1]}'>{STRAT[_bkey][0]}</b> · "
                f"손절 {_best['손절']}·익절 {_best['익절']}·점수 {_best['점수']} → {_best['수익률']} "
                f"<span style='color:var(--t3)'>(권장 기간 {_bhz})</span></div>",
                unsafe_allow_html=True)
            if _ac2.button(f"이 전략·기간 적용", key=f"{key_prefix}_apply", type="primary"):
                st.session_state["active_strategy"] = _bkey
                st.session_state["horizon"] = _bhz
                try: apply_horizon_to_live(_bhz)
                except Exception: pass
                st.success(f"적용됨 — 전략 ‘{STRAT[_bkey][0]}’ · 투자 기간 ‘{_bhz}’ "
                           f"(라이브·자동매매에 즉시 반영)")
                st.rerun()
        st.markdown("**전체 결과 (수익률 상위 30)**")
        st.dataframe(
            pd.DataFrame(_res[:30]).drop(
                columns=[c for c in ["_ret", "_key", "_sl", "_tp", "_ms"]
                         if c in (_res[0] if _res else {})]),
            hide_index=True, width="stretch")

def run_market_analysis():
    """시장 분석 동기 실행 (스레드 X → session_state 안정 반영)."""
    import market_analyzer
    with st.spinner("시장 분석 중…"):
        try:
            st.session_state["market_info"] = market_analyzer.analyze()
        except Exception as e:
            st.error(f"시장 분석 실패: {e}")
            return
    st.rerun()


def build_daemon_config(enabled: bool) -> dict:
    """세션 설정(전략·기간·배분·발굴)으로 데몬 설정 생성 — 단일 스키마.

    데몬(autotrader)·'지금 1회 실행'(trigger_live)·자동 트레이딩 페이지가
    전부 이 스키마(core.cycle.run_cycle 입력)를 공유한다. schedule 류의
    데몬 전용 키는 기존 설정값을 보존한다."""
    from core import control as _ctl
    _hp = horizon_params(st.session_state.get("horizon", "단기"))
    _act = st.session_state.get("active_strategy", "composite")
    _prev = _ctl.load_config()
    return {
        "enabled": enabled,
        "paper": _is_paper_mode(),
        "strategy": _act, "strategy_name": STRAT.get(_act, (_act,))[0],
        "horizon": st.session_state.get("horizon", "단기"),
        "interval": int(st.session_state.get("at_interval_sec", 300)),
        "dynamic": st.session_state.get("alloc_dynamic", True),
        "buy_mode": st.session_state.get("buy_mode", "전량"),
        "sell_mode": st.session_state.get("sell_mode", "전량"),
        "buy_pct": st.session_state.get("buy_pct", 100),
        "sell_pct": st.session_state.get("sell_pct", 100),
        "stop_loss": _hp["stop_loss"], "take_profit": _hp["take_profit"],
        "trail": _hp.get("trail", 0.08),
        "min_score": _hp["min_score"], "sell_score": _hp["sell_score"],
        "hold_strong": _hp["hold_strong"], "hold_medium": _hp["hold_medium"],
        "daily_loss_limit": st.session_state.get("daily_loss_limit", 0.05),
        "buy_price_min": float(st.session_state.get("buy_price_min", 0) or 0),
        "buy_price_max": float(st.session_state.get("buy_price_max", 0) or 0),
        "schedule": _prev.get("schedule", "interval"),
        "daily_time": _prev.get("daily_time", "10:00"),
        "discover": {
            "enabled": st.session_state.get("discover_on", False),
            "interval": st.session_state.get("discover_iv_sec", 14400),
            "universe": st.session_state.get("discover_uni", ["S&P 500"]),
            "top_k": st.session_state.get("discover_topk", 30),
            "cap": st.session_state.get("discover_cap", 50),
        },
    }


def trigger_live(paper, sn):
    """'지금 1회 실행' — 데몬과 완전히 같은 사이클(core.cycle.run_cycle)을
    1회만 수행한다. 상시 자동매매는 데몬 전용(이중 거래 원천 차단)."""
    # 워치독: 스레드가 비정상 종료해 live_running 이 잠기는 것 방지
    if st.session_state.get("live_running"):
        if time.time() - st.session_state.get("live_started", 0) < 120:
            return
        st.session_state["live_running"] = False
    apply_horizon_to_live(st.session_state.get("horizon", "단기"))
    cfg = build_daemon_config(True)
    cfg["paper"] = paper
    cfg["strategy"] = sn
    cfg["strategy_name"] = STRAT.get(sn, (sn,))[0]
    log: list = []
    st.session_state.update(live_running=True, live_log=log,
                            live_started=time.time(),
                            auto_last_run=time.time())

    def _run():
        try:
            from core.cycle import run_cycle
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] 사이클 시작 "
                       f"({'모의' if paper else '실거래'} · {cfg['strategy_name']})")
            res = run_cycle(cfg, log=lambda m: log.append(
                f"[{datetime.now().strftime('%H:%M:%S')}] {m}"))
            top = res.get("top") or []
            try:
                st.session_state["live_scores"] = {
                    "ts": datetime.now().strftime("%H:%M:%S"), "strategy": sn,
                    "top": top[:8], "n": len(top),
                    "buy_th": int(cfg.get("min_score", 60)),
                    "cash": _paper.cash() if paper else None,
                    "prices": res.get("prices", {}),
                }
            except Exception:
                pass
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] 완료")
        except Exception as e:
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] 오류: {e}")
        finally:
            try:
                st.session_state["live_running"] = False
            except Exception:
                pass

    _t = threading.Thread(target=_run, daemon=True)
    # 스레드에 스크립트 컨텍스트 부착 — st.session_state 접근용
    try:
        from streamlit.runtime.scriptrunner import add_script_run_ctx
        add_script_run_ctx(_t)
    except Exception:
        pass
    _t.start()

# ──────────────────────────────────────────────────────────────────────────────
# 통화 표기 헬퍼
# ──────────────────────────────────────────────────────────────────────────────
def money(usd, decimals=0):
    """USD 금액을 현재 선택 통화로 포맷. None이면 '미연동'."""
    if usd is None:
        return "—"
    cur = st.session_state.get("currency", "USD")
    if cur == "KRW":
        rate = usdkrw_rate()
        if rate <= 0:
            return f"${usd:,.{decimals}f}"   # 환율 못 가져오면 달러로
        return f"₩{usd*rate:,.0f}"
    return f"${usd:,.{decimals}f}"

def money_compact(usd):
    """큰 금액 축약 (헤더용)."""
    if usd is None:
        return "—"
    cur = st.session_state.get("currency", "USD")
    if cur == "KRW":
        rate = usdkrw_rate()
        if rate > 0:
            return f"₩{usd*rate:,.0f}"
    return f"${usd:,.2f}"

# ──────────────────────────────────────────────────────────────────────────────
# 공통 컴포넌트
# ──────────────────────────────────────────────────────────────────────────────
def kpi(col, label, val, sub=None, color="var(--t1)"):
    col.markdown(f"""
    <div class='kpi'>
      <div class='kpi-l'>{label}</div>
      <div class='kpi-v' style='color:{color}'>{val}</div>
      {"<div class='kpi-s'>"+sub+"</div>" if sub else ""}
    </div>""", unsafe_allow_html=True)


def _panel_quote(tk: str) -> dict:
    """실시간 시세 1건 (피드 캐시 우선, 없으면 yfinance fast_info)."""
    import yfinance as yf
    d = _rtf.get_price(tk)
    if d and d.get("price"):
        try:
            cur = getattr(yf.Ticker(tk).fast_info, "currency", "USD") or "USD"
        except Exception:
            cur = "USD"
        return dict(price=d["price"], prev=d.get("prev", d["price"]),
                    ok=True, currency=cur,
                    age=int(time.time()-d.get("ts", time.time())),
                    source=d.get("source", "yfinance"))
    try:
        fi = yf.Ticker(tk).fast_info
        price = float(getattr(fi, "last_price", 0) or 0)
        prev  = float(getattr(fi, "previous_close", price) or price)
        cur   = getattr(fi, "currency", "USD") or "USD"
        return dict(price=price, prev=prev, ok=price > 0, currency=cur,
                    age=30, source="yfinance")
    except Exception:
        return dict(price=0, prev=0, ok=False, currency="USD", age=0, source="")


def quick_trade_panel(ticker: str, *, key_prefix: str, paper_default: bool = True,
                      show_chart: bool = True):
    """어디서든 재사용 가능한 인라인 주문 패널.

    수동 매매 페이지의 주문 위젯과 동일한 로직(시세·금액/수량·실거래 2단계 확인)을
    key_prefix로 네임스페이스해 한 화면에 여러 개 띄워도 충돌하지 않게 한다.
    스캔 결과·대시보드 후보·전역 빠른주문에서 공통으로 호출한다.
    """
    from portfolio import PortfolioManager
    from broker import Broker
    tk = (ticker or "").upper().strip()
    if not tk:
        st.caption("종목 티커를 입력하세요"); return
    is_kr = tk.endswith((".KS", ".KQ"))

    # 전역 거래 모드(사이드바)를 따른다
    is_paper = st.session_state.get("trade_mode", "페이퍼(모의)").startswith("페이퍼")
    _qc = "#3B82F6" if is_paper else "#F04452"
    st.markdown(
        f"<div style='display:inline-block;font-size:.66rem;font-weight:800;margin:0 0 6px;"
        f"padding:3px 10px;border-radius:6px;color:{_qc};background:{_qc}1A;border:1px solid {_qc}66'>"
        f"● {'모의투자' if is_paper else '실전투자'}"
        f"<span style='color:var(--t3);font-weight:500'> · 사이드바에서 변경</span></div>",
        unsafe_allow_html=True)

    _rtf.subscribe([tk], interval=3.0)
    q = _panel_quote(tk)
    if not q["ok"]:
        st.markdown(f"<div class='fail'>{tk} 시세를 불러올 수 없습니다.</div>",
                    unsafe_allow_html=True); return
    sym = "₩" if q.get("currency") == "KRW" else "$"
    chg = (q["price"]-q["prev"])/q["prev"] if q["prev"] else 0
    cc = "var(--up)" if chg >= 0 else "var(--dn)"
    ar = "▲" if chg >= 0 else "▼"
    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:10px;margin:2px 0 8px;flex-wrap:wrap'>"
        f"<span style='font-size:.8rem;font-weight:800;color:var(--t3)'>{tk}</span>"
        f"<span style='font-size:1.35rem;font-weight:900'>{sym}{q['price']:,.2f}</span>"
        f"<span style='color:{cc};font-size:.82rem;font-weight:700'>{ar} {abs(chg):.2%}</span>"
        f"<span style='font-size:.62rem;color:var(--t3)'>{q.get('source','')} · {q.get('age',0)}s</span>"
        f"</div>", unsafe_allow_html=True)

    if show_chart and not is_kr:
        idf = fetch_intraday(tk)
        if not idf.empty:
            ic = idf["Close"].squeeze()
            op = float(ic.iloc[0]); lc = "#F04452" if q["price"] >= op else "#2F80ED"
            r2, g2, b2 = int(lc[1:3], 16), int(lc[3:5], 16), int(lc[5:7], 16)
            ymn, ymx = float(ic.min()), float(ic.max()); pad = (ymx-ymn)*0.15 or ymx*0.002
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=idf.index, y=ic, mode="lines",
                line=dict(color=lc, width=1.8), fill="tozeroy",
                fillcolor=f"rgba({r2},{g2},{b2},.06)"))
            fig.update_layout(**CL(height=110,
                xaxis=dict(**_XA, tickformat="%H:%M", showticklabels=False),
                yaxis=dict(gridcolor="#1A1A25", showgrid=True, zeroline=False,
                           tickfont=dict(size=9), tickprefix=sym,
                           range=[ymn-pad, ymx+pad])))
            st.plotly_chart(fig, width="stretch",
                            config={"displayModeBar": False}, key=f"{key_prefix}_chart")

    pm = PortfolioManager(paper=is_paper)
    held = pm.positions.get(tk)

    order_by = st.radio("주문 방식", [f"금액({sym})", "수량(주)"], horizontal=True,
                        key=f"{key_prefix}_orderby", label_visibility="collapsed")
    if "금액" in order_by:
        amt = st.number_input(f"금액 ({sym})",
                              value=(500000 if is_kr else 1000),
                              step=(10000 if is_kr else 100),
                              key=f"{key_prefix}_amt")
        shares = int(amt / q["price"]) if q["price"] > 0 else 0
        st.caption(f"≈ {shares}주")
    else:
        shares = st.number_input("수량 (주)", value=5, step=1, key=f"{key_prefix}_shares")
        _val = shares * q["price"]
        st.caption(f"≈ {sym}{_val:,.0f}" if is_kr else f"≈ {sym}{_val:,.2f}")

    if held:
        _ep = f"{held.entry_price:,.0f}" if is_kr else f"{held.entry_price:.2f}"
        st.markdown(f"<div style='font-size:.72rem;color:var(--t3);margin:4px 0'>"
                    f"보유: {held.shares}주 · 평단 {sym}{_ep}</div>", unsafe_allow_html=True)

    bcol, scol = st.columns(2)
    buy_clicked = bcol.button("매수", type="primary", key=f"{key_prefix}_buy",
                              disabled=(shares < 1 or is_kr))
    sell_clicked = scol.button("매도", key=f"{key_prefix}_sell",
                               disabled=(not held or is_kr))

    if is_kr:
        st.markdown("<div style='font-size:.72rem;color:#FF9500;margin-top:6px'>"
                    "🇰🇷 국내 종목은 조회 전용 (Alpaca는 미국 주식만 거래)</div>",
                    unsafe_allow_html=True)
        return
    if not is_paper:
        st.markdown("<div class='fail' style='margin-top:6px;font-size:.72rem'>"
                    "실거래 모드 — 실제 자금</div>", unsafe_allow_html=True)

    # 수동 주문도 단일 실행기(core.execution)를 거친다 — 경로별 중복 제거.
    def _do_buy(paper):
        from core.execution import execute_manual
        r = execute_manual(tk, int(shares), "buy", paper, est_price=q["price"], pm=pm)
        if r.get("warning"):
            st.toast(f"{tk}: {r['warning']}")
        return r["price"]

    def _do_sell(paper):
        from core.execution import execute_manual
        sq = min(int(shares), held.shares) if shares >= 1 else held.shares
        r = execute_manual(tk, int(sq), "sell", paper, est_price=q["price"], pm=pm)
        if r.get("warning"):
            st.toast(f"{tk}: {r['warning']}")
        return r["shares"], r.get("pnl_pct", 0.0)

    ck = f"{key_prefix}_confirm"
    if buy_clicked and shares >= 1:
        if is_paper:
            try:
                _do_buy(True)
                st.toast(f"모의 매수 체결 · {tk} {int(shares)}주 @ {sym}{q['price']:,.2f}")
                st.markdown(f"<div class='ok'>매수 완료: {tk} {shares}주 @ {sym}{q['price']:,.2f}</div>",
                            unsafe_allow_html=True)
            except Exception as e:
                st.toast(f"매수 실패: {e}")
                st.markdown(f"<div class='fail'>매수 실패: {e}</div>", unsafe_allow_html=True)
        else:
            st.session_state[ck] = {"action": "buy", "shares": int(shares),
                                    "price": q["price"], "total": shares*q["price"]}
            st.rerun()
    if sell_clicked and held:
        sq_ = min(int(shares), held.shares) if shares >= 1 else held.shares
        if is_paper:
            try:
                s, pnl = _do_sell(True)
                st.toast(f"모의 매도 체결 · {tk} {int(s)}주 ({pnl:+.1%})")
                st.markdown(f"<div class='ok'>매도 완료: {tk} {s}주 @ {sym}{q['price']:,.2f} ({pnl:+.1%})</div>",
                            unsafe_allow_html=True)
            except Exception as e:
                st.toast(f"매도 실패: {e}")
                st.markdown(f"<div class='fail'>매도 실패: {e}</div>", unsafe_allow_html=True)
        else:
            st.session_state[ck] = {"action": "sell", "shares": sq_,
                                    "price": q["price"], "total": sq_*q["price"],
                                    "pnl": (q["price"]-held.entry_price)/held.entry_price}
            st.rerun()

    cp = st.session_state.get(ck)
    if cp:
        ac_kr = "매수" if cp["action"] == "buy" else "매도"
        ac_color = "var(--up)" if cp["action"] == "buy" else "var(--dn)"
        pnl_line = ""
        if cp["action"] == "sell":
            pnl_c = "var(--up)" if cp["pnl"] >= 0 else "var(--dn)"
            pnl_line = (f"<div style='font-size:.78rem;color:{pnl_c};margin-top:4px'>"
                        f"예상 손익: {cp['pnl']:+.1%} (${cp['total']*cp['pnl']:+,.0f})</div>")
        st.markdown(f"""
        <div style='background:#1C1015;border:1.5px solid {ac_color};border-radius:10px;
          padding:13px 15px;margin-top:8px'>
          <div style='font-weight:800;font-size:.9rem;color:{ac_color};margin-bottom:6px'>
            실거래 {ac_kr} 확인</div>
          <div style='font-size:.82rem;color:var(--t1)'><b>{tk}</b> &nbsp;{cp['shares']}주
            &nbsp;@ ${cp['price']:.2f} &nbsp;<span style='color:var(--t3)'>합계 ${cp['total']:,.0f}</span></div>
          {pnl_line}
        </div>""", unsafe_allow_html=True)
        oc, cc2 = st.columns(2)
        if oc.button(f"✓ {ac_kr} 실행", type="primary", key=f"{key_prefix}_conf_ok"):
            try:
                if cp["action"] == "buy":
                    _do_buy(False)
                    st.markdown(f"<div class='ok'>매수 완료: {tk}</div>", unsafe_allow_html=True)
                else:
                    s, pnl = _do_sell(False)
                    st.markdown(f"<div class='ok'>매도 완료: {tk} ({pnl:+.1%})</div>",
                                unsafe_allow_html=True)
            except Exception as e:
                st.markdown(f"<div class='fail'>실패: {e}</div>", unsafe_allow_html=True)
            st.session_state[ck] = None
        if cc2.button("✕ 취소", key=f"{key_prefix}_conf_cancel"):
            st.session_state[ck] = None
            st.rerun()

def _autotrader_alive() -> bool:
    """백그라운드 데몬 생존 여부 — core.control 위임 (구 호출부 호환용)."""
    from core import control as _ctl
    return _ctl.daemon_alive()


# ──────────────────────────────────────────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────────────────────────────────────────
# 거래 흐름 순서: 시작(대시보드) → 스캔(전략) → 자동실행(라이브) → 모니터(포트폴리오)
#                → 분석(열람·백테스트) → 고급(직접 주문)·설정
PAGES = ["대시보드","전략 선택","자동 트레이딩","포트폴리오","주식 열람","직접 주문","설정"]

with st.sidebar:
    st.markdown("""
    <div style='padding:20px 20px 16px;border-bottom:1px solid var(--line)'>
      <div style='font-size:.96rem;font-weight:900;color:var(--t1);letter-spacing:-.03em'>
        AI 트레이딩</div>
      <div style='font-size:.68rem;color:var(--t3);margin-top:3px'>자본 자동 순환</div>
    </div>""", unsafe_allow_html=True)

    # ── 현재 거래 모드 배너 (항상 최상단 노출 — 모의/실거래 혼동 방지) ──────────
    # 모든 매수/매도(수동·자동)가 이 모드의 장부에서만 일어난다. 색으로 즉시 구분.
    _mb_paper = st.session_state.get("trade_mode", "페이퍼(모의)").startswith("페이퍼")
    if _mb_paper:
        _mb_c, _mb_bg, _mb_t, _mb_s = "#3B82F6", "rgba(59,130,246,.10)", "모의투자", "가상 자금 · 안전"
    else:
        _mb_c, _mb_bg, _mb_t, _mb_s = "#F04452", "rgba(240,68,82,.10)", "실전투자", "실제 자금 · 주의"
    st.markdown(
        f"<div style='margin:2px 0 10px;padding:9px 13px;border-radius:10px;"
        f"background:{_mb_bg};border:1.5px solid {_mb_c}'>"
        f"<div style='display:flex;align-items:center;gap:7px'>"
        f"<span style='width:9px;height:9px;border-radius:50%;background:{_mb_c};"
        f"box-shadow:0 0 6px {_mb_c}'></span>"
        f"<span style='font-size:.92rem;font-weight:900;color:{_mb_c};letter-spacing:-.02em'>{_mb_t}</span></div>"
        f"<div style='font-size:.6rem;color:var(--t3);margin-top:3px'>{_mb_s} · 모든 매수/매도가 이 모드로 실행</div>"
        f"</div>", unsafe_allow_html=True)

    # 라디오를 'page' 키에 직접 바인딩 → 페이지 전환 시 추가 st.rerun() 불필요
    # (기존엔 변경 감지 후 수동 rerun으로 매 전환마다 스크립트가 2번 실행돼 느렸음)
    st.radio("메뉴", PAGES, key="page", label_visibility="collapsed")

    st.markdown("<hr/>", unsafe_allow_html=True)

    # ── 현재 운용 요약 (항상 노출: 전략·투자기간·자본배분·보유) ──────────────
    _cur_strat = st.session_state.get("active_strategy", "composite")
    _cs_name, _cs_color = STRAT.get(_cur_strat, ("복합 (기본)", "#05C072"))
    _cur_hz = st.session_state.get("horizon", "단기")
    _cur_hz_lbl = HORIZONS.get(_cur_hz, HORIZONS.get("단기", {})).get("label", "")
    _cur_dyn = st.session_state.get("alloc_dynamic", True)
    try:
        from portfolio import PortfolioManager as _PMside
        _n_hold = len(_PMside(paper=_is_paper_mode()).positions)
    except Exception:
        _n_hold = 0

    def _side_kv(label, value, vc="var(--t1)"):
        return ("<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin:4px 0'>"
                f"<span style='font-size:.62rem;color:var(--t3);white-space:nowrap'>{label}</span>"
                f"<span style='font-size:.72rem;font-weight:800;color:{vc};text-align:right;"
                "overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>"
                f"{value}</span></div>")

    st.markdown(
        "<div style='background:var(--bg2);border:1px solid var(--line);border-radius:10px;"
        "padding:9px 12px;margin:0 0 10px'>"
        "<div style='font-size:.56rem;color:var(--t3);font-weight:800;letter-spacing:.05em;"
        "margin-bottom:5px'>현재 운용</div>"
        + _side_kv("전략", f"● {_cs_name}", _cs_color)
        + _side_kv("투자 기간", f"{_cur_hz} · {_cur_hz_lbl}" if _cur_hz_lbl else _cur_hz)
        + _side_kv("자본 배분", "유동형" if _cur_dyn else "고정형",
                   "#00C2A8" if _cur_dyn else "var(--t1)")
        + _side_kv("보유", f"{_n_hold}종목")
        + "</div>", unsafe_allow_html=True)

    # 자동 매매 토글 — 마스터 스위치. 실행 주체는 백그라운드 데몬 하나뿐이고
    # 앱은 관리/관제만 한다: ON=데몬 기동+매매, OFF=데몬 프로세스 종료
    # (OFF면 백그라운드에 아무것도 안 남는다 — '대기 상태로 떠있기' 없음).
    import market_hours as _mh
    from core import control as _ctl
    auto = st.toggle("자동 매매", value=st.session_state.get("auto_enabled", False),
                     key="auto_toggle")
    if auto != st.session_state.get("auto_enabled", False):
        st.session_state["auto_enabled"] = auto
        _ap = st.session_state.get("trade_mode", "페이퍼(모의)").startswith("페이퍼")
        if auto and not _ap and not broker_connected():
            st.session_state["auto_enabled"] = False
            st.toast("실거래 자동매매는 Alpaca 키 연동이 필요합니다 (설정에서 연결)")
        elif auto:
            try:
                _ctl.save_config(build_daemon_config(True))
                if not _ctl.daemon_alive():
                    _ctl.start_daemon()
                    st.toast("자동매매 시작 — 백그라운드 데몬 기동 (앱 꺼도 동작)")
                else:
                    st.toast("자동매매 재개 — 다음 사이클부터 반영")
            except Exception as _e:
                st.toast(f"데몬 제어 실패: {_e}")
        else:
            try:
                _ctl.stop_daemon()   # enabled=False 기록 + 데몬 프로세스 종료
                st.toast("자동매매 중지 — 백그라운드 데몬 종료")
            except Exception:
                pass

    _d_cfg = _ctl.load_config()
    _d_alive = _ctl.daemon_alive()
    _d_on = _d_alive and _d_cfg.get("enabled", False)
    _iv_sec_sb = int(_d_cfg.get("interval", 300))
    st.session_state["auto_iv_sec"] = _iv_sec_sb          # 라이브 페이지 상황판용
    st.session_state["auto_iv_lbl"] = (f"{_iv_sec_sb//60}분" if _iv_sec_sb >= 60
                                       else f"{_iv_sec_sb}초")
    _lbl, _is_open = _mh.market_status_label()
    _next_txt = ("" if _is_open
                 else f" · 개장까지 {_mh.seconds_until_open()/3600:.1f}h")
    _amode_p = _d_cfg.get("paper", True)
    _amode_c = "#3B82F6" if _amode_p else "#F04452"
    _amode_t = "모의" if _amode_p else "실거래"
    if _d_on:
        st.markdown(
            f"<div style='font-size:.64rem;color:var(--t3);margin-top:2px;line-height:1.55'>"
            f"<span style='color:#05C072;font-weight:700'>● ON</span> · "
            f"<span style='color:{_amode_c};font-weight:700'>{_amode_t} 장부</span> · {_lbl}{_next_txt}<br>"
            f"데몬이 장중 {st.session_state['auto_iv_lbl']}마다 평가 · 앱 꺼도 계속</div>",
            unsafe_allow_html=True)
    elif _d_alive:
        st.markdown(
            "<div style='font-size:.64rem;color:var(--t3);margin-top:2px'>"
            "<span style='color:#FF9500;font-weight:700'>◐ 대기</span> · "
            "데몬 실행 중 · 매매 일시정지</div>", unsafe_allow_html=True)
    else:
        st.markdown(
            "<div style='font-size:.64rem;color:var(--t3);margin-top:2px'>"
            "<span style='color:var(--t3)'>○ 정지</span> · 수동 실행만</div>",
            unsafe_allow_html=True)

    # ── 시장 자동 발굴 (스크리너) ────────────────────────────────────────
    # 넓은 지수를 주기적으로 훑어 유망주를 워치리스트에 자동 편입.
    # 보유종목·수동 추가는 항상 보존, 자동 슬롯만 교체.
    with st.expander("시장 자동 발굴", expanded=False):
        import watchlist as _wl_disc, screener as _scr_disc
        st.session_state.setdefault("discover_on", False)
        st.toggle("자동 발굴", key="discover_on")

        _UNI_OPTS = ["S&P 500", "나스닥 100", "다우 30"]
        st.session_state.setdefault("discover_uni", ["S&P 500"])
        st.caption("스캔 대상")
        st.pills("스캔 대상", _UNI_OPTS, selection_mode="multi",
                 key="discover_uni", label_visibility="collapsed")

        _DISC_IV = {"1시간": 3600, "2시간": 7200, "4시간": 14400, "하루": 86400}
        st.session_state.setdefault("discover_iv_lbl", "4시간")
        st.caption("발굴 주기")
        st.pills("발굴 주기", list(_DISC_IV), selection_mode="single",
                 key="discover_iv_lbl", label_visibility="collapsed")
        _ivl = st.session_state.get("discover_iv_lbl") or "4시간"
        st.session_state["discover_iv_sec"] = _DISC_IV.get(_ivl, 14400)

        st.session_state.setdefault("discover_topk", 30)
        st.session_state.setdefault("discover_cap", 50)
        st.caption("편입 후보 수 · 워치리스트 상한")
        _c1, _c2 = st.columns(2)
        _c1.number_input("후보", 5, 60, step=5, key="discover_topk",
                         label_visibility="collapsed")
        _c2.number_input("상한", 20, 120, step=10, key="discover_cap",
                         label_visibility="collapsed")

        # 현재 워치리스트 구성 — 컴팩트 미니카드 (Toss식 통계 행)
        _full = _wl_disc._load_full()
        _age = _wl_disc.auto_age_sec()
        _scan_txt = (f"{_age/3600:.1f}시간 전 스캔" if _age is not None
                     else "스캔 이력 없음")
        def _stat(lbl, val, c="var(--t1)"):
            return (f"<div style='flex:1;text-align:center'>"
                    f"<div style='font-size:.92rem;font-weight:800;color:{c};line-height:1'>{val}</div>"
                    f"<div style='font-size:.54rem;color:var(--t3);margin-top:2px'>{lbl}</div></div>")
        st.markdown(
            "<div style='background:var(--bg2);border:1px solid var(--line);border-radius:9px;"
            "padding:8px 6px;margin:8px 0 8px;display:flex;gap:2px'>"
            + _stat("활성", len(_full["stocks"]))
            + _stat("수동", len(_full["manual"]), "#3B82F6")
            + _stat("자동", len(_full["auto"]), "#05C072")
            + _stat("보유", len(_full["held"]), "#FF9500")
            + "</div>"
            f"<div style='font-size:.56rem;color:var(--t3);text-align:center;margin:0 0 8px'>{_scan_txt}</div>",
            unsafe_allow_html=True)

        if st.button("지금 시장 스캔", use_container_width=True, key="discover_now"):
            with st.spinner("시장 스캔 중… (S&P500은 30초~1분)"):
                try:
                    _pf = load_portfolio(paper=_is_paper_mode())
                    _held = list(_pf["pm"].positions.keys())
                    _res = _scr_disc.discover(
                        held=_held,
                        universe_names=st.session_state.get("discover_uni", ["S&P 500"]) or ["S&P 500"],
                        top_k=int(st.session_state.get("discover_topk", 30)),
                        cap=int(st.session_state.get("discover_cap", 50)))
                    _a = _res.get("added", [])
                    st.success(f"편입 {len(_a)}종목 · 활성 {_res.get('total')}종목"
                               + (f" — {', '.join(_a[:8])}" if _a else " (신규 없음)"))
                except Exception as _e:
                    st.error(f"스캔 실패: {_e}")
            st.rerun()

    st.markdown("<hr/>", unsafe_allow_html=True)

    # ── 전역 거래 모드 (페이퍼/실거래) — 모든 거래 패널이 이 값을 따른다 ──
    # key="trade_mode" 에 직접 바인딩 → 전환 즉시 상단 배너·자동매매가 같은 값을 본다.
    st.session_state.setdefault("trade_mode", "페이퍼(모의)")
    _tm = st.radio("거래 모드 전환", ["페이퍼(모의)", "실거래"],
                   horizontal=True, key="trade_mode")
    if _tm.startswith("페이퍼"):
        st.markdown("<div style='font-size:.62rem;color:#3B82F6;margin-top:-6px'>"
                    "● 모의 · 현재가 즉시 체결</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='font-size:.62rem;color:#F04452;margin-top:-6px'>"
                    "● 실거래 · 실제 자금 (API 필요)</div>", unsafe_allow_html=True)

    # 투자 기간(horizon)은 백테스트·라이브 창 내부에서 설정 — 사이드바에선 동기화만
    st.session_state.setdefault("horizon", "단기")
    apply_horizon_to_live(st.session_state["horizon"])

    # 통화 표기 토글 (USD / KRW)
    _cur_sel = st.radio("표기 통화", ["USD", "KRW"],
                        index=0 if st.session_state.get("currency","USD")=="USD" else 1,
                        horizontal=True, key="currency_radio")
    if _cur_sel != st.session_state.get("currency"):
        st.session_state["currency"] = _cur_sel
        st.rerun()
    if _cur_sel == "KRW":
        _rate_now = usdkrw_rate()
        if _rate_now > 0:
            st.markdown(f"<div style='font-size:.64rem;color:var(--t3);margin-top:-6px'>"
                        f"1 USD = ₩{_rate_now:,.0f}</div>", unsafe_allow_html=True)

    # 실시간 갱신은 항상 ON (토글 제거) — 의미있는 페이지에서만 autorefresh
    st.session_state["live_refresh"] = True
    live_r = True
    _LIVE_PAGES = {"대시보드", "자동 트레이딩", "직접 주문", "포트폴리오"}
    _cur_page = st.session_state.get("page", "대시보드")
    if _AR and _cur_page in _LIVE_PAGES:
        st_autorefresh(interval=5000, key="ar")    # 5초마다 자동 새로고침
    if live_r:
        _feed_all = _rtf.get_all()
        _feed_n   = len(_feed_all)
        _src      = "Finnhub" if any(v.get("source")=="finnhub"
                                     for v in _feed_all.values()) else "yfinance"
        _oldest   = min((v.get("ts",0) for v in _feed_all.values()), default=0)
        _age_s    = int(time.time() - _oldest) if _oldest else 0
        _health   = _rtf.feed_health()
        # 데이터 멈춤 감지: 60초 이상 갱신 없음 or 연속 실패
        _stale = _health["stale_sec"] > 60 or _health["fails"] >= 2
        if _stale and _feed_n > 0:
            _errtxt = _health["last_error"] or f"{_health['fails']}회 연속 실패"
            st.markdown(
                f"<div style='font-size:.7rem;color:#FF9500;margin-top:-4px'>"
                f"데이터 갱신 멈춤 ({_age_s}초)</div>"
                f"<div style='font-size:.6rem;color:var(--t3);margin-top:1px'>"
                f"{_errtxt[:60]}</div>",
                unsafe_allow_html=True)
        else:
            st.markdown(
                f"<div style='font-size:.7rem;color:#F04452;margin-top:-4px'>"
                f"● LIVE · {datetime.now().strftime('%H:%M:%S')}</div>"
                f"<div style='font-size:.65rem;color:var(--t3);margin-top:1px'>"
                f"{_src} · {_feed_n}종목 · {_age_s}초 전 갱신</div>",
                unsafe_allow_html=True)
        # 신규 구독 확인 (포지션 종목이 추가됐을 때)
        # 주의: 이 블록은 `state = load_portfolio()` 보다 위에서 실행된다.
        # 예전엔 state["pm"] 을 참조해 매번 NameError → except 로 삼켜졌고,
        # 그 결과 구독이 조용히 통째로 누락됐다. 장부에서 직접 읽는다.
        try:
            import watchlist as _wl_rt
            from portfolio import PortfolioManager as _PM_rt
            _rt_tickers = list(set(
                _wl_rt.load() +
                list(_PM_rt(paper=_is_paper_mode()).positions.keys()) +
                ["SPY","QQQ","^VIX"]
            ))
            _rtf.subscribe(_rt_tickers, interval=5.0)
        except Exception:
            pass
        # ── 가격 알림 15초 체크 ───────────────────────────────────────────
        try:
            import price_alerts as _pal
            _al_list = _pal.load()
            _active_als = [a for a in _al_list if not a.get("triggered")]
            if _active_als:
                _al_tickers = list({a["ticker"] for a in _active_als})
                _al_prices  = fetch_prices(tuple(_al_tickers))
                _fired = _pal.check(_al_prices)
                for _fa in _fired:
                    _cond_kr = "↑ 돌파" if _fa["condition"]=="above" else "↓ 이탈"
                    _msg = {
                        "title": f"가격 알림: {_fa['ticker']} {_cond_kr}",
                        "body": (f"{_fa['ticker']} {_cond_kr} "
                                 f"${_fa['triggered_price']:.2f}"
                                 f" (목표 ${_fa['target']:.2f})\n"
                                 f"{_fa.get('note','')}"),
                    }
                    import notifier as _ntf2
                    _ntf2._dispatch(_msg)
        except Exception as _ae:
            # 가격 알림 실패는 조용히 묻지 않고 사이드바에 경고
            st.markdown(f"<div style='font-size:.6rem;color:#FF9500;margin-top:2px'>"
                        f"가격 알림 체크 오류: {type(_ae).__name__}</div>",
                        unsafe_allow_html=True)

    st.markdown("<hr/>", unsafe_allow_html=True)

    # 현재 전략
    active = st.session_state.get("active_strategy", "composite")
    an, ac = STRAT[active]
    mi = st.session_state.get("market_info") or {}
    st.markdown(f"""
    <div style='padding:0 20px 14px'>
      <div style='font-size:.68rem;color:var(--t3);font-weight:600;letter-spacing:.05em;
        text-transform:uppercase;margin-bottom:8px'>현재 전략</div>
      <div style='font-weight:700;font-size:.86rem;color:{ac}'>{an}</div>
      {("<div style='font-size:.72rem;color:var(--t3);margin-top:4px'>시장: "+mi.get('trend','?').upper()+" · VIX "+str(round(mi.get('vix',0)))+"</div>") if mi else ""}
    </div>""", unsafe_allow_html=True)

    if st.button("시장 분석", key="sb_mkt"):
        run_market_analysis()
    if st.button("스캔 실행", key="sb_scan"):
        trigger_scan(active); st.rerun()

    # 스캔 현황
    scores = st.session_state.get("scan_results")
    if scores:
        ts_str = st.session_state.get("scan_ts", "")
        st.markdown(f"""
        <div style='padding:10px 20px 0'>
          <div style='font-size:.68rem;color:var(--t3);font-weight:600;
            letter-spacing:.05em;text-transform:uppercase;margin-bottom:8px'>
            스캔 결과 · {ts_str}</div>""", unsafe_allow_html=True)
        for s in scores[:5]:
            t = s["score"]
            st.markdown(f"""
            <div style='display:flex;justify-content:space-between;align-items:center;
              padding:5px 0;border-bottom:1px solid var(--line)'>
              <span style='font-weight:700;font-size:.83rem;color:var(--t1)'>{s["ticker"]}</span>
              <div style='display:flex;align-items:center;gap:6px'>
                <div style='width:44px;background:var(--bg4);border-radius:2px;height:3px'>
                  <div style='width:{t}%;height:3px;border-radius:2px;background:{ac}'></div>
                </div>
                <span style='font-weight:800;font-size:.82rem;color:{ac}'>{t:.0f}</span>
              </div>
            </div>""", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # 보유 종목
    state = load_portfolio()
    positions = state["positions"]
    if positions:
        st.markdown("""
        <div style='padding:12px 20px 0;border-top:1px solid var(--line);margin-top:10px'>
          <div style='font-size:.68rem;color:var(--t3);font-weight:600;
            letter-spacing:.05em;text-transform:uppercase;margin-bottom:8px'>
            보유 종목</div>""", unsafe_allow_html=True)
        for p in positions:
            c = "var(--up)" if p["pnl_pct"] >= 0 else "var(--dn)"
            st.markdown(f"""
            <div style='display:flex;justify-content:space-between;align-items:center;
              padding:5px 0;border-bottom:1px solid var(--line)'>
              <div>
                <div style='font-weight:700;font-size:.83rem;color:var(--t1)'>{p["ticker"]}</div>
                <div style='font-size:.68rem;color:var(--t3)'>{p["shares"]}주</div>
              </div>
              <div style='text-align:right'>
                <div style='font-size:.82rem;font-weight:700;color:var(--t1)'>{money(p["current"], 2)}</div>
                <div style='font-size:.7rem;color:{c};font-weight:600'>{p["pnl_pct"]:+.1%}</div>
              </div>
            </div>""", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# 지수 바 (항상 표시, 1시간 캐시)
# ──────────────────────────────────────────────────────────────────────────────
index_data = fetch_index_bar()

# 각 지수가 무엇을 뜻하는지 — 마우스 올리면 설명
INDEX_DESC = {
    "S&P 500":  "미국 대형주 500개. 시장 전체의 대표 지표.",
    "나스닥 100":"기술주 중심 100개. 성장주·위험선호 척도.",
    "다우존스": "우량주 30개. 전통 산업·경기 민감.",
    "VIX":      "공포지수. 높을수록(>20) 불안, 낮을수록(<15) 안정.",
    "미국채 10Y":"10년 국채금리. 오르면 성장주 부담·긴축 신호.",
    "러셀 2000":"소형주 2000개. 위험선호·내수경기 민감.",
    "하이일드":  "고위험 회사채. 오르면 위험선호(유동성 풍부) 신호.",
    "달러인덱스":"달러 강세 지표. 강하면 위험자산엔 역풍.",
    "코스피":   "한국 대형주 지수. 국내 시장 대표 지표.",
    "코스닥":   "한국 중소·기술주 지수. 성장주·위험선호 척도.",
    "원/달러":  "환율. 오르면(원화 약세) 외국인 매도·수출주 유리.",
}

_imap = {d["lbl"]: d for d in index_data}
_spark = _index_spark(tuple(s for s, _ in _INDEX_SYMS))
_icards = ""
for d in index_data:
    up = d["chg"] >= 0
    chex = "#F0454F" if up else "#3B82F6"
    a = "▲" if up else "▼"
    desc = INDEX_DESC.get(d["lbl"], "")
    svg = _spark_svg(_spark.get(d["sym"], []), chex)
    _icards += (
        f"<div class='icard' title='{desc}'>"
        f"<div class='ic-top'><span class='ic-name'>{d['lbl']}</span>"
        f"<span class='ic-chg' style='color:{chex}'>{a} {abs(d['chg']):.2%}</span></div>"
        f"<div class='ic-spark'>{svg}</div>"
        f"<div class='ic-price'>{d['price']:,.2f}</div>"
        f"</div>")
st.markdown(f"""
<style>
.igrid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(138px,1fr));
  gap:5px; margin-bottom:10px; }}
.icard {{ background:var(--bg2); border:1px solid var(--line); border-radius:6px;
  padding:7px 10px 6px; transition:border-color .12s ease; overflow:hidden; }}
.icard:hover {{ border-color:var(--line2); }}
.ic-top {{ display:flex; justify-content:space-between; align-items:baseline; gap:6px; }}
.ic-name {{ font-size:.62rem; color:var(--t2); font-weight:600; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }}
.ic-chg {{ font-size:.64rem; font-weight:700; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.ic-spark {{ height:30px; margin:4px 0 3px; }}
.ic-price {{ font-size:.92rem; font-weight:800; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; }}
</style>
<div class='igrid'>{_icards}</div>""", unsafe_allow_html=True)

# ── 시장 종합 진단 (유동성·강세/약세 한눈에) ──
def _diag():
    g = lambda k: _imap.get(k, {}).get("chg", 0)
    p = lambda k: _imap.get(k, {}).get("price", 0)
    vix = p("VIX")
    signals = []          # (라벨, 상태색)
    score = 0             # +면 위험선호(강세), -면 위험회피(약세)

    # 1) 변동성(공포)
    if vix and vix < 15:   signals.append(("변동성 낮음 · 안정", "var(--green)")); score += 1
    elif vix and vix > 22: signals.append(("변동성 높음 · 불안", "var(--up)")); score -= 1
    else:                  signals.append(("변동성 보통", "var(--t2)"))
    # 2) 위험선호: 나스닥·러셀 강세 = 공격적
    risk_on = g("나스닥 100") + g("러셀 2000")
    if risk_on > 0.004:    signals.append(("위험선호 (성장·소형 강세)", "var(--up)")); score += 1
    elif risk_on < -0.004: signals.append(("위험회피 (성장·소형 약세)", "var(--dn)")); score -= 1
    # 3) 신용/유동성: 하이일드 강세 = 돈이 위험자산으로
    hyg = g("하이일드")
    if hyg > 0.002:    signals.append(("유동성 풍부 (하이일드 강세)", "var(--green)")); score += 1
    elif hyg < -0.002: signals.append(("유동성 위축 (하이일드 약세)", "var(--orange)")); score -= 1
    # 4) 금리·달러 역풍
    if g("미국채 10Y") > 0.01 or g("달러인덱스") > 0.005:
        signals.append(("금리·달러 역풍", "var(--orange)")); score -= 0.5

    if score >= 1.5:   verdict, vc = "강세 · 위험선호", "var(--up)"
    elif score <= -1.5:verdict, vc = "약세 · 위험회피", "var(--dn)"
    else:              verdict, vc = "중립 · 관망", "var(--t2)"
    return verdict, vc, signals

def _market_sessions():
    """국내/미국 장 개장 상태. (라벨, 열림여부, 보조문구) 리스트."""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo
    out = []
    # 한국 (09:00~15:30 KST, 평일)
    kst = datetime.now(ZoneInfo("Asia/Seoul"))
    kr_open = (kst.weekday() < 5 and dtime(9,0) <= kst.time() <= dtime(15,30))
    out.append(("국내", kr_open, kst.strftime("%H:%M KST")))
    # 미국 (09:30~16:00 ET, 평일) — DST는 zoneinfo가 자동 처리
    et = datetime.now(ZoneInfo("America/New_York"))
    us_open = (et.weekday() < 5 and dtime(9,30) <= et.time() <= dtime(16,0))
    out.append(("미국", us_open, et.strftime("%H:%M ET")))
    return out

_verdict, _vc, _sigs = _diag()
_chips = "".join(
    f"<span style='background:var(--bg3);border:1px solid var(--line);border-radius:6px;"
    f"padding:3px 9px;font-size:.7rem;color:{col};font-weight:600'>{txt}</span>"
    for txt, col in _sigs)
# 개장 상태 배지
_sess = "".join(
    f"<span style='display:inline-flex;align-items:center;gap:5px;"
    f"background:var(--bg3);border:1px solid var(--line);border-radius:6px;"
    f"padding:3px 9px;font-size:.7rem;font-weight:600'>"
    f"<span style='width:6px;height:6px;border-radius:50%;"
    f"background:{'#0FB873' if op else '#565E6B'};"
    f"{'box-shadow:0 0 5px #0FB873' if op else ''}'></span>"
    f"<span style='color:var(--t2)'>{lbl}</span>"
    f"<span style='color:{'#0FB873' if op else 'var(--t3)'}'>{'개장' if op else '마감'}</span>"
    f"<span style='color:var(--t3)'>{sub}</span></span>"
    for lbl, op, sub in _market_sessions())
st.markdown(f"""
<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin:-4px 0 12px;padding:6px 11px;background:var(--bg1);
  border:1px solid var(--line);border-radius:9px'>
  {_sess}
  <span style='color:var(--line2)'>|</span>
  <span style='font-size:.72rem;color:var(--t3);font-weight:700'>시장 진단</span>
  <span style='font-size:.86rem;font-weight:800;color:{_vc}'>{_verdict}</span>
  <span style='color:var(--line2)'>|</span>
  {_chips}
</div>""", unsafe_allow_html=True)


# ── 텔레그램 봇 명령 폴링 기동 (1회만, telegram 활성 시) ──────────────────────
try:
    import notifier as _ntf_boot
    _ncfg_bot = st.session_state.get("notify_cfg") or _ntf_boot.load_config()
    _tg_bot = _ncfg_bot.get("telegram", {})
    if _tg_bot.get("enabled") and _tg_bot.get("bot_token"):
        import telegram_bot as _tgb_boot
        if _tgb_boot.start(_tg_bot["bot_token"], _tg_bot.get("chat_id", "")):
            # 자동매매 OFF 등 사이클이 안 돌 때도 기본 상태는 보이도록 가벼운 초기 기록
            try:
                _pm_b0 = load_portfolio(paper=_is_paper_mode())
                _tgb_boot.write_status({
                    "auto_on": st.session_state.get("auto_enabled", False),
                    "paper": _is_paper_mode(),
                    "mode": "모의(페이퍼)" if _is_paper_mode() else "실거래",
                    "strategy": STRAT.get(st.session_state.get("active_strategy","composite"), ("복합",))[0],
                    "horizon": st.session_state.get("horizon", "단기"),
                    "alloc": "유동형" if st.session_state.get("alloc_dynamic", True) else "고정형",
                    "equity": round(_pm_b0.get("equity", 0)), "cash": round(_pm_b0.get("cash", 0)),
                    "invested": round(_pm_b0.get("t_cur", 0)),
                    "upnl": round(_pm_b0.get("t_cur", 0) - _pm_b0.get("t_inv", 0)),
                    "n_positions": len(_pm_b0.get("positions", [])),
                })
            except Exception:
                pass
except Exception:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# 메인 페이지
# ──────────────────────────────────────────────────────────────────────────────
cur = st.session_state["page"]

# ── 전역 빠른 주문: 대시보드 제외 모든 페이지에서 1클릭 매수/매도 ──────────────────
# (대시보드는 실시간 랭킹·종목 상세에 거래가 이미 통합돼 있어 제외)
if cur != "대시보드" and hasattr(st, "popover"):
    _qcol = st.columns([4, 1])[1]
    with _qcol:
        with st.popover("빠른 주문", width="stretch"):
            _qt = st.text_input("종목 티커", value=st.session_state.get("quick_ticker", "AAPL"),
                                key="quick_ticker").upper().strip()
            quick_trade_panel(_qt, key_prefix="quick", show_chart=False)

# ── "자동 트레이딩" = 라이브 + 백테스트 + AI 분석 통합 (서브탭으로 cur 재매핑) ──
# 기존 거대한 페이지 블록을 재들여쓰기하지 않기 위해, 한 사이드바 항목 아래
# 서브탭을 두고 cur 를 기존 블록 키로 바꿔치기한다(라이브/백테스트 블록 그대로 재사용).
if cur == "자동 트레이딩":
    _auto_tabs = {"라이브": "실시간 자동·수동 매매",
                  "백테스트": "과거 데이터로 전략 검증",
                  "AI 분석": "종목 진단·시그널"}
    _sub = st.segmented_control(
        "모드", list(_auto_tabs),
        default=st.session_state.get("auto_sub", "라이브"),
        key="auto_sub", label_visibility="collapsed") or \
        st.session_state.get("auto_sub", "라이브")
    st.markdown(f"<div style='font-size:.66rem;color:var(--t3);margin:2px 0 8px'>"
                f"{_auto_tabs.get(_sub, '')}</div>", unsafe_allow_html=True)
    cur = {"라이브": "라이브 트레이딩", "백테스트": "백테스트",
           "AI 분석": "AI 분석"}[_sub]

# ══════════════════════════════════════════════════════════════════════════════
if cur == "대시보드":
    import market_data as md

    connected = state["connected"]
    cash = state["cash"]; equity = state["equity"]
    t_inv = state["t_inv"]; t_cur = state["t_cur"]
    pnl = t_cur - t_inv; pnl_p = pnl / t_inv if t_inv > 0 else 0
    pup = pnl >= 0
    live_on = st.session_state.get("live_refresh", False)
    no_pos = len(positions) == 0

    # ════════ 토스식 통합 밴드: 좌=실시간 랭킹/상세 · 우=계좌/내 투자 도크 ════════
    import watchlist as _wl_dash
    _watch = _wl_dash.load()

    @st.cache_data(ttl=300, show_spinner=False)
    def _watch_rank(tickers: tuple) -> list:
        """워치리스트 일괄 시세 → 현재가·등락률·30일 시리즈·거래대금 (1회 배치)."""
        import yfinance as _yf4
        out = []
        if not tickers:
            return out
        try:
            raw = _yf4.download(list(tickers), period="1mo", interval="1d",
                                auto_adjust=True, progress=False, threads=True)
            cl = raw["Close"]
            vol = raw["Volume"] if "Volume" in getattr(raw, "columns",
                  raw.columns if hasattr(raw, "columns") else []) else None
            for t in tickers:
                try:
                    s = (cl[t] if hasattr(cl, "columns") else cl).dropna().tolist()
                    if len(s) < 2:
                        continue
                    price = float(s[-1]); prev = float(s[-2])
                    last_vol = 0.0
                    if vol is not None:
                        try:
                            vs = (vol[t] if hasattr(vol, "columns") else vol).dropna().tolist()
                            last_vol = float(vs[-1]) if vs else 0.0
                        except Exception:
                            last_vol = 0.0
                    out.append(dict(ticker=t, price=price,
                                    chg=(price-prev)/prev if prev else 0,
                                    series=[float(x) for x in s[-30:]],
                                    volume=last_vol, value=price*last_vol))
                except Exception:
                    pass
        except Exception:
            pass
        return out

    @st.cache_data(ttl=3600, show_spinner=False)
    def _fg():
        return md.fear_greed_index()

    _feed_snap = _rtf.get_all()
    _feed_src = "Finnhub" if any(v.get("source") == "finnhub"
                                 for v in _feed_snap.values()) else "yfinance"
    _feed_age = int(time.time() - min(
        (v.get("ts", time.time()) for v in _feed_snap.values()), default=time.time()))

    _main_col, _rail_col = st.columns([2.5, 1], gap="medium")

    # ─────────── 우측 도크: 계좌 + 내 투자 ───────────
    with _rail_col:
        if state.get("paper_mode"):
            _acc_badge = "<span style='color:#3F8CFF'>● 모의</span>"
        elif connected:
            _acc_badge = "<span style='color:#0FB873'>● 실계좌</span>"
        else:
            _acc_badge = "<span style='color:#FF9500'>● 실거래·미연동</span>"
        _acc_main = money_compact(equity)
        _pcol = "#F04452" if pup else "#2F80ED"
        st.markdown(
            "<div class='card' style='margin-bottom:8px'>"
            "<div style='display:flex;justify-content:space-between;align-items:center'>"
            "<span style='font-size:.58rem;color:var(--t3);font-weight:700;letter-spacing:.05em'>총 자산</span>"
            f"<span style='font-size:.58rem'>{_acc_badge}</span></div>"
            f"<div style='font-size:1.65rem;font-weight:900;letter-spacing:-.04em;line-height:1.15;margin-top:3px'>{_acc_main}</div>"
            f"<div style='margin-top:4px;font-size:.72rem;font-weight:700;color:{_pcol}'>"
            f"{'▲' if pup else '▼'} {money(abs(pnl))} ({pnl_p:+.2%}) "
            "<span style='color:var(--t3);font-weight:500'>미실현</span></div>"
            "<div style='display:flex;gap:10px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)'>"
            "<div style='flex:1'><div style='font-size:.55rem;color:var(--t3)'>현금</div>"
            f"<div style='font-size:.8rem;font-weight:700'>{money(cash)}</div></div>"
            "<div style='flex:1'><div style='font-size:.55rem;color:var(--t3)'>투자 중</div>"
            f"<div style='font-size:.8rem;font-weight:700'>{money(t_inv)}</div></div></div></div>",
            unsafe_allow_html=True)

        st.markdown(
            "<div style='font-size:.68rem;font-weight:800;margin:2px 0 2px'>내 투자 "
            f"<span style='color:var(--t3);font-weight:500;font-size:.6rem'>· {len(positions)}종목</span></div>",
            unsafe_allow_html=True)
        if not positions:
            st.markdown("<div style='font-size:.7rem;color:var(--t3);padding:6px 0'>보유 종목 없음</div>",
                        unsafe_allow_html=True)
        for p in sorted(positions, key=lambda x: x["pnl_usd"], reverse=True):
            _pc = "#F04452" if p["pnl_pct"] >= 0 else "#2F80ED"
            hr = st.columns([1.4, 1.2], vertical_alignment="center")
            clickable_ticker(hr[0], p["ticker"], key=f"holdrow_{p['ticker']}")
            hr[1].markdown(
                f"<div style='text-align:right'>"
                f"<div style='font-size:.74rem;font-weight:700'>{money(p['current']*p['shares'])}</div>"
                f"<div style='font-size:.62rem;font-weight:700;color:{_pc}'>{p['pnl_pct']:+.2%}</div></div>",
                unsafe_allow_html=True)

        _dash_trades = load_trades()
        _r_pnl = sum((t["exit_price"] - t["entry_price"]) * t.get("shares", 0)
                     for t in _dash_trades if t.get("entry_price"))
        _r_wins = sum(1 for t in _dash_trades
                      if t.get("entry_price") and t["exit_price"] > t["entry_price"])
        fg = _fg()
        _rp_c = "#F04452" if _r_pnl >= 0 else "#2F80ED"
        _wr_txt = f"{_r_wins}/{len(_dash_trades)}건" if _dash_trades else "—"
        st.markdown(
            "<div style='display:flex;gap:6px'>"
            "<div class='card' style='flex:1;margin-bottom:0'>"
            "<div style='font-size:.55rem;color:var(--t3)'>실현손익</div>"
            f"<div style='font-size:.88rem;font-weight:800;color:{_rp_c}'>{money(_r_pnl)}</div>"
            f"<div style='font-size:.58rem;color:var(--t3)'>{_wr_txt}</div></div>"
            "<div class='card' style='flex:1;margin-bottom:0'>"
            "<div style='font-size:.55rem;color:var(--t3)'>공포탐욕</div>"
            f"<div style='font-size:.88rem;font-weight:800;color:{fg['color']}'>{fg['score']}</div>"
            f"<div style='font-size:.58rem;color:var(--t3)'>{fg['label']}</div></div></div>"
            f"<div style='text-align:right;font-size:.56rem;color:var(--t3);margin-top:6px'>"
            f"{'● LIVE' if live_on else '○ STATIC'} · {_feed_src} · {_feed_age}s</div>",
            unsafe_allow_html=True)

        # 모의 계좌 관리 (페이퍼 모드일 때만)
        if state.get("paper_mode"):
            _seed = _paper.seed()
            _equity_now = _paper.cash() + t_cur          # 현금 + 보유 평가액
            _tot_ret = (_equity_now - _seed) / _seed if _seed else 0.0
            _tr_c = "#F04452" if _tot_ret >= 0 else "#2F80ED"
            _n_trades = len(_dash_trades)
            _wr = (_r_wins / _n_trades) if _n_trades else 0.0
            st.markdown(
                "<div class='card' style='margin-top:8px;border:1px solid rgba(63,140,255,.25)'>"
                "<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>"
                "<span style='font-size:.62rem;font-weight:800;color:#3F8CFF'>모의투자 성과</span>"
                f"<span style='font-size:.56rem;color:var(--t3)'>시작 {money_compact(_seed)}</span></div>"
                "<div style='display:flex;gap:8px'>"
                "<div style='flex:1'><div style='font-size:.54rem;color:var(--t3)'>누적 수익률</div>"
                f"<div style='font-size:.92rem;font-weight:900;color:{_tr_c}'>{_tot_ret:+.2%}</div></div>"
                "<div style='flex:1'><div style='font-size:.54rem;color:var(--t3)'>승률</div>"
                f"<div style='font-size:.92rem;font-weight:900'>{_wr:.0%} "
                f"<span style='font-size:.54rem;color:var(--t3);font-weight:600'>({_r_wins}/{_n_trades})</span></div></div>"
                "</div></div>",
                unsafe_allow_html=True)
            with st.popover("모의 계좌 설정", width="stretch"):
                st.caption(f"현금 {money(_paper.cash())} · 평가액 {money(t_cur)} · "
                           f"총 {money(_equity_now)}")
                st.caption("Alpaca 키 없이 현재가로 즉시 체결되는 연습 계좌입니다. "
                           "실거래 장부와 완전히 분리돼 있어 마음껏 연습할 수 있어요.")
                _seed_in = st.number_input("시작 자본 ($)", min_value=100,
                                           value=int(_seed), step=1000, key="paper_seed_in")
                _rc1, _rc2 = st.columns(2)
                if _rc1.button("이 금액으로 초기화", key="paper_reset_btn", type="primary"):
                    _paper.reset(float(_seed_in))
                    from portfolio import PortfolioManager as _PMr
                    _pr = _PMr(paper=True); _pr.positions = {}; _pr._save_state()
                    st.toast(f"모의 계좌 초기화 — 시작 자본 {money(_seed_in)}")
                    st.rerun()
                if _rc2.button("보유만 청산(현금 유지)", key="paper_clear_pos"):
                    from portfolio import PortfolioManager as _PMr
                    _pr = _PMr(paper=True)
                    # 보유 전량을 현재가로 청산 → 현금 환입
                    for _t, _pos in list(_pr.positions.items()):
                        _px = fetch_prices((_t,)).get(_t, _pos.entry_price)
                        _paper.adjust(_pos.shares * _px)
                        _pr.record_sell(_t, exit_price=_px, reason="manual")
                    st.toast("보유 종목을 모두 청산했습니다 (현금 환입)")
                    st.rerun()

    # ─────────── 좌측: 실시간 랭킹 테이블 + 종목 상세 ───────────
    with _main_col:
        st.markdown("<div class='stitle'>실시간 랭킹 "
                    "<span style='font-size:.66rem;color:var(--t3);font-weight:500'>· 내 워치리스트</span></div>",
                    unsafe_allow_html=True)
        _rk = _watch_rank(tuple(_watch))
        _flt = st.segmented_control(
            "필터", ["거래대금", "급상승", "급하락"],
            default=st.session_state.get("dash_rank_flt", "거래대금"),
            key="dash_rank_flt", label_visibility="collapsed") or "거래대금"
        if _flt == "급상승":
            _rk = sorted(_rk, key=lambda d: d["chg"], reverse=True)
        elif _flt == "급하락":
            _rk = sorted(_rk, key=lambda d: d["chg"])
        else:
            _rk = sorted(_rk, key=lambda d: d.get("value", 0), reverse=True)

        # 헤더
        st.markdown(
            "<div style='display:flex;font-size:.56rem;color:var(--t3);font-weight:600;"
            "border-bottom:1px solid var(--line2);padding:2px 0 4px;margin-top:4px'>"
            "<div style='width:7%'>#</div><div style='width:31%'>종목</div>"
            "<div style='width:17%;text-align:right'>현재가</div>"
            "<div style='width:15%;text-align:right'>등락률</div>"
            "<div style='width:16%;text-align:right'>거래대금</div>"
            "<div style='width:14%;text-align:right'>30일</div></div>",
            unsafe_allow_html=True)
        if not _rk:
            st.markdown("<div style='color:var(--t3);font-size:.74rem;padding:10px'>"
                        "시세를 불러오는 중…</div>", unsafe_allow_html=True)
        for i, r in enumerate(_rk[:14], 1):
            _up = r["chg"] >= 0; _chex = "#F0454F" if _up else "#3B82F6"
            _sv = _spark_svg(r["series"], _chex, w=64, h=20)
            cc = st.columns([0.6, 3.0, 1.7, 1.5, 1.6, 1.4], gap="small",
                            vertical_alignment="center")
            cc[0].markdown(f"<div style='color:var(--t3);font-size:.7rem'>{i}</div>",
                           unsafe_allow_html=True)
            if cc[1].button(f"{r['ticker']}  {_nm(r['ticker'])}",
                            key=f"rkbtn_{r['ticker']}", type="tertiary"):
                _stock_detail_dialog(r["ticker"])
            cc[2].markdown(f"<div style='text-align:right;font-weight:700;font-size:.8rem'>"
                           f"{r['price']:,.2f}</div>", unsafe_allow_html=True)
            cc[3].markdown(f"<div style='text-align:right;font-weight:700;font-size:.74rem;"
                           f"color:{_chex}'>{'▲' if _up else '▼'} {abs(r['chg']):.2%}</div>",
                           unsafe_allow_html=True)
            cc[4].markdown(f"<div style='text-align:right;font-size:.72rem;color:var(--t2)'>"
                           f"{_fmt_val(r.get('value',0))}</div>", unsafe_allow_html=True)
            cc[5].markdown(f"<div>{_sv}</div>", unsafe_allow_html=True)
        st.caption("종목명을 클릭하면 상세 차트·거래 창이 열립니다")

    # ── 실적 발표 캘린더 (워치리스트 기준, 병렬) ─────────────────────────────
    @st.cache_data(ttl=3600*6, show_spinner=False)
    def _earnings_calendar(tickers_tuple: tuple) -> list[dict]:
        import yfinance as yf, concurrent.futures as _cf
        def _one(tk):
            try:
                cal = yf.Ticker(tk).calendar
                if cal is None: return None
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed and len(ed) > 0:
                        return {"ticker": tk, "date": str(ed[0])[:10],
                                "eps_est": cal.get("EPS Estimate", [None])[0]}
                elif hasattr(cal, "T"):
                    _row = cal.T
                    if "Earnings Date" in _row.columns:
                        return {"ticker": tk, "date": str(_row["Earnings Date"].iloc[0])[:10],
                                "eps_est": None}
            except Exception:
                return None
            return None
        result = []
        with _cf.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(_one, tickers_tuple):
                if r: result.append(r)
        result.sort(key=lambda x: x.get("date",""))
        return result

    import watchlist as _wl_ec
    _ec_tickers = tuple(_wl_ec.load()[:20])
    _ec_data = _earnings_calendar(_ec_tickers)
    _ec_upcoming = [e for e in _ec_data
                    if e.get("date","") >= date.today().isoformat()][:8]
    if _ec_upcoming:
        st.markdown("<br/>", unsafe_allow_html=True)
        st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:8px'>"
                    "실적 발표 예정</div>", unsafe_allow_html=True)
        _ec_cols = st.columns(min(len(_ec_upcoming), 4))
        for _ei, _ec in enumerate(_ec_upcoming):
            _days = (pd.Timestamp(_ec["date"]) - pd.Timestamp.now()).days
            _dc = "var(--up)" if _days <= 3 else "var(--t2)"
            _ec_cols[_ei % 4].markdown(f"""
            <div class='card' style='padding:10px 12px;text-align:center'>
              <div style='font-weight:800;font-size:.9rem'>{_ec["ticker"]}</div>
              <div style='font-size:.72rem;color:{_dc};margin-top:3px'>
                {_ec["date"][5:]} ({_days}일 후)</div>
              {f"<div style='font-size:.68rem;color:var(--t3)'>EPS예상 ${_ec['eps_est']:.2f}</div>" if _ec.get("eps_est") else ""}
            </div>""", unsafe_allow_html=True)

    st.markdown("<br/>", unsafe_allow_html=True)

    # ══ 전략 매매 후보 (현재 전략이 매수 확률 높다고 본 종목 + 차트) ═══════════
    _astrat = st.session_state.get("active_strategy", "composite")
    _aname, _acolor = STRAT.get(_astrat, ("복합", "#05C072"))
    cand = strategy_candidates(_astrat, top_n=6)
    if cand:
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:10px'>"
            f"<span style='font-size:.9rem;font-weight:800'>전략 매매 후보</span>"
            f"<span style='font-size:.72rem;color:{_acolor};font-weight:700'>{_aname}</span>"
            f"<span style='font-size:.7rem;color:var(--t3)'>· 점수=매수 확률</span></div>",
            unsafe_allow_html=True)
        cgrid = st.columns([3, 2])
        with cgrid[0]:
            # 탭으로 후보 차트 (점수 높은 순)
            tabs = st.tabs([f"{c['ticker']} {c['score']:.0f}" for c in cand])
            for tab, c in zip(tabs, cand):
                with tab:
                    sig = ("매수" if c["score"] >= 65 else
                           "관심" if c["score"] >= 50 else "관망")
                    sig_c = ("#0FB873" if c["score"] >= 65 else
                             "#FF9500" if c["score"] >= 50 else "#565E6B")
                    chg_c = "#F0454F" if c["chg"] >= 0 else "#3B82F6"
                    st.markdown(
                        f"<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:6px'>"
                        f"<span style='font-size:1.5rem;font-weight:900'>${c['current']:.2f}</span>"
                        f"<span style='color:{chg_c};font-size:.84rem;font-weight:700'>"
                        f"{'▲' if c['chg']>=0 else '▼'} {abs(c['chg']):.1f}% (1M)</span>"
                        f"<span style='margin-left:auto;background:rgba(0,0,0,.0);"
                        f"color:{sig_c};font-weight:800;font-size:.82rem;"
                        f"border:1px solid {sig_c};border-radius:7px;padding:2px 10px'>"
                        f"{sig} · {c['score']:.0f}점</span></div>",
                        unsafe_allow_html=True)
                    ys = c.get("series") or []
                    if len(ys) >= 2:
                        r_,g_,b_ = int(chg_c[1:3],16),int(chg_c[3:5],16),int(chg_c[5:7],16)
                        ymn,ymx = min(ys),max(ys); pad=(ymx-ymn)*0.12 or ymx*0.01
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=list(range(len(ys))), y=ys, mode="lines",
                            line=dict(color=chg_c, width=2.5),
                            fill="tozeroy", fillcolor=f"rgba({r_},{g_},{b_},.06)",
                            hovertemplate="$%{y:.2f}<extra></extra>"))
                        fig.update_layout(**CL(height=200,
                            xaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
                            yaxis=dict(gridcolor="#1A1A25", showgrid=True, zeroline=False,
                                       tickfont=dict(size=10), tickprefix="$",
                                       range=[ymn-pad, ymx+pad])))
                        st.plotly_chart(fig, width="stretch",
                                        config={"displayModeBar":False})
        with cgrid[1]:
            st.markdown("<div style='font-size:.7rem;color:var(--t3);font-weight:600;"
                        "letter-spacing:.05em;text-transform:uppercase;margin-bottom:8px'>"
                        "매수 우선순위</div>", unsafe_allow_html=True)
            for c in cand:
                t_ = c["score"]
                bar_c = ("#0FB873" if t_>=65 else "#FF9500" if t_>=50 else "#565E6B")
                pcc = st.columns([1.5, 2, 0.6], vertical_alignment="center")
                clickable_ticker(pcc[0], c["ticker"], key=f"candrow_{c['ticker']}")
                pcc[1].markdown(
                    f"<div style='width:100%;height:5px;background:var(--bg4);border-radius:3px;margin-top:8px'>"
                    f"<div style='width:{min(t_,100):.0f}%;height:5px;border-radius:3px;background:{bar_c}'></div></div>",
                    unsafe_allow_html=True)
                pcc[2].markdown(
                    f"<div style='font-weight:800;font-size:.84rem;color:{bar_c};text-align:right'>{t_:.0f}</div>",
                    unsafe_allow_html=True)
            if st.toggle("후보 바로 거래", key="dash_cand_on"):
                _cand_tks = [c["ticker"] for c in cand]
                _pick = st.selectbox("종목", _cand_tks, key="dash_cand_pick",
                                     label_visibility="collapsed")
                quick_trade_panel(_pick, key_prefix="dash_cand", show_chart=False)
            st.markdown("<div style='font-size:.66rem;color:var(--t3);margin-top:6px'>"
                        "자동 자본배분(전량/분할)은 라이브 트레이딩에서 실행</div>",
                        unsafe_allow_html=True)
        st.markdown("<br/>", unsafe_allow_html=True)

    # ══ 보유 종목 있을 때 ══════════════════════════════════════════════════════
    if not no_pos:
        cc, cp = st.columns([3, 2])
        with cc:
            t1, t2 = st.tabs(["자산 추이","인트라데이"])
            with t1:
                pb = st.columns(5)
                pm2 = {"1M":21,"3M":63,"6M":126,"1Y":252,"전체":504}
                sel = st.session_state.get("period","6M")
                for i, lb in enumerate(pm2):
                    if pb[i].button(lb, key=f"p_{lb}"):
                        st.session_state["period"] = lb; sel = lb
                spy = fetch_history("SPY","2y")
                # 기준 자산: 연동 시 실제 equity, 미연동 시 보유 평가액
                _base_eq = equity if (connected and equity) else (t_cur or 0)
                if not spy.empty and _base_eq > 0:
                    cs = spy["Close"].squeeze().iloc[-pm2[sel]:]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=cs.index,y=(cs/cs.iloc[0])*_base_eq,
                        name="포트폴리오",mode="lines",line=dict(color="#3182F6",width=2.5),
                        fill="tozeroy",fillcolor="rgba(49,130,246,.05)"))
                    fig.add_trace(go.Scatter(x=cs.index,y=(cs/cs.iloc[0])*_base_eq,
                        name="S&P500",mode="lines",line=dict(color="#2A2A38",width=1.5,dash="dot")))
                    fig.update_layout(**CL(height=200,
                        yaxis=dict(**_YA,tickprefix="$",tickformat=",.0f"),
                        legend=dict(orientation="h",y=1.12,x=0,font=dict(size=10))))
                    st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
            with t2:
                it = ["SPY"]+[p["ticker"] for p in positions[:3]]
                sel_i = st.selectbox("",it,label_visibility="collapsed",key="isel")
                idf = fetch_intraday(sel_i)
                if not idf.empty:
                    ic = idf["Close"].squeeze()
                    op2 = float(ic.iloc[0]); cur2 = float(ic.iloc[-1])
                    dc = (cur2-op2)/op2
                    lc = "#F04452" if dc>=0 else "#2F80ED"
                    r2,g2,b2 = int(lc[1:3],16),int(lc[3:5],16),int(lc[5:7],16)
                    st.markdown(f"""<div style='display:flex;align-items:baseline;
                      gap:10px;margin-bottom:6px'>
                      <span style='font-size:1.4rem;font-weight:900'>${cur2:.2f}</span>
                      <span style='color:{lc};font-size:.86rem;font-weight:700'>
                        {"▲" if dc>=0 else "▼"} {abs(dc):.2%}</span>
                    </div>""", unsafe_allow_html=True)
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=idf.index,y=ic,mode="lines",
                        line=dict(color=lc,width=2.5),fill="tozeroy",
                        fillcolor=f"rgba({r2},{g2},{b2},.06)",
                        hovertemplate="%{x|%H:%M}  $%{y:.2f}<extra></extra>"))
                    fig.add_hline(y=op2,line_dash="dash",line_color="#33333F",
                        annotation_text="시가",annotation_font_color="#4A5260",
                        annotation_font_size=10)
                    _imn,_imx = float(ic.min()),float(ic.max())
                    _ipad = (_imx-_imn)*0.15 or _imx*0.002
                    fig.update_layout(**CL(height=185,
                        xaxis=dict(**_XA,tickformat="%H:%M"),
                        yaxis=dict(gridcolor="#1A1A25",showgrid=True,zeroline=False,
                                   tickfont=dict(size=10),tickprefix="$",
                                   range=[_imn-_ipad,_imx+_ipad])))
                    st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
                else:
                    st.markdown("<div style='text-align:center;padding:36px;color:var(--t3);"
                                "font-size:.84rem'>장 마감 후 데이터 없음</div>",unsafe_allow_html=True)

        with cp:
            st.markdown("<div style='font-weight:700;font-size:.84rem;margin-bottom:10px;"
                        "color:var(--t2)'>보유 종목</div>", unsafe_allow_html=True)
            for p in positions:
                c = "#F04452" if p["pnl_pct"]>=0 else "#2F80ED"
                st.markdown(f"""
                <div class='srow'>
                  <div>
                    <div class='sticker'>{p["ticker"]}</div>
                    <div class='ssub'>{p["shares"]}주 · {p["held"]}일 보유</div>
                  </div>
                  <div style='text-align:right'>
                    <div style='font-weight:700;font-size:.88rem'>${p["current"]:.2f}</div>
                    <div style='color:{c};font-size:.76rem;font-weight:600;margin-top:1px'>
                      {"▲" if p["pnl_pct"]>=0 else "▼"} {p["pnl_pct"]:+.2%}</div>
                  </div>
                </div>""", unsafe_allow_html=True)

    # ══ 보유 종목 없을 때 → 시장 쇼케이스 ════════════════════════════════════
    else:
        # 주식 쇼케이스 (자동 순환)
        @st.cache_data(ttl=120, show_spinner=False)
        def _showcase(): return md.showcase_data()
        showcase = _showcase()

        if showcase:
            # 30초마다 다음 종목으로 자동 전환
            idx = (int(time.time()) // 30) % len(showcase)
            s = showcase[idx]
            next_idx = (idx+1) % len(showcase)
            next_t = showcase[next_idx]["ticker"]

            chg_c = "#F04452" if s["chg"] >= 0 else "#2F80ED"
            r_, g_, b_ = int(chg_c[1:3],16), int(chg_c[3:5],16), int(chg_c[5:7],16)
            arr_ = "▲" if s["chg"] >= 0 else "▼"

            sc1, sc2 = st.columns([3, 2])
            with sc1:
                st.markdown(f"""
                <div style='margin-bottom:10px;display:flex;align-items:center;
                  justify-content:space-between'>
                  <div style='font-size:.68rem;color:var(--t3)'>
                    주요 종목 · {idx+1}/{len(showcase)} · 30초마다 자동 전환
                    <span style='color:var(--t3);margin-left:8px'>다음: {next_t}</span>
                  </div>
                </div>
                <div style='display:flex;align-items:baseline;gap:12px;margin-bottom:8px'>
                  <span style='font-size:1.1rem;font-weight:800;color:var(--t3)'>{s["ticker"]}</span>
                  <span style='font-size:1.8rem;font-weight:900;letter-spacing:-.04em'>${s["current"]:.2f}</span>
                  <span style='color:{chg_c};font-size:.96rem;font-weight:700'>
                    {arr_} {abs(s["chg"]):.2f}%</span>
                </div>""", unsafe_allow_html=True)

                if s.get("series"):
                    ys = s["series"]
                    xs = list(range(len(ys)))
                    ymn, ymx = min(ys), max(ys)
                    pad = (ymx - ymn) * 0.15 or ymx * 0.002
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines",
                        line=dict(color=chg_c, width=2.5),
                        fill="tozeroy", fillcolor=f"rgba({r_},{g_},{b_},.07)",
                        hovertemplate="$%{y:.2f}<extra></extra>"))
                    fig.add_hline(y=s["open"], line_dash="dash",
                        line_color="#33333F",
                        annotation_text="시가", annotation_font_color="#4A5260",
                        annotation_font_size=10)
                    fig.update_layout(**CL(height=220,
                        xaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
                        yaxis=dict(gridcolor="#1A1A25", showgrid=True, zeroline=False,
                                   tickfont=dict(size=10), tickprefix="$",
                                   range=[ymn-pad, ymx+pad])))
                    st.plotly_chart(fig, width="stretch",
                                    config={"displayModeBar":False})

                # 종목 탭
                tabs_show = st.tabs([s2["ticker"] for s2 in showcase[:6]])
                for ti, (tab, s2) in enumerate(zip(tabs_show, showcase[:6])):
                    with tab:
                        if s2.get("series"):
                            yy = s2["series"]
                            tc2 = "#F04452" if s2["chg"]>=0 else "#2F80ED"
                            r2_,g2_,b2_ = int(tc2[1:3],16),int(tc2[3:5],16),int(tc2[5:7],16)
                            arr2 = "▲" if s2["chg"]>=0 else "▼"
                            st.markdown(f"""
                            <div style='display:flex;align-items:baseline;gap:8px'>
                              <span style='font-size:1.2rem;font-weight:800'>${s2["current"]:.2f}</span>
                              <span style='color:{tc2};font-size:.82rem;font-weight:700'>
                                {arr2} {abs(s2["chg"]):.2f}%</span>
                            </div>""", unsafe_allow_html=True)
                            ymn2, ymx2 = min(yy), max(yy)
                            pad2 = (ymx2-ymn2)*0.15 or ymx2*0.002
                            fig2 = go.Figure()
                            fig2.add_trace(go.Scatter(x=list(range(len(yy))),y=yy,
                                mode="lines",line=dict(color=tc2,width=2),
                                fill="tozeroy",fillcolor=f"rgba({r2_},{g2_},{b2_},.06)"))
                            fig2.update_layout(**CL(height=130,
                                xaxis=dict(showgrid=False,showticklabels=False,zeroline=False),
                                yaxis=dict(gridcolor="#1A1A25",showgrid=True,zeroline=False,
                                           tickfont=dict(size=10),tickprefix="$",
                                           range=[ymn2-pad2,ymx2+pad2])))
                            st.plotly_chart(fig2,width="stretch",
                                           config={"displayModeBar":False})

            with sc2:
                # 상승/하락 상위
                @st.cache_data(ttl=300, show_spinner=False)
                def _movers(): return md.top_movers(5)
                movers = _movers()

                st.markdown("<div style='font-size:.7rem;color:var(--t3);font-weight:600;"
                            "letter-spacing:.05em;text-transform:uppercase;"
                            "margin-bottom:8px'>상승</div>", unsafe_allow_html=True)
                for m in movers.get("gainers",[]):
                    st.markdown(f"""
                    <div class='srow'>
                      <span style='font-weight:700;font-size:.86rem'>{m["ticker"]}</span>
                      <span style='color:#F04452;font-weight:700;font-size:.84rem'>
                        ▲ {abs(m["chg"]):.2f}%</span>
                    </div>""", unsafe_allow_html=True)

                st.markdown("<div style='font-size:.7rem;color:var(--t3);font-weight:600;"
                            "letter-spacing:.05em;text-transform:uppercase;"
                            "margin:12px 0 8px'>하락</div>", unsafe_allow_html=True)
                for m in reversed(movers.get("losers",[])):
                    st.markdown(f"""
                    <div class='srow'>
                      <span style='font-weight:700;font-size:.86rem'>{m["ticker"]}</span>
                      <span style='color:#2F80ED;font-weight:700;font-size:.84rem'>
                        ▼ {abs(m["chg"]):.2f}%</span>
                    </div>""", unsafe_allow_html=True)

    # ── 섹터 히트맵 ─────────────────────────────────────────────────────────
    st.markdown("<br/>", unsafe_allow_html=True)
    sec1, sec2 = st.columns([3, 2])

    with sec1:
        st.markdown("<div style='font-size:.7rem;color:var(--t3);font-weight:600;"
                    "letter-spacing:.05em;text-transform:uppercase;"
                    "margin-bottom:10px'>섹터 성과 (5일)</div>", unsafe_allow_html=True)

        @st.cache_data(ttl=3600, show_spinner=False)
        def _sectors(): return md.sector_performance("5d")
        sectors = _sectors()

        if sectors:
            names = [s["name"] for s in sectors]
            rets  = [s["ret"]  for s in sectors]
            colors = ["#F04452" if r>=0 else "#2F80ED" for r in rets]
            fig_s = go.Figure(go.Bar(
                x=rets, y=names, orientation="h",
                marker_color=colors, text=[f"{r:+.2f}%" for r in rets],
                textposition="outside", textfont=dict(size=10, color="#8B95A1"),
                hovertemplate="%{y}: %{x:.2f}%<extra></extra>"))
            fig_s.update_layout(**CL(height=220,
                xaxis=dict(gridcolor="#1A1A25", showgrid=True, ticksuffix="%",
                           zeroline=True, zerolinecolor="#33333F", zerolinewidth=1,
                           tickfont=dict(size=10)),
                yaxis=dict(tickfont=dict(size=10, color="#8B95A1")),
                margin=dict(l=0,r=40,t=6,b=0)))
            st.plotly_chart(fig_s, width="stretch",
                            config={"displayModeBar":False})

    with sec2:
        st.markdown("<div style='font-size:.7rem;color:var(--t3);font-weight:600;"
                    "letter-spacing:.05em;text-transform:uppercase;"
                    "margin-bottom:10px'>시장 심리</div>", unsafe_allow_html=True)
        fg = _fg()
        score = fg["score"]
        bar_w = score
        fg_bg = "rgba(240,68,82,.08)" if score>=50 else "rgba(47,128,237,.08)"
        fg_bd = "rgba(240,68,82,.3)" if score>=50 else "rgba(47,128,237,.3)"
        st.markdown(f"""
        <div style='background:{fg_bg};border:1px solid {fg_bd};
          border-radius:12px;padding:18px 20px'>
          <div style='font-size:.7rem;color:var(--t3);margin-bottom:10px'>
            공포탐욕지수</div>
          <div style='font-size:2rem;font-weight:900;color:{fg["color"]}'>{score}</div>
          <div style='font-size:.84rem;font-weight:700;color:{fg["color"]};
            margin-top:4px'>{fg["label"]}</div>
          <div style='background:var(--bg4);border-radius:4px;height:6px;margin-top:12px'>
            <div style='width:{bar_w}%;height:6px;border-radius:4px;
              background:{fg["color"]};transition:width .3s'></div>
          </div>
          <div style='display:flex;justify-content:space-between;
            font-size:.62rem;color:var(--t3);margin-top:4px'>
            <span>극공포 0</span><span>50 중립</span><span>100 극탐욕</span>
          </div>
        </div>""", unsafe_allow_html=True)

        # 뉴스 헤드라인
        @st.cache_data(ttl=600, show_spinner=False)
        def _news(): return md.market_news(4)
        news = _news()
        if news:
            st.markdown("<div style='font-size:.7rem;color:var(--t3);font-weight:600;"
                        "letter-spacing:.05em;text-transform:uppercase;"
                        "margin:14px 0 8px'>시장 뉴스</div>", unsafe_allow_html=True)
            for n in news:
                st.markdown(f"""
                <div style='padding:7px 0;border-bottom:1px solid var(--line)'>
                  <div style='font-size:.78rem;font-weight:600;color:var(--t1);
                    line-height:1.4'>{n["headline"]}</div>
                  <div style='font-size:.66rem;color:var(--t3);margin-top:2px'>
                    {n["source"]}</div>
                </div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "주식 열람":
    import stock_browser as sb
    st.markdown("<div class='stitle'>주식 열람</div>", unsafe_allow_html=True)

    # ── 검색바 + 탭 ────────────────────────────────────────────────────────────
    _db_sz = sb.db_size()
    brow_query = st.text_input(
        "", "", placeholder=f" 티커 또는 회사명 — DB {_db_sz}종목 · 없는 종목도 입력 가능",
        label_visibility="collapsed", key="browser_query"
    )

    # 국내/미국 탭
    us_tab, kr_tab = st.tabs(["🇺🇸  미국", "🇰🇷  국내"])

    # ── 공통: 빠른 배치 스냅샷 함수 (60초 캐시) ──────────────────────────────
    @st.cache_data(ttl=60, show_spinner=False)
    def _snap(tickers_tuple: tuple) -> dict:
        return sb.get_batch_snapshot(list(tickers_tuple), period="6d")

    # 필터 칩 렌더링 헬퍼
    def _filter_chips(prefix: str, opts: list[str]) -> str:
        cur_f = st.session_state.get(f"bfilt_{prefix}", opts[0])
        cols_f = st.columns(len(opts))
        for ci, opt in enumerate(opts):
            label = opt
            if cols_f[ci].button(label, key=f"bf_{prefix}_{opt}",
                                 type="primary" if opt==cur_f else "secondary"):
                st.session_state[f"bfilt_{prefix}"] = opt
                cur_f = opt
        return cur_f

    # ── 종목 목록 테이블 렌더러 ────────────────────────────────────────────────
    def _render_table(tickers: list[str], snap: dict, db_map: dict,
                      filter_mode: str, prefix: str, is_kr: bool = False):
        if not tickers or not snap:
            st.markdown("<div style='color:var(--t3);padding:12px;font-size:.8rem'>"
                        "데이터 없음</div>", unsafe_allow_html=True)
            return

        # 정렬
        def _sort_key(tk):
            s = snap.get(tk, {})
            if filter_mode in ("급등", "급하락"):
                return s.get("change_pct", 0)
            if filter_mode == "거래량":
                return s.get("volume", 0)
            # 실시간 시총 우선, 없으면 근사값
            return s.get("market_cap", sb._APPROX_MCAP.get(tk, 0) * 1e8)

        rev = (filter_mode != "급하락")
        sorted_tks = sorted(
            [t for t in tickers if t in snap],
            key=_sort_key, reverse=rev
        )[:40]

        price_sym = "₩" if is_kr else "$"

        # 헤더
        st.markdown(f"""
        <div style='display:grid;
          grid-template-columns:32px 160px 1fr 96px 88px 88px 68px;
          gap:0;padding:7px 8px 6px;font-size:.64rem;color:var(--t3);
          font-weight:700;letter-spacing:.05em;text-transform:uppercase;
          border-bottom:1px solid var(--line);margin-top:4px'>
          <span>#</span><span>종목</span><span></span>
          <span style='text-align:right'>현재가</span>
          <span style='text-align:right'>등락</span>
          <span style='text-align:right'>거래량</span>
          <span style='text-align:right'>5일</span>
        </div>""", unsafe_allow_html=True)

        rows_html = ""
        for i, tk in enumerate(sorted_tks, 1):
            s   = snap[tk]
            price = s["price"]; chg = s["change_pct"]; vol = s["volume"]
            spark = sb.sparkline_svg(s.get("sparkline", []))
            cc  = "#F04452" if chg >= 0 else "#2F80ED"
            ar  = "▲" if chg >= 0 else "▼"
            # 가격 포맷: 국내는 정수 원화, 미국은 달러
            pf = f"{price:,.0f}" if is_kr else f"{price:,.2f}"
            vol_s = (f"{vol/1e8:.1f}억" if is_kr and vol>1e8 else
                     f"{vol/1e4:.0f}만" if is_kr else
                     f"{vol/1e6:.1f}M" if vol>1e6 else
                     f"{vol/1e3:.0f}K" if vol>1e3 else str(vol))
            nm  = db_map.get(tk, tk)[:16]
            # 배지
            badge_c = ("#F04452" if chg>3 else "#FF9500" if chg>1 else
                       "#2F80ED" if chg<-3 else "#3B82F6" if chg<-1 else "#3B3B4A")
            rows_html += f"""
            <div onclick="window.parent.postMessage({{tk:'{tk}'}}, '*')"
              id="br_{prefix}_{i}"
              style='display:grid;
                grid-template-columns:32px 160px 1fr 96px 88px 88px 68px;
                gap:0;padding:9px 8px;border-bottom:1px solid var(--line);
                align-items:center;cursor:pointer;transition:background .1s'
              onmouseover="this.style.background='#16161C'"
              onmouseout="this.style.background='transparent'">
              <span style='font-size:.68rem;color:var(--t3);font-weight:600'>{i}</span>
              <div>
                <div style='font-weight:800;font-size:.86rem'>{tk.replace(".KS","").replace(".KQ","")}</div>
                <div style='font-size:.68rem;color:var(--t3);margin-top:1px'>{nm}</div>
              </div>
              <span></span>
              <span style='text-align:right;font-weight:800;font-size:.88rem'>{price_sym}{pf}</span>
              <span style='text-align:right;color:{cc};font-weight:700;font-size:.82rem'>
                {ar} {abs(chg):.2%}</span>
              <span style='text-align:right;font-size:.72rem;color:var(--t3)'>{vol_s}</span>
              <span style='display:flex;justify-content:flex-end'>{spark}</span>
            </div>"""

        st.markdown(rows_html, unsafe_allow_html=True)

        # 클릭 → selectbox 연동 (버튼 방식)
        st.markdown("<div style='margin-top:8px;display:flex;flex-wrap:wrap;gap:6px'>",
                    unsafe_allow_html=True)
        btn_cols = st.columns(min(len(sorted_tks), 8))
        for i, tk in enumerate(sorted_tks[:8]):
            s = snap.get(tk, {}); chg = s.get("change_pct",0)
            cc = "#F04452" if chg>=0 else "#2F80ED"
            with btn_cols[i % 8]:
                if st.button(tk.replace(".KS","").replace(".KQ",""),
                             key=f"bq_{prefix}_{tk}"):
                    st.session_state["selected_stock"] = tk
        st.markdown("</div>", unsafe_allow_html=True)

        # 전체 목록 클릭용 셀렉트박스
        sel_opt = st.selectbox(
            "종목 선택 (전체 목록)",
            ["— 선택 —"] + sorted_tks,
            key=f"bsel_{prefix}",
            label_visibility="collapsed"
        )
        if sel_opt and sel_opt != "— 선택 —":
            st.session_state["selected_stock"] = sel_opt

    # ── 광역 스캔 캐시 (급등·급락용, 5분 TTL) ────────────────────────────────
    @st.cache_data(ttl=300, show_spinner=False)
    def _broad_snap_us() -> dict:
        """S&P500 전체 배치 스캔 — 5분 캐시."""
        db, _ = sb._get_db()
        all_us = [r[0] for r in db if not r[0].endswith((".KS", ".KQ"))][:200]
        return sb.get_batch_snapshot(all_us, period="3d")

    @st.cache_data(ttl=300, show_spinner=False)
    def _broad_snap_kr() -> dict:
        """국내 전체 배치 스캔 — 5분 캐시."""
        return sb.get_batch_snapshot(list(sb.KR_TOP), period="3d")

    # ── 탭별 유니버스 결정 ────────────────────────────────────────────────────
    def _get_universe(is_kr: bool, query: str, filt: str) -> list[str]:
        if query.strip():
            res = sb.search(query, "국내" if is_kr else "전체")
            if is_kr:
                res = [r for r in res if r["ticker"].endswith((".KS",".KQ"))]
            else:
                res = [r for r in res if not r["ticker"].endswith((".KS",".KQ"))]
            return [r["ticker"] for r in res[:40]]
        if is_kr:
            return list(sb.KR_TOP)
        # 급등·급락은 광역 유니버스
        if filt in ("급등", "급하락"):
            db, _ = sb._get_db()
            return [r[0] for r in db if not r[0].endswith((".KS",".KQ"))][:200]
        return list(sb.US_TOP)

    _db, _dbi = sb._get_db()
    _name_map = {r[0]: r[1] for r in _db}

    # ── 미국 탭 ──────────────────────────────────────────────────────────────
    with us_tab:
        _FILT_US = ["시가총액", "급등", "급하락", "거래량"]
        filt_us = _filter_chips("us", _FILT_US)

        us_universe = _get_universe(False, brow_query, filt_us)

        if filt_us in ("급등", "급하락") and not brow_query.strip():
            with st.spinner(" S&P500 광역 스캔 중… (5분 캐시)"):
                us_snap = _broad_snap_us()
        else:
            with st.spinner(" 시세 로딩…"):
                us_snap = _snap(tuple(us_universe))

        _render_table(us_universe, us_snap, _name_map, filt_us, "us", is_kr=False)

    # ── 국내 탭 ──────────────────────────────────────────────────────────────
    with kr_tab:
        st.markdown("<div style='font-size:.7rem;color:#FF9500;margin:2px 0 6px'>"
                    "🇰🇷 국내 종목은 시세 조회 전용입니다 (현재 브로커 미지원)</div>",
                    unsafe_allow_html=True)
        _FILT_KR = ["시가총액", "급등", "급하락", "거래량"]
        filt_kr = _filter_chips("kr", _FILT_KR)

        kr_universe = _get_universe(True, brow_query, filt_kr)

        if filt_kr in ("급등", "급하락") and not brow_query.strip():
            with st.spinner(" 국내 광역 스캔 중… (5분 캐시)"):
                kr_snap = _broad_snap_kr()
        else:
            with st.spinner(" 시세 로딩…"):
                kr_snap = _snap(tuple(kr_universe))

        _render_table(kr_universe, kr_snap, _name_map, filt_kr, "kr", is_kr=True)

    sel2 = st.session_state.get("selected_stock")
    if sel2:
        st.markdown("<hr/>", unsafe_allow_html=True)
        with st.spinner(f"{sel2} 로딩…"):
            detail = sb.get_quote(sel2)
            ohlcv = sb.get_ohlcv(sel2, "1y")
        dc3 = detail.get("change_pct", 0)
        dcc3 = "var(--up)" if dc3 >= 0 else "var(--dn)"
        st.markdown(f"""
        <div class='card' style='margin-top:8px'>
          <div style='display:flex;justify-content:space-between;align-items:center'>
            <div>
              <div style='font-size:.68rem;color:var(--t3);margin-bottom:4px'>
                {detail.get("sector","")}</div>
              <div style='font-size:1.05rem;font-weight:800'>{sel2}</div>
              <div style='font-size:.8rem;color:var(--t2);margin-top:2px'>
                {detail.get("name","")}</div>
            </div>
            <div style='text-align:right'>
              <div style='font-size:1.6rem;font-weight:900;letter-spacing:-.03em'>
                ${detail.get("price",0):.2f}</div>
              <div style='color:{dcc3};font-size:.86rem;font-weight:700;margin-top:3px'>
                {"▲" if dc3>=0 else "▼"} {abs(dc3):.2%}</div>
            </div>
          </div>
        </div>""", unsafe_allow_html=True)

        d1, d2, d3, d4 = st.columns(4)
        kpi(d1, "P/E", f"{detail['pe']:.1f}" if detail.get("pe") else "—",
            f"Fwd {detail['forward_pe']:.1f}" if detail.get("forward_pe") else None)
        rg = detail.get("revenue_growth")
        kpi(d2, "매출 성장", f"{rg:+.1%}" if rg else "—", None,
            "var(--green)" if (rg or 0) > 0 else "var(--up)")
        tp3 = detail.get("target_price"); cp3 = detail.get("price", 0)
        kpi(d3, "목표주가", f"${tp3:.0f}" if tp3 else "—",
            f"{(tp3-cp3)/cp3:+.1%}" if tp3 and cp3 else None, "var(--orange)")
        hi = detail.get("52w_high"); lo = detail.get("52w_low")
        kpi(d4, "52주 범위", f"${lo:.0f}~${hi:.0f}" if hi and lo else "—",
            f"위치 {(cp3-lo)/(hi-lo)*100:.0f}%" if hi and lo and cp3 else None)

        # ── 기간/간격 선택
        _tf_row = st.columns([*([1]*len(TF_PRESETS)), 2])
        tf_sel = st.session_state.get("tf_sel", "일")
        for i, lb in enumerate(TF_PRESETS):
            if _tf_row[i].button(lb, key=f"tf_{lb}",
                                 type="primary" if lb == tf_sel else "secondary"):
                st.session_state["tf_sel"] = lb; tf_sel = lb
        # 지표 선택 체크박스
        with _tf_row[-1]:
            _ind_opts = st.multiselect("지표", ["BB", "RSI", "MACD"],
                                       default=st.session_state.get("chart_inds", []),
                                       key="chart_inds_sel", label_visibility="collapsed")
            st.session_state["chart_inds"] = _ind_opts

        tf_period, tf_interval = TF_PRESETS[tf_sel]
        chart_df = fetch_ohlcv_tf(sel2, tf_period, tf_interval)

        if not chart_df.empty:
            import ta
            cl_ = chart_df["Close"].squeeze()
            hi_ = chart_df["High"].squeeze()
            lo_ = chart_df["Low"].squeeze()

            _inds = st.session_state.get("chart_inds", [])
            _show_rsi  = "RSI"  in _inds
            _show_macd = "MACD" in _inds
            _show_bb   = "BB"   in _inds
            _n_sub = sum([_show_rsi, _show_macd])  # 서브패널 개수

            # ── 서브플롯 구성 ──────────────────────────────────────────────
            from plotly.subplots import make_subplots
            _row_h = [0.55] + [0.225]*_n_sub if _n_sub else [1.0]
            _rows  = 1 + _n_sub
            fig = make_subplots(rows=_rows, cols=1, shared_xaxes=True,
                                vertical_spacing=0.04, row_heights=_row_h)

            # ── 캔들 ──────────────────────────────────────────────────────
            fig.add_trace(go.Candlestick(
                x=chart_df.index,
                open=chart_df["Open"].squeeze(), high=hi_,
                low=lo_, close=cl_,
                increasing_line_color="#F04452", decreasing_line_color="#2F80ED",
                increasing_fillcolor="rgba(240,68,82,.7)",
                decreasing_fillcolor="rgba(47,128,237,.7)", name=sel2),
                row=1, col=1)

            # ── 이동평균 ───────────────────────────────────────────────────
            if len(cl_) >= 20:
                for _ma, _mc in [(20,"#FF9500"),(60,"#A855F7")]:
                    if len(cl_) >= _ma:
                        fig.add_trace(go.Scatter(x=chart_df.index,
                            y=cl_.rolling(_ma).mean(), name=f"MA{_ma}",
                            line=dict(color=_mc, width=1.2), mode="lines"),
                            row=1, col=1)

            # ── 볼린저밴드 ─────────────────────────────────────────────────
            if _show_bb and len(cl_) >= 20:
                _bb = ta.volatility.BollingerBands(cl_, window=20, window_dev=2)
                fig.add_trace(go.Scatter(x=chart_df.index, y=_bb.bollinger_hband(),
                    name="BB상단", line=dict(color="rgba(100,160,255,.6)",width=1),
                    mode="lines"), row=1, col=1)
                fig.add_trace(go.Scatter(x=chart_df.index, y=_bb.bollinger_lband(),
                    name="BB하단", line=dict(color="rgba(100,160,255,.6)",width=1),
                    fill="tonexty", fillcolor="rgba(100,160,255,.05)",
                    mode="lines"), row=1, col=1)
                fig.add_trace(go.Scatter(x=chart_df.index, y=_bb.bollinger_mavg(),
                    name="BB중간", line=dict(color="rgba(100,160,255,.35)",
                    width=1, dash="dot"), mode="lines"), row=1, col=1)

            # ── RSI 서브패널 ───────────────────────────────────────────────
            _cur_sub = 2
            if _show_rsi and len(cl_) >= 14:
                _rsi = ta.momentum.RSIIndicator(cl_, window=14).rsi()
                fig.add_trace(go.Scatter(x=chart_df.index, y=_rsi,
                    name="RSI", line=dict(color="#FF9500", width=1.5), mode="lines"),
                    row=_cur_sub, col=1)
                fig.add_hline(y=70, line_dash="dot", line_color="#F04452",
                              line_width=1, row=_cur_sub, col=1)
                fig.add_hline(y=30, line_dash="dot", line_color="#2F80ED",
                              line_width=1, row=_cur_sub, col=1)
                fig.add_hrect(y0=70, y1=100, fillcolor="rgba(240,68,82,.05)",
                              line_width=0, row=_cur_sub, col=1)
                fig.add_hrect(y0=0, y1=30, fillcolor="rgba(47,128,237,.05)",
                              line_width=0, row=_cur_sub, col=1)
                fig.update_yaxes(title_text="RSI", range=[0,100],
                    tickfont=dict(size=9), gridcolor="#1A1A25",
                    row=_cur_sub, col=1)
                _cur_sub += 1

            # ── MACD 서브패널 ──────────────────────────────────────────────
            if _show_macd and len(cl_) >= 26:
                _macd_ind = ta.trend.MACD(cl_, window_slow=26, window_fast=12,
                                          window_sign=9)
                _macd_line = _macd_ind.macd()
                _signal    = _macd_ind.macd_signal()
                _hist      = _macd_ind.macd_diff()
                _bar_colors = ["#F04452" if v >= 0 else "#2F80ED"
                               for v in (_hist.fillna(0))]
                fig.add_trace(go.Bar(x=chart_df.index, y=_hist,
                    name="히스토그램", marker_color=_bar_colors, opacity=0.6),
                    row=_cur_sub, col=1)
                fig.add_trace(go.Scatter(x=chart_df.index, y=_macd_line,
                    name="MACD", line=dict(color="#3B82F6", width=1.5),
                    mode="lines"), row=_cur_sub, col=1)
                fig.add_trace(go.Scatter(x=chart_df.index, y=_signal,
                    name="Signal", line=dict(color="#FF9500", width=1.2),
                    mode="lines"), row=_cur_sub, col=1)
                fig.add_hline(y=0, line_color="#3A3A4A", line_width=1,
                              row=_cur_sub, col=1)
                fig.update_yaxes(title_text="MACD",
                    tickfont=dict(size=9), gridcolor="#1A1A25",
                    row=_cur_sub, col=1)

            # ── 레이아웃 ───────────────────────────────────────────────────
            _total_h = 300 + _n_sub * 130
            _ymn, _ymx = float(cl_.min()), float(cl_.max())
            _pad = (_ymx - _ymn) * 0.08 or _ymx * 0.01
            fig.update_layout(**CL(height=_total_h,
                xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", y=1.05, x=0, font=dict(size=9))))
            fig.update_yaxes(gridcolor="#1A1A25", showgrid=True, zeroline=False,
                             tickfont=dict(size=10), tickprefix="$",
                             range=[_ymn-_pad, _ymx+_pad], row=1, col=1)
            st.plotly_chart(fig, width="stretch",
                            config={"displayModeBar": False})

        import watchlist as wl
        if sel2 not in wl.load():
            if st.button(f"워치리스트 추가 ({sel2})"):
                res = wl.add(sel2)
                if res["ok"]: st.markdown(f"<div class='ok'>추가됨: {sel2}</div>",
                                          unsafe_allow_html=True)
                else: st.markdown(f"<div class='fail'>{res['error']}</div>",
                                  unsafe_allow_html=True)
        else:
            st.markdown("<span class='bg_'>워치리스트 등록됨</span>",
                        unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "전략 선택":
    st.markdown("<div class='stitle'>전략 선택</div>", unsafe_allow_html=True)

    if mi:
        rec = mi.get("recommended_strategy","composite")
        rn, rc = STRAT.get(rec, ("복합","#05C072"))
        spy1m = mi.get("spy_1m",0); vix_ = mi.get("vix",0); br_ = mi.get("breadth",50)
        sp_cls = "bu" if spy1m > 0 else "bd"
        # ── 현재 시장에 맞는 추천 투자 기간 + 대략적 청산 시점 계산 ──
        _trend = mi.get("trend", "neutral")
        if _trend == "volatile" or vix_ >= 28:   _rec_hz = "단타"
        elif _trend == "bear":                   _rec_hz = "단기"
        elif _trend == "bull":                   _rec_hz = "중장기"
        else:                                    _rec_hz = "단기"
        _rhp = horizon_params(_rec_hz)
        _dmin, _dmax = _rhp["days"]
        from datetime import timedelta as _td_rec
        # 거래일 → 달력일 근사(×1.4) 후 대략적 청산 시점
        _ex_lo = (date.today() + _td_rec(days=int(_dmin*1.4))).strftime("%Y-%m-%d")
        _ex_hi = (date.today() + _td_rec(days=int(_dmax*1.4))).strftime("%Y-%m-%d")
        st.markdown(f"""
        <div class='card' style='border-color:rgba(49,130,246,.3);
          background:rgba(49,130,246,.04);margin-bottom:16px'>
          <div style='font-size:.68rem;color:var(--blue);font-weight:700;
            letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px'>
            AI 자동 추천</div>
          <div style='font-size:1rem;font-weight:800;color:{rc}'>{rn}</div>
          <div style='font-size:.8rem;color:var(--t2);margin-top:5px'>
            {mi.get("reason","")}</div>
          <div style='margin-top:10px;padding-top:9px;border-top:1px solid var(--line)'>
            <span style='font-size:.7rem;color:var(--t3)'>추천 투자 기간</span>
            <span style='font-size:.86rem;font-weight:800;color:var(--t1);margin-left:6px'>{_rec_hz}</span>
            <span style='font-size:.72rem;color:var(--t3)'>({_rhp['label']})</span>
            <div style='font-size:.74rem;color:var(--t2);margin-top:3px'>
              대략 보유 {_dmin}~{_dmax}거래일 · 예상 청산 시점 <b style='color:var(--t1)'>{_ex_lo} ~ {_ex_hi}</b></div>
          </div>
          <div style='display:flex;gap:6px;margin-top:10px'>
            <span class='bn'>VIX {vix_:.0f}</span>
            <span class='{sp_cls}'>SPY {spy1m:+.1f}%</span>
            <span class='bn'>시장폭 {br_:.0f}%</span>
          </div>
        </div>""", unsafe_allow_html=True)
        _ra, _rb = st.columns(2)
        if _ra.button(f"추천 전략 적용 ({rn})", type="primary"):
            st.session_state["active_strategy"] = rec
            st.success(f"'{rn}' 전략이 적용됐습니다.")
        if _rb.button(f"추천 기간 적용 ({_rec_hz})"):
            st.session_state["horizon"] = _rec_hz
            st.session_state["horizon_ctl"] = _rec_hz   # 사이드바 셀렉트도 동기화
            apply_horizon_to_live(_rec_hz)
            st.success(f"투자 기간이 '{_rec_hz}'로 설정됐습니다.")
            st.rerun()
    else:
        if st.button("시장 분석 실행"):
            run_market_analysis()

    st.markdown("<br/>", unsafe_allow_html=True)
    active = st.session_state.get("active_strategy","composite")

    # 보기 방식: 카테고리별 / 정렬
    vc1, vc2 = st.columns([1,2])
    view_mode = vc1.radio("보기", ["카테고리별", "정렬"], horizontal=True,
                          label_visibility="collapsed", key="strat_view")
    sort_by = "유명한 순"
    if view_mode == "정렬":
        sort_by = vc2.selectbox("정렬 기준", list(scat.SORT_OPTIONS.keys()),
                                label_visibility="collapsed", key="strat_sort")

    def _strat_card(col, key):
        m = scat.meta(key)
        name, color = m["name"], m["color"]
        is_s = active == key
        r_,g_,b_ = int(color[1:3],16), int(color[3:5],16), int(color[5:7],16)
        card_bg     = f"rgba({r_},{g_},{b_},.10)" if is_s else "#141419"
        card_border = color if is_s else "#1E1E27"
        # 유명도 별 / 위험 점
        fame  = "★"*(6-m["fame"]) + "☆"*(m["fame"]-1)
        risk  = "●"*m["risk"] + "○"*(5-m["risk"])
        risk_c = "#F04452" if m["risk"]>=4 else "#FF9500" if m["risk"]==3 else "#05C072"
        col.markdown(
            f"<div style='background:{card_bg};border:2px solid {card_border};"
            f"border-radius:12px;padding:14px;margin-bottom:6px;min-height:130px'>"
            f"<div style='display:flex;justify-content:space-between;align-items:start'>"
            f"<div style='font-size:.88rem;font-weight:800;color:{color}'>{name}</div>"
            f"<div style='font-size:.6rem;color:#FFD60A'>{fame}</div></div>"
            f"<div style='font-size:.7rem;color:var(--t2);margin-top:6px;line-height:1.4;"
            f"min-height:38px'>{m['desc']}</div>"
            f"<div style='display:flex;justify-content:space-between;margin-top:8px;"
            f"font-size:.62rem;color:var(--t3)'>"
            f"<span>{m['horizon']}</span>"
            f"<span style='color:{risk_c}'>위험 {risk}</span></div>"
            f"<div style='font-size:.6rem;color:var(--t3);margin-top:4px'>{m['origin']}</div>"
            f"</div>", unsafe_allow_html=True)
        if col.button("적용됨" if is_s else "선택", key=f"stk_{key}",
                      type="primary" if is_s else "secondary"):
            st.session_state["active_strategy"] = key; st.rerun()

    if view_mode == "카테고리별":
        for cat_name, keys in STRAT_CAT.items():
            if not keys: continue
            st.markdown(f"<div style='font-size:.72rem;color:var(--t3);font-weight:700;"
                        f"letter-spacing:.05em;text-transform:uppercase;"
                        f"margin:10px 0 8px'>{cat_name} · {len(keys)}</div>",
                        unsafe_allow_html=True)
            for row_start in range(0, len(keys), 4):
                cols = st.columns(4)
                for i, key in enumerate(keys[row_start:row_start+4]):
                    _strat_card(cols[i], key)
    else:
        all_keys = scat.sorted_keys(sort_by)
        for row_start in range(0, len(all_keys), 4):
            cols = st.columns(4)
            for i, key in enumerate(all_keys[row_start:row_start+4]):
                _strat_card(cols[i], key)

    # ── 선택 전략의 매수/매도 규칙 상세 ──
    _bn, _bc = STRAT[active]
    _buy_rule, _sell_rule = scat.rules(active)
    _exits = "".join(
        f"<li style='margin:3px 0'>{e}</li>" for e in scat.COMMON_EXITS)
    st.markdown(f"""
    <div class='card' style='border-left:3px solid {_bc}'>
      <div style='font-weight:800;font-size:.92rem;color:{_bc};margin-bottom:12px'>
        {_bn} — 매매 규칙</div>
      <div style='display:flex;gap:16px;flex-wrap:wrap'>
        <div style='flex:1;min-width:240px'>
          <div style='font-size:.7rem;color:var(--green);font-weight:700;
            letter-spacing:.04em;margin-bottom:5px'>▲ 매수 타이밍</div>
          <div style='font-size:.82rem;color:var(--t1);line-height:1.5'>{_buy_rule}</div>
        </div>
        <div style='flex:1;min-width:240px'>
          <div style='font-size:.7rem;color:var(--up);font-weight:700;
            letter-spacing:.04em;margin-bottom:5px'>▼ 매도 타이밍</div>
          <div style='font-size:.82rem;color:var(--t1);line-height:1.5'>{_sell_rule}</div>
        </div>
      </div>
      <div style='margin-top:14px;padding-top:12px;border-top:1px solid var(--line)'>
        <div style='font-size:.68rem;color:var(--t3);font-weight:700;
          letter-spacing:.04em;margin-bottom:6px'>공통 청산 규칙 (항상 적용)</div>
        <ul style='font-size:.76rem;color:var(--t2);line-height:1.5;
          margin:0;padding-left:16px'>{_exits}</ul>
      </div>
    </div>""", unsafe_allow_html=True)

    # ── 작동 논리 & 근거 (정확히 어떤 원리로 동작하는지) ──
    _detail = scat.detail(active)
    _origin = scat.meta(active).get("origin", "")
    st.markdown(
        f"<div class='card' style='border-left:3px solid {_bc};margin-top:10px'>"
        f"<div style='font-weight:800;font-size:.86rem;color:{_bc};margin-bottom:8px'>"
        f"작동 논리 &amp; 근거</div>"
        f"<div style='font-size:.84rem;color:var(--t1);line-height:1.65'>{_detail}</div>"
        + (f"<div style='margin-top:10px;font-size:.66rem;color:var(--t3)'>"
           f"근거 · 출처: <span style='color:var(--t2);font-weight:600'>{_origin}</span></div>"
           if _origin else "")
        + "</div>", unsafe_allow_html=True)

    # ── 매수 가격대 제한 (모든 전략 공통 — 자동매매 신규 진입에 적용) ──────────
    st.markdown("<div style='font-size:.8rem;font-weight:800;margin:12px 0 2px'>"
                "매수 가격대 제한</div>"
                "<div style='font-size:.7rem;color:var(--t3);margin-bottom:6px'>"
                "자동매매가 신규 매수할 종목의 주가 범위를 제한합니다 (0 = 무제한). "
                "예: 하한 $20 → 동전주 배제 · 상한 $500 → 고가주 배제로 분산 확보. "
                "이미 보유 중인 종목의 매도·관리에는 영향 없음.</div>",
                unsafe_allow_html=True)
    st.session_state.setdefault("buy_price_min", 0)
    st.session_state.setdefault("buy_price_max", 0)
    _bp1, _bp2, _bp3 = st.columns([1, 1, 2])
    _bp1.number_input("하한가 ($)", 0, 100000, step=10, key="buy_price_min")
    _bp2.number_input("상한가 ($, 0=무제한)", 0, 100000, step=50, key="buy_price_max")
    import portfolio as _pf_band
    _pf_band.BUY_PRICE_MIN = float(st.session_state["buy_price_min"] or 0)
    _pf_band.BUY_PRICE_MAX = float(st.session_state["buy_price_max"] or 0)
    _bmin, _bmax = st.session_state["buy_price_min"], st.session_state["buy_price_max"]
    _band_txt = ("제한 없음" if not _bmin and not _bmax else
                 f"${_bmin:,} ~ " + (f"${_bmax:,}" if _bmax else "무제한"))
    _bp3.markdown(f"<div style='padding-top:30px;font-size:.76rem;color:var(--t2)'>"
                  f"현재: <b>{_band_txt}</b> · 손절 후 {_pf_band.REENTRY_COOLDOWN_DAYS}일 "
                  f"재진입 금지(휩쏘 방지)도 자동 적용</div>", unsafe_allow_html=True)

    if active == "composite":
        st.markdown("<br/>", unsafe_allow_html=True)
        st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:12px'>"
                    "가중치 설정</div>", unsafe_allow_html=True)
        import config as cfg
        c1,c2,c3,c4 = st.columns(4)
        wi  = c1.slider("기관 매수세", 0,100,int(cfg.SIGNAL_WEIGHTS["institutional"]*100),format="%d%%")
        ws  = c2.slider("뉴스/소셜",   0,100,int(cfg.SIGNAL_WEIGHTS["sentiment"]*100),format="%d%%")
        wse = c3.slider("섹터 흐름",   0,100,int(cfg.SIGNAL_WEIGHTS["sector"]*100),format="%d%%")
        wf  = c4.slider("펀더멘털",    0,100,int(cfg.SIGNAL_WEIGHTS["fundamental"]*100),format="%d%%")
        tw = wi+ws+wse+wf
        tc2 = "var(--green)" if tw==100 else "var(--up)"
        st.markdown(f"<div style='color:{tc2};font-size:.82rem;font-weight:700'>"
                    f"합계: {tw}% {'✓' if tw==100 else '— 100이 되어야 합니다'}</div>",
                    unsafe_allow_html=True)
        if st.button("저장") and tw == 100:
            cfg.SIGNAL_WEIGHTS = {"institutional":wi/100,"sentiment":ws/100,
                                  "sector":wse/100,"fundamental":wf/100}
            # rules_config.json에 병합 저장 (재시작 후에도 유지)
            _sw_file = Path(__file__).parent / "rules_config.json"
            try:
                _sw_d = json.loads(_sw_file.read_text()) if _sw_file.exists() else {}
                _sw_d["signal_weights"] = cfg.SIGNAL_WEIGHTS
                _sw_file.write_text(json.dumps(_sw_d, indent=2))
            except Exception: pass
            st.markdown("<div class='ok'>저장됨 — 재시작 후에도 유지</div>",
                        unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<br/>", unsafe_allow_html=True)
    # ── 스캔 대상 유니버스 선택 (외부 지수 동적 로드)
    import universe as uni
    uv_opts = ["내 워치리스트"] + uni.available_indexes()
    uc1, uc2 = st.columns([2,1])
    uv_sel = uc1.selectbox("스캔 대상", uv_opts, key="scan_universe",
                           help="S&P 500 등 외부 지수 전체를 스캔하면 더 많은 기회 발굴")
    if uv_sel == "내 워치리스트":
        import watchlist as wl
        scan_uni = wl.load()
    elif uv_sel == "코스피·코스닥":
        scan_uni = uni.get_kr_universe()
    else:
        with st.spinner(f"{uv_sel} 구성종목 로딩…"):
            scan_uni = uni.get_index(uv_sel)
    uc2.markdown(f"<div style='padding-top:30px;font-size:.8rem;color:var(--t2)'>"
                 f"{len(scan_uni)}개 종목</div>", unsafe_allow_html=True)

    if st.button(f"이 전략으로 스캔 실행 ({len(scan_uni)}종목)", type="primary"):
        trigger_scan(active, universe=scan_uni); st.rerun()
    st.caption("손절×익절×진입점수 등 파라미터 조합을 일괄 비교하는 "
               "‘그리드 스윕’은 → 자동 트레이딩 → 백테스트 → 그리드 스윕 에 있습니다.")
    scores = st.session_state.get("scan_results")
    if scores:
        sc4 = st.session_state.get("scan_strategy","composite")
        if sc4 == active:
            sn2, sc2 = STRAT[active]

            # ── 스캔 필터 (결과를 다양한 파라미터로 좁히기 — 재스캔 없이 즉시 적용) ──
            _has_meta = isinstance(scores[0], dict) and "price" in scores[0]
            with st.expander("스캔 필터 — 점수·등락·가격·섹터·거래대금", expanded=True):
                for _k, _v in (("scanf_minsc", 0), ("scanf_dir", "전체"),
                               ("scanf_dvol", 0), ("scanf_pmin", 0),
                               ("scanf_pmax", 0), ("scanf_topn", 15)):
                    st.session_state.setdefault(_k, _v)
                ff1, ff2, ff3 = st.columns(3)
                ff1.slider("최소 점수", 0, 100, step=5, key="scanf_minsc")
                ff2.segmented_control("등락", ["전체", "상승", "하락"], key="scanf_dir")
                ff3.number_input("최소 거래대금($M)", 0, 100000, step=50, key="scanf_dvol")
                fg1, fg2, fg3 = st.columns(3)
                fg1.number_input("최소가($)", 0, 1000000, step=10, key="scanf_pmin")
                fg2.number_input("최대가($, 0=무제한)", 0, 1000000, step=10, key="scanf_pmax")
                _sec_opts = sorted({s.get("sector", "") for s in scores
                                    if isinstance(s, dict) and s.get("sector")})
                if _sec_opts:
                    fg3.multiselect("섹터(비우면 전체)", _sec_opts, default=[], key="scanf_secs")
                st.slider("표시 개수", 5, 50, step=5, key="scanf_topn")
                _f_minsc = st.session_state["scanf_minsc"]
                _f_dir = st.session_state.get("scanf_dir") or "전체"
                _f_dvol = st.session_state["scanf_dvol"]
                _f_pmin = st.session_state["scanf_pmin"]
                _f_pmax = st.session_state["scanf_pmax"]
                _f_secs = st.session_state.get("scanf_secs", [])
                _f_topn = st.session_state["scanf_topn"]

            # ── 필터 적용 ──
            def _passes(s):
                if not isinstance(s, dict):
                    return True
                if s.get("score", 0) < _f_minsc:
                    return False
                if _has_meta:
                    _c = s.get("change_pct", 0)
                    if _f_dir == "상승" and _c < 0: return False
                    if _f_dir == "하락" and _c > 0: return False
                    _p = s.get("price", 0)
                    if _f_pmin and _p < _f_pmin: return False
                    if _f_pmax and _p > _f_pmax: return False
                    if _f_dvol and s.get("dollar_vol", 0) < _f_dvol * 1e6: return False
                    if _f_secs and s.get("sector", "") not in _f_secs: return False
                return True

            _filtered = [s for s in scores if _passes(s)]
            st.markdown(
                f"<div style='font-size:.76rem;color:var(--t3);margin:8px 0'>"
                f"완료 {st.session_state.get('scan_ts','')} · 전체 {len(scores)}개 "
                f"→ 필터 통과 <b style='color:var(--t1)'>{len(_filtered)}개</b></div>",
                unsafe_allow_html=True)
            if not _filtered:
                st.markdown("<div style='font-size:.78rem;color:#FF9500;padding:6px'>"
                            "조건에 맞는 종목이 없습니다 — 필터를 완화해 보세요</div>",
                            unsafe_allow_html=True)
            for s in _filtered[:_f_topn]:
                t_ = s["score"]
                _sig = "매수" if t_>=65 else "관망" if t_>=50 else "약세"
                _sgc = "#0FB873" if t_>=65 else "#FF9500" if t_>=50 else "#F0454F"
                _pr = s.get("price", 0) if _has_meta else 0
                _ch = s.get("change_pct", 0) if _has_meta else 0
                _chc = "#F0454F" if _ch >= 0 else "#2F80ED"
                rc = st.columns([2.4, 0.9, 1.4, 2.2, 0.7], vertical_alignment="center")
                clickable_ticker(rc[0], s["ticker"], key=f"scanrow_{s['ticker']}", with_name=True)
                rc[1].markdown(f"<span style='color:{_sgc};font-weight:700;font-size:.72rem'>{_sig}</span>",
                               unsafe_allow_html=True)
                if _has_meta:
                    rc[2].markdown(
                        f"<div style='text-align:right;font-size:.72rem'>"
                        f"<span style='font-weight:700'>${_pr:,.2f}</span><br>"
                        f"<span style='color:{_chc};font-size:.66rem'>{'▲' if _ch>=0 else '▼'}{abs(_ch):.1f}%</span></div>",
                        unsafe_allow_html=True)
                else:
                    rc[2].markdown("")
                rc[3].markdown(
                    f"<div style='background:var(--bg4);border-radius:2px;height:5px;margin-top:8px'>"
                    f"<div style='width:{min(t_,100):.0f}%;height:5px;border-radius:2px;background:{sc2}'></div></div>",
                    unsafe_allow_html=True)
                rc[4].markdown(f"<div style='font-weight:900;font-size:.86rem;color:{sc2};text-align:right'>{t_:.0f}</div>",
                               unsafe_allow_html=True)
            # 스캔 결과에서 바로 거래 — toggle 게이트로 켤 때만 시세 조회(지연 실행)
            if _filtered and st.toggle("스캔 종목 바로 거래", key="scan_trade_on"):
                _scan_tks = [s["ticker"] for s in _filtered[:_f_topn]]
                _scan_pick = st.selectbox("종목", _scan_tks, key="scan_trade_pick",
                                          label_visibility="collapsed")
                quick_trade_panel(_scan_pick, key_prefix="scan_trade")

    # ── 그리드 스윕 (전략 선택에서 바로 — 파라미터 조합 직접 커스텀 검색) ──────
    st.markdown("<br/>", unsafe_allow_html=True)
    st.markdown("<div style='font-size:.82rem;font-weight:800;margin-bottom:4px'>"
                "그리드 스윕 — 파라미터 조합 직접 검색</div>"
                "<div style='font-size:.72rem;color:var(--t3);margin-bottom:6px'>"
                "손절·익절·진입점수 값을 직접 골라 모든 조합을 여러 전략에 일괄 "
                "백테스트 → 어떤 조합이 가장 좋은지 한 번에 찾습니다.</div>",
                unsafe_allow_html=True)
    with st.expander("그리드 스윕 열기 · 직접 커스텀", expanded=False):
        import universe as _uni_gs
        _gs_src = st.selectbox("종목 소스", ["내 워치리스트"] + _uni_gs.available_indexes(),
                               key="gs_uni_src",
                               help="그리드 스윕은 조합마다 전체 백테스트라 무겁습니다 — "
                                    "큰 지수는 상위 일부로 자동 제한")
        if _gs_src == "내 워치리스트":
            import watchlist as _wl_gs
            _gs_uni = _wl_gs.load()
        elif _gs_src == "코스피·코스닥":
            _gs_uni = _uni_gs.get_kr_universe()
        else:
            with st.spinner(f"{_gs_src} 구성종목 로딩…"):
                _gs_uni = _uni_gs.get_index(_gs_src)
        _GS_CAP = 40
        if len(_gs_uni) > _GS_CAP:
            st.caption(f"속도를 위해 상위 {_GS_CAP}종목으로 제한 (전체 {len(_gs_uni)}종목)")
            _gs_uni = _gs_uni[:_GS_CAP]
        from datetime import timedelta as _tdgs
        _GS_PER = {"1년": 365, "2년": 730, "3년": 1095}
        _gs_per = st.selectbox("기간", list(_GS_PER), index=1, key="gs_period")
        _gs_end = date.today(); _gs_start = _gs_end - _tdgs(days=_GS_PER[_gs_per])
        render_grid_sweep("gs_strat", _gs_uni, _gs_start, _gs_end, 10000.0, [active])


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "직접 주문":
    import yfinance as yf
    from portfolio import PortfolioManager
    st.markdown("<div class='stitle'>직접 주문 <span style='font-size:.72rem;color:var(--t3);font-weight:600'>· 고급</span></div>", unsafe_allow_html=True)
    st.markdown("<div style='font-size:.78rem;color:var(--t3);margin:-6px 0 14px'>"
                "내가 직접 종목을 골라 매수/매도합니다. 자동매매와 독립적으로 작동.</div>",
                unsafe_allow_html=True)

    mt = st.text_input("종목 티커", value=st.session_state.get("manual_ticker","AAPL"),
                       key="manual_ticker_in").upper().strip()
    is_paper_m = st.session_state.get("trade_mode", "페이퍼(모의)").startswith("페이퍼")
    _dc = "#3B82F6" if is_paper_m else "#F04452"
    st.markdown(
        f"<div style='display:inline-block;font-size:.66rem;font-weight:800;margin:0 0 8px;"
        f"padding:3px 10px;border-radius:6px;color:{_dc};background:{_dc}1A;border:1px solid {_dc}66'>"
        f"● {'모의투자' if is_paper_m else '실전투자'}"
        f"<span style='color:var(--t3);font-weight:500'> · 사이드바에서 변경</span></div>",
        unsafe_allow_html=True)

    # 국내 종목 여부 자동 감지
    _is_kr_stock = mt.endswith((".KS", ".KQ"))
    _price_sym   = "₩" if _is_kr_stock else "$"

    if mt:
        # 실시간 시세 — 피드 캐시 우선, 없으면 fast_info
        _rtf.subscribe([mt], interval=3.0)   # 수동 매매 중인 종목 3초 주기 갱신
        def _quote(tk):
            d = _rtf.get_price(tk)
            if d and d.get("price"):
                cur = getattr(yf.Ticker(tk).fast_info, "currency", "USD") or "USD"
                return dict(price=d["price"], prev=d["prev"],
                            high=d.get("high",0), low=d.get("low",0),
                            ok=True, currency=cur,
                            age=int(time.time()-d.get("ts",time.time())))
            try:
                fi = yf.Ticker(tk).fast_info
                price = float(getattr(fi,"last_price",0) or 0)
                prev  = float(getattr(fi,"previous_close",price) or price)
                cur   = getattr(fi,"currency","USD") or "USD"
                return dict(price=price, prev=prev, ok=price>0, currency=cur, age=30)
            except:
                return dict(price=0, prev=0, ok=False, currency="USD", age=0)
        q = _quote(mt)
        if not q["ok"]:
            st.markdown(f"<div class='fail'>{mt} 시세를 불러올 수 없습니다.</div>",
                        unsafe_allow_html=True)
        else:
            chg = (q["price"]-q["prev"])/q["prev"] if q["prev"] else 0
            cc = "var(--up)" if chg>=0 else "var(--dn)"
            ar = "▲" if chg>=0 else "▼"
            # 화폐 기호 확정 (fast_info currency 기반)
            _price_sym = "₩" if q.get("currency") in ("KRW",) else "$"
            qc1, qc2 = st.columns([2,1])
            with qc1:
                _age_s = q.get("age", 0)
                _src_label = "Finnhub" if _rtf.get_price(mt) and _rtf.get_price(mt).get("source")=="finnhub" else "yfinance"
                st.markdown(f"""
                <div style='display:flex;align-items:baseline;gap:12px;margin:6px 0;flex-wrap:wrap'>
                  <span style='font-size:1.1rem;font-weight:800;color:var(--t3)'>{mt}</span>
                  <span style='font-size:2rem;font-weight:900'>{_price_sym}{q['price']:,.2f}</span>
                  <span style='color:{cc};font-size:.95rem;font-weight:700'>
                    {ar} {abs(chg):.2%}</span>
                  <span style='font-size:.68rem;color:var(--t3);margin-left:4px'>
                    {_src_label} · {_age_s}s ago</span>
                </div>
                {f"<div style='font-size:.72rem;color:var(--t3)'>H {_price_sym}{q.get('high',0):,.2f} &nbsp; L {_price_sym}{q.get('low',0):,.2f}</div>" if q.get('high') else ""}
                """, unsafe_allow_html=True)
                idf = fetch_intraday(mt)
                if not idf.empty:
                    ic = idf["Close"].squeeze()
                    op = float(ic.iloc[0]); lc="#F04452" if q["price"]>=op else "#2F80ED"
                    r2,g2,b2=int(lc[1:3],16),int(lc[3:5],16),int(lc[5:7],16)
                    ymn,ymx=float(ic.min()),float(ic.max()); pad=(ymx-ymn)*0.15 or ymx*0.002
                    fig=go.Figure()
                    fig.add_trace(go.Scatter(x=idf.index,y=ic,mode="lines",
                        line=dict(color=lc,width=2),fill="tozeroy",
                        fillcolor=f"rgba({r2},{g2},{b2},.06)"))
                    fig.update_layout(**CL(height=180,
                        xaxis=dict(**_XA,tickformat="%H:%M"),
                        yaxis=dict(gridcolor="#1A1A25",showgrid=True,zeroline=False,
                                   tickfont=dict(size=10),
                                   tickprefix=_price_sym,
                                   range=[ymn-pad,ymx+pad])))
                    st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})

            with qc2:
                st.markdown("<div style='font-weight:700;font-size:.84rem;margin-bottom:8px'>"
                            "주문</div>", unsafe_allow_html=True)
                _amt_label = f"금액({'₩' if _is_kr_stock else '$'})"
                order_by = st.radio("주문 방식", [_amt_label,"수량(주)"], horizontal=True,
                                    key="manual_orderby")
                if "금액" in order_by:
                    _amt_default = 500000 if _is_kr_stock else 1000
                    _amt_step    = 10000  if _is_kr_stock else 100
                    amt = st.number_input(f"금액 ({_price_sym})", value=_amt_default,
                                          step=_amt_step, key="manual_amt")
                    shares = int(amt / q["price"]) if q["price"]>0 else 0
                    st.markdown(f"<div style='font-size:.74rem;color:var(--t2)'>"
                                f"≈ {shares}주</div>", unsafe_allow_html=True)
                else:
                    shares = st.number_input("수량 (주)", value=5, step=1, key="manual_shares")
                    _val = shares * q["price"]
                    _val_fmt = f"{_val:,.0f}" if _is_kr_stock else f"{_val:,.2f}"
                    st.markdown(f"<div style='font-size:.74rem;color:var(--t2)'>"
                                f"≈ {_price_sym}{_val_fmt}</div>", unsafe_allow_html=True)

                # 보유 여부
                _pm = PortfolioManager(paper=is_paper_m)
                held = _pm.positions.get(mt)
                if held:
                    _ep_fmt = (f"{held.entry_price:,.0f}" if _is_kr_stock
                               else f"{held.entry_price:.2f}")
                    st.markdown(f"<div style='font-size:.72rem;color:var(--t3);margin:6px 0'>"
                                f"보유: {held.shares}주 · 평단 {_price_sym}{_ep_fmt}</div>",
                                unsafe_allow_html=True)

                # 국내 종목은 Alpaca(미국 브로커)로 실거래 불가 → 조회 전용
                bcol, scol = st.columns(2)
                buy_clicked = bcol.button("매수", type="primary", key="manual_buy",
                                          disabled=(shares<1 or _is_kr_stock))
                sell_clicked = scol.button("매도", key="manual_sell",
                                           disabled=(not held or _is_kr_stock))

                if _is_kr_stock:
                    st.markdown("""
                    <div style='background:rgba(255,149,0,.08);
                      border:1px solid rgba(255,149,0,.3);border-radius:8px;
                      padding:9px 12px;margin-top:8px;font-size:.74rem;color:#FF9500'>
                      🇰🇷 국내 종목은 <b>조회 전용</b>입니다.<br/>
                      현재 브로커(Alpaca)는 미국 주식만 거래할 수 있어요.
                      국내 실거래는 한국투자·키움 OpenAPI 연동이 필요합니다.
                    </div>""", unsafe_allow_html=True)
                elif not is_paper_m:
                    st.markdown("<div class='fail' style='margin-top:8px;font-size:.74rem'>"
                                "실거래 모드 — 실제 자금</div>", unsafe_allow_html=True)

                # 실행 — 페이퍼는 즉시, 실거래는 2단계 확인.
                # 체결은 단일 실행기(core.execution.execute_manual)로 위임.
                def _do_buy(paper):
                    from core.execution import execute_manual
                    try:
                        r = execute_manual(mt, int(shares), "buy", paper,
                                           est_price=q["price"], pm=_pm)
                    finally:
                        st.session_state["confirm_pending"] = None
                    if r.get("warning"):
                        st.toast(f"{mt}: {r['warning']}")
                    return r["price"]

                def _do_sell(paper):
                    from core.execution import execute_manual
                    sell_qty = min(int(shares), held.shares) if shares >= 1 else held.shares
                    try:
                        r = execute_manual(mt, int(sell_qty), "sell", paper,
                                           est_price=q["price"], pm=_pm)
                    finally:
                        st.session_state["confirm_pending"] = None
                    if r.get("warning"):
                        st.toast(f"{mt}: {r['warning']}")
                    return r["shares"], r.get("pnl_pct", 0.0)

                if buy_clicked and shares>=1:
                    if is_paper_m:
                        try:
                            _do_buy(paper=True)
                            st.markdown(f"<div class='ok'>매수 완료: {mt} {shares}주 "
                                        f"@ ${q['price']:.2f}</div>", unsafe_allow_html=True)
                        except Exception as e:
                            st.markdown(f"<div class='fail'>매수 실패: {e}</div>",
                                        unsafe_allow_html=True)
                    else:
                        # 실거래: 확인 대기 상태로 전환
                        st.session_state["confirm_pending"] = {
                            "action": "buy", "ticker": mt, "shares": shares,
                            "price": q["price"], "total": shares * q["price"]
                        }
                        st.rerun()

                if sell_clicked and held:
                    sell_qty_ = min(int(shares), held.shares) if shares>=1 else held.shares
                    if is_paper_m:
                        try:
                            sq, pnl = _do_sell(paper=True)
                            st.markdown(f"<div class='ok'>매도 완료: {mt} {sq}주 "
                                        f"@ ${q['price']:.2f} ({pnl:+.1%})</div>",
                                        unsafe_allow_html=True)
                        except Exception as e:
                            st.markdown(f"<div class='fail'>매도 실패: {e}</div>",
                                        unsafe_allow_html=True)
                    else:
                        pnl_ = (q["price"]-held.entry_price)/held.entry_price
                        st.session_state["confirm_pending"] = {
                            "action": "sell", "ticker": mt, "shares": sell_qty_,
                            "price": q["price"], "pnl": pnl_,
                            "total": sell_qty_ * q["price"]
                        }
                        st.rerun()

                # ── 실거래 확인 모달 ──────────────────────────────────────
                cp = st.session_state.get("confirm_pending")
                if cp and cp["ticker"] == mt:
                    ac = cp["action"]
                    ac_kr = "매수" if ac=="buy" else "매도"
                    ac_color = "var(--up)" if ac=="buy" else "var(--dn)"
                    pnl_line = ""
                    if ac == "sell":
                        pnl_c = "var(--up)" if cp["pnl"]>=0 else "var(--dn)"
                        pnl_line = (f"<div style='font-size:.8rem;color:{pnl_c};"
                                    f"margin-top:4px'>예상 손익: {cp['pnl']:+.1%} "
                                    f"(${cp['total']*cp['pnl']:+,.0f})</div>")
                    st.markdown(f"""
                    <div style='background:#1C1015;border:1.5px solid {ac_color};
                      border-radius:10px;padding:16px 18px;margin-top:8px'>
                      <div style='font-weight:800;font-size:.95rem;color:{ac_color};
                        margin-bottom:8px'>실거래 {ac_kr} 확인</div>
                      <div style='font-size:.84rem;color:var(--t1)'>
                        <b>{cp['ticker']}</b> &nbsp;{cp['shares']}주 &nbsp;@ ${cp['price']:.2f}
                        &nbsp;&nbsp;<span style='color:var(--t3)'>합계 ${cp['total']:,.0f}</span>
                      </div>
                      {pnl_line}
                      <div style='font-size:.72rem;color:var(--t3);margin-top:6px'>
                        실제 자금이 이동합니다. 계속하시겠습니까?</div>
                    </div>""", unsafe_allow_html=True)
                    conf_ok, conf_cancel = st.columns(2)
                    if conf_ok.button(f"✓ {ac_kr} 실행", type="primary",
                                      key="confirm_exec"):
                        try:
                            if ac == "buy":
                                _do_buy(paper=False)
                                st.markdown(f"<div class='ok'>매수 완료: {cp['ticker']} "
                                            f"{cp['shares']}주 @ ${cp['price']:.2f}</div>",
                                            unsafe_allow_html=True)
                            else:
                                sq, pnl = _do_sell(paper=False)
                                st.markdown(f"<div class='ok'>매도 완료: {cp['ticker']} "
                                            f"{sq}주 @ ${cp['price']:.2f} "
                                            f"({pnl:+.1%})</div>",
                                            unsafe_allow_html=True)
                        except Exception as e:
                            st.session_state["confirm_pending"] = None
                            st.markdown(f"<div class='fail'>실패: {e}</div>",
                                        unsafe_allow_html=True)
                    if conf_cancel.button("✕ 취소", key="confirm_cancel"):
                        st.session_state["confirm_pending"] = None
                        st.rerun()

    # 최근 수동 주문 내역
    recent = [o for o in load_orders() if o.get("source")=="manual"][-8:]
    if recent:
        st.markdown("<br/><div style='font-weight:700;font-size:.84rem;margin-bottom:8px'>"
                    "최근 수동 주문</div>", unsafe_allow_html=True)
        for o in reversed(recent):
            sc_ = "var(--up)" if o["side"]=="buy" else "var(--dn)"
            sl_ = "매수" if o["side"]=="buy" else "매도"
            st.markdown(f"""<div class='card-xs' style='display:flex;
              justify-content:space-between;align-items:center'>
              <div><span style='font-weight:700'>{o['ticker']}</span>
                <span style='color:{sc_};font-weight:700;margin-left:8px'>{sl_}</span>
                <span style='color:var(--t3);font-size:.74rem;margin-left:8px'>
                  {o['shares']:.0f}주 @ ${o['price']:.2f}</span></div>
              <span style='font-size:.72rem;color:var(--t3)'>{o['ts'][5:16].replace('T',' ')}</span>
            </div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "라이브 트레이딩":
    # ════════ 자동 트레이딩 상황판 — 현 상태를 한눈에 (전략·기간·타이밍·시장) ════════
    import market_hours as _mhx
    auto_on = st.session_state.get("auto_enabled", False)

    # 전략(매매법)
    _act = st.session_state.get("active_strategy", "composite")
    _aname, _acolor = STRAT.get(_act, ("복합", "#05C072"))
    _adesc = scat.meta(_act).get("desc", "")
    # 투자 기간(모드)
    _hzk = st.session_state.get("horizon", "단기")
    _hpd = horizon_params(_hzk)
    _hzlabel = HORIZONS.get(_hzk, HORIZONS["단기"])["label"]
    # 자본 배분
    _is_dyn_alloc = st.session_state.get("alloc_dynamic", True)
    _bm = st.session_state.get("buy_mode", "분할"); _bpc = st.session_state.get("buy_pct", 50)
    _sm = st.session_state.get("sell_mode", "전량"); _spc = st.session_state.get("sell_pct", 50)
    if _is_dyn_alloc:
        _alloc_main = "유동형"
        _alloc_sub = "시드 내 신호 비례 동적 배분·리밸런싱"
    else:
        _buy_txt = f"{_bm}" + (f" {_bpc:.0f}%" if _bm == "분할" else "")
        _sell_txt = f"{_sm}" + (f" {_spc:.0f}%" if _sm == "분할" else "")
        _alloc_main = f"매수 {_buy_txt}"
        _alloc_sub = f"매도 {_sell_txt}"
    # 거래 모드 / 보유
    _is_paper_live = state.get("paper_mode", True)
    _mode_txt = "모의(페이퍼)" if _is_paper_live else "실거래"
    _mode_c = "#3F8CFF" if _is_paper_live else "#F04452"
    _n_pos = len(positions); _cash_now = state.get("cash", 0)
    # 타이밍
    _is_open = _mhx.is_market_open()
    _iv_sec = st.session_state.get("auto_iv_sec", 30)
    _iv_lbl = st.session_state.get("auto_iv_lbl", "30초")
    _last_run = st.session_state.get("auto_last_run", 0)
    _now_t = time.time()
    _next_eval = max(0, _iv_sec - (_now_t - _last_run)) if _last_run else 0
    # 시장 자동 발굴 상태
    _disc_on = st.session_state.get("discover_on", False)
    try:
        import watchlist as _wl_stat
        _wl_full = _wl_stat._load_full()
        _wl_age = _wl_stat.auto_age_sec()
    except Exception:
        _wl_full = {"stocks": [], "manual": [], "auto": [], "held": []}
        _wl_age = None
    _disc_last = st.session_state.get("discover_last") or {}
    _disc_added = _disc_last.get("added") or []

    def _fmt_dur(s):
        s = int(max(0, s))
        if s < 60:   return f"{s}초"
        if s < 3600: return f"{s//60}분 {s%60}초" if s % 60 else f"{s//60}분"
        return f"{s/3600:.1f}시간"

    _et = _mhx.now_et()
    if _is_open:
        _close = _et.replace(hour=_mhx.CLOSE_T.hour, minute=_mhx.CLOSE_T.minute,
                             second=0, microsecond=0)
        _sess_secs = max(0, (_close - _et).total_seconds())
        _sess_txt = f"개장 중 · 마감까지 {_fmt_dur(_sess_secs)}"
        _sess_c = "#05C072"
    else:
        _sess_secs = _mhx.seconds_until_open()
        _sess_txt = f"마감 · 개장까지 {_fmt_dur(_sess_secs)}"
        _sess_c = "#FF9500"
    # 시장 진단
    try:
        _verdict, _vc, _ = _diag()
    except Exception:
        _verdict, _vc = "—", "var(--t2)"
    _is_bear = "약세" in _verdict

    # 마지막/다음 실행 시각
    _last_txt = datetime.fromtimestamp(_last_run).strftime("%H:%M:%S") if _last_run else "—"
    if auto_on and _is_open:
        _next_txt = f"{_fmt_dur(_next_eval)} 후 ({_iv_lbl}마다)"
    elif auto_on and not _is_open:
        _next_txt = "장 마감 — 다음 개장에 재개"
    else:
        _next_txt = "자동 매매 꺼짐"

    _hdr_c = "#05C072" if auto_on else "var(--t3)"
    _hdr_bg = "rgba(5,192,114,.06)" if auto_on else "var(--bg2)"
    _hdr_bd = "rgba(5,192,114,.32)" if auto_on else "var(--line)"
    _dot = "#05C072" if (auto_on and _is_open) else ("#FF9500" if auto_on else "var(--t3)")
    _state_txt = ("실행 중 · 장중 자동 주문" if (auto_on and _is_open)
                  else "대기 중 · 장 마감 (개장 시 자동 재개)" if auto_on
                  else "꺼짐 · 수동 실행만 가능")

    def _card(label, value, sub="", vc="var(--t1)"):
        return (
            "<div style='flex:1;min-width:150px;background:var(--bg2);border:1px solid var(--line);"
            "border-radius:10px;padding:9px 12px'>"
            f"<div style='font-size:.56rem;color:var(--t3);font-weight:700;letter-spacing:.03em'>{label}</div>"
            f"<div style='font-size:.84rem;font-weight:800;color:{vc};margin-top:2px;line-height:1.25'>{value}</div>"
            + (f"<div style='font-size:.58rem;color:var(--t3);margin-top:2px'>{sub}</div>" if sub else "")
            + "</div>")

    st.markdown(
        f"<div style='background:{_hdr_bg};border:1px solid {_hdr_bd};border-radius:13px;"
        "padding:14px 16px;margin-bottom:14px'>"
        # 헤더 줄
        "<div style='display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px'>"
        "<div style='display:flex;align-items:center;gap:8px'>"
        f"<span style='color:{_dot};font-size:1.0rem;line-height:1'>●</span>"
        f"<span style='font-size:.92rem;font-weight:800;color:{_hdr_c}'>자동 트레이딩 {'ON' if auto_on else 'OFF'}</span>"
        f"<span style='font-size:.64rem;color:var(--t3)'>{_state_txt}</span></div>"
        f"<span style='font-size:.64rem;font-weight:800;color:{_mode_c};border:1px solid {_mode_c}55;"
        f"border-radius:6px;padding:2px 8px'>{_mode_txt} 모드</span></div>"
        # 카드 그리드 1
        "<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:10px'>"
        + _card("전략(매매법)", _aname, _adesc[:34], _acolor)
        + _card("투자 기간", f"{_hzk} · {_hzlabel}",
                f"손절 {_hpd['stop_loss']:.0%} · 익절 {_hpd['take_profit']:.0%} · 트레일 {_hpd.get('trail',0.08):.0%}")
        + _card("실행 주기 · 다음 평가", _next_txt, f"마지막 실행 {_last_txt}",
                "#05C072" if auto_on else "var(--t3)")
        + _card("시장 세션", _sess_txt, f"진단: {_verdict}", _sess_c)
        + "</div>"
        # 카드 그리드 2
        "<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:8px'>"
        + _card("자본 배분", _alloc_main, _alloc_sub,
                "#00C2A8" if _is_dyn_alloc else "var(--t1)")
        + _card("보유 현황", f"{_n_pos}종목", f"현금 {money(_cash_now)}")
        + _card("방어 로직", "약세장 매수 보류" if _is_bear else "정상 가동",
                "신규 매수 차단 중" if _is_bear else "매수·매도 모두 활성",
                "#FF9500" if _is_bear else "#05C072")
        + _card("시장 자동 발굴",
                (f"ON · 활성 {len(_wl_full['stocks'])}종목" if _disc_on
                 else f"OFF · 활성 {len(_wl_full['stocks'])}종목"),
                (f"수동 {len(_wl_full['manual'])} · 자동 {len(_wl_full['auto'])} · "
                 f"보유 {len(_wl_full['held'])}"
                 + (f" · {_wl_age/3600:.1f}h 전 스캔" if _wl_age is not None else "")
                 + (f" · 최근편입 {', '.join(_disc_added[:3])}" if _disc_added else "")),
                "#05C072" if _disc_on else "var(--t3)")
        + "</div>"
        # 푸터
        "<div style='font-size:.6rem;color:var(--t3);margin-top:10px;border-top:1px solid var(--line);"
        "padding-top:7px'>"
        f"장중 {_iv_lbl}마다 재평가 → 자동 매수/매도/관망 · "
        "<b style='color:var(--t1)'>실행</b> 버튼=즉시 1회 · "
        "데몬이 실행 — 앱·화면을 꺼도 계속 동작</div>"
        "</div>",
        unsafe_allow_html=True)

    # ── 자산 요약 + 검토 현황 (자동매매가 지금 무엇을 보고 있는지 실시간) ──────
    _cash_v = state.get("cash", 0); _tcur = state.get("t_cur", 0)
    _tinv = state.get("t_inv", 0)
    _equity_v = state.get("equity", _cash_v + _tcur)
    _upnl = _tcur - _tinv
    _upnl_c = "#F0454F" if _upnl >= 0 else "#2F80ED"
    def _ac(lbl, val, vc="var(--t1)"):
        return ("<div style='flex:1;min-width:120px;background:var(--bg2);border:1px solid var(--line);"
                "border-radius:10px;padding:9px 12px'>"
                f"<div style='font-size:.56rem;color:var(--t3);font-weight:700'>{lbl}</div>"
                f"<div style='font-size:.95rem;font-weight:800;color:{vc};margin-top:2px'>{val}</div></div>")
    st.markdown(
        "<div style='display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px'>"
        + _ac("총 가치", money(_equity_v))
        + _ac("투자 중", money(_tcur))
        + _ac("현금", money(_cash_v), "#3B82F6")
        + _ac("평가손익", f"{'▲' if _upnl>=0 else '▼'} {money(abs(_upnl))}", _upnl_c)
        + "</div>", unsafe_allow_html=True)

    _ls = st.session_state.get("live_scores")
    if _ls and _ls.get("top"):
        _bth = _ls.get("buy_th", 60)
        _snm = STRAT.get(_ls.get("strategy"), ("?", "#888"))[0]
        st.markdown(
            f"<div style='font-size:.82rem;font-weight:800;margin:4px 0 2px'>검토 현황 "
            f"<span style='font-size:.66rem;color:var(--t3);font-weight:500'>· {_snm} · "
            f"{_ls.get('ts','')} · {_ls.get('n',0)}종목 채점 · 매수문턱 {_bth}</span></div>",
            unsafe_allow_html=True)
        for r in _ls["top"]:
            _sv = r["score"]; _tk = r["ticker"]
            _sig = "매수" if _sv >= _bth else "관망" if _sv >= _bth-10 else "약세"
            _sigc = "#0FB873" if _sv >= _bth else "#FF9500" if _sv >= _bth-10 else "#8E8E93"
            _px = (_ls.get("prices") or {}).get(_tk)
            _pxs = f"${_px:,.2f}" if _px else "—"
            rc = st.columns([2.2, 1, 1.2, 2.4, 0.7], vertical_alignment="center")
            rc[0].markdown(f"<span style='font-weight:700;font-size:.82rem'>{_tk}</span>",
                           unsafe_allow_html=True)
            rc[1].markdown(f"<span style='color:{_sigc};font-weight:700;font-size:.72rem'>{_sig}</span>",
                           unsafe_allow_html=True)
            rc[2].markdown(f"<span style='font-size:.74rem;color:var(--t2)'>{_pxs}</span>",
                           unsafe_allow_html=True)
            rc[3].markdown(
                f"<div style='background:var(--bg4);border-radius:2px;height:5px;margin-top:8px'>"
                f"<div style='width:{min(_sv,100):.0f}%;height:5px;border-radius:2px;"
                f"background:{_sigc}'></div></div>", unsafe_allow_html=True)
            rc[4].markdown(f"<div style='font-weight:900;font-size:.84rem;color:{_sigc};"
                           f"text-align:right'>{_sv:.0f}</div>", unsafe_allow_html=True)
        st.caption("자동매매가 매 사이클 이 종목들을 채점해 매수문턱 이상이면 매수합니다 "
                   "(자세한 요청·체결 내역은 아래 실행 로그).")
    else:
        st.caption("검토 현황은 자동매매가 한 사이클 돌면 표시됩니다 "
                   "(자동 매매 ON + 장중, 또는 아래 ‘지금 1회 실행’).")

    st.markdown("<div class='stitle'>라이브 트레이딩</div>", unsafe_allow_html=True)

    ca, cb = st.columns([3,2])
    with ca:
        st.markdown("<div style='font-weight:700;font-size:.86rem;color:var(--t2);"
                    "margin-bottom:12px'>Alpaca 계좌</div>", unsafe_allow_html=True)

        import config as cfg
        api_ok = "your_" not in cfg.ALPACA_API_KEY
        if not api_ok:
            st.markdown("""
            <div style='background:rgba(255,149,0,.07);border:1px solid rgba(255,149,0,.3);
              border-radius:9px;padding:10px 14px;font-size:.8rem;color:#FF9500;
              margin-bottom:10px'>
              설정 → API 연결에서 Alpaca 키를 먼저 입력하세요
            </div>""", unsafe_allow_html=True)

        if st.button("계좌 조회"):
            try:
                from broker import Broker
                acct_new = Broker(paper="paper" in cfg.ALPACA_BASE_URL).get_account()
                st.session_state["alpaca_acct"] = acct_new
            except Exception as e:
                st.session_state["alpaca_acct"] = None
                st.markdown(f"<div class='fail'>연결 실패: {e}</div>",
                            unsafe_allow_html=True)

        acct = st.session_state.get("alpaca_acct")
        if acct:
            a1,a2,a3 = st.columns(3)
            kpi(a1,"자산",    f"${acct['equity']:,.0f}")
            kpi(a2,"현금",    f"${acct['cash']:,.0f}",  color="#3182F6")
            kpi(a3,"매수 가능",f"${acct['buying_power']:,.0f}", color="#05C072")
        else:
            st.markdown("<div style='color:var(--t3);font-size:.82rem;padding:4px 0'>"
                        "계좌 조회 버튼을 누르면 잔고를 확인합니다</div>",
                        unsafe_allow_html=True)

        # ── 계좌 정합성 대조 ──────────────────────────────────────────────
        if api_ok and st.button("정합성 대조 (로컬 vs 브로커)"):
            try:
                from broker import Broker
                from portfolio import PortfolioManager
                _bk = Broker(paper="paper" in cfg.ALPACA_BASE_URL)
                _broker_pos = _bk.get_positions()
                _local_pos = {t: p.shares for t, p in PortfolioManager(paper=False).positions.items()}
                st.session_state["recon"] = {
                    "broker": _broker_pos, "local": _local_pos
                }
            except Exception as e:
                st.markdown(f"<div class='fail'>대조 실패: {e}</div>",
                            unsafe_allow_html=True)

        _recon = st.session_state.get("recon")
        if _recon:
            _bp = _recon["broker"]; _lp = _recon["local"]
            _all_tk = sorted(set(_bp) | set(_lp))
            if not _all_tk:
                st.markdown("<div style='color:var(--t3);font-size:.78rem;padding:4px 0'>"
                            "양쪽 모두 보유 종목 없음 (일치)</div>", unsafe_allow_html=True)
            _mismatch = 0
            for _tk in _all_tk:
                _bs = _bp.get(_tk, {}).get("shares", 0)
                _ls = _lp.get(_tk, 0)
                _ok = abs(_bs - _ls) < 0.01
                if not _ok: _mismatch += 1
                _ic = "var(--green)" if _ok else "var(--up)"
                _isym = "✓" if _ok else "✗"
                st.markdown(f"""
                <div style='display:flex;justify-content:space-between;
                  padding:5px 8px;border-bottom:1px solid var(--line);font-size:.78rem'>
                  <span style='font-weight:700'>{_tk}</span>
                  <span style='color:var(--t3)'>브로커 {_bs:.0f} · 로컬 {_ls:.0f}</span>
                  <span style='color:{_ic};font-weight:700'>{_isym}</span>
                </div>""", unsafe_allow_html=True)
            if _mismatch:
                st.markdown(f"""
                <div style='background:rgba(240,68,82,.07);border:1px solid rgba(240,68,82,.3);
                  border-radius:8px;padding:8px 12px;margin-top:8px;font-size:.78rem;color:#F04452'>
                  {_mismatch}개 종목 불일치 — 로컬 장부와 브로커 실계좌가 어긋났습니다.
                  수동 거래·부분 체결·앱 외부 주문 여부를 확인하세요.</div>""",
                unsafe_allow_html=True)
                if st.button("로컬 장부를 브로커 기준으로 강제 동기화"):
                    try:
                        from portfolio import PortfolioManager, Position
                        _pm_sync = PortfolioManager(paper=False)
                        _new = {}
                        for _tk, _info in _recon["broker"].items():
                            _new[_tk] = Position(
                                ticker=_tk, entry_price=_info["avg_price"],
                                shares=_info["shares"],
                                entry_date=date.today().isoformat(),
                                score_at_entry=50.0)
                        _pm_sync.positions = _new
                        _pm_sync._save_state()
                        st.session_state["recon"] = None
                        st.markdown("<div class='ok'>동기화 완료</div>", unsafe_allow_html=True)
                        st.rerun()
                    except Exception as e:
                        st.markdown(f"<div class='fail'>{e}</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='ok' style='margin-top:6px'>"
                            "✓ 로컬 장부와 브로커 실계좌 일치</div>",
                            unsafe_allow_html=True)

    with cb:
        st.markdown("<div style='font-weight:700;font-size:.86rem;color:var(--t2);"
                    "margin-bottom:12px'>실행 설정</div>", unsafe_allow_html=True)
        is_p = st.session_state.get("trade_mode", "페이퍼(모의)").startswith("페이퍼")
        st.markdown(
            f"<div style='font-size:.74rem;margin-bottom:6px;color:{'#3B82F6' if is_p else '#F04452'}'>"
            f"● {'모의(페이퍼)' if is_p else '실거래'} 모드 "
            f"<span style='color:var(--t3);font-weight:500'>· 사이드바에서 변경</span></div>",
            unsafe_allow_html=True)
        render_horizon_picker("horizon_live")   # 투자 기간 (라이브)
        active = st.session_state.get("active_strategy","composite")
        an2, ac2 = STRAT[active]
        st.markdown(f"""
        <div style='margin-top:12px;padding-top:12px;border-top:1px solid var(--line)'>
          <div style='font-size:.7rem;color:var(--t3);margin-bottom:4px'>전략</div>
          <div style='font-weight:700;color:{ac2};font-size:.88rem'>{an2}</div>
        </div>""", unsafe_allow_html=True)
        if not is_p:
            st.markdown("<div class='fail' style='margin-top:10px;font-size:.78rem'>"
                        "실거래 모드 — 실제 자금이 사용됩니다</div>",
                        unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # ── 자본 배분 모드 ──
    st.markdown("<div style='font-size:.72rem;color:var(--t3);font-weight:600;"
                "letter-spacing:.04em;margin:14px 0 6px'>자본 배분</div>",
                unsafe_allow_html=True)
    _ALLOC = {"유동형": "유동형 (동적 배분)", "고정형": "고정형 (전량·분할)"}
    st.session_state.setdefault("alloc_mode_lbl", "유동형")
    _alloc_sel = st.segmented_control(
        "배분 방식", list(_ALLOC.values()),
        default=_ALLOC[st.session_state.get("alloc_mode_lbl", "유동형")],
        key="alloc_mode_seg",
        label_visibility="collapsed") or _ALLOC["유동형"]
    _is_dyn = _alloc_sel.startswith("유동형")
    st.session_state["alloc_mode_lbl"] = "유동형" if _is_dyn else "고정형"
    st.session_state["alloc_dynamic"] = _is_dyn

    if _is_dyn:
        # 유동형: 고정 비율 컨트롤 없음 — 시드 내 신호 강도 비례 동적 배분
        bm = sm = "전량"  # (고정형 변수 폴백, 미사용)
        st.session_state["buy_mode"] = bm; st.session_state["sell_mode"] = sm
        st.markdown(
            "<div style='font-size:.7rem;color:var(--t2);line-height:1.55;"
            "background:rgba(0,194,168,.06);border:1px solid rgba(0,194,168,.25);"
            "border-radius:8px;padding:8px 11px;margin-top:2px'>"
            "<b style='color:#00C2A8'>유동형</b> — 제한된 시드 안에서 신호가 강한 종목에 "
            "더 큰 비중(종목당 최대 25%)으로 자동 배분하고, '보유 ↔ 목표' 차이만큼만 "
            "유동적으로 매수/매도합니다. 약해진 종목의 자본은 더 강한 기회로 회전. "
            "고정 %가 아니라 상황에 맞춰 금액이 결정됩니다.</div>",
            unsafe_allow_html=True)
    else:
        am1, am2 = st.columns(2)
        bm = am1.segmented_control(
            "매수", ["전량", "분할"],
            default=st.session_state.get("buy_mode", "분할"),
            key="buy_mode_ctl",
            help="전량=목표 비중 한 번에 / 분할=목표 비중의 일부 %만 매수") or "전량"
        sm = am2.segmented_control(
            "매도", ["전량", "분할"],
            default=st.session_state.get("sell_mode", "전량"),
            key="sell_mode_ctl",
            help="전량=보유 전부 / 분할=보유 수량의 일부 %만 (손절은 항상 전량)") or "전량"
        st.session_state["buy_mode"] = bm
        st.session_state["sell_mode"] = sm
        pc1, pc2 = st.columns(2)
        if bm == "분할":
            st.session_state["buy_pct"] = pc1.slider(
                "매수 비율 %", 1, 100, int(st.session_state.get("buy_pct", 50)),
                key="buy_pct_ctl", help="목표 비중의 몇 %를 이번 사이클에 매수할지")
        else:
            st.session_state["buy_pct"] = 100
        if sm == "분할":
            st.session_state["sell_pct"] = pc2.slider(
                "매도 비율 %", 1, 100, int(st.session_state.get("sell_pct", 50)),
                key="sell_pct_ctl", help="보유 수량의 몇 %를 매도할지 (손절은 항상 전량)")
        else:
            st.session_state["sell_pct"] = 100
        _bt = f" {st.session_state['buy_pct']}%" if bm == "분할" else ""
        _stt = f" {st.session_state['sell_pct']}%" if sm == "분할" else ""
        st.markdown(f"<div style='font-size:.72rem;color:var(--t2);margin-top:2px'>"
                    f"매수 <b style='color:var(--green)'>{bm}{_bt}</b> · "
                    f"매도 <b style='color:var(--up)'>{sm}{_stt}</b></div>",
                    unsafe_allow_html=True)

    st.markdown("<br/>", unsafe_allow_html=True)
    b1,b2,_ = st.columns([1,1,2])
    _running = st.session_state["live_running"]
    if b1.button("지금 1회 실행" if not _running else "실행 중…",
                 type="primary", disabled=_running):
        trigger_live(is_p, active); st.rerun()
    if b2.button("스캔만 (주문 없음)"):
        trigger_scan(active); st.rerun()

    # 주문 미리보기
    if scores:
        st.markdown("<br/>", unsafe_allow_html=True)
        st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:10px'>"
                    "주문 미리보기</div>", unsafe_allow_html=True)
        from portfolio import PortfolioManager
        from scorer import StockScore
        _pv_paper = _is_paper_mode()
        pm2 = PortfolioManager(paper=_pv_paper)
        s_o = [StockScore(r["ticker"],r["score"],0,0,0,0) for r in scores]
        all_t2 = list(set(s.ticker for s in s_o)|set(pm2.positions.keys()))
        cur_p2 = fetch_prices(tuple(all_t2))
        orders = pm2.generate_orders(s_o, cur_p2,
                    buy_mode=st.session_state.get("buy_mode","분할"),
                    sell_mode=st.session_state.get("sell_mode","전량"),
                    buy_pct=st.session_state.get("buy_pct",100)/100.0,
                    sell_pct=st.session_state.get("sell_pct",100)/100.0,
                    available_override=(_paper.cash() if _pv_paper else None))
        o1,o2 = st.columns(2)
        with o1:
            st.markdown(f"<div style='font-weight:700;color:var(--up);font-size:.82rem;"
                        f"margin-bottom:8px'>매도 {len(orders['sell'])}건</div>",
                        unsafe_allow_html=True)
            for o in orders["sell"]:
                st.markdown(f"""<div class='card-xs'>
                  <span style='font-weight:800'>{o["ticker"]}</span>
                  &nbsp;<span class='bu'>{o["reason"].split()[0]}</span>
                  <div style='font-size:.72rem;color:var(--t3);margin-top:3px'>
                    {o["shares"]}주 · ${o["est_price"]:.2f}</div>
                </div>""", unsafe_allow_html=True)
            if not orders["sell"]: st.markdown(
                "<div style='color:var(--t3);font-size:.8rem'>없음</div>",
                unsafe_allow_html=True)
        with o2:
            st.markdown(f"<div style='font-weight:700;color:var(--green);font-size:.82rem;"
                        f"margin-bottom:8px'>매수 {len(orders['buy'])}건</div>",
                        unsafe_allow_html=True)
            for o in orders["buy"]:
                st.markdown(f"""<div class='card-xs'>
                  <span style='font-weight:800'>{o["ticker"]}</span>
                  &nbsp;<span class='bg_'>스코어 {o["score"]:.0f}</span>
                  <div style='font-size:.72rem;color:var(--t3);margin-top:3px'>
                    {o["shares"]}주 · ${o["est_cost"]:,.0f}</div>
                </div>""", unsafe_allow_html=True)
            if not orders["buy"]: st.markdown(
                "<div style='color:var(--t3);font-size:.8rem'>없음</div>",
                unsafe_allow_html=True)

    # ── 일일 손실 kill switch ──────────────────────────────────────────────────
    st.markdown("<br/>", unsafe_allow_html=True)
    st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:8px'>"
                "일일 손실 한도 (Kill Switch)</div>", unsafe_allow_html=True)
    import risk_guard as _rg_ui
    _kc1, _kc2 = st.columns([2, 1])
    _limit_pct = _kc1.slider("일일 손실 한도", 1, 20,
                             int(st.session_state.get("daily_loss_limit", 0.05)*100),
                             format="-%d%%",
                             help="당일 시작 자산 대비 이 %만큼 손실 시 신규 매수 전면 중단")
    st.session_state["daily_loss_limit"] = _limit_pct / 100
    # 현재 자산으로 상태 평가 (미연동 시 보유 평가액 사용)
    _cur_eq_ui = state["equity"] if state["connected"] else state["t_cur"]
    _rg_st = _rg_ui.check(_cur_eq_ui or 0, loss_limit=_limit_pct/100)
    _dp = _rg_st["daily_pnl_pct"]
    _dpc = "var(--up)" if _dp >= 0 else "var(--dn)"
    if _rg_st["halted"]:
        _kc2.markdown(f"""<div class='kpi' style='border:1px solid rgba(240,68,82,.4)'>
          <div class='kpi-l'>상태</div>
          <div class='kpi-v' style='color:var(--up);font-size:.9rem'>거래 중단</div>
        </div>""", unsafe_allow_html=True)
    else:
        _kc2.markdown(f"""<div class='kpi'>
          <div class='kpi-l'>당일 손익</div>
          <div class='kpi-v' style='color:{_dpc};font-size:1rem'>{_dp:+.2%}</div>
        </div>""", unsafe_allow_html=True)
    if _rg_st["halted"]:
        st.markdown(f"""<div style='background:rgba(240,68,82,.07);
          border:1px solid rgba(240,68,82,.3);border-radius:8px;padding:8px 12px;
          margin-top:8px;font-size:.78rem;color:#F04452'>
          {_rg_st['halt_reason']} — 신규 매수가 차단된 상태입니다.
          청산(매도)은 계속 가능합니다.</div>""", unsafe_allow_html=True)
        if st.button("거래 재개 (한도 해제)"):
            _rg_ui.reset(); st.rerun()
    else:
        st.markdown(f"<div style='font-size:.72rem;color:var(--t3);margin-top:6px'>"
                    f"시작 자산 ${_rg_st['start_equity']:,.0f} 기준 · "
                    f"-{_limit_pct}% 도달 시 자동 매수 중단</div>",
                    unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── 백그라운드 자동매매 데몬 — 유일한 자동매매 실행 주체 ──────────────────
    st.markdown("<br/>", unsafe_allow_html=True)
    st.markdown("<div style='font-weight:800;font-size:.9rem;margin-bottom:2px'>"
                "백그라운드 자동매매 데몬</div>"
                "<div style='font-size:.74rem;color:var(--t3);margin-bottom:8px'>"
                "자동매매는 이 데몬 하나만 실행합니다 — 앱·화면을 꺼도 미국장 시간 내내 동작. "
                "앱은 관제(시작/중지/설정)만 담당합니다.</div>",
                unsafe_allow_html=True)

    from core import control as _ctl_at
    _at_cfg_now = _ctl_at.load_config()
    _at_running = _ctl_at.daemon_alive()
    _at_should_run = _at_cfg_now.get("enabled", False)
    # 데드맨 스위치(앱 측): 켜져 있어야 하는 데몬이 죽어 있으면 경고
    if _at_should_run and not _at_running:
        st.error("⚠️ 백그라운드 데몬이 비정상 중지된 상태입니다 (켜져 있어야 함) — "
                 "아래 '시작'으로 재시작하세요. 그동안 자동매매는 멈춰 있습니다.")

    _AT_IV = {"5초": 5, "10초": 10, "30초": 30, "1분": 60,
              "3분": 180, "5분": 300, "15분": 900}
    _iv_now = int(_at_cfg_now.get("interval", 300))
    st.session_state.setdefault(
        "at_interval_lbl",
        next((k for k, v in _AT_IV.items() if v == _iv_now), "5분"))
    _atc1, _atc2 = st.columns([1, 2])
    _at_iv_lbl = _atc1.selectbox("실행 주기", list(_AT_IV), key="at_interval_lbl")
    st.session_state["at_interval_sec"] = _AT_IV[_at_iv_lbl]
    _atc2.markdown(
        f"<div class='kpi' style='margin-top:0'><div class='kpi-l'>데몬 상태</div>"
        f"<div class='kpi-v' style='font-size:.9rem;color:"
        f"{'#05C072' if (_at_running and _at_should_run) else ('#FF9500' if _at_running else 'var(--t3)')}'>"
        f"{('● 실행 중 (24시간)' if _at_should_run else '◐ 대기 중 (매매 일시정지 — 자동 매매 토글 OFF)') if _at_running else '○ 정지'}</div>"
        f"<div style='font-size:.64rem;color:var(--t3)'>"
        f"{('모의' if _at_cfg_now.get('paper', True) else '실거래')} · "
        f"{_at_cfg_now.get('strategy_name', '복합')} · "
        f"{('매일 1회 ' + _at_cfg_now.get('daily_time', '10:00') + ' ET') if _at_cfg_now.get('schedule') == 'daily' else _at_iv_lbl + '마다'}</div></div>",
        unsafe_allow_html=True)

    # 실행 스케줄 — 구 스케줄러(매일 1회) 흡수: 데몬의 모드로 통합
    _sched_lbl = st.radio(
        "실행 스케줄", ["주기 실행 (장중 계속)", "매일 1회 (개장 후 지정 시각)"],
        horizontal=True, key="at_schedule_mode",
        index=(1 if _at_cfg_now.get("schedule") == "daily" else 0))
    _is_daily = _sched_lbl.startswith("매일")
    if _is_daily:
        _dt_opts = ["09:35", "10:00", "10:30", "11:00", "12:00"]
        _dt_cur = _at_cfg_now.get("daily_time", "10:00")
        st.selectbox("실행 시각 (ET)", _dt_opts, key="at_daily_time",
                     index=_dt_opts.index(_dt_cur) if _dt_cur in _dt_opts else 1)

    def _at_make_cfg(enabled):
        _c = build_daemon_config(enabled)
        _c["schedule"] = "daily" if _is_daily else "interval"
        if _is_daily:
            _c["daily_time"] = st.session_state.get("at_daily_time", "10:00")
        return _c

    _atb1, _atb2, _atb3 = st.columns(3)
    if _atb1.button("▶ 시작", disabled=_at_running, type="primary"):
        if not _is_paper_mode() and not broker_connected():
            st.error("실거래 자동매매는 Alpaca 키 연동이 필요합니다 (설정에서 연결).")
        else:
            _ctl_at.save_config(_at_make_cfg(True))
            _ctl_at.start_daemon()
            st.session_state["auto_enabled"] = True   # 토글 = 마스터 스위치 동기화
            st.success("백그라운드 자동매매 시작 — 앱·화면을 꺼도 동작합니다. "
                       "(사이드바 '자동 매매' 토글 OFF = 데몬 완전 종료)")
            time.sleep(0.5); st.rerun()
    if _atb2.button("■ 중지", disabled=not _at_running):
        _ctl_at.stop_daemon()
        st.session_state["auto_enabled"] = False
        st.success("백그라운드 자동매매 중지됨"); time.sleep(0.5); st.rerun()
    if _at_running and _atb3.button("설정 갱신"):
        # 전략·기간·주기·스케줄을 바꾼 뒤 누르면 다음 사이클부터 반영
        _ctl_at.save_config(_at_make_cfg(_at_should_run))
        st.success("설정 갱신됨 — 다음 사이클부터 반영")

    if _at_running:
        # 최근 데몬 로그 (마지막 12줄)
        try:
            _atlog = (Path(__file__).parent / "autotrader.log").read_text(
                encoding="utf-8").splitlines()[-12:]
            if _atlog:
                st.markdown("<div style='font-size:.66rem;color:var(--t3);margin-top:6px'>"
                            "데몬 로그</div>", unsafe_allow_html=True)
                st.code("\n".join(_atlog), language=None)
        except Exception:
            pass
    st.markdown("<div style='font-size:.68rem;color:var(--t3);margin-top:4px'>"
                "터미널에서 직접 실행도 가능: <code>python autotrader.py</code> · "
                "윈도우는 <code>run_bot.bat</code></div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # 로그
    _rd = st.session_state["live_running"]
    st.markdown(f"<div style='font-weight:700;font-size:.84rem;margin-bottom:10px;"
                f"color:{'var(--blue)' if _rd else 'var(--t2)'}'>"
                f"{'실행 중…' if _rd else '실행 로그'}</div>", unsafe_allow_html=True)
    if _rd and _AR: st_autorefresh(interval=2000, key="live_poll")
    logs = st.session_state.get("live_log", [])
    if logs:
        lh = "".join(
            f"<div class='logline' style='color:{'var(--green)' if '완료' in l else 'var(--up)' if '실패' in l else 'var(--t2)'}'>{l}</div>"
            for l in reversed(logs))
        st.markdown(f"<div style='max-height:220px;overflow-y:auto'>{lh}</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<div style='color:var(--t3);font-size:.8rem'>기록 없음</div>",
                    unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "백테스트":
    import watchlist as wl2
    st.markdown("<div class='stitle'>백테스트 — 전체 전략 비교</div>", unsafe_allow_html=True)
    st.markdown("<div style='font-size:.74rem;color:var(--t3);margin:-6px 0 10px'>"
                "한 번 실행 → 전 매매법 동일 조건 비교 · 카드 클릭 시 상세</div>",
                unsafe_allow_html=True)

    # ── 공통 파라미터 ──────────────────────────────────────────────────────────
    with st.expander("파라미터", expanded=True):
        render_horizon_picker("horizon_bt")   # 투자 기간 (백테스트)
        c1,c2,c3 = st.columns(3)
        from datetime import timedelta as _td_bt
        _today = date.today()
        sd = c1.date_input("시작일", value=_today - _td_bt(days=730))
        ed = c2.date_input("종료일", value=_today)
        cap = c3.number_input("초기 자본 ($)", value=10000, step=1000)
        c4,c5,c6,c7 = st.columns(4)
        np2 = c4.slider("최대 포지션", 1,10,3)
        nms = c5.slider("진입 스코어", 40,90,55)
        nsl = c6.slider("손절 %", 1,20,8)
        ntr = c7.slider("트레일링 스톱 %", 5,40,15,
                        help="고점 대비 N% 하락 시 수익 실현")
        c8,c9 = st.columns(2)
        ntf = c8.checkbox("하락장 매수 차단 (SPY 200MA)", value=True)
        nadapt = c9.checkbox("적응형 (강세장 공격·약세장 방어)", value=True)
        c10, c11 = st.columns(2)
        nrisk = c10.checkbox("변동성 타겟 사이징 (리스크 패리티)", value=False,
                             help="동일비중 대신 변동성 역수로 비중 배분 → 각 종목이 비슷한 리스크를 기여. "
                                  "변동성 큰 종목은 작게, 작은 종목은 크게 담아 변동성을 평준화합니다.")
        nvt = c11.slider("목표 변동성 (연율 %)", 10, 40, 25, step=5,
                         help="리스크 패리티 기준선. 낮을수록 보수적으로 담습니다.",
                         disabled=not nrisk) / 100
        cc1, cc2 = st.columns(2)
        n_comm = cc1.slider("수수료 (%)", 0.0, 0.5, 0.1, step=0.05) / 100
        n_slip = cc2.slider("슬리피지 (%)", 0.0, 0.3, 0.05, step=0.01) / 100
        # ── 테스트 종목 다변화: 워치리스트 / 외부 지수 ──
        import universe as _uni_bt
        _u_opts = ["내 워치리스트"] + _uni_bt.available_indexes() + ["코스피·코스닥"]
        _u_src = st.selectbox("종목 소스", _u_opts, key="bt_uni_src",
                              help="S&P 500 등 외부 지수로 테스트 종목을 다변화")
        if _u_src == "내 워치리스트":
            _u_base = wl2.load()
        elif _u_src == "코스피·코스닥":
            _u_base = _uni_bt.get_kr_universe()
        else:
            with st.spinner(f"{_u_src} 구성종목 로딩…"):
                _u_base = _uni_bt.get_index(_u_src)
        _BT_CAP_N = 60   # 속도: 대형 지수는 상위 N종목으로 제한
        sel_u = st.multiselect("종목 추리기 (비우면 소스 전체)", _u_base, default=[])
        if sel_u:
            universe = sel_u
        elif len(_u_base) > _BT_CAP_N:
            universe = _u_base[:_BT_CAP_N]
            st.caption(f"속도를 위해 상위 {_BT_CAP_N}종목으로 제한 (직접 고르면 전체 사용)")
        else:
            universe = _u_base

    _hzp = horizon_params(st.session_state.get("horizon", "단기"))
    st.markdown(f"<div style='font-size:.78rem;color:var(--t3);margin-bottom:10px'>"
                f"종목 {len(universe)}개 · {sd} ~ {ed} · 전략 {len(STRAT)}개 · "
                f"기간모드 <b style='color:var(--t2)'>{st.session_state.get('horizon','단기')}</b>"
                f"(보유~{_hzp['hold_strong']}일·익절 {_hzp['take_profit']:.0%})</div>",
                unsafe_allow_html=True)

    # ── 심화 분석: 기간 프리셋 비교 + 파라미터 그리드 스윕 ─────────────────────
    st.markdown("<div style='font-size:.8rem;color:var(--t2);margin:14px 0 4px;font-weight:700'>"
                "그리드 스윕 · 심화 분석</div>"
                "<div style='font-size:.72rem;color:var(--t3);margin-bottom:6px'>"
                "손절×익절×진입점수 등 파라미터 조합을 여러 전략에 일괄 적용해 "
                "'어떤 조합이 가장 좋은지' 한 번에 검색합니다.</div>",
                unsafe_allow_html=True)
    with st.expander("그리드 스윕 열기 (파라미터 조합 일괄 검색 · 기간 프리셋 비교)",
                     expanded=True):
        import backtester as _btm2

        def _bt_row(extra: dict, r):
            return {**extra, "수익률": f"{r.total_return:+.1%}", "CAGR": f"{r.cagr:+.1%}",
                    "MDD": f"{r.mdd:.1%}", "샤프": f"{r.sharpe:.2f}",
                    "승률": f"{r.win_rate:.0%}", "거래": len(r.trades), "_ret": r.total_return}

        _sw_strat = st.selectbox("기준 전략", list(STRAT.keys()),
                                 format_func=lambda k: STRAT[k][0], key="sweep_strat")
        _ca, _cb = st.columns(2)

        if _ca.button("① 기간 프리셋 비교 (4종)", key="sweep_preset"):
            with st.spinner("시세 로딩…"):
                _bd = _btm2.load_market_data(universe, str(sd), str(ed))
            _rows = []; _pg = st.progress(0.0)
            for _i, _hz in enumerate(HORIZON_ORDER):
                _hp2 = horizon_params(_hz)
                _pg.progress((_i+1)/len(HORIZON_ORDER), text=f"{_hz}…")
                try:
                    _r = _btm2.run(start=str(sd), end=str(ed), capital=float(cap),
                        universe=universe, strategy=_sw_strat,
                        stop_loss=_hp2["stop_loss"], take_profit=_hp2["take_profit"],
                        hold_strong=_hp2["hold_strong"], hold_medium=_hp2["hold_medium"],
                        min_score=_hp2["min_score"], sell_score=_hp2["sell_score"],
                        rebalance_days=_hp2["rebalance_days"], prefetched=_bd)
                    _rows.append(_bt_row({"기간모드": f"{_hz}({_hp2['label']})"}, _r))
                except Exception:
                    pass
            _pg.empty()
            st.session_state["sweep_preset_res"] = _rows

        _pr = st.session_state.get("sweep_preset_res")
        if _pr:
            st.markdown("**기간 프리셋 비교 결과**")
            st.dataframe(pd.DataFrame(_pr).drop(columns=["_ret"]),
                         width="stretch", hide_index=True)

        st.markdown("<div style='border-top:1px solid var(--line);margin:12px 0 8px'></div>",
                    unsafe_allow_html=True)
        st.markdown("**② 그리드 스윕 — 손절×익절×진입점수 조합 (직접 커스텀)**")
        st.caption("한 전략만 보면 우연(노이즈)에 좌우되기 쉬워요 — 여러 전략에 같은 "
                   "조합을 적용하고 평균을 내면 '진짜 좋은 조합인지 운인지' 더 신뢰할 수 있습니다.")
        render_grid_sweep("sweep_grid", universe, sd, ed, float(cap), [_sw_strat])

    # ── 전체 전략 실행 ─────────────────────────────────────────────────────────
    if st.button("전체 전략 백테스트 실행", type="primary"):
        import backtester as _btm
        results = {}
        prog = st.progress(0.0, text="시세 데이터 다운로드 중…")
        try:
            _bundle = _btm.load_market_data(universe, str(sd), str(ed))
        except Exception as e:
            st.error(f"데이터 로드 실패: {e}"); st.stop()
        _keys = list(STRAT.keys())
        for _i, _k in enumerate(_keys):
            prog.progress((_i+1)/len(_keys), text=f"[{STRAT[_k][0]}] 백테스트 중… ({_i+1}/{len(_keys)})")
            try:
                results[_k] = _btm.run(
                    start=str(sd), end=str(ed), capital=float(cap),
                    universe=universe, strategy=_k,
                    max_positions=np2, min_score=nms, sell_score=max(nms-25,10),
                    stop_loss=nsl/100, trailing_stop=ntr/100,
                    # 기간 모드 → 보유기간·익절·리밸런스 주입
                    take_profit=_hzp["take_profit"],
                    hold_strong=_hzp["hold_strong"], hold_medium=_hzp["hold_medium"],
                    rebalance_days=_hzp["rebalance_days"],
                    use_trend_filter=ntf, adaptive_regime=nadapt,
                    commission=n_comm, slippage=n_slip,
                    risk_sizing=nrisk, vol_target=nvt,
                    prefetched=_bundle,
                )
            except Exception as _e:
                results[_k] = None
        prog.empty()
        st.session_state["bt_all"] = results
        st.session_state["bt_all_cap"] = float(cap)
        st.session_state["bt_all_range"] = (str(sd), str(ed))

    results = st.session_state.get("bt_all")
    if not results:
        st.markdown("<div class='card' style='text-align:center;padding:40px'>"
                    "<div style='color:var(--t3)'>‘전체 전략 백테스트 실행’을 누르세요</div>"
                    "</div>", unsafe_allow_html=True); st.stop()

    _scap = st.session_state.get("bt_all_cap", 10000)
    _srange = st.session_state.get("bt_all_range", (str(sd), str(ed)))

    # ── 지표 계산 ──────────────────────────────────────────────────────────────
    def _metrics(res, cap0):
        curve = res.equity_curve
        fe = float(curve.iloc[-1]); tr = (fe/cap0)-1
        yr = len(curve)/252; cagr = (fe/cap0)**(1/max(yr,.01))-1
        rm = curve.cummax(); mdd = float(((curve-rm)/rm).min())
        rr = curve.pct_change().dropna()
        sharpe = float(rr.mean()/rr.std()*np.sqrt(252)) if rr.std()>0 else 0
        wins = [t for t in res.trades if t.pnl_pct > 0]
        wr = len(wins)/len(res.trades) if res.trades else 0
        return dict(tr=tr, cagr=cagr, mdd=mdd, sharpe=sharpe, wr=wr,
                    n=len(res.trades), fe=fe)

    # SPY 벤치마크
    _spy_r = 0.0
    _spy_df = fetch_history("SPY","5y")
    if not _spy_df.empty:
        try:
            _s = _spy_df["Close"].squeeze()
            _s = _s[(_s.index>=pd.Timestamp(_srange[0])) & (_s.index<=pd.Timestamp(_srange[1]))]
            if not _s.empty: _spy_r = float(_s.iloc[-1]/_s.iloc[0]-1)
        except Exception: pass

    rows = []
    for _k, _res in results.items():
        if _res is None:
            rows.append((_k, None)); continue
        rows.append((_k, _metrics(_res, _scap)))
    # 수익률 내림차순 정렬 (실패는 맨 뒤)
    rows.sort(key=lambda x: (x[1]["tr"] if x[1] else -9e9), reverse=True)

    # 요약 헤더
    _ok = [m for _,m in rows if m]
    if _ok:
        _best = rows[0]
        st.markdown(f"<div style='font-size:.8rem;color:var(--t2);margin-bottom:8px'>"
                    f"S&P500 {(_spy_r):+.1%} · 최고 전략 "
                    f"<b style='color:{STRAT[_best[0]][1]}'>{STRAT[_best[0]][0]}</b> "
                    f"{_best[1]['tr']:+.1%}</div>", unsafe_allow_html=True)

    # ── 전략별 요약 카드 (클릭 시 상세 expander) ─────────────────────────────
    for _rank, (_k, _m) in enumerate(rows, 1):
        _name, _color = STRAT[_k]
        if _m is None:
            st.markdown(f"<div class='card-xs' style='opacity:.5'>"
                        f"#{_rank} {_name} — 실패/데이터 부족</div>",
                        unsafe_allow_html=True)
            continue
        _trc = "#F04452" if _m["tr"]>=0 else "#2F80ED"
        _vs_spy = _m["tr"] - _spy_r
        _medal = {1:"",2:"",3:""}.get(_rank, f"#{_rank}")
        with st.expander(
            f"{_medal}  {_name}   ·   수익률 {_m['tr']:+.1%}   ·   "
            f"CAGR {_m['cagr']:+.1%}   ·   MDD {_m['mdd']:.1%}   ·   "
            f"샤프 {_m['sharpe']:.2f}   ·   승률 {_m['wr']:.0%} ({_m['n']}건)"):
            _res = results[_k]
            # 상세 KPI
            kk = st.columns(6)
            for _col,_lbl,_val,_cc in [
                (kk[0],"총 수익률",f"{_m['tr']:+.1%}", _trc),
                (kk[1],"CAGR",f"{_m['cagr']:+.1%}","var(--green)"),
                (kk[2],"최대 낙폭",f"{_m['mdd']:.1%}","var(--orange)"),
                (kk[3],"샤프",f"{_m['sharpe']:.2f}","var(--blue)"),
                (kk[4],"승률",f"{_m['wr']:.0%} ({_m['n']}건)","var(--t1)"),
                (kk[5],"S&P 초과",f"{_vs_spy:+.1%}","var(--green)" if _vs_spy>0 else "var(--up)"),
            ]: kpi(_col,_lbl,_val,None,_cc)
            # 수익 곡선
            _curve = _res.equity_curve
            _r3,_g3,_b3 = int(_color[1:3],16),int(_color[3:5],16),int(_color[5:7],16)
            _figc = go.Figure()
            _figc.add_trace(go.Scatter(x=_curve.index,y=_curve.values,name=_name,
                mode="lines",line=dict(color=_color,width=2),
                fill="tozeroy",fillcolor=f"rgba({_r3},{_g3},{_b3},.07)"))
            _figc.add_hline(y=_scap,line_dash="dash",line_color="#1E1E27",
                annotation_text="원금",annotation_font_color="#4A5260",
                annotation_font_size=10)
            _figc.update_layout(**CL(height=220,
                yaxis=dict(**_YA,tickprefix="$",tickformat=",.0f"),
                legend=dict(orientation="h",y=1.12,x=0,font=dict(size=10))))
            st.plotly_chart(_figc,width="stretch",
                            config={"displayModeBar":False},key=f"btc_{_k}")
            # 월별 히트맵
            try:
                _mo = _curve.resample("ME").last().pct_change().dropna()
                _mdf = pd.DataFrame({"year":_mo.index.year,"month":_mo.index.month,
                                     "ret":_mo.values*100})
                _piv = _mdf.pivot(index="year",columns="month",values="ret")
                _mkr = {i:f"{i}월" for i in range(1,13)}
                _piv.columns = [_mkr.get(m,str(m)) for m in _piv.columns]
                _figh = go.Figure(go.Heatmap(z=_piv.values,x=_piv.columns,
                    y=[str(y) for y in _piv.index],
                    colorscale=[[0,"#081528"],[.5,"#12121A"],[1,"#1f0909"]],zmid=0,
                    text=[[f"{v:.1f}%" if not np.isnan(v) else "" for v in r] for r in _piv.values],
                    texttemplate="%{text}",textfont=dict(size=10),showscale=False))
                _figh.update_layout(**{**_CL,"margin":dict(l=0,r=0,t=24,b=0),"height":130,
                    "xaxis":dict(side="top",tickfont=dict(size=10,color="#4A5260")),
                    "yaxis":dict(tickfont=dict(size=10,color="#4A5260"),autorange="reversed")})
                st.markdown("<div style='font-size:.72rem;color:var(--t3);margin:8px 0 4px'>"
                            "월별 수익률</div>",unsafe_allow_html=True)
                st.plotly_chart(_figh,width="stretch",
                                config={"displayModeBar":False},key=f"bth_{_k}")
            except Exception:
                pass
            # 거래 내역
            _trs = [dict(종목=t.ticker,진입=str(t.entry_date),청산=str(t.exit_date),
                         진입가=f"${t.entry_price:.2f}",청산가=f"${t.exit_price:.2f}",
                         수익률=f"{t.pnl_pct:+.1%}",손익=f"${t.pnl_usd:+,.0f}",
                         사유=REASON_KR.get(t.reason,t.reason)) for t in _res.trades]
            if _trs:
                st.dataframe(pd.DataFrame(_trs),width="stretch",hide_index=True)
            else:
                st.markdown("<div style='color:var(--t3);font-size:.8rem'>거래 없음</div>",
                            unsafe_allow_html=True)



# ══════════════════════════════════════════════════════════════════════════════
elif cur == "AI 분석":
    import watchlist as _wl_ai
    st.markdown("<div class='stitle'>AI 분석 "
                "<span style='font-size:.66rem;color:var(--t3);font-weight:500'>· 기술적 진단 + 시그널 + 트레이드 플랜</span></div>",
                unsafe_allow_html=True)

    _held = {p["ticker"]: p for p in positions}
    _uni = sorted(set(_wl_ai.load()) | set(_held.keys())) or ["AAPL"]
    _selc1, _selc2 = st.columns([2, 1])
    _aisel = _selc1.selectbox("종목", _uni, key="ai_analysis_sym", format_func=_tk_label)
    _ai_hz = _selc2.selectbox("투자 기간", HORIZON_ORDER, key="ai_horizon",
                              index=HORIZON_ORDER.index(st.session_state.get("horizon", "단기"))
                              if st.session_state.get("horizon", "단기") in HORIZON_ORDER else 2,
                              format_func=lambda x: f"{x}")

    @st.cache_data(ttl=1800, show_spinner=False)
    def _ai_score(sym):
        from scorer import _score_one
        try:
            s = _score_one(sym)
            return dict(total=s.total, institutional=s.institutional,
                        sentiment=s.sentiment, sector=s.sector, fundamental=s.fundamental)
        except Exception:
            return None

    with st.spinner("기술적 지표·시그널 분석 중…"):
        _tech = ai_technicals(_aisel)
        _sc = _ai_score(_aisel)
        _rel = ai_relative_strength(_aisel)

    # ── 종합 진단 점수 (시그널 50% + 기술적 50%) ──────────────────────────
    def _tech_score(t: dict, rel) -> float:
        _tmap = {"강한 상승": 90, "상승": 72, "횡보·혼조": 50,
                 "하락": 30, "강한 하락": 12, "데이터 부족": 50}
        s = _tmap.get(t.get("trend"), 50)
        s = 0.6 * s + 0.4 * (t.get("pos52", 0.5) * 100)
        _r = t.get("rsi")
        if _r is not None:
            if _r > 75:   s -= 8
            elif _r < 25: s += 6
        if rel is not None:
            s += 5 if rel > 0 else -5
        return max(0.0, min(100.0, s))

    _hp = horizon_params(_ai_hz)
    _cL, _cR = st.columns([2, 1.05], gap="medium")

    with _cL:
        _h = fetch_history(_aisel, "1y")
        if (not _h.empty) and _tech:
            _cl = _h["Close"].squeeze()
            _cur_px = _tech["px"]
            _base6 = float(_cl.iloc[-126]) if len(_cl) > 126 else float(_cl.iloc[0])
            _chgp = (_cur_px - _base6) / _base6 if _base6 else 0
            _lc = "#F0454F" if _chgp >= 0 else "#3B82F6"
            _r, _g, _b = int(_lc[1:3], 16), int(_lc[3:5], 16), int(_lc[5:7], 16)
            st.markdown(
                "<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:4px;flex-wrap:wrap'>"
                f"<span style='font-size:1.5rem;font-weight:900'>${_cur_px:,.2f}</span>"
                f"<span style='color:{_lc};font-weight:700;font-size:.84rem'>"
                f"{'▲' if _chgp>=0 else '▼'} {abs(_chgp):.2%} (6M)</span>"
                f"<span style='font-size:.66rem;color:var(--t3)'>추세: {_tech['trend']}</span></div>",
                unsafe_allow_html=True)
            # 차트 — 가격 + 이동평균 2종(50/200) + 지지/저항 (깔끔하게)
            _fig = go.Figure()
            _fig.add_trace(go.Scatter(x=_cl.index, y=_cl, mode="lines", name="가격",
                line=dict(color=_lc, width=2), fill="tozeroy",
                fillcolor=f"rgba({_r},{_g},{_b},.05)"))
            for _mn, _mc, _mp in [("50일선", "#8B93A1", 50), ("200일선", "#4B5563", 200)]:
                if len(_cl) >= _mp:
                    _maser = _cl.rolling(_mp).mean()
                    _fig.add_trace(go.Scatter(x=_cl.index, y=_maser, mode="lines",
                        name=_mn, line=dict(color=_mc, width=1.2)))
            # 지지/저항 — 은은한 점선, 텍스트 주석 없이 (아래 캡션으로 값 표기)
            _fig.add_hline(y=_tech["support"], line=dict(color="rgba(15,184,115,.45)", width=1, dash="dash"))
            _fig.add_hline(y=_tech["resistance"], line=dict(color="rgba(240,68,82,.45)", width=1, dash="dash"))
            _fig.update_layout(**CL(height=300, yaxis=dict(**_YA, tickprefix="$")))
            _fig.update_layout(showlegend=True, legend=dict(orientation="h", yanchor="bottom",
                y=1.0, x=0, font=dict(size=9), bgcolor="rgba(0,0,0,0)"))
            st.plotly_chart(_fig, width="stretch", config={"displayModeBar": False}, key="ai_chart")
            st.markdown(
                f"<div style='font-size:.6rem;color:var(--t3);margin-top:-6px;text-align:right'>"
                f"지지 ${_tech['support']:,.2f} · 저항 ${_tech['resistance']:,.2f}</div>",
                unsafe_allow_html=True)

            # 모멘텀 스트립 (기간별 수익률)
            def _retbox(lbl, v):
                if v is None:
                    return (f"<div style='flex:1;text-align:center'>"
                            f"<div style='font-size:.54rem;color:var(--t3)'>{lbl}</div>"
                            f"<div style='font-size:.8rem;font-weight:800;color:var(--t3)'>—</div></div>")
                _c = "#F04452" if v >= 0 else "#2F80ED"
                return (f"<div style='flex:1;text-align:center'>"
                        f"<div style='font-size:.54rem;color:var(--t3)'>{lbl}</div>"
                        f"<div style='font-size:.8rem;font-weight:800;color:{_c}'>{v:+.1%}</div></div>")
            st.markdown(
                "<div class='card' style='display:flex;gap:4px;margin-top:8px'>"
                + _retbox("1주", _tech["ret1w"]) + _retbox("1개월", _tech["ret1m"])
                + _retbox("3개월", _tech["ret3m"]) + _retbox("6개월", _tech["ret6m"])
                + "</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div class='card'>차트·지표 데이터를 불러오지 못했습니다 (심볼/네트워크 확인).</div>",
                        unsafe_allow_html=True)

    with _cR:
        # ── 종합 진단 카드 ──
        if _tech and _sc:
            _ts = _tech_score(_tech, _rel)
            _combined = 0.5 * _sc["total"] + 0.5 * _ts
            if   _combined >= 68: _vd, _vc = "매수 우위", "#0FB873"
            elif _combined >= 55: _vd, _vc = "긍정적 관찰", "#3BA776"
            elif _combined >= 45: _vd, _vc = "중립", "#FF9500"
            elif _combined >= 32: _vd, _vc = "관망·약세", "#3B82F6"
            else:                 _vd, _vc = "회피", "#2F80ED"
            st.markdown(
                "<div class='card' style='border:1px solid rgba(63,140,255,.25)'>"
                "<div style='display:flex;justify-content:space-between;align-items:center'>"
                "<span style='font-size:.7rem;font-weight:800'>종합 진단</span>"
                f"<span style='font-weight:900;font-size:.92rem;color:{_vc}'>{_vd}</span></div>"
                "<div style='display:flex;gap:8px;margin-top:6px'>"
                "<div style='flex:1'><div style='font-size:.52rem;color:var(--t3)'>종합점수</div>"
                f"<div style='font-size:1.0rem;font-weight:900;color:{_vc}'>{_combined:.0f}</div></div>"
                "<div style='flex:1'><div style='font-size:.52rem;color:var(--t3)'>시그널</div>"
                f"<div style='font-size:1.0rem;font-weight:900'>{_sc['total']:.0f}</div></div>"
                "<div style='flex:1'><div style='font-size:.52rem;color:var(--t3)'>기술적</div>"
                f"<div style='font-size:1.0rem;font-weight:900'>{_ts:.0f}</div></div></div>"
                "<div style='font-size:.54rem;color:var(--t3);margin-top:6px'>"
                "시그널·기술 50:50 가중 · 참고용, 투자 판단은 본인 책임</div></div>",
                unsafe_allow_html=True)

        # ── 기술적 스냅샷 ──
        if _tech:
            _rsi = _tech.get("rsi")
            if _rsi is None:        _rsi_txt, _rsi_c = "—", "var(--t3)"
            elif _rsi >= 70:        _rsi_txt, _rsi_c = f"{_rsi:.0f} 과매수", "#F04452"
            elif _rsi <= 30:        _rsi_txt, _rsi_c = f"{_rsi:.0f} 과매도", "#0FB873"
            else:                   _rsi_txt, _rsi_c = f"{_rsi:.0f} 중립", "var(--t1)"
            _pos = _tech.get("pos52", 0.5)
            _vol = _tech.get("vol")
            _vol_txt = f"{_vol:.0%}" if _vol is not None else "—"
            _vr = _tech.get("vol_ratio")
            _vr_txt = (f"{_vr:.1f}× " + ("급증" if _vr and _vr >= 1.5 else
                       "증가" if _vr and _vr >= 1.1 else "보통")) if _vr else "—"
            _relc = "#0FB873" if (_rel or 0) > 0 else "#2F80ED"
            _rel_txt = f"{_rel:+.1%} vs 시장" if _rel is not None else "—"
            def _row(lbl, val, c="var(--t1)"):
                return ("<div style='display:flex;justify-content:space-between;"
                        "padding:3px 0;font-size:.66rem'>"
                        f"<span style='color:var(--t2)'>{lbl}</span>"
                        f"<span style='font-weight:700;color:{c}'>{val}</span></div>")
            st.markdown(
                "<div class='card'>"
                "<div style='font-size:.66rem;font-weight:800;margin-bottom:4px'>기술적 스냅샷</div>"
                + _row("추세", _tech["trend"])
                + _row("RSI(14)", _rsi_txt, _rsi_c)
                + _row("52주 위치", f"{_pos:.0%} (저 ${_tech['lo52']:.0f}~고 ${_tech['hi52']:.0f})")
                + _row("연율 변동성", _vol_txt)
                + _row("거래량", _vr_txt)
                + _row("시장 상대강도", _rel_txt, _relc)
                + "</div>", unsafe_allow_html=True)

        # ── 트레이드 플랜 (투자 기간 연계) ──
        if _tech:
            _px = _tech["px"]
            _target = _px * (1 + _hp["take_profit"])
            _stop = _px * (1 - _hp["stop_loss"])
            _entry_lo = max(_tech["support"], _px * 0.97)
            _rr = (_hp["take_profit"] / _hp["stop_loss"]) if _hp["stop_loss"] else 0
            _dmin, _dmax = _hp["days"]
            st.markdown(
                "<div class='card'>"
                "<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:5px'>"
                "<span style='font-size:.66rem;font-weight:800'>트레이드 플랜</span>"
                f"<span style='font-size:.56rem;color:var(--t3)'>{_ai_hz}·{_hp['label']}</span></div>"
                "<div style='display:flex;gap:6px'>"
                "<div style='flex:1'><div style='font-size:.52rem;color:var(--t3)'>진입 구간</div>"
                f"<div style='font-size:.74rem;font-weight:800'>${_entry_lo:,.2f}~${_px:,.2f}</div></div>"
                "<div style='flex:1'><div style='font-size:.52rem;color:#0FB873'>목표(익절)</div>"
                f"<div style='font-size:.74rem;font-weight:800;color:#0FB873'>${_target:,.2f} (+{_hp['take_profit']:.0%})</div></div>"
                "</div>"
                "<div style='display:flex;gap:6px;margin-top:5px'>"
                "<div style='flex:1'><div style='font-size:.52rem;color:#F04452'>손절</div>"
                f"<div style='font-size:.74rem;font-weight:800;color:#F04452'>${_stop:,.2f} (-{_hp['stop_loss']:.0%})</div></div>"
                "<div style='flex:1'><div style='font-size:.52rem;color:var(--t3)'>손익비·보유</div>"
                f"<div style='font-size:.74rem;font-weight:800'>1:{_rr:.1f} · ~{_dmin}~{_dmax}일</div></div>"
                "</div></div>", unsafe_allow_html=True)

        # ── 시그널 분해 ──
        if _sc:
            def _w(v):
                return ("강함" if v >= 70 else "양호" if v >= 55 else "중립"
                        if v >= 45 else "약함" if v >= 30 else "매우 약함")
            _sig = [("기관 매수세", _sc["institutional"]), ("뉴스·심리", _sc["sentiment"]),
                    ("섹터 흐름", _sc["sector"]), ("펀더멘털", _sc["fundamental"])]
            _bars = ""
            for _lbl, _v in _sig:
                _bc = "#0FB873" if _v >= 55 else "#FF9500" if _v >= 45 else "#3B82F6"
                _bars += (
                    "<div style='margin:5px 0'>"
                    "<div style='display:flex;justify-content:space-between;font-size:.62rem;color:var(--t2)'>"
                    f"<span>{_lbl}</span><span style='font-weight:700;color:{_bc}'>{_v:.0f} · {_w(_v)}</span></div>"
                    "<div style='height:4px;background:var(--bg4);border-radius:3px;margin-top:2px'>"
                    f"<div style='height:4px;width:{min(_v,100):.0f}%;background:{_bc};border-radius:3px'></div></div></div>")
            st.markdown(
                "<div class='card'>"
                "<div style='font-size:.66rem;font-weight:800;margin-bottom:3px'>시그널 분해</div>"
                f"{_bars}</div>", unsafe_allow_html=True)

        # ── 보유 + 바로 거래 ──
        if _aisel in _held:
            _p = _held[_aisel]; _pc = "#F04452" if _p["pnl_pct"] >= 0 else "#2F80ED"
            st.markdown(
                "<div class='card'>"
                "<div style='font-size:.6rem;color:var(--t3);margin-bottom:3px'>내 보유</div>"
                f"<div style='font-size:.8rem;font-weight:700'>{_p['shares']:g}주 · 평단 {money(_p['entry'])}</div>"
                f"<div style='font-size:.72rem;font-weight:700;color:{_pc};margin-top:2px'>"
                f"{_p['pnl_pct']:+.2%} ({money(_p['pnl_usd'])})</div></div>",
                unsafe_allow_html=True)

        if st.toggle("바로 거래", key="ai_trade_on"):
            quick_trade_panel(_aisel, key_prefix="ai_trade", show_chart=False)


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "포트폴리오":
    trades = load_trades()
    st.markdown("<div class='stitle'>포트폴리오</div>", unsafe_allow_html=True)

    def _make_rt_badge(ticker: str) -> str:
        """실시간 피드 데이터로 고/저가 + 갱신 시각 뱃지 생성."""
        d = _rtf.get_price(ticker)
        if not d:
            return ""
        age = int(time.time() - d.get("ts", time.time()))
        hi = d.get("high", 0); lo = d.get("low", 0)
        src = "F" if d.get("source") == "finnhub" else "Y"
        return (f"<div style='font-size:.62rem;color:var(--t3);margin-top:3px'>"
                f"H ${hi:.2f}  L ${lo:.2f}<br>"
                f"<span style='color:var(--t3)'>{src} · {age}s</span></div>")

    # ══ 리스크 지표 계산 ══════════════════════════════════════════════════════
    @st.cache_data(ttl=3600, show_spinner=False)
    def _calc_risk(tickers_tuple: tuple) -> dict:
        """베타(vs SPY), 섹터 집중도, 간이 VaR 계산."""
        if not tickers_tuple:
            return {}
        import yfinance as yf
        tks = list(tickers_tuple)
        try:
            raw = yf.download(" ".join(tks + ["SPY"]), period="1y",
                              interval="1d", auto_adjust=True, progress=False)
            if raw.empty:
                return {}
            close = raw["Close"] if isinstance(raw["Close"], pd.DataFrame) else raw[["Close"]]
            rets  = close.pct_change().dropna()
            spy_r = rets.get("SPY", pd.Series()) if isinstance(rets, pd.DataFrame) else pd.Series()
            betas = {}
            if not spy_r.empty:
                for tk in tks:
                    col = rets.get(tk) if isinstance(rets, pd.DataFrame) else None
                    if col is not None and len(col.dropna()) > 30:
                        cov = col.dropna().cov(spy_r.loc[col.dropna().index])
                        var_spy = spy_r.var()
                        betas[tk] = round(cov / var_spy, 2) if var_spy else 1.0
            return {"betas": betas}
        except Exception:
            return {}

    if positions:
        _risk = _calc_risk(tuple(p["ticker"] for p in positions))
        _betas = _risk.get("betas", {})

        # 섹터 집중도
        import watchlist as _wl_risk
        _sector_map = {}
        for p in positions:
            _sec = _wl_risk._ticker_to_sector(p["ticker"])
            _val = p["current"] * p["shares"]
            _sector_map[_sec] = _sector_map.get(_sec, 0) + _val
        _total_inv = sum(_sector_map.values()) or 1
        _max_sector_pct = max(_sector_map.values()) / _total_inv if _sector_map else 0

        # 포트폴리오 가중 평균 베타
        _port_beta = 0.0
        if _betas and positions:
            for p in positions:
                _w = (p["current"] * p["shares"]) / max(_total_inv, 1)
                _port_beta += _w * _betas.get(p["ticker"], 1.0)

        # 간이 1일 VaR (95%, 정규분포 가정) = 총자산 × 1.65 × 일변동성
        # 일변동성 = 포트폴리오 베타 × SPY 연변동성(약 15%) / sqrt(252)
        _spy_daily_vol = 0.15 / (252 ** 0.5)
        _port_daily_vol = _port_beta * _spy_daily_vol
        _port_value = sum(p["current"] * p["shares"] for p in positions)
        _var_95 = _port_value * 1.645 * _port_daily_vol  # 95% VaR

        # 리스크 KPI 표시
        _risk_warn = _max_sector_pct > 0.6 or _port_beta > 1.5
        if _risk_warn:
            st.markdown(f"""
            <div style='background:rgba(240,68,82,.07);border:1px solid rgba(240,68,82,.3);
              border-radius:8px;padding:10px 14px;margin-bottom:12px;
              font-size:.8rem;color:#F04452'>
              리스크 경고:
              {"섹터 집중도 " + f"{_max_sector_pct:.0%}" + " 초과 (60% 기준)" if _max_sector_pct > 0.6 else ""}
              {"· " if _max_sector_pct > 0.6 and _port_beta > 1.5 else ""}
              {"포트폴리오 베타 " + f"{_port_beta:.2f}" + " (고위험)" if _port_beta > 1.5 else ""}
            </div>""", unsafe_allow_html=True)

        rk1, rk2, rk3, rk4 = st.columns(4)
        _bc = "var(--up)" if _port_beta > 1.5 else "var(--green)" if _port_beta < 0.8 else "var(--t1)"
        kpi(rk1, "포트폴리오 베타", f"{_port_beta:.2f}",
            "시장 대비 민감도", _bc)
        _sc = "var(--up)" if _max_sector_pct > 0.6 else "var(--t1)"
        _top_sec = max(_sector_map, key=_sector_map.get) if _sector_map else "—"
        _sec_kr = {"XLK":"테크","XLF":"금융","XLE":"에너지","XLV":"헬스케어",
                   "XLY":"소비재","XLI":"산업재","XLP":"필수소비"}
        kpi(rk2, "최대 섹터 집중", f"{_max_sector_pct:.0%}",
            _sec_kr.get(_top_sec, _top_sec), _sc)
        kpi(rk3, "일일 VaR 95%", f"${_var_95:,.0f}",
            "하루 최대 손실 추정", "var(--up)" if _var_95 > _port_value*0.05 else "var(--t1)")
        _pos_betas = [(p["ticker"], _betas.get(p["ticker"], 1.0)) for p in positions]
        _high_beta = max(_pos_betas, key=lambda x: x[1]) if _pos_betas else ("—", 0)
        kpi(rk4, f"최고 베타 종목",
            _high_beta[0], f"β {_high_beta[1]:.2f}")

        # 섹터 비중 바
        if _sector_map:
            _sec_items = sorted(_sector_map.items(), key=lambda x: -x[1])
            _bars = ""
            for _s, _v in _sec_items:
                _pct = _v / _total_inv
                _sc_name = _sec_kr.get(_s, _s)
                _bc2 = "#F04452" if _pct > 0.5 else "#3B82F6"
                _bars += f"""
                <div style='display:flex;align-items:center;gap:8px;margin-bottom:4px'>
                  <div style='width:56px;font-size:.7rem;color:var(--t3);text-align:right'>{_sc_name}</div>
                  <div style='flex:1;background:#1C1C23;border-radius:2px;height:6px'>
                    <div style='width:{_pct*100:.0f}%;height:6px;background:{_bc2};border-radius:2px'></div>
                  </div>
                  <div style='width:36px;font-size:.7rem;color:var(--t2);text-align:right'>{_pct:.0%}</div>
                </div>"""
            st.markdown(f"<div style='margin:8px 0 14px'>{_bars}</div>",
                        unsafe_allow_html=True)

    _connected_pf = state["connected"]
    cd, cdet = st.columns([2,3])
    with cd:
        # 미연동이면 현금 비중 제외 (실제 현금 잔고를 모름)
        vals = [p["current"]*p["shares"] for p in positions]
        labels = [p["ticker"] for p in positions]
        colors_ = [tc(p["ticker"]) for p in positions]
        _has_cash_pf = _connected_pf or state.get("paper_mode")
        if _has_cash_pf and state["cash"]:
            vals.append(state["cash"]); labels.append("현금"); colors_.append("#1E1E27")
        fig5 = go.Figure(go.Pie(values=vals or [1], labels=labels or ["없음"], hole=.72,
            marker=dict(colors=colors_ or ["#1E1E27"], line=dict(color="#09090D", width=3)),
            textinfo="none",
            hovertemplate="%{label}<br>%{value:,.0f}<extra></extra>"))
        tv2 = sum(vals)
        _center = money_compact(tv2) if vals else "—"
        _center_lbl = "총 자산 (원금 포함)" if _has_cash_pf else "보유 평가액"
        fig5.add_annotation(text=f"<b>{_center}</b>", x=.5, y=.56,
            font=dict(size=15, color="#ECEEF1"), showarrow=False)
        fig5.add_annotation(text=_center_lbl, x=.5, y=.43,
            font=dict(size=10, color="#4A5260"), showarrow=False)
        fig5.update_layout(**{**_CL, "margin":dict(l=0,r=0,t=0,b=0), "height":210,
            "showlegend":True,
            "legend":dict(orientation="h",y=-.1,font=dict(size=10,color="#8B95A1"))})
        st.plotly_chart(fig5, width="stretch", config={"displayModeBar":False})
        if not _has_cash_pf:
            st.markdown("<div style='text-align:center;font-size:.66rem;color:#FF9500;"
                        "margin-top:-6px'>※ 현금 잔고 미연동 — 보유 종목 평가액만 표시</div>",
                        unsafe_allow_html=True)

        tp2 = sum(p["pnl_usd"] for p in positions)
        _cost_basis_pf = sum(p["entry"] * p["shares"] for p in positions)
        _tp2_pct = (tp2 / _cost_basis_pf) if _cost_basis_pf else 0
        _tp2_c = "var(--up)" if tp2 >= 0 else "var(--dn)"
        st.markdown(f"""
        <div class='kpi' style='text-align:center;margin-top:4px'>
          <div class='kpi-l'>미실현 손익</div>
          <div class='kpi-v' style='color:{_tp2_c}'>{money(tp2)}
            <span style='font-size:.78rem;font-weight:700'>({_tp2_pct:+.2%})</span></div>
          <div style='font-size:.62rem;color:var(--t3);margin-top:2px'>
            투자 원금 {money(_cost_basis_pf)} 대비</div>
        </div>""", unsafe_allow_html=True)

    with cdet:
        if positions:
            from config import STOP_LOSS_PCT, TAKE_PROFIT_PCT
            for p in positions:
                c3 = "var(--up)" if p["pnl_pct"] >= 0 else "var(--dn)"
                sc_ = st.session_state.get("scan_results") or []
                sn2 = next((s["score"] for s in sc_ if s["ticker"]==p["ticker"]), p["score"])
                bc3 = "bg_" if sn2>=65 else "bn" if sn2>=35 else "bu"
                _denom = state["equity"] if state["connected"] and state["equity"] else state["t_cur"]
                pct = (p["current"]*p["shares"])/_denom*100 if _denom else 0
                # 손절/익절까지 남은 거리
                to_sl = -STOP_LOSS_PCT - p["pnl_pct"]   # 음수면 손절 근접
                to_tp = TAKE_PROFIT_PCT - p["pnl_pct"]  # 작을수록 익절 근접
                sl_price = p["entry"] * (1 - STOP_LOSS_PCT)
                tp_price = p["entry"] * (1 + TAKE_PROFIT_PCT)
                # 진행 바: 손절~익절 사이 현재 위치
                _range = STOP_LOSS_PCT + TAKE_PROFIT_PCT
                _pos_pct = min(max((p["pnl_pct"] + STOP_LOSS_PCT) / _range, 0), 1) * 100
                bar_c = "#F04452" if p["pnl_pct"]<0 else "#0FB873"
                sl_warn = " " if to_sl > -0.02 else ""  # 손절 2% 이내
                st.markdown(f"""
                <div class='card' style='padding:14px 16px'>
                  <div style='display:flex;justify-content:space-between;align-items:center'>
                    <div style='flex:1'>
                      <div style='font-weight:800;font-size:.94rem'>{p["ticker"]}{sl_warn}</div>
                      <div style='font-size:.72rem;color:var(--t3);margin-top:2px'>
                        {p["shares"]}주 · 평단 ${p["entry"]:.2f} · {p["held"]}일</div>
                      <div style='margin-top:6px;display:flex;gap:5px'>
                        <span class='{bc3}'>스코어 {sn2:.0f}</span>
                        <span class='bn'>{pct:.0f}%</span>
                      </div>
                      <!-- 손절~익절 진행바 -->
                      <div style='margin-top:8px;font-size:.66rem;color:var(--t3);
                        display:flex;justify-content:space-between'>
                        <span style='color:#2F80ED'>SL ${sl_price:.2f}</span>
                        <span style='color:#0FB873'>TP ${tp_price:.2f}</span>
                      </div>
                      <div style='height:4px;background:#1C1C23;border-radius:2px;
                        margin-top:2px;overflow:hidden'>
                        <div style='height:100%;width:{_pos_pct:.0f}%;
                          background:{bar_c};border-radius:2px;
                          transition:width .3s'></div>
                      </div>
                      <div style='font-size:.64rem;color:var(--t3);margin-top:2px;
                        display:flex;justify-content:space-between'>
                        <span>손절까지 {abs(to_sl):.1%}</span>
                        <span>익절까지 {to_tp:.1%}</span>
                      </div>
                    </div>
                    <div style='text-align:right;margin-left:12px'>
                      <div style='font-size:1.05rem;font-weight:800'>${p["current"]:.2f}</div>
                      <div style='color:{c3};font-size:.82rem;font-weight:700;margin-top:3px'>
                        {"▲" if p["pnl_pct"]>=0 else "▼"} {p["pnl_pct"]:+.2%}</div>
                      <div style='color:{c3};font-size:.72rem'>${p["pnl_usd"]:+,.0f}</div>
                      {_make_rt_badge(p["ticker"])}
                    </div>
                  </div>
                </div>""", unsafe_allow_html=True)

                # ── 카드 액션: 차트·상세 / 전량 매도 ──────────────────────────
                _tk_pf = p["ticker"]
                _pfb1, _pfb2, _pfb3 = st.columns([1, 1, 2])
                if _pfb1.button("차트·상세", key=f"pf_det_{_tk_pf}"):
                    _stock_detail_dialog(_tk_pf)
                _sell_ck = f"pf_sell_confirm_{_tk_pf}"
                if _pfb2.button("전량 매도", key=f"pf_sell_{_tk_pf}"):
                    st.session_state[_sell_ck] = True
                if st.session_state.get(_sell_ck):
                    _pfc1, _pfc2, _ = st.columns([1.4, 1, 2])
                    _is_p_pf = state.get("paper_mode", True)
                    if _pfc1.button(
                            f"✓ {'모의' if _is_p_pf else '실거래'} 매도 확정 "
                            f"({p['shares']:.0f}주 · ${p['current']*p['shares']:,.0f})",
                            key=f"pf_sell_ok_{_tk_pf}", type="primary"):
                        try:
                            _pm_pf = state["pm"]
                            _pos_pf = _pm_pf.positions.get(_tk_pf)
                            _fp_pf = p["current"]
                            _qty_pf = _pos_pf.shares if _pos_pf else p["shares"]
                            _pnl_pf = p["pnl_pct"]
                            if _is_p_pf:
                                _pm_pf.record_sell(_tk_pf, exit_price=_fp_pf,
                                                   reason="manual", shares=_qty_pf)
                                _paper.adjust(_qty_pf * _fp_pf)
                            else:
                                from broker import Broker as _Bpf, validate_fill as _vfpf
                                _res_pf = _Bpf(paper=False).place_sell(_tk_pf, int(_qty_pf))
                                _v_pf = _vfpf(_res_pf, est_price=_fp_pf,
                                              req_shares=int(_qty_pf))
                                if not _v_pf["ok"]:
                                    raise RuntimeError(_v_pf["warning"])
                                _fp_pf, _qty_pf = _v_pf["fill_price"], _v_pf["filled_qty"]
                                _pm_pf.record_sell(_tk_pf, exit_price=_fp_pf,
                                                   reason="manual", shares=_qty_pf)
                            log_order(_tk_pf, "sell", _qty_pf, _fp_pf,
                                      source=("paper" if _is_p_pf else "manual"),
                                      pnl_pct=_pnl_pf, reason="manual")
                            st.session_state[_sell_ck] = False
                            st.toast(f"매도 체결 · {_tk_pf} {_qty_pf:.0f}주 @ ${_fp_pf:,.2f} "
                                     f"({_pnl_pf:+.1%})")
                            st.rerun()
                        except Exception as _e_pf:
                            st.error(f"매도 실패: {_e_pf}")
                    if _pfc2.button("취소", key=f"pf_sell_no_{_tk_pf}"):
                        st.session_state[_sell_ck] = False
                        st.rerun()
        else:
            st.markdown("""
            <div class='card' style='text-align:center;padding:48px'>
              <div style='color:var(--t3);font-size:.84rem'>보유 종목이 없습니다</div>
            </div>""", unsafe_allow_html=True)

    # ══ 포트폴리오 수익 곡선 ════════════════════════════════════════════════════
    st.markdown("<br/>", unsafe_allow_html=True)
    _eqh1, _eqh2 = st.columns([1, 1])
    _eqh1.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:8px'>"
                   "수익 곡선</div>", unsafe_allow_html=True)
    with _eqh2:
        _eq_period = st.segmented_control(
            "기간", ["전체", "오늘", "7일", "30일", "90일"],
            default="전체", key="pf_eq_period", label_visibility="collapsed")

    def _build_trade_rows(trades_list: list[dict]) -> pd.DataFrame:
        """trades.json → 거래별 손익 행 (청산 시각 순)."""
        if not trades_list:
            return pd.DataFrame()
        rows = []
        for t in sorted(trades_list, key=lambda x: x.get("exit_date", "")):
            rows.append({"date": pd.Timestamp(t["exit_date"]),
                         "pnl": (t["exit_price"] - t["entry_price"]) * t.get("shares", 0),
                         "ticker": t["ticker"],
                         "reason": t.get("reason", "")})
        return pd.DataFrame(rows)

    eq_all = _build_trade_rows(trades)
    _unrealized = sum(p["pnl_usd"] for p in positions)
    _total_realized = float(eq_all["pnl"].sum()) if not eq_all.empty else 0.0

    # ── 기간 필터 ───────────────────────────────────────────────────────────
    _now_ts = pd.Timestamp.now()
    _cutoffs = {"오늘": _now_ts.normalize(), "7일": _now_ts - pd.Timedelta(days=7),
                "30일": _now_ts - pd.Timedelta(days=30),
                "90일": _now_ts - pd.Timedelta(days=90)}
    eq_df = eq_all
    if not eq_all.empty and _eq_period in _cutoffs:
        eq_df = eq_all[eq_all["date"] >= _cutoffs[_eq_period]].reset_index(drop=True)

    if not eq_df.empty:
        eq_df = eq_df.copy()
        eq_df["cumulative_pnl"] = eq_df["pnl"].cumsum()
        _cum = eq_df["cumulative_pnl"]
        _final = float(_cum.iloc[-1])          # 기간 내 실현손익
        _now_total = _total_realized + _unrealized  # 전체 실현 + 현재 미실현

        # X축: 청산일이 충분히 분산되면 일별 집계, 아니면 거래 순번
        _n_days = eq_df["date"].dt.normalize().nunique()
        _by_seq = _n_days < 5
        if _by_seq:
            _x = list(range(1, len(eq_df) + 1))
            _hover = [f"#{i} · {d:%m-%d %H:%M} · {tk}"
                      for i, (d, tk) in enumerate(
                          zip(eq_df["date"], eq_df["ticker"]), 1)]
            _y = _cum
            _pnl_pts = eq_df["pnl"].tolist()
        else:
            _daily = eq_df.groupby(eq_df["date"].dt.normalize()).agg(
                cumulative_pnl=("cumulative_pnl", "last"),
                pnl=("pnl", "sum"))
            _x = _daily.index
            _hover = [f"{d:%Y-%m-%d}" for d in _daily.index]
            _y = _daily["cumulative_pnl"]
            _pnl_pts = _daily["pnl"].tolist()

        fig_eq = go.Figure()
        _line_color = "#F04452" if _final >= 0 else "#2F80ED"
        r2,g2,b2 = (int(_line_color[1:3],16),
                    int(_line_color[3:5],16),
                    int(_line_color[5:7],16))
        # 손익분기선 (0)
        fig_eq.add_hline(y=0, line_dash="dot", line_color="#3A3A4A",
                         line_width=1, annotation_text="손익분기 (0)",
                         annotation_font=dict(size=9, color="#565E6B"))
        # 누적 실현손익 곡선
        fig_eq.add_trace(go.Scatter(
            x=_x, y=_y, mode="lines+markers",
            line=dict(color=_line_color, width=2.5),
            fill="tozeroy", fillcolor=f"rgba({r2},{g2},{b2},.06)",
            marker=dict(size=6, color=_line_color,
                        line=dict(color="#09090D", width=1.5)),
            name="누적 실현손익",
            text=_hover,
            hovertemplate="%{text}<br>누적 %{y:$,.0f}<extra></extra>",
        ))
        # 포인트 손익 주석 — 12개 이하일 때만 (겹침 방지)
        if len(_pnl_pts) <= 12:
            for _xi, _yi, _pn in zip(_x, _y, _pnl_pts):
                bc = "#F04452" if _pn >= 0 else "#2F80ED"
                fig_eq.add_annotation(
                    x=_xi, y=_yi,
                    text=f"<b>{'+' if _pn>=0 else ''}{_pn:,.0f}</b>",
                    font=dict(size=8, color=bc),
                    showarrow=False, yshift=14, xanchor="center")

        _xaxis = {**_XA, "tickfont": dict(size=10)}
        if _by_seq:
            _xaxis.update(title=dict(text="거래 순번", font=dict(
                size=10, color="#565E6B")), dtick=max(1, len(_x)//12))
        fig_eq.update_layout(**CL(height=220,
            xaxis=_xaxis,
            yaxis=dict(gridcolor="#1A1A25", showgrid=True, zeroline=False,
                       tickprefix="$", tickfont=dict(size=10)),
            legend=dict(orientation="h", y=1.1, x=0, font=dict(size=10))))
        st.plotly_chart(fig_eq, width="stretch",
                        config={"displayModeBar": False})

        # 요약 지표
        _running_max = _cum.cummax()
        _dd = (_cum - _running_max)
        _max_dd = float(_dd.min()) if len(_dd) else 0   # 절대 낙폭($)
        _tc = "var(--up)" if _final >= 0 else "var(--dn)"
        _equity_lbl = ("총 보유금액" if (_connected_pf or state.get("paper_mode"))
                       else "보유 평가액")
        _equity_val = (state["equity"]
                       if (_connected_pf or state.get("paper_mode"))
                       else state["t_cur"])
        ec0, ec1, ec2, ec3, ec4 = st.columns(5)
        kpi(ec0, _equity_lbl, money(_equity_val),
            f"현금 {money(state['cash'])} 포함"
            if (_connected_pf or state.get("paper_mode")) else "현금 미연동")
        kpi(ec1, f"실현손익 ({_eq_period})", money(_final),
            f"{len(eq_df)}건 거래", _tc)
        kpi(ec2, "누적 실현손익 (전체)", money(_total_realized),
            f"{len(trades)}건 거래",
            "var(--up)" if _total_realized >= 0 else "var(--dn)")
        kpi(ec3, "현재 미실현", money(_unrealized),
            f"{len(positions)}개 보유",
            "var(--up)" if _unrealized >= 0 else "var(--dn)")
        kpi(ec4, "최대 낙폭", money(_max_dd),
            "고점 대비", "var(--dn)" if _max_dd < 0 else "var(--t1)")
    elif not eq_all.empty:
        st.markdown(f"<div style='color:var(--t3);font-size:.8rem;padding:8px'>"
                    f"선택한 기간({_eq_period}) 내 완료된 거래가 없습니다 — "
                    f"전체 누적 실현손익 {money(_total_realized)} ({len(trades)}건)</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<div style='color:var(--t3);font-size:.8rem;padding:8px'>"
                    "완료된 거래가 있으면 수익 곡선이 표시됩니다.</div>",
                    unsafe_allow_html=True)

    # ── 최근 주문 내역 (매수·매도 전체 — 체결 즉시 기록됨) ───────────────────
    st.markdown("<br/>", unsafe_allow_html=True)
    st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:8px'>"
                "최근 주문 내역</div>", unsafe_allow_html=True)
    _po = load_orders()
    # 현재 모드(모의/실거래) 주문만: paper=모의, auto/manual=공통 — 출처로 구분 표시
    _po = sorted(_po, key=lambda o: o.get("ts", ""), reverse=True)[:30]
    if not _po:
        st.markdown("<div style='font-size:.78rem;color:var(--t3);padding:8px'>"
                    "아직 주문 내역이 없습니다 — 자동매매가 매수하거나 직접 주문하면 여기 즉시 기록됩니다.</div>",
                    unsafe_allow_html=True)
    else:
        _src_kr = {"auto": "자동", "paper": "모의", "manual": "수동"}
        _rows_html = ""
        for o in _po:
            _side_kr = "매수" if o["side"] == "buy" else "매도"
            _sc = "#0FB873" if o["side"] == "buy" else "#F0454F"
            _ts = o.get("ts", "")[5:16].replace("T", " ")
            _amt = o.get("shares", 0) * o.get("price", 0)
            _rows_html += (
                "<div style='display:grid;grid-template-columns:80px 60px 1fr 110px 110px 60px;"
                "gap:0;padding:7px 8px;font-size:.76rem;border-bottom:1px solid var(--line);"
                "align-items:center'>"
                f"<span style='color:var(--t3);font-size:.68rem'>{_ts}</span>"
                f"<span style='font-weight:800;color:{_sc}'>{_side_kr}</span>"
                f"<span style='font-weight:700'>{o['ticker']}</span>"
                f"<span style='text-align:right'>{o.get('shares',0):.0f}주 @ ${o.get('price',0):,.2f}</span>"
                f"<span style='text-align:right;font-weight:700'>${_amt:,.0f}</span>"
                f"<span style='text-align:right;color:var(--t3);font-size:.66rem'>"
                f"{_src_kr.get(o.get('source','manual'),'')}</span></div>")
        st.markdown(
            "<div style='background:var(--bg2);border:1px solid var(--line);border-radius:10px;"
            "overflow:hidden'>"
            "<div style='display:grid;grid-template-columns:80px 60px 1fr 110px 110px 60px;"
            "padding:6px 8px;font-size:.62rem;color:var(--t3);font-weight:700;"
            "border-bottom:1px solid var(--line2);background:var(--bg3)'>"
            "<span>시각</span><span>구분</span><span>종목</span>"
            "<span style='text-align:right'>수량·단가</span>"
            "<span style='text-align:right'>금액</span>"
            "<span style='text-align:right'>출처</span></div>"
            + _rows_html + "</div>", unsafe_allow_html=True)

    # ── 종목별 거래 기록 차트 (매수▲/매도▼ 마커) ──
    st.markdown("<br/>", unsafe_allow_html=True)
    st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:8px'>"
                "거래 기록 차트</div>", unsafe_allow_html=True)
    all_orders = load_orders()
    traded_tickers = sorted({o["ticker"] for o in all_orders})
    if traded_tickers:
        tcc1, tcc2 = st.columns([1,3])
        sel_tk = tcc1.selectbox("종목", traded_tickers, key="trade_chart_tk")
        period_tk = tcc2.radio("기간", ["1개월","3개월","6개월","1년"],
                               horizontal=True, key="trade_chart_period")
        _pmap = {"1개월":"1mo","3개월":"3mo","6개월":"6mo","1년":"1y"}
        hist = fetch_history(sel_tk, _pmap[period_tk])
        if not hist.empty:
            close = hist["Close"].squeeze()
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=close.index, y=close, mode="lines",
                line=dict(color="#8B95A1", width=1.6), name=sel_tk,
                hovertemplate="%{x|%Y-%m-%d}  $%{y:.2f}<extra></extra>"))
            # 주문 마커
            tk_orders = [o for o in all_orders if o["ticker"]==sel_tk]
            buys_x, buys_y, sells_x, sells_y = [], [], [], []
            buys_txt, sells_txt = [], []
            for o in tk_orders:
                try:
                    d = pd.Timestamp(o["date"])
                    if d < close.index[0]: continue
                    px = o["price"]
                    if o["side"]=="buy":
                        buys_x.append(d); buys_y.append(px)
                        buys_txt.append(f"매수 ${px:.2f}<br>{int(o.get('shares',0))}주")
                    else:
                        sells_x.append(d); sells_y.append(px)
                        sells_txt.append(f"매도 ${px:.2f}<br>{int(o.get('shares',0))}주")
                except: pass
            if buys_x:
                fig.add_trace(go.Scatter(x=buys_x, y=buys_y, mode="markers",
                    marker=dict(symbol="triangle-up", size=13, color="#F04452",
                                line=dict(color="#fff",width=1)),
                    name="매수", text=buys_txt,
                    hovertemplate="%{text}<extra></extra>"))
            if sells_x:
                fig.add_trace(go.Scatter(x=sells_x, y=sells_y, mode="markers",
                    marker=dict(symbol="triangle-down", size=13, color="#2F80ED",
                                line=dict(color="#fff",width=1)),
                    name="매도", text=sells_txt,
                    hovertemplate="%{text}<extra></extra>"))

            # ── 실현손익 라벨: 매수→매도 페어 매칭 ──────────────────────
            # trades.json 에서 해당 종목 완료 거래 매칭
            tk_trades = [t for t in trades if t.get("ticker")==sel_tk]
            for tr in tk_trades:
                try:
                    ex_d = pd.Timestamp(tr["exit_date"])
                    if ex_d < close.index[0]: continue
                    en_d = pd.Timestamp(tr["entry_date"])
                    ex_p = tr["exit_price"]; en_p = tr["entry_price"]
                    pp = (ex_p - en_p) / en_p if en_p else 0
                    pnl_usd = (ex_p - en_p) * tr.get("shares", 0)
                    lc2 = "#F04452" if pp >= 0 else "#2F80ED"
                    sign = "+" if pp >= 0 else ""
                    # 라인: 진입→청산
                    if en_d >= close.index[0]:
                        fig.add_shape(type="line",
                            x0=en_d, y0=en_p, x1=ex_d, y1=ex_p,
                            line=dict(color=lc2, width=1.2, dash="dot"),
                            opacity=0.5)
                    # 손익 텍스트 라벨
                    fig.add_annotation(
                        x=ex_d, y=ex_p,
                        text=f"<b>{sign}{pp:.1%}</b><br>${pnl_usd:+,.0f}",
                        font=dict(size=9, color=lc2),
                        bgcolor="rgba(9,9,13,0.75)",
                        bordercolor=lc2, borderwidth=1,
                        showarrow=True, arrowcolor=lc2, arrowsize=0.6,
                        ax=0, ay=-30 if pp>=0 else 30,
                        xanchor="center"
                    )
                except: pass
            ymn,ymx = float(close.min()), float(close.max())
            pad = (ymx-ymn)*0.08 or ymx*0.01
            fig.update_layout(**CL(height=300,
                yaxis=dict(gridcolor="#1A1A25",showgrid=True,zeroline=False,
                           tickfont=dict(size=10),tickprefix="$",range=[ymn-pad,ymx+pad]),
                legend=dict(orientation="h",y=1.12,x=0,font=dict(size=10))))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar":False})
            # 요약
            nb = len(buys_x); ns_ = len(sells_x)
            src = {"auto":0,"manual":0}
            for o in tk_orders: src[o.get("source","auto")] = src.get(o.get("source","auto"),0)+1
            st.markdown(f"<div style='font-size:.74rem;color:var(--t3);margin-top:-4px'>"
                        f"{sel_tk}: 매수 {nb}회 · 매도 {ns_}회 "
                        f"(자동 {src['auto']} · 수동 {src['manual']})</div>",
                        unsafe_allow_html=True)
    else:
        st.markdown("<div style='color:var(--t3);font-size:.8rem;padding:6px'>"
                    "아직 체결된 주문이 없습니다. 매매하면 여기에 차트로 표시됩니다.</div>",
                    unsafe_allow_html=True)

    st.markdown("<br/>", unsafe_allow_html=True)
    _trade_head_cols = st.columns([3, 1, 1])
    _trade_head_cols[0].markdown(
        "<div style='font-weight:700;font-size:.86rem;padding-top:6px'>"
        "거래 내역</div>", unsafe_allow_html=True)
    if trades:
        # CSV / Excel 내보내기
        _export_df = pd.DataFrame([{
            "종목": t["ticker"],
            "진입일": t.get("entry_date",""),
            "청산일": t.get("exit_date",""),
            "진입가": t["entry_price"],
            "청산가": t["exit_price"],
            "수량": t.get("shares", 0),
            "수익률(%)": round((t["exit_price"]-t["entry_price"])/t["entry_price"]*100, 2)
                         if t["entry_price"] else 0,
            "손익($)": round((t["exit_price"]-t["entry_price"])*t.get("shares",0), 2),
            "사유": REASON_KR.get(t.get("reason",""), t.get("reason","")),
        } for t in trades])
        _csv = _export_df.to_csv(index=False, encoding="utf-8-sig")
        _trade_head_cols[1].download_button(
            "CSV", data=_csv,
            file_name=f"trades_{date.today()}.csv",
            mime="text/csv", key="dl_csv")
        try:
            import io
            _xls_buf = io.BytesIO()
            _export_df.to_excel(_xls_buf, index=False, engine="openpyxl")
            _trade_head_cols[2].download_button(
                "Excel", data=_xls_buf.getvalue(),
                file_name=f"trades_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_xlsx")
        except ImportError:
            pass
    if trades:
        # ── 실현손익 통계 요약 ──
        pnls = [(t["exit_price"]-t["entry_price"])/t["entry_price"]
                for t in trades if t["entry_price"]]
        pnl_usds = [(t["exit_price"]-t["entry_price"])*t.get("shares",0)
                    for t in trades if t["entry_price"]]
        wins = [p for p in pnls if p > 0]
        win_rate = len(wins)/len(pnls)*100 if pnls else 0
        total_pnl = sum(pnl_usds)
        avg_win = (sum(p for p in pnl_usds if p>0)/len(wins)) if wins else 0
        loss_us = [p for p in pnl_usds if p<0]
        avg_loss= (sum(loss_us)/len(loss_us)) if loss_us else 0
        pf = abs(avg_win/avg_loss) if avg_loss else float("inf")
        tp_c = "var(--up)" if total_pnl>=0 else "var(--dn)"
        wr_c = "var(--up)" if win_rate>=50 else "var(--dn)"
        st.markdown(f"""
        <div style='display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap'>
          <div class='kpi' style='flex:1;min-width:100px'>
            <div class='kpi-l'>실현손익</div>
            <div class='kpi-v' style='color:{tp_c}'>${total_pnl:+,.0f}</div>
          </div>
          <div class='kpi' style='flex:1;min-width:100px'>
            <div class='kpi-l'>승률</div>
            <div class='kpi-v' style='color:{wr_c}'>{win_rate:.0f}%</div>
          </div>
          <div class='kpi' style='flex:1;min-width:100px'>
            <div class='kpi-l'>손익비</div>
            <div class='kpi-v'>{pf:.2f}x</div>
          </div>
          <div class='kpi' style='flex:1;min-width:100px'>
            <div class='kpi-l'>총 거래</div>
            <div class='kpi-v'>{len(trades)}건</div>
          </div>
        </div>""", unsafe_allow_html=True)

        for t in reversed(trades[-20:]):
            pp = (t["exit_price"]-t["entry_price"])/t["entry_price"] if t["entry_price"] else 0
            pnl_d = (t["exit_price"]-t["entry_price"])*t.get("shares",0)
            lbl = REASON_KR.get(t.get("reason",""), t.get("reason",""))
            bc4 = "bg_" if pp>0 else "bu" if pp<0 else "bn"
            pc4 = "var(--up)" if pp >= 0 else "var(--dn)"
            st.markdown(f"""
            <div class='card-xs' style='display:flex;justify-content:space-between;
              align-items:center'>
              <div>
                <span style='font-weight:800'>{t["ticker"]}</span>
                &nbsp;<span class='{bc4}'>{lbl}</span>
                <div style='font-size:.71rem;color:var(--t3);margin-top:3px'>
                  {t.get("entry_date","")} → {t.get("exit_date","")} ·
                  ${t["entry_price"]:.2f} → ${t["exit_price"]:.2f}
                  · {int(t.get("shares",0))}주</div>
              </div>
              <div style='text-align:right'>
                <div style='font-size:.9rem;font-weight:800;color:{pc4}'>{pp:+.1%}</div>
                <div style='font-size:.74rem;color:{pc4}'>${pnl_d:+,.0f}</div>
              </div>
            </div>""", unsafe_allow_html=True)
    else:
        st.markdown("<div style='color:var(--t3);font-size:.8rem;padding:8px'>"
                    "거래 내역 없음</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
elif cur == "설정":
    import notifier, watchlist as wl3
    st.markdown("<div class='stitle'>설정</div>", unsafe_allow_html=True)
    t1,t2,t3,t4 = st.tabs(["API 연결","알림","종목 관리","매매 규칙"])

    with t1:
        st.markdown("<div style='font-weight:700;margin-bottom:12px'>Alpaca 브로커</div>",
                    unsafe_allow_html=True)
        import config as cfg
        c1,c2 = st.columns(2)
        ak = c1.text_input("API Key",
            value=cfg.ALPACA_API_KEY if "your_" not in cfg.ALPACA_API_KEY else "",
            placeholder="PKXXXXX…", type="password")
        sk = c2.text_input("Secret Key",
            value=cfg.ALPACA_SECRET_KEY if "your_" not in cfg.ALPACA_SECRET_KEY else "",
            placeholder="XXXXXXX…", type="password")
        mode = st.radio("모드", ["페이퍼 (모의투자)","실거래"], horizontal=True)
        if st.button("연결 테스트"):
            with st.spinner():
                try:
                    if ak: cfg.ALPACA_API_KEY = ak
                    if sk: cfg.ALPACA_SECRET_KEY = sk
                    from broker import Broker
                    a = Broker(paper="페이퍼" in mode).get_account()
                    st.session_state["alpaca_acct"] = a
                    st.markdown(f"<div class='ok'>연결 성공 · 자산 ${a['equity']:,.0f}"
                                f" · 현금 ${a['cash']:,.0f}</div>", unsafe_allow_html=True)
                except Exception as e:
                    st.markdown(f"<div class='fail'>연결 실패: {e}</div>",
                                unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("<div style='font-weight:700;margin-bottom:12px'>Finnhub</div>",
                    unsafe_allow_html=True)
        fk = st.text_input("API Key",
            value=cfg.FINNHUB_API_KEY if "your_" not in cfg.FINNHUB_API_KEY else "",
            placeholder="cXXXXXX…", type="password")
        if st.button("Finnhub 테스트"):
            with st.spinner():
                try:
                    import finnhub
                    if fk: cfg.FINNHUB_API_KEY = fk
                    q = finnhub.Client(api_key=cfg.FINNHUB_API_KEY).quote("AAPL")
                    msg = f"연결 성공 · AAPL ${q.get('c',0):.2f}" if q.get("c") else "응답 없음"
                    cls = "ok" if q.get("c") else "fail"
                    if q.get("c") and fk:
                        # .env 에 영속 — 데몬(별도 프로세스)도 이 키로 WebSocket
                        # 실시간 스트림을 쓸 수 있게 한다.
                        try:
                            _envp = Path(__file__).parent / ".env"
                            _lines = (_envp.read_text().splitlines()
                                      if _envp.exists() else [])
                            _lines = [l for l in _lines
                                      if not l.startswith("FINNHUB_API_KEY=")]
                            _lines.append(f"FINNHUB_API_KEY={fk.strip()}")
                            _envp.write_text("\n".join(_lines) + "\n")
                            os.environ["FINNHUB_API_KEY"] = fk.strip()
                            msg += " · 키 저장됨 (데몬 실시간 스트림에도 적용)"
                        except Exception:
                            pass
                    st.markdown(f"<div class='{cls}'>{msg}</div>", unsafe_allow_html=True)
                except Exception as e:
                    st.markdown(f"<div class='fail'>오류: {e}</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with t2:
        cfg2 = st.session_state["notify_cfg"]

        # 데스크톱 알림 (현재 OS 자동: macOS 알림센터 / Windows 풍선 / Linux notify-send)
        import sys as _sys_os
        _os_lbl = ("macOS 알림" if _sys_os.platform == "darwin"
                   else "Windows 알림" if _sys_os.platform.startswith("win")
                   else "데스크톱 알림")
        c1, c2 = st.columns([1, 4])
        cfg2.setdefault("macos", {"enabled": True})
        cfg2["macos"]["enabled"] = c1.toggle(_os_lbl, value=cfg2["macos"].get("enabled", True))
        c2.markdown("<div style='font-size:.74rem;color:var(--t3);padding-top:8px'>"
                    f"매수·매도·손절 임박 시 {_os_lbl}으로 즉시 표시</div>",
                    unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        # 텔레그램
        cfg2.setdefault("telegram", {"enabled": False, "bot_token": "", "chat_id": ""})
        # 자동 찾기로 채워질 Chat ID 대기값 — 위젯 생성 '전에' 키에 반영
        if "tg_chat_pending" in st.session_state:
            st.session_state["tg_chat_id"] = st.session_state.pop("tg_chat_pending")
        st.session_state.setdefault("tg_bot_token", cfg2["telegram"].get("bot_token", ""))
        st.session_state.setdefault("tg_chat_id", cfg2["telegram"].get("chat_id", ""))
        ct1, ct2 = st.columns([1, 4])
        cfg2["telegram"]["enabled"] = ct1.toggle("텔레그램", value=cfg2["telegram"].get("enabled", False))
        with ct2:
            t_b, t_c = st.columns(2)
            t_b.text_input("Bot Token", key="tg_bot_token",
                           placeholder="123456:ABC-…", type="password")
            t_c.text_input("Chat ID", key="tg_chat_id",
                           placeholder="예: 123456789 (아래 자동 찾기 권장)")
        cfg2["telegram"]["bot_token"] = st.session_state["tg_bot_token"]
        cfg2["telegram"]["chat_id"] = st.session_state["tg_chat_id"]

        # ── Chat ID 자동 찾기 (토큰만 넣고 클릭) ──
        if st.button("내 Chat ID 자동 찾기", key="tg_find_btn"):
            with st.spinner("텔레그램에서 최근 대화 확인 중…"):
                _chats, _err = notifier.telegram_get_chat_ids(
                    st.session_state.get("tg_bot_token", ""))
            st.session_state["tg_found"] = {"chats": _chats, "err": _err}
        _tgf = st.session_state.get("tg_found")
        if _tgf:
            if _tgf["err"]:
                st.markdown(f"<div class='fail'>{_tgf['err']}</div>", unsafe_allow_html=True)
            for _c in _tgf["chats"]:
                if st.button(f"✓ 이 대화로 저장: {_c['name']} ({_c['chat_id']})",
                             key=f"tgpick_{_c['chat_id']}"):
                    cfg2["telegram"]["chat_id"] = _c["chat_id"]
                    cfg2["telegram"]["enabled"] = True
                    notifier.save_config(cfg2)
                    st.session_state["notify_cfg"] = cfg2
                    st.session_state["tg_chat_pending"] = _c["chat_id"]  # 다음 런에 칸 채움
                    st.session_state.pop("tg_found", None)
                    st.rerun()

        with st.expander("텔레그램 설정 방법 (1분)"):
            st.markdown("""
1. 텔레그램에서 [@BotFather](https://t.me/BotFather) → `/newbot` → **Bot Token** 발급 → 위 칸에 붙여넣기
2. 방금 만든 **내 봇과 1:1 대화**를 열고 아무 메시지나 1개 전송
3. **"내 Chat ID 자동 찾기"** 클릭 → 뜬 내 대화 버튼을 누르면 Chat ID 자동 저장
4. **테스트 전송** 으로 확인 (휴대폰에 알림이 오면 성공)
            """)
        st.markdown("</div>", unsafe_allow_html=True)

        # Slack / 카카오
        for title, key, field, ph in [
            ("Slack", "slack", "webhook_url", "https://hooks.slack.com/…"),
            ("카카오톡", "kakao", "access_token", "카카오 REST API 토큰"),
        ]:
            c1, c2 = st.columns([1, 4])
            cfg2[key]["enabled"] = c1.toggle(title, value=cfg2[key]["enabled"])
            cfg2[key][field] = c2.text_input(title, value=cfg2[key][field],
                placeholder=ph, type="password", label_visibility="collapsed")
            st.markdown("</div>", unsafe_allow_html=True)

        # 이메일
        c1, _ = st.columns([1, 4])
        cfg2["email"]["enabled"] = c1.toggle("이메일", value=cfg2["email"]["enabled"])
        e1, e2 = st.columns(2)
        cfg2["email"]["user"] = e1.text_input("발신", value=cfg2["email"]["user"])
        cfg2["email"]["password"] = e2.text_input("앱 비밀번호",
            value=cfg2["email"]["password"], type="password")
        cfg2["email"]["to"] = st.text_input("수신", value=cfg2["email"]["to"])
        st.markdown("</div>", unsafe_allow_html=True)

        b1_, b2_ = st.columns(2)
        if b1_.button("저장"):
            notifier.save_config(cfg2)
            st.session_state["notify_cfg"] = cfg2
            st.markdown("<div class='ok'>저장됨</div>", unsafe_allow_html=True)
        if b2_.button("테스트 전송"):
            with st.spinner("전송 중…"):
                results_ = notifier.test_all(cfg2) or {}
            for ch, ok in results_.items():
                if ch == "telegram":
                    continue   # 텔레그램은 사유까지 따로 표시(아래)
                st.markdown(f"<div class='{'ok' if ok else 'fail'}'>"
                            f"{'✓' if ok else '✕'} {ch}: "
                            f"{'성공' if ok else '실패'}</div>",
                            unsafe_allow_html=True)
            # 텔레그램은 실패 원인(API 사유)까지 표시 → 무엇을 고칠지 바로 알 수 있게
            if cfg2.get("telegram", {}).get("enabled"):
                _ok_t, _why_t = notifier.telegram_test(
                    cfg2["telegram"].get("bot_token", ""),
                    cfg2["telegram"].get("chat_id", ""))
                st.markdown(
                    f"<div class='{'ok' if _ok_t else 'fail'}'>"
                    f"{'✓' if _ok_t else '✕'} telegram: {_why_t}</div>",
                    unsafe_allow_html=True)
                if not _ok_t:
                    st.markdown(
                        "<div style='font-size:.72rem;color:var(--t3);margin-top:4px;line-height:1.6'>"
                        "자주 나는 원인 — <b>chat not found</b>: 봇과 먼저 1:1 대화를 시작(메시지 1개)한 뒤 "
                        "<code>getUpdates</code>로 나온 <b>본인 Chat ID(양수)</b>를 넣으세요. "
                        "채널/그룹(-100…)이면 봇을 그 채널에 <b>관리자로 추가</b>해야 합니다. · "
                        "<b>Unauthorized</b>: Bot Token이 틀렸습니다(@BotFather에서 재확인).</div>",
                        unsafe_allow_html=True)

    with t3:
        stks = wl3.load()
        a1,a2 = st.columns([3,1])
        new_t = a1.text_input("", "", placeholder="티커 추가 (예: PLTR, COIN…)",
                              label_visibility="collapsed").upper().strip()
        if a2.button("추가", type="primary") and new_t:
            with st.spinner(): res = wl3.add(new_t)
            if res["ok"]:
                st.markdown(f"<div class='ok'>추가됨: {res['ticker']}</div>",
                            unsafe_allow_html=True); stks = wl3.load()
            else:
                st.markdown(f"<div class='fail'>{res['error']}</div>",
                            unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown(f"<div class='card' style='margin-top:6px'>"
                    f"<div style='font-weight:700;margin-bottom:10px'>"
                    f"워치리스트 ({len(stks)}개)</div>", unsafe_allow_html=True)
        grp2: dict[str,list] = {}
        for t in stks: grp2.setdefault(wl3._ticker_to_sector(t),[]).append(t)
        sl2 = {"XLK":"테크","XLF":"금융","XLE":"에너지","XLV":"헬스케어",
               "XLY":"소비재","XLI":"산업재","XLP":"필수소비",
               "XLB":"소재","XLU":"유틸리티","XLRE":"리츠"}
        for sec2, tg2 in sorted(grp2.items()):
            st.markdown(f"<div style='font-size:.7rem;color:var(--t3);"
                        f"margin:10px 0 5px'>{sl2.get(sec2,sec2)}</div>",
                        unsafe_allow_html=True)
            cols_ = st.columns(min(len(tg2),6))
            for i,t in enumerate(tg2):
                if cols_[i%6].button(f"X  {t}", key=f"rm_{t}"):
                    wl3.remove(t); st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        if st.button("기본 종목으로 초기화"):
            wl3.save(wl3.DEFAULT_STOCKS); st.rerun()

        # ── 가격 알림 관리 ──────────────────────────────────────────────
        st.markdown("<br/>", unsafe_allow_html=True)
        st.markdown("<div style='font-weight:700;font-size:.86rem;margin-bottom:8px'>"
                    "가격 알림</div>", unsafe_allow_html=True)
        import price_alerts as _pal3
        pa1, pa2, pa3, pa4 = st.columns([2, 1.2, 1.2, 1])
        _pal_tk   = pa1.text_input("", placeholder="티커 (예: AAPL)",
                                   key="pal_tk", label_visibility="collapsed").upper().strip()
        _pal_cond = pa2.selectbox("", ["이상 (above)", "이하 (below)"],
                                  key="pal_cond", label_visibility="collapsed")
        _pal_tgt  = pa3.number_input("", value=100.0, step=1.0, format="%.2f",
                                     key="pal_tgt", label_visibility="collapsed")
        _pal_note = pa4.text_input("", placeholder="메모",
                                   key="pal_note", label_visibility="collapsed")
        if st.button("알림 추가", key="pal_add") and _pal_tk:
            _cond_val = "above" if "above" in _pal_cond else "below"
            _pal3.add(_pal_tk, _cond_val, _pal_tgt, _pal_note)
            st.markdown(f"<div class='ok'>추가됨: {_pal_tk} "
                        f"{'≥' if _cond_val=='above' else '≤'} ${_pal_tgt:.2f}</div>",
                        unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        _all_alerts = _pal3.load()
        if _all_alerts:
            for _al in _all_alerts:
                _td = _al.get("triggered")
                _al_c = "var(--t3)" if _td else "var(--t1)"
                _cond_s = "≥" if _al["condition"]=="above" else "≤"
                _status = (f"✓ 발동 @ ${_al.get('triggered_price',0):.2f}"
                           if _td else "대기 중")
                _sc = "var(--green)" if _td else "var(--t3)"
                st.markdown(f"""
                <div class='card-xs' style='display:flex;justify-content:space-between;
                  align-items:center;gap:8px'>
                  <div>
                    <span style='font-weight:800;color:{_al_c}'>{_al["ticker"]}</span>
                    <span style='color:var(--t3);font-size:.8rem'>
                      &nbsp;{_cond_s} ${_al["target"]:.2f}</span>
                    {f"<span style='font-size:.72rem;color:var(--t3)'>&nbsp;{_al['note']}</span>" if _al.get("note") else ""}
                  </div>
                  <span style='font-size:.74rem;color:{_sc}'>{_status}</span>
                </div>""", unsafe_allow_html=True)
                _ra1, _ra2 = st.columns([1,1])
                if _td and _ra1.button("재활성화", key=f"pal_rst_{_al['id']}"):
                    _pal3.reset_triggered(_al["id"]); st.rerun()
                if _ra2.button("삭제", key=f"pal_del_{_al['id']}"):
                    _pal3.remove(_al["id"]); st.rerun()

    with t4:
        import config as cfg
        _RULES_FILE = Path(__file__).parent / "rules_config.json"

        def _save_rules(d):
            # 기존 파일과 병합 (signal_weights 등 다른 키 보존)
            existing = {}
            if _RULES_FILE.exists():
                try: existing = json.loads(_RULES_FILE.read_text())
                except Exception: pass
            existing.update(d)
            _RULES_FILE.write_text(json.dumps(existing, indent=2))

        c1,c2 = st.columns(2)
        nc = c1.number_input("총 자본 (USD)", value=cfg.CAPITAL_TOTAL, step=1000)
        nm = c2.number_input("최대 포지션", value=cfg.MAX_POSITIONS, step=1)
        c3,c4 = st.columns(2)
        np3 = c3.slider("포지션 최대 %",10,50,int(cfg.MAX_POSITION_PCT*100),format="%d%%")
        nm2 = c4.slider("진입 최소 스코어",40,90,cfg.MIN_SCORE_TO_BUY)
        c5,c6 = st.columns(2)
        ns  = c5.slider("손절",2,20,int(cfg.STOP_LOSS_PCT*100),format="-%d%%")
        nt  = c6.slider("익절",5,50,int(cfg.TAKE_PROFIT_PCT*100),format="+%d%%")
        if st.button("저장", type="primary"):
            cfg.CAPITAL_TOTAL    = nc;      cfg.MAX_POSITIONS    = int(nm)
            cfg.MAX_POSITION_PCT = np3/100; cfg.MIN_SCORE_TO_BUY = nm2
            cfg.STOP_LOSS_PCT    = ns/100;  cfg.TAKE_PROFIT_PCT  = nt/100
            _save_rules({
                "capital_total": nc, "max_positions": int(nm),
                "max_position_pct": np3/100, "min_score_to_buy": nm2,
                "stop_loss_pct": ns/100, "take_profit_pct": nt/100,
            })
            st.markdown("<div class='ok'>✓ 저장됨 — 재시작 후에도 유지됩니다</div>",
                        unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
