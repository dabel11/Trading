"""매매 사이클 안전성 프로브 — 모든 상태파일을 임시 디렉터리로 격리, 네트워크 0.

각 프로브는 코드 리딩으로 의심한 결함을 실제 실행으로 확인한다.
실장부(state*.json, paper_account.json, orders_log.json, config)는 건드리지 않는다.
"""
import json
import sys
import tempfile
import types
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="trade_probe_"))

import safe_store
safe_store._TRADE_LOCK_FILE = TMP / ".trade.lock"
import portfolio
portfolio.STATE_FILE = TMP / "state.json"
import paper_account as P
P.PAPER_FILE = TMP / "paper_account.json"
from core import execution as EX
EX.ORDERS_FILE = TMP / "orders_log.json"
from core import control
control.CONFIG_FILE = TMP / "autotrader_config.json"
import risk_guard as rg
rg.DAY_FILE = TMP / "day_state.json"
import notifier                      # 실제 알림 발송 차단
notifier.notify_buy = lambda *a, **k: None
notifier.notify_sell = lambda *a, **k: None

results = []


def report(name, ok_bug, detail):
    results.append((name, ok_bug, detail))
    print(f"[{'BUG' if ok_bug else 'ok '}] {name}: {detail}")


def fresh_paper(cash=10000):
    for f in ("state_paper.json", "trades_paper.json"):
        (TMP / f).unlink(missing_ok=True)
    P.reset(cash)
    return portfolio.PortfolioManager(paper=True)


# ── 1. 이중 매도 → 모의 현금 유령 입금 ───────────────────────────────────
pm = fresh_paper()
EX.execute_orders({"sell": [], "buy": [{"ticker": "AAPL", "shares": 10, "score": 80,
                   "est_price": 100.0, "est_cost": 1000.0}]}, {"AAPL": 100.0}, True, pm)
sell = {"sell": [{"ticker": "AAPL", "shares": 10, "reason": "stop_loss (-8%)",
                  "est_price": 92.0}], "buy": []}
EX.execute_orders(sell, {"AAPL": 92.0}, True, portfolio.PortfolioManager(paper=True))  # 틱가드
EX.execute_orders(sell, {"AAPL": 92.0}, True, pm)  # 사이클(같은 결정, 묵은 스냅샷)
exp = 10000 - 1000 + 920
report("double-sell phantom cash", P.cash() != exp,
       f"현금 {P.cash():.0f} (정상 {exp:.0f}), 주문로그 매도 "
       f"{sum(1 for o in json.loads(EX.ORDERS_FILE.read_text())['orders'] if o['side']=='sell')}건")

# ── 2. 매도 수량 > 보유 수량 (부분 수동매도 후 사이클 전량매도) ────────────
pm = fresh_paper()
EX.execute_orders({"sell": [], "buy": [{"ticker": "MSFT", "shares": 10, "score": 80,
                   "est_price": 100.0, "est_cost": 1000.0}]}, {"MSFT": 100.0}, True, pm)
EX.execute_manual("MSFT", 6, "sell", True, est_price=100.0)       # 사용자가 6주 매도
EX.execute_orders({"sell": [{"ticker": "MSFT", "shares": 10, "reason": "score_drop",
                   "est_price": 100.0}], "buy": []}, {"MSFT": 100.0}, True, pm)
exp = 10000
report("sell qty not clamped to holdings", abs(P.cash() - exp) > 1e-6,
       f"현금 {P.cash():.0f} (정상 {exp:.0f}) — 실보유 4주인데 10주 대금 입금")


# ── 가짜 Alpaca 클라이언트 ───────────────────────────────────────────────
class FakeOrder:
    def __init__(self, oid, status, fap, fq):
        self.id, self.status, self.filled_avg_price, self.filled_qty = oid, status, fap, fq


class FakeTrading:
    """script: 주문별 (status, filled_qty) 시퀀스. 체결 누적을 broker_pos 에 반영.

    실제 Alpaca 와 같게: 체결 수량은 그 주문의 수량을 넘지 않고, 취소 요청을 받은
    미종결 주문은 canceled 가 된다(체결분은 남는다)."""

    _FINAL = ("filled", "canceled", "expired", "rejected")

    def __init__(self, script, price=100.0):
        self.script, self.price = script, price
        self.n = 0
        self.orders = {}
        self.cancels = []
        self.submitted = []

    def submit_order(self, req):
        oid = f"o{self.n}"
        status, fq = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        fq = min(fq, int(float(req.qty)))
        self.orders[oid] = FakeOrder(oid, status, self.price if fq else None, fq)
        kind = "limit" if getattr(req, "limit_price", None) else "market"
        self.submitted.append((req.client_order_id, float(req.qty), kind))
        return self.orders[oid]

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def cancel_order_by_id(self, oid):
        self.cancels.append(oid)
        o = self.orders[oid]
        if o.status not in self._FINAL:
            o.status = "canceled"

    def filled_total(self):
        return sum(int(o.filled_qty or 0) for o in self.orders.values())


