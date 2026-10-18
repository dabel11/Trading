"""
감시·리포트(monitor.py) 테스트 — 장중 멈춤·프로세스 꺼짐 경보, 장외 체결 감지, 일일/주간 리포트.

모든 상태 파일은 tmp_path 로 격리(control.CONFIG_FILE 을 옮기면 monitor 파일도 따라간다).
실행: python3 -m pytest test_monitor.py -q
"""
import json
from datetime import datetime

import pytest
import pytz

ET = pytz.timezone("America/New_York")


def et_ts(y, mo, d, h, mi=0):
    return ET.localize(datetime(y, mo, d, h, mi)).timestamp()


TUE_11 = et_ts(2026, 10, 20, 11, 0)        # 화요일 장중
TUE_1645 = et_ts(2026, 10, 20, 16, 45)     # 화요일 마감 후
FRI_1645 = et_ts(2026, 10, 23, 16, 45)     # 금요일 마감 후
CFG = {"enabled": True, "auto_running": True, "interval": 300, "paper": True}


@pytest.fixture
def mon(tmp_path, monkeypatch):
    from core import control, execution as EX
    import portfolio
    import monitor
    monkeypatch.setattr(control, "CONFIG_FILE", tmp_path / "autotrader_config.json")
    monkeypatch.setattr(EX, "ORDERS_FILE", tmp_path / "orders_log.json")
    monkeypatch.setattr(portfolio, "STATE_FILE", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(monitor, "_push", lambda text: sent.append(text) or True)
    monitor._sent = sent
    return monitor


def _push_to(lst):
    return lambda text: lst.append(text) or True


# ── 사이클 기록 ──────────────────────────────────────────────────────────
def test_record_cycle_counts_by_et_day(mon):
    mon.record_cycle(True, ts=TUE_11)
    mon.record_cycle(False, "boom", ts=TUE_11 + 60)
    st = mon.load_state()
    day = st["days"]["2026-10-20"]
    assert day["ok"] == 1 and day["err"] == 1 and day["last_error"] == "boom"
    assert st["last_cycle_ts"] == TUE_11


# ── 건강 검사 ────────────────────────────────────────────────────────────
def test_health_quiet_when_closed_or_user_stopped(mon):
    sat = et_ts(2026, 10, 24, 11)
    assert mon.health(CFG, {}, sat)[0] is None
    assert mon.health({**CFG, "auto_running": False}, {}, TUE_11)[0] is None
    assert mon.health({**CFG, "enabled": False}, {}, TUE_11, process_alive=False)[0] is None


def test_health_dead_and_stale_and_grace(mon):
    assert mon.health(CFG, {}, TUE_11, process_alive=False)[0] == "dead"
    early = et_ts(2026, 10, 20, 9, 45)                    # 개장 15분 — 유예
    assert mon.health(CFG, {}, early)[0] is None
    stale = {"last_cycle_ts": TUE_11 - 40 * 60}
    kind, detail = mon.health(CFG, stale, TUE_11)
    assert kind == "stale" and "40분째" in detail
    fresh = {"last_cycle_ts": TUE_11 - 4 * 60}
    assert mon.health(CFG, fresh, TUE_11)[0] is None


def test_health_daily_schedule(mon):
    cfg = {**CFG, "schedule": "daily", "daily_time": "10:00"}
    assert mon.health(cfg, {}, TUE_11)[0] == "stale"      # 10:45 지나도록 0회
    ran = {"days": {"2026-10-20": {"ok": 1}}}
    assert mon.health(cfg, ran, TUE_11)[0] is None


def test_alert_once_then_recover(mon):
    sent = []
    mon.record_cycle(True, ts=TUE_11 - 40 * 60)
    m1 = mon.check_and_alert(CFG, push=_push_to(sent), now_ts=TUE_11)
    m2 = mon.check_and_alert(CFG, push=_push_to(sent), now_ts=TUE_11 + 60)
    assert m1 and "이상 감지" in m1 and m2 is None and len(sent) == 1
    mon.record_cycle(True, ts=TUE_11 + 120)
    m3 = mon.check_and_alert(CFG, push=_push_to(sent), now_ts=TUE_11 + 180)
    assert m3 and "정상화" in m3
    assert len(mon.load_state()["incidents"]) == 1 and mon.load_state()["incident"] is None


def test_incident_closed_at_market_close_is_not_called_recovery(mon):
    sent = []
    mon.check_and_alert(CFG, process_alive=False, push=_push_to(sent), now_ts=TUE_11)
    msg = mon.check_and_alert(CFG, process_alive=False, push=_push_to(sent),
                              now_ts=et_ts(2026, 10, 20, 16, 5))
    assert "장 마감" in msg and "정상화" not in msg


# ── 장외 체결 ────────────────────────────────────────────────────────────
def _write_orders(path, orders):
    path.write_text(json.dumps({"orders": orders}))


def _order(ts_epoch, tk="AAA", side="buy"):
    return {"ts": datetime.fromtimestamp(ts_epoch).isoformat(timespec="seconds"),
            "date": datetime.fromtimestamp(ts_epoch, ET).date().isoformat(),
            "ticker": tk, "side": side, "shares": 3.0, "price": 10.0, "source": "paper"}


def test_offhours_fill_detection(mon, tmp_path):
    from core import execution as EX
    sent = []
    _write_orders(EX.ORDERS_FILE, [_order(TUE_11 - 3600, "OLD")])
    assert mon.check_offhours_fills(_push_to(sent)) == []        # 첫 실행 = 기준점만
    sun = et_ts(2026, 10, 25, 11)
    _write_orders(EX.ORDERS_FILE, [_order(TUE_11 - 3600, "OLD"), _order(TUE_11, "INS"),
                                   _order(sun, "WKD", "sell")])
    found = mon.check_offhours_fills(_push_to(sent))
    assert [o["ticker"] for o in found] == ["WKD"]
    assert sent and "장외 체결" in sent[0] and "WKD" in sent[0]
    assert mon.check_offhours_fills(_push_to(sent)) == []        # 같은 건 재경보 없음


# ── 리포트 본문 ──────────────────────────────────────────────────────────
def test_daily_report_content(mon):
    prev = {"date": "2026-10-19", "equity": 10000.0, "spy": 500.0}
    base = {"date": "2026-10-16", "equity": 10000.0, "spy": 495.0}
    snap = {"date": "2026-10-20", "equity": 10120.0, "cash": 4000.0, "spy": 502.0,
            "positions": [{"ticker": "NVDA", "pnl_pct": 0.031}, {"ticker": "LLY", "pnl_pct": -0.01}]}
    fills = [{"side": "buy", "ticker": "NVDA", "shares": 3, "price": 200.0},
             {"side": "sell", "ticker": "AAPL", "shares": 5, "price": 105.0}]
    closed = [{"ticker": "AAPL", "entry_price": 100.0, "exit_price": 105.0, "shares": 5, "reason": "ev_exit"}]
    msg = mon.build_daily_report("2026-10-20", snap, prev, base, fills, closed,
                                 {"BUY": 1, "WAIT": 4, "HOLD": 2, "SELL": 0},
                                 {"ok": 78, "err": 0, "max_gap": 6}, 0)
    assert "10/20(화)" in msg and "오늘 +1.2%" in msg
    assert "S&P 500 +0.4% → 초과 +0.8%p" in msg
    assert "누적(10/16(금) 기준)" in msg
    assert "실현 $+25" in msg and "+5.0% · ev_exit" in msg
    assert "NVDA +3.1%" in msg and "대기 4" in msg
    assert "사이클 78회" in msg and msg.rstrip().endswith("✅")


def test_daily_report_flags_problems(mon):
    snap = {"date": "2026-10-20", "equity": 9900.0, "cash": 9900.0, "spy": None, "positions": []}
    msg = mon.build_daily_report("2026-10-20", snap, None, None, [], [], None,
                                 {"ok": 3, "err": 2, "max_gap": 140}, 1)
    assert "장외 체결 1" in msg and msg.rstrip().endswith("⚠️")


def test_weekly_report_content(mon):
    days = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    start = {"date": "2026-10-16", "equity": 10000.0, "spy": 500.0}
    snaps = [{"date": d, "equity": e, "spy": s} for d, e, s in
             zip(days, [10100, 9950, 10200, 10150, 10300], [501, 499, 503, 504, 505])]
    closed = [{"ticker": "A", "entry_price": 100, "exit_price": 106},
              {"ticker": "B", "entry_price": 100, "exit_price": 97},
              {"ticker": "C", "entry_price": 100, "exit_price": 102}]
    msg = mon.build_weekly_report(days, snaps, start, start, closed, 0, 0, 0, 5)
    assert "10/19(월)~10/23(금)" in msg and "주간 +3.0%" in msg and "초과 +2.0%p" in msg
    assert "최대낙폭 -1.5%" in msg
    assert "승률 67%" in msg and "최고 A +6.0%" in msg and "최악 B -3.0%" in msg
    assert "일일 리포트 5/5 ✅" in msg and "⚠️" not in msg


def test_max_gap_and_week_boundaries(mon):
    o, c = 0.0, 390 * 60.0
    assert mon.max_gap_minutes([60, 360, 3000], o, c) == pytest.approx((c - 3000) / 60)
    assert mon.max_gap_minutes([], o, c) == 390
    assert mon.is_last_trading_day_of_week("2026-10-23")          # 금
    assert not mon.is_last_trading_day_of_week("2026-10-21")      # 수
    assert mon.is_last_trading_day_of_week("2026-04-02")          # 목 (4/3 성금요일 휴장)


# ── 일일·주간 작업 ──────────────────────────────────────────────────────
def _snap(day, eq=10050.0):
    return {"date": day, "equity": eq, "cash": eq, "spy": 501.0, "positions": []}


def test_daily_job_once_after_close(mon):
    sent = []
    mon.save_snapshot({"date": "2026-10-19", "equity": 10000.0, "cash": 10000.0, "spy": 500.0})
    assert mon.daily_job(CFG, now_ts=TUE_11, push=_push_to(sent), snap_fn=_snap) is None
    msg = mon.daily_job(CFG, now_ts=TUE_1645, push=_push_to(sent), snap_fn=_snap)
    assert msg and "일일 리포트" in msg and len(sent) == 1
    assert mon.daily_job(CFG, now_ts=TUE_1645 + 600, push=_push_to(sent), snap_fn=_snap) is None
    assert [s["date"] for s in mon.load_history()] == ["2026-10-19", "2026-10-20"]
    assert mon.load_state()["reports"]["2026-10-20"] is True


def test_friday_sends_weekly_too(mon):
    sent = []
    mon.save_snapshot({"date": "2026-10-16", "equity": 10000.0, "cash": 10000.0, "spy": 500.0})
    mon.daily_job(CFG, now_ts=FRI_1645, push=_push_to(sent), snap_fn=lambda d: _snap(d, 10300.0))
    assert len(sent) == 2 and "주간 리포트" in sent[1] and "주간 +3.0%" in sent[1]


def test_ensure_baseline_uses_last_trading_day(mon, monkeypatch):
    import market_hours as mh
    monkeypatch.setattr(mh, "now_et", lambda: ET.localize(datetime(2026, 10, 18, 11, 38)))  # 일요일
    mon.ensure_baseline(snap_fn=_snap)
    h = mon.load_history()
    assert len(h) == 1 and h[0]["date"] == "2026-10-16" and h[0]["baseline"] is True
    mon.ensure_baseline(snap_fn=_snap)                           # 이미 있으면 그대로
    assert len(mon.load_history()) == 1


def test_preview_report_does_not_record(mon, monkeypatch):
    monkeypatch.setattr(mon, "account_snapshot", lambda day, paper=True: _snap(day))
    out = mon.preview_report(CFG)
    assert out.startswith("📊 리포트 미리보기")
    assert mon.load_history() == [] and "reported_daily" not in mon.load_state()
