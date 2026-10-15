"""
NiceGUI 트레이딩 앱 — 탈-Streamlit 이전본 (7페이지 전체).

기존 백엔드(portfolio / paper_account / realtime_feed / core.* / strategy_catalog /
universe)를 한 줄도 건드리지 않고 view 레이어만 NiceGUI로 재구성.

안전선(메모리의 과잉매매/공유장부 사고 기준 [[trade-app-process-architecture]]):
  - 기본 읽기 전용. run_cycle / execute_* / start_daemon / save_config 등
    '공유 상태를 바꾸는 액션'은 전부 GATED (확인 다이얼로그만, 실제 반영 X).
    → 사용자의 실제 가동 중 앱과 config·장부를 공유하므로 충돌 방지.
  - realtime_feed.subscribe()는 기존 app.py와 동일한 읽기 전용 시세 폴링.
  - 보유가 비면 화면 표시용 샘플을 메모리에만 올림(디스크 저장 X).

실행:  ngvenv/bin/python -m ui_ng.dashboard   (프로젝트 루트에서)
"""
from __future__ import annotations
import os, json, time, threading
from datetime import date, datetime
from pathlib import Path

from nicegui import ui, run

# ── 기존 백엔드 (그대로 재사용, 전부 읽기) ───────────────────────────────────
import paper_account as pa
import realtime_feed as rtf
import strategy_catalog as scat
import universe as uni
from portfolio import PortfolioManager, _sector_of
try:
    from core.control import load_config, daemon_alive, daemon_pid
except Exception:
    def load_config(): return {}
    def daemon_alive(): return False
    def daemon_pid(): return None
try:
    from core import guard as _guard
except Exception:
    _guard = None

ROOT = Path(__file__).resolve().parent.parent
PAPER = True
DEMO = os.environ.get("POC_DEMO", "1") == "1"

# ── 상태 격리: 실가동 앱과 완전히 분리된 PoC 전용 state 디렉터리 ──────────────
# 모든 상태 파일(config·pid·현금·장부·주문·lock)을 ui_ng/_poc_state/ 로 리다이렉트.
# 백엔드 함수들은 내부에서 import 한 '같은' 모듈 전역을 쓰므로(모듈 싱글톤),
# 여기서 모듈 상수만 바꾸면 인프로세스 쓰기 전부가 이 디렉터리로 격리된다.
import portfolio as _pf_mod
import core.control as _ctl_mod
import core.execution as _ex_mod
import safe_store as _ss_mod

POC_STATE = ROOT / "ui_ng" / "_poc_state"
POC_STATE.mkdir(parents=True, exist_ok=True)
_ctl_mod.CONFIG_FILE = POC_STATE / "autotrader_config.json"
_ctl_mod.PID_FILE = POC_STATE / "autotrader.pid"
pa.PAPER_FILE = POC_STATE / "paper_account.json"
_pf_mod.STATE_FILE = POC_STATE / "state.json"
_pf_mod.PAPER_STATE_FILE = POC_STATE / "state_paper.json"
_ex_mod.ORDERS_FILE = POC_STATE / "orders_log.json"
_ss_mod._TRADE_LOCK_FILE = POC_STATE / ".trade.lock"

# 최초 시드 — config(모의·복합·자동매매 off) + 모의현금 $10,000
if not _ctl_mod.CONFIG_FILE.exists():
    _seed = dict(_ctl_mod.DEFAULT_CONFIG)
    _seed["enabled"] = False
    _seed["paper"] = True
    _ctl_mod.CONFIG_FILE.write_text(json.dumps(_seed, ensure_ascii=False))
if not pa.PAPER_FILE.exists():
    pa.PAPER_FILE.write_text(json.dumps({"cash": 10000.0, "seed": 10000.0}))

PAGES = ["대시보드", "전략 선택", "자동 트레이딩", "포트폴리오",
         "주식 열람", "직접 주문", "설정"]
# 자동 트레이딩/폼 페이지는 LIVE에서 제외 — 2초 재렌더가 버튼 클릭을 삼키지 않도록.
# (헤더의 자동매매 점등은 meta_box 가 항상 갱신하므로 상태는 계속 보인다.)
LIVE_PAGES = {"대시보드", "포트폴리오"}

INDEX_SYMS = [
    ("SPY", "S&P 500"), ("QQQ", "나스닥100"), ("^DJI", "다우"),
    ("^VIX", "VIX"), ("^TNX", "美10Y"), ("^KS11", "코스피"),
    ("KRW=X", "원/달러"),
]
SAMPLE = [
    ("AAPL", 12, 212.40, "2026-09-18", 84),
    ("NVDA", 8, 164.10, "2026-09-29", 91),
    ("MSFT", 6, 441.80, "2026-08-25", 78),
    ("GOOGL", 15, 168.20, "2026-10-02", 73),
    ("TSLA", 5, 402.50, "2026-10-08", 66),
]
SECTOR_KO = {
    "XLK": "기술", "XLF": "금융", "XLV": "헬스케어", "XLY": "자유소비재",
    "XLP": "필수소비재", "XLE": "에너지", "XLI": "산업재", "XLB": "소재",
    "XLRE": "부동산", "XLU": "유틸리티", "XLC": "커뮤니케이션", "": "기타",
}

# ── 테마 ─────────────────────────────────────────────────────────────────────
STYLE = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
:root{
  --bg0:#0B0B0F; --bg1:#111116; --bg2:#16161C; --bg3:#1C1C23; --bg4:#24242D;
  --line:#20202A; --line2:#2A2A35;
  --t1:#F2F3F5; --t2:#9099A6; --t3:#565E6B;
  --up:#F0454F; --dn:#3B82F6; --blue:#3B82F6; --green:#0FB873; --gold:#C9A227;
}
html,body{ background:var(--bg0)!important; color:var(--t1)!important;
  font-family:'Pretendard',-apple-system,sans-serif!important;
  -webkit-font-smoothing:antialiased; letter-spacing:-.01em;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1; }