import broker as B
_orig_time, _orig_sleep = B.time.time, B.time.sleep
B.time.sleep = lambda s: None                         # 폴링 대기 제거
_t = [0.0]
B.time.time = lambda: (_t.__setitem__(0, _t[0] + 1.0) or _t[0])   # 가상 시계


def fake_broker(script, price=100.0, requote=None):
    b = B.Broker.__new__(B.Broker)
    b.paper = False
    b._trading = FakeTrading(script, price)
    b.get_prices = lambda tks: {tks[0]: requote if requote is not None else price}
    return b


# ── 3. 부분체결 후 재시도 → 전량 재주문(과체결) + 1차 체결분 장부 누락 ────
portfolio.STATE_FILE = TMP / "state.json"
(TMP / "state.json").unlink(missing_ok=True)
pm_live = portfolio.PortfolioManager(paper=False)
bk = fake_broker([("partially_filled", 30), ("filled", 50)])
EX.execute_orders({"sell": [], "buy": [{"ticker": "NVDA", "shares": 50, "score": 80,
                   "est_price": 100.0, "est_cost": 5000.0}]}, {"NVDA": 100.0},
                  False, pm_live, broker=bk)
led = portfolio.PortfolioManager(paper=False).positions.get("NVDA")
report("partial fill + retry overfill", bk._trading.filled_total() != (led.shares if led else 0),
       f"브로커 체결 합계 {bk._trading.filled_total()}주 / 장부 {led.shares if led else 0}주 "
       f"/ 의도 50주 · 제출 {bk._trading.submitted}")

# ── 4. 부분체결 후 취소 상태 → 장부 미기록 ──────────────────────────────
(TMP / "state.json").unlink(missing_ok=True)
pm_live = portfolio.PortfolioManager(paper=False)
bk = fake_broker([("canceled", 20)])
B.Broker.execute_order.__defaults__  # noqa
res = bk.execute_order("AMD", 50, "buy", 100.0, max_retries=0)
v = B.validate_fill(res, est_price=100.0, req_shares=50)
report("canceled-with-partial-fill not recorded", not v["ok"] and bk._trading.filled_total() > 0,
       f"브로커 20주 체결 → validate_fill ok={v['ok']} → 장부 {v['filled_qty']}주")

# ── 5. 손절 매도가 급락 시 '추격 포기'로 미집행 ─────────────────────────
# 지정가 미체결, 재조회 -4.3%. 추격(재가격·시장가)으로 낸 주문은 체결된다 —
# 예전 스크립트 [("new",0)] 는 시장가까지 영원히 미체결이라 어떤 구현도 통과 불가였다.
bk = fake_broker([("new", 0), ("new", 0), ("filled", 10)], price=92.0, requote=88.0)
res = bk.execute_order("TSLA", 10, "sell", 92.0)
report("stop-loss sell abandoned on fast drop", res.get("status") != "filled",
       f"status={res.get('status')} · note={res.get('note')} · 제출 {bk._trading.submitted}")

# ── 6. execute_manual(실거래) 시장가 미체결 → 예외만, 주문 취소 없음 ─────
fb = fake_broker([("accepted", 0)])
orig_cls = B.Broker
B.Broker = lambda paper=False: fb
try:
    err = None
    try:
        EX.execute_manual("META", 3, "buy", False, est_price=500.0)
    except RuntimeError as e:
        err = str(e)
finally:
    B.Broker = orig_cls
report("manual live order left open on timeout", err is not None and not fb._trading.cancels,
       f"예외='{(err or '')[:40]}…' · 취소 호출 {len(fb._trading.cancels)}회"
       + ("" if fb._trading.cancels else " (주문은 브로커에 살아있음)"))

B.time.time, B.time.sleep = _orig_time, _orig_sleep   # time 모듈 전역 원복

# ── 7. 모의 킬스위치가 미실현 손실을 못 봄 ──────────────────────────────
import core.cycle as CY
pm = fresh_paper()
EX.execute_orders({"sell": [], "buy": [{"ticker": "AAPL", "shares": 50, "score": 80,
                   "est_price": 100.0, "est_cost": 5000.0}]}, {"AAPL": 100.0}, True, pm)


def cycle_equity(px):
    """run_cycle 이 킬스위치에 넘기는 자산 (모의 모드)."""
    if hasattr(CY, "mark_to_market_equity"):
        return CY.mark_to_market_equity(pm, {"AAPL": px}, True)
    return P.cash() + pm.invested_capital()      # 수정 전 식


rg.DAY_FILE.unlink(missing_ok=True)
rg.check(cycle_equity(100.0), loss_limit=0.05)   # 장 시작 앵커
cur_eq_cycle = cycle_equity(70.0)                # 주가 -30% (자산 -15%)
r = rg.check(cur_eq_cycle, loss_limit=0.05)
true_eq = P.cash() + 50 * 70.0
report("paper kill switch ignores unrealized loss", not r["halted"],
       f"run_cycle 자산식 ${cur_eq_cycle:,.0f} (실제 ${true_eq:,.0f}, "
       f"{(true_eq-10000)/10000:+.0%}) → halted={r['halted']}")

# ── 시계 주입: 로컬(KST) 날짜와 ET 현재시각을 함께 가짜로 ───────────────
import pytz
import market_hours as mh
from core import runner as RN
ET = pytz.timezone("America/New_York")
KST = pytz.timezone("Asia/Seoul")
_real_now_et = mh.now_et
_CLOCK_MODS = [rg, RN, portfolio, EX]
_saved_dt = {m: (getattr(m, "date", None), getattr(m, "datetime", None)) for m in _CLOCK_MODS}


def at_utc(y, m, d, hh, mm):
    u = pytz.utc.localize(datetime(y, m, d, hh, mm))

    class FDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return u.astimezone(tz) if tz else u.astimezone(KST).replace(tzinfo=None)

    class FD(date):
        @classmethod
        def today(cls):
            return u.astimezone(KST).date()
    for mod in _CLOCK_MODS:
        if _saved_dt[mod][0] is not None:
            mod.date = FD
        if _saved_dt[mod][1] is not None:
            mod.datetime = FDT
    mh.now_et = lambda: u.astimezone(ET)
    return u


def real_clock():
    for mod, (d, dt) in _saved_dt.items():
        if d is not None:
            mod.date = d
        if dt is not None:
            mod.datetime = dt
    mh.now_et = _real_now_et


# ── 8. daily 모드: KST 자정이 미 장중이라 하루 2회 실행 ──────────────────
cfg = {"daily_time": "10:00"}
runs = []
r8 = RN.Runner(log=lambda m: None)
for hh, mm in [(14, 5), (14, 30), (15, 5), (15, 30), (19, 0)]:   # UTC, 2026-07-15 (EDT)
    at_utc(2026, 7, 15, hh, mm)
    if RN._daily_due(cfg, r8._last_daily):
        runs.append(f"{hh-4:02d}:{mm:02d} ET")
        r8._mark_daily_done()                      # step() 이 실행 성공 후 하는 기록
real_clock()
report("daily schedule double-run (KST date)", len(runs) > 1, f"7/15 하루 실행 시각: {runs}")

# ── 9. 킬스위치 하루 경계: ET 장중에 halted 가 풀리는가 ─────────────────
rg.DAY_FILE.unlink(missing_ok=True)
at_utc(2026, 7, 15, 14, 30)                       # ET 10:30 / KST 23:30
rg.check(10000.0, loss_limit=0.05)                # 당일 앵커
rg.check(9400.0, loss_limit=0.05)                 # -6% → halted
h_before = rg.is_halted()
at_utc(2026, 7, 15, 15, 30)                       # ET 11:30 / KST 다음날 00:30
h_after = rg.is_halted()
r9 = rg.check(9400.0, loss_limit=0.05)
real_clock()
report("kill-switch day boundary is local (KST)", h_before and not (h_after and r9["halted"]),
       f"ET 10:30 halted={h_before} → 같은 ET 날 11:30 halted={h_after}, check={r9['halted']}")

# ── 10. 백테스트가 라이브 프로세스 전역을 오염 (앱 백테스트 경로) ─────────
import re
src = (REPO / "ui_ng" / "dashboard.py").read_text()
m = re.search(r"\ndef page_backtest\(.*?(?=\ndef )", src, re.S)
body = m.group(0) if m else ""
in_process = ("import backtester" in body) or ("_bt.run" in body)
uses_iso = "_iso.call" in body
leak = None
try:
    from core import isolated as ISO
    ISO.call("portfolio:set_clock", date(2023, 1, 3))
    ISO.call("builtins:exec", "import portfolio; portfolio.CAPITAL_TOTAL = 1e6; "
             "portfolio.MAX_POSITIONS = 99")
    leak = (portfolio._CLOCK is not None or portfolio.CAPITAL_TOTAL == 1e6
            or portfolio.MAX_POSITIONS == 99)