.nicegui-content{ padding:0!important; gap:0!important; align-items:stretch!important; }
.q-page{ background:var(--bg0)!important; }
*{ box-sizing:border-box; }
.mono{ font-variant-numeric:tabular-nums; }
::-webkit-scrollbar{ width:9px; height:0; }
::-webkit-scrollbar-thumb{ background:var(--bg4); border-radius:5px; }

/* 헤더 */
.topbar{ display:flex; align-items:center; justify-content:space-between;
  height:48px; padding:0 16px; background:var(--bg1);
  border-bottom:1px solid var(--line); }
.brand{ font-size:.95rem; font-weight:800; letter-spacing:-.02em; white-space:nowrap; }
.nav{ display:flex; gap:1px; margin-left:16px; }
.navlink{ font-size:.8rem; font-weight:600; color:var(--t3); padding:6px 11px;
  border-radius:6px; cursor:pointer; white-space:nowrap; transition:all .1s; user-select:none; }
.navlink:hover{ color:var(--t2); background:var(--bg3); }
.navlink.on{ color:var(--t1); background:var(--bg3); }
.hmeta{ display:flex; align-items:center; gap:12px; font-size:.74rem; color:var(--t2); white-space:nowrap; }
.badge{ font-size:.68rem; font-weight:700; padding:3px 8px; border-radius:5px;
  background:rgba(59,130,246,.14); color:#8FB8FF; }
.dot{ width:7px; height:7px; border-radius:50%; display:inline-block; margin-right:5px; }

/* 티커테이프 */
.tape{ display:flex; gap:0; overflow-x:auto; background:var(--bg1);
  border-bottom:1px solid var(--line); padding:0 6px; }
.tick{ display:flex; align-items:baseline; gap:7px; padding:7px 14px;
  border-right:1px solid var(--line); white-space:nowrap; }
.tick .l{ font-size:.68rem; color:var(--t3); font-weight:600; }
.tick .p{ font-size:.82rem; font-weight:700; }
.tick .c{ font-size:.72rem; font-weight:700; }

/* 본문 */
.wrap{ padding:14px 16px; width:100%; max-width:1680px; margin:0 auto; }
.sec-h{ font-size:.7rem; font-weight:700; color:var(--t3); text-transform:uppercase;
  letter-spacing:.08em; margin:4px 2px 8px; display:flex; justify-content:space-between; align-items:center; }
.main2{ display:grid; grid-template-columns:minmax(0,1fr) 320px; gap:12px; align-items:start; }
@media (max-width:1100px){ .main2{ grid-template-columns:1fr; } }
.col-main{ min-width:0; }
.side{ display:flex; flex-direction:column; gap:8px; }

/* 카드 공통 */
.card{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:12px 14px;
  transition:border-color .12s; }
.card:hover{ border-color:var(--line2); }
.panel{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:11px 13px; }
.panel-h{ font-size:.6rem; font-weight:700; color:var(--t3); text-transform:uppercase;
  letter-spacing:.07em; margin-bottom:9px; }

/* KPI */
.kpis{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:14px; }
.kpi{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
.kpi-l{ font-size:.62rem; color:var(--t3); font-weight:700; letter-spacing:.05em;
  text-transform:uppercase; margin-bottom:6px; }
.kpi-v{ font-size:1.42rem; font-weight:800; letter-spacing:-.03em; line-height:1; }
.kpi-s{ font-size:.7rem; color:var(--t2); margin-top:5px; font-weight:600; }

/* 테이블 */
.tbl{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.tr{ display:grid; align-items:center; padding:9px 14px; border-bottom:1px solid var(--line); font-size:.82rem; }
.tr:last-child{ border-bottom:none; }
.tr.h{ font-size:.64rem; color:var(--t3); font-weight:700; text-transform:uppercase;
  letter-spacing:.05em; background:var(--bg1); }
.tr.r:hover{ background:var(--bg3); }
.hold-g{ grid-template-columns:1.4fr .7fr 1fr 1fr 1.1fr 1.1fr .8fr .7fr .7fr; }
.hist-g{ grid-template-columns:1.1fr .6fr .8fr .7fr .9fr .7fr; }
.univ-g{ grid-template-columns:1.2fr 1fr 1fr 1fr; }
.tk{ font-weight:800; }
.num{ text-align:right; font-weight:600; font-variant-numeric:tabular-nums; }
.pill{ display:inline-block; font-size:.66rem; font-weight:700; padding:2px 7px; border-radius:4px; }
.empty{ padding:28px; text-align:center; color:var(--t3); font-size:.82rem; }
.demo-banner{ font-size:.7rem; color:var(--gold); background:rgba(201,162,39,.1);
  border:1px solid rgba(201,162,39,.25); border-radius:6px; padding:6px 10px; margin-bottom:10px; font-weight:600; }

/* 사이드 레일 */
.fh{ display:flex; align-items:center; justify-content:space-between; font-size:.74rem; padding:3px 0; color:var(--t2); }
.fh b{ color:var(--t1); font-weight:700; }
.chg{ display:flex; align-items:baseline; justify-content:space-between; gap:6px;
  font-size:.74rem; padding:5px 0; border-bottom:1px solid var(--line); }
.chg:last-child{ border-bottom:none; }
.chg .ct{ font-weight:700; }
.chg .cp{ font-variant-numeric:tabular-nums; font-weight:700; }
.side-empty{ font-size:.72rem; color:var(--t3); padding:4px 0; }

/* 섹터 배분 바 */
.salloc{ display:flex; flex-direction:column; gap:8px; }
.sbar-row{ font-size:.76rem; }
.sbar-top{ display:flex; justify-content:space-between; margin-bottom:3px; }
.sbar-top b{ color:var(--t1); font-weight:700; }
.sbar-top span{ color:var(--t2); }
.sbar{ height:7px; background:var(--bg4); border-radius:4px; overflow:hidden; }
.sbar i{ display:block; height:100%; border-radius:4px; }

/* 상태/통계 그리드 */
.stat3{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
.stat{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:12px 14px; text-align:center; }
.stat .sv{ font-size:1.5rem; font-weight:800; }
.stat .sl{ font-size:.64rem; color:var(--t3); font-weight:600; text-transform:uppercase; letter-spacing:.05em; margin-top:4px; }

/* 전략 카탈로그 */
.scat-cat{ font-size:.68rem; font-weight:700; color:var(--t3); margin:14px 2px 7px; text-transform:uppercase; letter-spacing:.06em; }
.scards{ display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:8px; }
.scard{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:11px 13px; cursor:pointer; transition:all .12s; }
.scard:hover{ border-color:var(--line2); background:var(--bg3); }
.scard.on{ border-color:var(--blue); box-shadow:0 0 0 1px var(--blue) inset; }
.scard .sn{ font-size:.88rem; font-weight:800; margin-bottom:4px; }
.scard .sd{ font-size:.72rem; color:var(--t2); line-height:1.45; min-height:31px; }
.scard .smeta{ display:flex; gap:8px; margin-top:8px; font-size:.64rem; color:var(--t3); font-weight:600; }

/* 정의형 리스트(설정·주문 미리보기) */
.kv{ display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--line); font-size:.8rem; }
.kv:last-child{ border-bottom:none; }
.kv .k{ color:var(--t2); }
.kv .v{ color:var(--t1); font-weight:700; font-variant-numeric:tabular-nums; }

/* 폼 */
.form-grid{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 16px; }
.gate-note{ font-size:.72rem; color:var(--gold); margin-top:10px; }
.q-btn{ text-transform:none!important; }
.tbtn{ background:var(--bg3)!important; color:var(--t1)!important; border:1px solid var(--line2)!important;
  border-radius:6px!important; font-weight:700!important; font-size:.8rem!important; box-shadow:none!important; }
.tbtn-primary{ background:var(--blue)!important; color:#fff!important; border-color:var(--blue)!important; }
.tbtn-danger{ background:var(--up)!important; color:#fff!important; border-color:var(--up)!important; }
.seg{ display:inline-flex; background:var(--bg3); border:1px solid var(--line); border-radius:7px; padding:2px; }
.segbtn{ font-size:.78rem; font-weight:700; padding:5px 16px; border-radius:5px; cursor:pointer; color:var(--t2); user-select:none; transition:all .1s; }
.segbtn.on-buy{ background:var(--up); color:#fff; }
.segbtn.on-sell{ background:var(--dn); color:#fff; }
</style>
"""
ui.add_head_html(STYLE, shared=True)


# ── 유틸 ─────────────────────────────────────────────────────────────────────
def _col(v: float) -> str:
    return "var(--up)" if v > 0 else ("var(--dn)" if v < 0 else "var(--t2)")


def _fmt(n, dec=2) -> str:
    try:
        return f"{float(n):,.{dec}f}"
    except Exception:
        return "—"


def _price(tk: str):
    d = rtf.get_price(tk)
    return d.get("price") if d else None


def _subscribe(tickers, interval=5.0):
    try:
        rtf.subscribe(list(dict.fromkeys(tickers)), interval=interval)
    except Exception:
        pass


def read_orders(limit=40):
    try:
        with open(POC_STATE / "orders_log.json") as f:
            d = json.load(f)
        return list(reversed(d.get("orders", [])))[:limit]
    except Exception:
        return []


def collect():
    cash = pa.cash()
    pm = PortfolioManager(paper=PAPER)
    raw, is_demo = [], False
    if pm.positions:
        for tk, p in pm.positions.items():
            raw.append((tk, p.shares, p.entry_price, p.entry_date, p.score_at_entry))
    elif DEMO:
        is_demo, raw = True, SAMPLE
    rows, invested, cost = [], 0.0, 0.0
    for tk, sh, ep, dt, sc in raw:
        cur = _price(tk) or ep
        mv, cb = cur * sh, ep * sh
        pnl = mv - cb
        try:
            held = (date.today() - date.fromisoformat(dt)).days
        except Exception:
            held = 0
        invested += mv
        cost += cb
        rows.append(dict(tk=tk, sh=sh, ep=ep, cur=cur, mv=mv, pnl=pnl,
                         pnl_pct=(pnl / cb * 100) if cb else 0, held=held,
                         score=sc, sector=_sector_of(tk)))
    return dict(cash=cash, invested=invested, cost=cost, total_pnl=invested - cost,
                equity=cash + invested, rows=rows, is_demo=is_demo)


def confirm(title: str, body: str, on_ok, ok_label: str = "실행", danger: bool = False):
    """실행 확인 다이얼로그. 확인 시 on_ok() 호출(격리된 PoC 장부에만 반영)."""
    with ui.dialog() as d, ui.card().style(
            "background:var(--bg2);border:1px solid var(--line2);border-radius:10px;"
            "max-width:460px;color:var(--t1);gap:6px"):
        ui.html(f'<div style="font-size:.95rem;font-weight:800;margin-bottom:2px">{title}</div>'
                f'<div style="font-size:.8rem;color:var(--t2);line-height:1.6">{body}</div>'
                '<div style="font-size:.7rem;color:var(--t3);margin-top:8px">'
                '· 격리된 PoC 전용 장부(ui_ng/_poc_state)에만 반영됩니다. 실가동 앱과 무관.</div>')
        with ui.row().classes("w-full justify-end").style("gap:8px;margin-top:8px"):
            ui.button("취소", on_click=d.close).classes("tbtn")

            def _go():
                d.close()
                try:
                    on_ok()
                except Exception as ex:
                    ui.notify(f"실패: {ex}", type="negative", position="top")
            ui.button(ok_label, on_click=_go).classes(
                "tbtn " + ("tbtn-danger" if danger else "tbtn-primary"))
    d.open()


class _PoCDaemon:
    """인프로세스 자동매매 루프(run_cycle). 외부 프로세스를 띄우지 않아
    좀비·중복 데몬이 원천적으로 생길 수 없다 — 격리된 PoC 장부에만 작동."""

    def __init__(self):
        self._t = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.runs = 0
        self.last_run = 0.0
        self.last_log = []

    def running(self) -> bool:
        return self._t is not None and self._t.is_alive()

    def start(self) -> bool:
        if self.running():
            return False
        self._stop.clear()
        try:
            _ctl_mod.set_enabled(True)
        except Exception:
            pass
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return True

    def stop(self):
        self._stop.set()
        try:
            _ctl_mod.set_enabled(False)
        except Exception:
            pass

    def run_once(self):
        threading.Thread(target=self._cycle, daemon=True).start()

    def _cycle(self):
        if not self._lock.acquire(blocking=False):
            return  # 이미 사이클 실행 중 — 중복 방지
        try:
            from core.cycle import run_cycle
            logs = []
            run_cycle(load_config(), log=lambda m: logs.append(str(m)))
            self.runs += 1
            self.last_run = time.time()
            self.last_log = logs[-10:] or ["완료 — 변동 없음"]
        except Exception as e:
            self.last_log = [f"오류: {e}"]
        finally:
            self._lock.release()

    def _loop(self):
        self._cycle()
        while not self._stop.is_set():
            interval = max(30, int(load_config().get("interval", 300) or 300))
            if self._stop.wait(interval):
                break
            if load_config().get("enabled", True):
                self._cycle()


DAEMON = _PoCDaemon()

# 쓰기 후 현재 페이지를 다시 그리기 위한 훅(main_page에서 content.refresh 로 설정)
_screen = {"refresh": lambda: None}


# ── 공통 렌더 조각 ────────────────────────────────────────────────────────────
def render_kpis(data, compact=False):
    pnl_pct = (data["total_pnl"] / data["cost"] * 100) if data["cost"] else 0.0
    cards = [
        ("총 평가자산", f'${_fmt(data["equity"])}', f'현금 ${_fmt(data["cash"])}', None),
        ("투자원금", f'${_fmt(data["cost"])}', f'보유 {len(data["rows"])}종목', None),
        ("가용 현금", f'${_fmt(data["cash"])}', "모의투자 계좌", None),
        ("평가손익", f'{"+" if data["total_pnl"]>=0 else ""}${_fmt(data["total_pnl"])}',
         f'{"+" if pnl_pct>=0 else ""}{pnl_pct:.2f}%', _col(data["total_pnl"])),
    ]
    with ui.element("div").classes("kpis"):
        for lbl, val, sub, col in cards:
            style = f'style="color:{col}"' if col else ""
            ui.html(f'<div class="kpi"><div class="kpi-l">{lbl}</div>'
                    f'<div class="kpi-v mono" {style}>{val}</div>'
                    f'<div class="kpi-s mono">{sub}</div></div>')


def render_holdings(data):
    if data["is_demo"]:
        ui.html('<div class="demo-banner">⚠ 샘플 데이터 — 실제 보유종목이 없어 화면 표시용으로만 띄운 값입니다 '
                '(디스크 저장 안 함). 현재가·지수는 실시간입니다.</div>')
    with ui.element("div").classes("tbl"):
        ui.html('<div class="tr h hold-g"><div>종목</div><div class="num">수량</div>'
                '<div class="num">평단</div><div class="num">현재가</div><div class="num">평가액</div>'
                '<div class="num">손익</div><div class="num">손익%</div><div class="num">보유</div>'
                '<div class="num">스코어</div></div>')
        if not data["rows"]:
            ui.html('<div class="empty">보유 종목이 없습니다.</div>')
            return
        for r in data["rows"]:
            c = _col(r["pnl"])
            ui.html(
                f'<div class="tr r hold-g"><div class="tk">{r["tk"]}</div>'
                f'<div class="num mono">{r["sh"]:g}</div>'
                f'<div class="num mono">{_fmt(r["ep"])}</div>'
                f'<div class="num mono">{_fmt(r["cur"])}</div>'
                f'<div class="num mono">${_fmt(r["mv"])}</div>'
                f'<div class="num mono" style="color:{c}">{"+" if r["pnl"]>=0 else ""}{_fmt(r["pnl"])}</div>'
                f'<div class="num mono" style="color:{c}">{"+" if r["pnl_pct"]>=0 else ""}{r["pnl_pct"]:.2f}%</div>'
                f'<div class="num mono">{r["held"]}d</div>'
                f'<div class="num"><span class="pill" style="background:rgba(15,184,115,.14);color:var(--green)">{r["score"]:g}</span></div></div>')


def render_feed_rail(data):
    try:
        h = rtf.feed_health()
    except Exception:
        h = {}
    ok, fails = h.get("ok", False), h.get("fails", 0)
    stale = h.get("stale_sec", 9e9)
    if ok:
        sc, stt = "var(--green)", "정상"
    elif fails:
        sc, stt = "var(--up)", "오류"
    else:
        sc, stt = "var(--t3)", "대기"
    fresh = "—" if stale > 1e8 else (f"{stale:.0f}초 전" if stale < 60 else f"{stale/60:.0f}분 전")
    src = f'WS {h.get("ws_symbols",0)}' if h.get("ws") else "REST 폴링"
    with ui.element("div").classes("panel"):
        ui.html('<div class="panel-h">실시간 피드</div>'
                f'<div class="fh"><span>상태</span><b style="color:{sc}">● {stt}</b></div>'
                f'<div class="fh"><span>캐시 종목</span><b>{h.get("n_cached",0)}</b></div>'
                f'<div class="fh"><span>최신성</span><b>{fresh}</b></div>'
                f'<div class="fh"><span>소스</span><b>{src}</b></div>')
    try:
        changes = list(reversed(rtf.recent_changes(9)))
    except Exception:
        changes = []
    with ui.element("div").classes("panel"):
        if not changes:
            ui.html('<div class="panel-h">최근 가격 변동</div><div class="side-empty">아직 변동 기록이 없습니다.</div>')
            return
        rows = ['<div class="panel-h">최근 가격 변동</div>']
        for ch in changes:
            frm, to = ch.get("from", 0), ch.get("to", 0)
            c = _col(to - frm)
            t = time.strftime("%H:%M:%S", time.localtime(ch.get("ts", time.time())))
            rows.append(f'<div class="chg"><span class="ct">{ch.get("ticker","")}</span>'
                        f'<span class="cp" style="color:{c}">{"▲" if to>=frm else "▼"} {_fmt(to)} '
                        f'<span style="color:var(--t3);font-size:.66rem;font-weight:400">{t}</span></span></div>')
        ui.html("".join(rows))


# ── 페이지들 ──────────────────────────────────────────────────────────────────
def page_dashboard():
    d = collect()
    ui.html('<div class="sec-h">계좌 요약</div>')
    render_kpis(d)
    with ui.element("div").classes("main2"):
        with ui.element("div").classes("col-main"):
            ui.html('<div class="sec-h">보유 종목</div>')
            render_holdings(d)
        with ui.element("div").classes("side"):
            render_feed_rail(d)


def page_portfolio():
    d = collect()
    ui.html('<div class="sec-h">포트폴리오 · 평가</div>')
    render_kpis(d)
    with ui.element("div").classes("main2"):
        with ui.element("div").classes("col-main"):
            ui.html('<div class="sec-h">보유 종목</div>')
            render_holdings(d)
            ui.html('<div class="sec-h" style="margin-top:14px">거래 내역</div>')
            render_history()
        with ui.element("div").classes("side"):
            render_sector_alloc(d)


def render_sector_alloc(data):
    agg = {}
    for r in data["rows"]:
        agg[r["sector"]] = agg.get(r["sector"], 0) + r["mv"]
    total = sum(agg.values()) or 1
    items = sorted(agg.items(), key=lambda x: -x[1])
    palette = ["#3B82F6", "#0FB873", "#F0454F", "#C9A227", "#9B6BF0", "#E8833A", "#56B4C9"]
    with ui.element("div").classes("panel"):
        if not items:
            ui.html('<div class="panel-h">섹터 배분</div><div class="side-empty">보유 종목이 없습니다.</div>')
            return
        html = ['<div class="panel-h">섹터 배분</div><div class="salloc">']
        for i, (sec, val) in enumerate(items):
            pct = val / total * 100
            col = palette[i % len(palette)]
            html.append(f'<div class="sbar-row"><div class="sbar-top"><b>{SECTOR_KO.get(sec, sec or "기타")}</b>'
                        f'<span>{pct:.0f}% · ${_fmt(val,0)}</span></div>'
                        f'<div class="sbar"><i style="width:{pct:.1f}%;background:{col}"></i></div></div>')
        html.append("</div>")
        ui.html("".join(html))


def render_history():
    orders = read_orders(40)
    with ui.element("div").classes("tbl"):
        ui.html('<div class="tr h hist-g"><div>일시</div><div>종목</div><div class="num">구분</div>'
                '<div class="num">수량</div><div class="num">체결가</div><div class="num">출처</div></div>')
        if not orders:
            ui.html('<div class="empty">거래 내역이 없습니다.</div>')
            return
        for o in orders:
            buy = str(o.get("side", "")).lower() == "buy"
            c = "var(--up)" if buy else "var(--dn)"
            ts = str(o.get("ts", o.get("date", "")))[:16].replace("T", " ")
            ui.html(f'<div class="tr r hist-g"><div class="mono" style="font-size:.74rem;color:var(--t2)">{ts}</div>'
                    f'<div class="tk">{o.get("ticker","")}</div>'
                    f'<div class="num" style="color:{c};font-weight:700">{"매수" if buy else "매도"}</div>'
                    f'<div class="num mono">{_fmt(o.get("shares",0),0)}</div>'
                    f'<div class="num mono">{_fmt(o.get("price",0))}</div>'
                    f'<div class="num" style="color:var(--t3);font-size:.72rem">{o.get("source","")}</div></div>')


def page_auto():
    cfg = load_config()
    running = DAEMON.running()
    ui.html('<div class="sec-h">자동 트레이딩 · 상태</div>')
    dcol = "var(--green)" if running else "var(--t3)"
    last = (time.strftime("%H:%M:%S", time.localtime(DAEMON.last_run))
            if DAEMON.last_run else "—")
    with ui.element("div").classes("card"):
        ui.html(f'<div style="display:flex;align-items:center;gap:10px">'
                f'<span class="dot" style="width:10px;height:10px;background:{dcol}"></span>'
                f'<span style="font-size:1.05rem;font-weight:800">자동매매 {"실행 중" if running else "정지"}</span>'
                f'<span style="font-size:.74rem;color:var(--t3)">인프로세스 루프 · 격리 장부</span></div>')
    with ui.element("div").classes("stat3").style("margin-top:10px"):
        for v, l in [(DAEMON.runs, "사이클 실행"), (last, "마지막 실행"),
                     ("모의", "장부 모드")]:
            ui.html(f'<div class="stat"><div class="sv mono" style="font-size:1.15rem">{v}</div><div class="sl">{l}</div></div>')
    ui.html('<div class="sec-h" style="margin-top:14px">현재 설정</div>')
    with ui.element("div").classes("card"):
        rows = [("전략", cfg.get("strategy_name", cfg.get("strategy", "—"))),
                ("투자 기간", cfg.get("horizon", "—")),
                ("실행 주기", f'{cfg.get("interval", 0)}초'),
                ("모드", "모의(페이퍼)" if cfg.get("paper", True) else "실거래"),
                ("손절 / 익절", f'{cfg.get("stop_loss",0)*100:.0f}% / {cfg.get("take_profit",0)*100:.0f}%'),
                ("매수/매도 스코어", f'{cfg.get("min_score","—")} / {cfg.get("sell_score","—")}')]
        ui.html("".join(f'<div class="kv"><span class="k">{k}</span><span class="v">{v}</span></div>' for k, v in rows))
    ui.html('<div class="sec-h" style="margin-top:14px">제어</div>')
    def _start():
        DAEMON.start()
        ui.notify("자동매매를 시작했습니다. 첫 사이클 실행 중…", position="top")
        ui.timer(7.0, _screen["refresh"], once=True)
        _screen["refresh"]()

    def _stop():
        DAEMON.stop()
        ui.notify("자동매매를 정지했습니다.", position="top")
        _screen["refresh"]()

    def _once():
        DAEMON.run_once()
        ui.notify("사이클 1회 실행 중… (완료 후 로그 갱신)", position="top")
        ui.timer(7.0, _screen["refresh"], once=True)

    with ui.row().classes("gap-2"):
        if running:
            ui.button("■ 자동매매 정지", on_click=_stop).classes("tbtn tbtn-danger")
        else:
            ui.button("▶ 자동매매 시작", on_click=lambda: confirm(
                "자동매매 시작",
                f'{cfg.get("interval",300)}초 주기로 run_cycle(발굴·채점·주문)을 실행합니다. '
                '격리된 모의계좌로 실제 매매 결정·체결이 일어납니다.',
                _start, ok_label="시작")).classes("tbtn tbtn-primary")
        ui.button("↻ 지금 1회 실행", on_click=lambda: confirm(
            "지금 1회 실행", "run_cycle 사이클을 1회 실행합니다. 모의계좌에 주문이 발생할 수 있습니다.",
            _once, ok_label="실행")).classes("tbtn")
    # 최근 사이클 로그
    if DAEMON.last_log:
        ui.html('<div class="sec-h" style="margin-top:14px">최근 사이클 로그</div>')
        with ui.element("div").classes("card"):
            ui.html("".join(
                f'<div style="font-size:.74rem;color:var(--t2);padding:2px 0;font-variant-numeric:tabular-nums">{str(m)[:140]}</div>'
                for m in DAEMON.last_log))


def page_order():
    _subscribe([s for s, _ in INDEX_SYMS])
    ui.html('<div class="sec-h">직접 주문 (모의)</div>')
    state = {"side": "buy"}
    with ui.element("div").classes("card").style("max-width:560px"):
        tk_in = ui.input("종목 티커", value="AAPL").props("outlined dense dark").classes("w-full")
        with ui.element("div").classes("seg").style("margin:10px 0"):
            buy_b = ui.label("매수").classes("segbtn on-buy")
            sell_b = ui.label("매도").classes("segbtn")
        qty = ui.number("수량(주)", value=1, min=0, format="%.4g").props("outlined dense dark").classes("w-full")
        preview = ui.html('')

        def refresh_preview():
            tk = (tk_in.value or "").upper().strip()
            if tk:
                _subscribe([tk], interval=3.0)
            px = _price(tk) or 0
            amt = px * (qty.value or 0)
            side_ko = "매수" if state["side"] == "buy" else "매도"
            sc = "var(--up)" if state["side"] == "buy" else "var(--dn)"
            preview.content = (
                f'<div style="margin-top:12px">'
                f'<div class="kv"><span class="k">현재가</span><span class="v mono">${_fmt(px)}</span></div>'
                f'<div class="kv"><span class="k">구분</span><span class="v" style="color:{sc}">{side_ko}</span></div>'
                f'<div class="kv"><span class="k">예상 체결금액</span><span class="v mono">${_fmt(amt)}</span></div></div>')

        def set_side(s):
            state["side"] = s
            buy_b.classes(remove="on-buy"); sell_b.classes(remove="on-sell")
            (buy_b if s == "buy" else sell_b).classes(add=("on-buy" if s == "buy" else "on-sell"))
            refresh_preview()

        buy_b.on("click", lambda e: set_side("buy"))
        sell_b.on("click", lambda e: set_side("sell"))
        tk_in.on("blur", lambda e: refresh_preview())
        qty.on("blur", lambda e: refresh_preview())
        refresh_preview()

        def _exec_order(tk, q, side, px):
            from core.execution import execute_manual
            res = execute_manual(tk, q, side, True, px)
            ui.notify(f'{tk} {"매수" if side=="buy" else "매도"} 체결: '
                      f'{res.get("shares")}주 @ ${_fmt(res.get("price"))}',
                      type="positive", position="top")
            refresh_preview()

        def do_order():
            tk = (tk_in.value or "").upper().strip()
            q = int(qty.value or 0)
            px = _price(tk) or 0
            if not tk or q <= 0 or px <= 0:
                ui.notify("티커·수량·현재가를 확인하세요.", type="warning", position="top")
                return
            side = state["side"]
            side_ko = "매수" if side == "buy" else "매도"
            confirm(f"{tk} {side_ko} 주문", f'{q}주 @ ${_fmt(px)} = ${_fmt(px*q)} (모의)',
                    lambda: _exec_order(tk, q, side, px), ok_label=side_ko, danger=(side == "sell"))

        ui.button("주문 실행", on_click=do_order).classes("tbtn tbtn-primary").style("margin-top:12px")


def _set_strategy(key, name):
    cfg = load_config()
    cfg["strategy"] = key
    cfg["strategy_name"] = name
    _ctl_mod.save_config(cfg)
    ui.notify(f'전략을 "{name}"(으)로 변경했습니다.', type="positive", position="top")
    _screen["refresh"]()


def page_strategy():
    cfg = load_config()
    active = cfg.get("strategy", "composite")
    ui.html('<div class="sec-h">전략 선택</div>')
    try:
        m = scat.meta(active)
    except Exception:
        m = {}
    with ui.element("div").classes("card"):
        ui.html(f'<div style="display:flex;align-items:center;gap:8px">'
                f'<span style="width:9px;height:9px;border-radius:50%;background:{m.get("color","#888")};display:inline-block"></span>'
                f'<span style="font-size:1rem;font-weight:800">현재 전략 · {m.get("name", active)}</span></div>'
                f'<div style="font-size:.78rem;color:var(--t2);margin-top:6px;line-height:1.5">{m.get("desc","")}</div>')
    try:
        cats = scat.keys_by_category()
    except Exception:
        cats = {}
    for cat, keys in cats.items():
        ui.html(f'<div class="scat-cat">{cat}</div>')
        with ui.element("div").classes("scards"):
            for k in keys:
                try:
                    mm = scat.meta(k)
                except Exception:
                    mm = {"name": k, "desc": "", "color": "#888", "risk": 0, "fame": 0, "horizon": ""}
                on = " on" if k == active else ""
                risk = "●" * int(mm.get("risk", 0) or 0) + "○" * (5 - int(mm.get("risk", 0) or 0))
                card = ui.html(
                    f'<div class="scard{on}">'
                    f'<div class="sn" style="color:{mm.get("color","#fff")}">{mm.get("name",k)}</div>'
                    f'<div class="sd">{mm.get("desc","")}</div>'
                    f'<div class="smeta"><span>위험 {risk}</span><span>· {mm.get("horizon","")}</span></div></div>')
                card.on("click", lambda e, key=k, nm=mm.get("name", k): confirm(
                    f"전략 변경 · {nm}", f'활성 전략을 "{nm}"(으)로 변경합니다.',
                    (lambda key=key, nm=nm: _set_strategy(key, nm)), ok_label="변경"))


def page_stocks():
    ui.html('<div class="sec-h">주식 열람</div>')
    try:
        idx_opts = ["주요 지수"] + list(uni.available_indexes())
    except Exception:
        idx_opts = ["주요 지수"]
    state = {"tickers": [s for s, _ in INDEX_SYMS], "loading": False}

    @ui.refreshable
    def table():
        _subscribe(state["tickers"], interval=5.0)
        with ui.element("div").classes("tbl"):
            ui.html('<div class="tr h univ-g"><div>종목</div><div class="num">현재가</div>'
                    '<div class="num">변동</div><div class="num">변동%</div></div>')
            if state["loading"]:
                ui.html('<div class="empty">인덱스 종목을 불러오는 중…</div>')
                return
            shown = state["tickers"][:60]
            if not shown:
                ui.html('<div class="empty">표시할 종목이 없습니다.</div>')
                return
            for tk in shown:
                dpx = rtf.get_price(tk)
                if dpx:
                    chg = dpx.get("change", 0)
                    pct = dpx.get("change_pct", 0) * 100
                    c = _col(pct)
                    ui.html(f'<div class="tr r univ-g"><div class="tk">{tk}</div>'
                            f'<div class="num mono">{_fmt(dpx.get("price",0))}</div>'
                            f'<div class="num mono" style="color:{c}">{"+" if chg>=0 else ""}{_fmt(chg)}</div>'
                            f'<div class="num mono" style="color:{c}">{"+" if pct>=0 else ""}{pct:.2f}%</div></div>')
                else:
                    ui.html(f'<div class="tr r univ-g"><div class="tk">{tk}</div>'
                            '<div class="num" style="color:var(--t3)">로딩…</div><div></div><div></div></div>')

    async def load_index(name):
        if name == "주요 지수":
            state["tickers"] = [s for s, _ in INDEX_SYMS]
            table.refresh()
            return
        state["loading"] = True
        table.refresh()
        try:
            if "코스피" in (name or ""):
                tks = await run.io_bound(uni.get_kr_universe)
            else:
                tks = await run.io_bound(uni.get_index, name)
            state["tickers"] = list(tks)[:60]
        except Exception:
            state["tickers"] = []
        state["loading"] = False
        table.refresh()

    def add_ticker():
        tk = (add_in.value or "").upper().strip()
        if tk and tk not in state["tickers"]:
            state["tickers"].insert(0, tk)
            add_in.value = ""
            table.refresh()

    with ui.row().classes("items-center gap-2").style("margin-bottom:10px"):
        sel = ui.select(idx_opts or ["(인덱스 없음)"], label="인덱스",
                        value=(idx_opts[0] if idx_opts else None)).props("outlined dense dark").style("min-width:200px")
        sel.on("update:model-value", lambda e: load_index(sel.value))
        add_in = ui.input("티커 추가").props("outlined dense dark").style("min-width:140px")
        add_in.on("keydown.enter", lambda e: add_ticker())
        ui.button("추가", on_click=add_ticker).classes("tbtn")
        ui.button("↻", on_click=lambda: table.refresh()).classes("tbtn")
    table()


def page_settings():
    cfg = load_config()
    ui.html('<div class="sec-h">설정</div>')
    # (key, label, value, kind)  kind: pct | int | text
    specs = [
        ("strategy_name", "전략", cfg.get("strategy_name", ""), "text"),
        ("horizon", "투자 기간", cfg.get("horizon", ""), "text"),
        ("interval", "실행 주기(초)", cfg.get("interval", 300), "int"),
        ("min_score", "매수 스코어", cfg.get("min_score", 60), "int"),
        ("sell_score", "매도 스코어", cfg.get("sell_score", 35), "int"),
        ("stop_loss", "손절(%)", round(cfg.get("stop_loss", 0.07) * 100, 1), "pct"),
        ("take_profit", "익절(%)", round(cfg.get("take_profit", 0.15) * 100, 1), "pct"),
        ("trail", "트레일링(%)", round(cfg.get("trail", 0.07) * 100, 1), "pct"),
        ("daily_loss_limit", "일일 손실한도(%)", round(cfg.get("daily_loss_limit", 0.05) * 100, 1), "pct"),
        ("daily_time", "예약 실행 시각", cfg.get("daily_time", "10:00"), "text"),
    ]
    inputs = {}
    with ui.element("div").classes("card"):
        with ui.element("div").classes("form-grid"):
            for key, label, val, kind in specs:
                if kind == "text":
                    inputs[key] = (ui.input(label, value=str(val)).props("outlined dense dark"), kind)
                else:
                    inputs[key] = (ui.number(label, value=val, format="%g").props("outlined dense dark"), kind)

        def _save():
            newcfg = dict(load_config())
            for key, (el, kind) in inputs.items():
                v = el.value
                if kind == "pct":
                    newcfg[key] = float(v) / 100.0
                elif kind == "int":
                    newcfg[key] = int(float(v))
                else:
                    newcfg[key] = v
            _ctl_mod.save_config(newcfg)
            ui.notify("설정을 저장했습니다.", type="positive", position="top")
            _screen["refresh"]()

        def _reset():
            d = dict(_ctl_mod.DEFAULT_CONFIG)
            d["enabled"] = load_config().get("enabled", False)
            d["paper"] = True
            _ctl_mod.save_config(d)
            ui.notify("기본값으로 복원했습니다.", type="positive", position="top")
            _screen["refresh"]()

        with ui.row().style("margin-top:14px;gap:8px"):
            ui.button("설정 저장", on_click=lambda: confirm(
                "설정 저장", "변경한 설정을 저장합니다. 다음 사이클부터 반영됩니다.",
                _save, ok_label="저장")).classes("tbtn tbtn-primary")
            ui.button("기본값", on_click=lambda: confirm(
                "기본값 복원", "설정을 기본값으로 되돌립니다.", _reset,
                ok_label="복원", danger=True)).classes("tbtn")


PAGE_FUNCS = {
    "대시보드": page_dashboard, "전략 선택": page_strategy, "자동 트레이딩": page_auto,
    "포트폴리오": page_portfolio, "주식 열람": page_stocks, "직접 주문": page_order,
    "설정": page_settings,
}


# ── 앱 셸 ─────────────────────────────────────────────────────────────────────
@ui.page("/")
def main_page():
    state = {"page": "대시보드"}
    _subscribe([s for s, _ in INDEX_SYMS] + [t for t, *_ in SAMPLE])

    # 헤더: 브랜드 + nav(전환 시만 갱신) + 메타(타이머 갱신)
    with ui.element("div").classes("topbar"):
        with ui.element("div").style("display:flex;align-items:center;min-width:0;overflow:hidden"):
            ui.html('<span class="brand">📈 AI 트레이딩</span>')

            @ui.refreshable
            def nav_bar():
                with ui.element("nav").classes("nav"):
                    for name in PAGES:
                        lbl = ui.label(name).classes("navlink on" if state["page"] == name else "navlink")
                        lbl.on("click", lambda e, n=name: go(n))
            nav_bar()

        @ui.refreshable
        def meta_box():
            alive = DAEMON.running()
            dcol = "var(--green)" if alive else "var(--t3)"
            now = datetime.now().strftime("%H:%M:%S")
            ui.html(f'<div class="hmeta"><span class="badge">모의투자</span>'
                    f'<span><span class="dot" style="background:{dcol}"></span>자동매매 {"실행 중" if alive else "정지"}</span>'
                    f'<span class="mono">{now}</span></div>')
        meta_box()

    # 티커테이프(타이머 갱신)
    @ui.refreshable
    def tape_box():
        with ui.element("div").classes("tape"):
            for sym, lbl in INDEX_SYMS:
                dd = rtf.get_price(sym)
                with ui.element("div").classes("tick"):
                    ui.html(f'<span class="l">{lbl}</span>')
                    if dd:
                        pct = dd.get("change_pct", 0) * 100
                        ui.html(f'<span class="p">{_fmt(dd["price"])}</span>'
                                f'<span class="c" style="color:{_col(pct)}">{"+" if pct>=0 else ""}{pct:.2f}%</span>')
                    else:
                        ui.html('<span class="p" style="color:var(--t3)">—</span>')
    tape_box()

    @ui.refreshable
    def content():
        with ui.element("div").classes("wrap"):
            try:
                PAGE_FUNCS.get(state["page"], page_dashboard)()
            except Exception as ex:
                ui.html(f'<div class="empty">이 페이지를 그리는 중 오류: {type(ex).__name__}: {ex}</div>')
    content()
    _screen["refresh"] = content.refresh

    def go(name):
        state["page"] = name
        nav_bar.refresh()
        content.refresh()

    def tick():
        meta_box.refresh()
        tape_box.refresh()
        if state["page"] in LIVE_PAGES:
            content.refresh()

    ui.timer(2.0, tick)


if __name__ in {"__main__", "__mp_main__"}:
    # WebSocket 메시지 크기 한도 상향(기본 1MB → 32MB). 데이터 많은 페이지에서
    # 'Message too large' 로 소켓이 끊기는 것을 방지.
    try:
        from nicegui import core as _ngcore
        _ngcore.sio.eio.max_http_buffer_size = 32 * 1024 * 1024
    except Exception:
        pass
    ui.run(host="127.0.0.1", port=8502, title="AI 트레이딩 · NiceGUI",
           show=False, reload=False, dark=True, favicon="📈")