except ImportError:
    leak = True                                   # 격리 실행기 없음 = 같은 프로세스
# 같은 프로세스였다면: 진입일 오염 → 다음 사이클 강제 매도 (원래 재현 시나리오)
pm = fresh_paper()
portfolio.set_clock(date(2023, 1, 3)); pm.record_buy("GOOG", 5, 150.0, 70); portfolio.set_clock(None)
_why = portfolio.PortfolioManager(paper=True).should_sell(
    portfolio.PortfolioManager(paper=True).positions["GOOG"], 151.0, 70)
report("backtest clock leaks into live ledger", in_process or not uses_iso or leak,
       f"앱 백테스트 인프로세스={in_process} · 격리호출={uses_iso} · 자식→부모 전역누수={leak} "
       f"(같은 프로세스였다면 → {_why})")

# ── 11. 설정 파일 손상 읽기 → 기본값으로 덮어씀 / 매매 재개 ─────────────
control._LAST_GOOD = {} if hasattr(control, "_LAST_GOOD") else None
user_cfg = {"enabled": False, "paper": False, "stop_loss": 0.03, "strategy": "momentum"}
control.CONFIG_FILE.write_text(json.dumps(user_cfg))
control.load_config()                        # 정상 읽기 (마지막 정상값 캐시)
control.CONFIG_FILE.write_text("")           # 쓰는 도중 잘린 파일을 읽었다고 가정
c1 = control.load_config()
kept = control.CONFIG_FILE.read_text() == ""
# 프로세스가 막 떠서 정상값 캐시가 없는 상태에서 손상 파일을 읽은 경우
if hasattr(control, "_LAST_GOOD"):
    control._LAST_GOOD = {}
c2 = control.load_config()
report("torn config read overwrites with defaults",
       not kept or c1.get("enabled") is not False or c2.get("enabled") is True,
       f"파일 보존={kept} · 캐시有 enabled={c1.get('enabled')} paper={c1.get('paper')} · "
       f"캐시無 enabled={c2.get('enabled')}")

# ── 12. 휴장일·조기폐장 ────────────────────────────────────────────────
import market_hours as mh
tg = ET.localize(datetime(2026, 11, 26, 11, 0))   # 추수감사절(휴장)
bf = ET.localize(datetime(2026, 11, 27, 14, 30))  # 블랙프라이데이 13:00 조기폐장
report("holiday / early close treated as open", mh.is_market_open(tg) or mh.is_market_open(bf),
       f"Thanksgiving 11:00 open={mh.is_market_open(tg)} · BlackFriday 14:30 open={mh.is_market_open(bf)}")

# ── 13. 장부 손상 → 조용히 빈 포지션(fail-open) ─────────────────────────
pm = fresh_paper()
pm.record_buy("AAPL", 5, 100.0, 70)
(TMP / "state_paper.json").write_text("{not json")
after = portfolio.PortfolioManager(paper=True).positions
report("corrupt ledger fails open (positions={})", after == {},
       f"손상 후 로드 positions={after} · .corrupt 백업만 남고 매매는 계속")

# ── 14. 실행 루프 장 마감 게이트 (루프·'지금 1회 실행' 공용) ─────────────
control.PID_FILE = TMP / "autotrader.pid"          # 실가동 앱 PID 파일 보호
control.save_config({"enabled": True, "paper": True, "interval": 300})
import core.cycle as CY
_calls = []
_orig_rc = CY.run_cycle
CY.run_cycle = lambda cfg, log=None: _calls.append(1) or {"sold": [], "bought": []}
_orig_open = mh.is_market_open
mh.is_market_open = lambda dt=None: False          # 장 마감
try:
    d14 = RN.Runner(log=lambda m: None).step()
    g14 = RN.run_cycle_guarded(control.load_config(), log=lambda m: None)
    n_closed = len(_calls)
    mh.is_market_open = lambda dt=None: True       # 개장
    RN.Runner(log=lambda m: None).step()
    n_open = len(_calls) - n_closed
finally:
    mh.is_market_open = _orig_open
    CY.run_cycle = _orig_rc
report("cycle runs while market closed", n_closed > 0 or n_open != 1,
       f"마감 중 run_cycle {n_closed}회(루프 대기 {d14:.0f}s, 지금1회={g14}) · 개장 중 {n_open}회")

print("\nTMP =", TMP)
print(f"{sum(1 for r in results if r[1])}/{len(results)} 결함 재현")
