"""감시·리포트 — '깨끗한 모의 운영(1단계)'을 사람이 매일 들여다보지 않아도 검증되게.

  · 장중 사이클 멈춤 / 자동매매 프로세스 꺼짐 경보 + 복구 알림      check_and_alert
  · 장외 체결 감지 (장 마감·주말 체결 = 1단계 실패 조건)          check_offhours_fills
  · 장 마감 후 일일 리포트, 주 마지막 거래일엔 주간 리포트        daily_job

앱(ui_ng) 백그라운드 스레드(start)와 외부 감시(watchdog.py, launchd 5분 주기)가 같은
상태 파일을 공유한다 — 경보는 사건(incident)당 1회, 어느 쪽이 먼저 보내든 중복되지 않는다.
사이클 실행 기록은 core.cycle.run_cycle 이 record_cycle 로 남긴다.
날짜는 전부 ET 거래일 기준(KST 자정은 미국 장중이라 하루 경계로 쓰면 안 된다).
"""
from __future__ import annotations

import fcntl
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional



def _base() -> Path:
    # 호출 시점에 계산 — 격리 모드(POC_ISOLATE)·프로브가 control 경로를 바꾸면 따라간다
    from core import control
    return control.CONFIG_FILE.parent


def _state_file() -> Path:
    return _base() / "monitor_state.json"


def _history_file() -> Path:
    return _base() / "equity_history.json"


def _lock_file() -> Path:
    return _base() / ".monitor.lock"


OPEN_GRACE = 600          # 개장 후 첫 사이클까지 유예(초)
REPORT_DELAY = 300        # 마감 후 이만큼 지나서 일일 리포트(종가 반영 대기)
KEEP_DAYS = 30

_TLOCK = threading.Lock()
_WD = ["일", "월", "화", "수", "목", "금", "토"]


# ── 상태 파일 (프로세스 간 read-modify-write 직렬화) ─────────────────────
@contextmanager
def _locked():
    with _TLOCK:
        lf = _lock_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def load_state() -> dict:
    from safe_store import safe_read_json
    d = safe_read_json(_state_file(), default={}) or {}
    d.setdefault("days", {})
    d.setdefault("incidents", [])
    d.setdefault("offhours", [])
    return d


def _save_state(d: dict):
    from safe_store import atomic_write_json
    days = d.get("days", {})
    for k in sorted(days)[:-KEEP_DAYS]:
        days.pop(k, None)
    d["incidents"] = d.get("incidents", [])[-100:]
    d["offhours"] = d.get("offhours", [])[-100:]
    atomic_write_json(_state_file(), d)


def _update(fn):
    with _locked():
        d = load_state()
        out = fn(d)
        _save_state(d)
        return out


def _et(ts: Optional[float] = None) -> datetime:
    import market_hours as mh
    now = mh.now_et()
    return now if ts is None else datetime.fromtimestamp(ts, now.tzinfo)


def _day_key(ts: Optional[float] = None) -> str:
    return _et(ts).date().isoformat()


def _push(text: str) -> bool:
    try:
        import telegram_bot as tg
        return bool(tg.push(text))
    except Exception:
        return False


# ── 사이클 기록 (core.cycle 이 호출) ────────────────────────────────────
def record_cycle(ok: bool = True, error: str = "", ts: Optional[float] = None):
    ts = ts or time.time()
    key = _day_key(ts)

    def f(d):
        day = d["days"].setdefault(key, {"ok": 0, "err": 0, "ts": []})
        if ok:
            day["ok"] += 1
            day["ts"] = (day.get("ts", []) + [round(ts)])[-600:]
            d["last_cycle_ts"] = ts
        else:
            day["err"] += 1
            day["last_error"] = str(error)[:200]
    try:
        _update(f)
    except Exception:
        pass                       # 감시 기록 실패가 매매를 막으면 안 된다


# ── 건강 검사 ────────────────────────────────────────────────────────────
def expected_gap(cfg: dict) -> float:
    """이보다 오래 사이클이 없으면 '멈춤' (주기의 3배 + 2분, 최소 5분)."""
    return max(60, int(cfg.get("interval", 300) or 300)) * 3 + 120


def health(cfg: dict, state: dict, now_ts: Optional[float] = None,
           process_alive: Optional[bool] = None) -> tuple:
    """(kind, detail). kind: None(정상·점검 대상 아님) | 'dead' | 'stale'."""
    import market_hours as mh
    now_ts = now_ts or time.time()
    now = _et(now_ts)
    if not mh.is_market_open(now):
        return None, ""
    if not (cfg.get("enabled", True) and cfg.get("auto_running", False)):
        return None, ""            # 사용자가 멈춘 상태는 사건이 아니다
    if process_alive is False:
        return "dead", "자동매매 프로세스가 꺼져 있습니다 (앱이 종료됐거나 멈춤)"
    opened = now.replace(hour=mh.OPEN_T.hour, minute=mh.OPEN_T.minute, second=0, microsecond=0)
    since_open = (now - opened).total_seconds()
    day = state.get("days", {}).get(now.date().isoformat(), {})
    if cfg.get("schedule") == "daily":
        hh, mm = (cfg.get("daily_time") or "10:00").split(":")
        due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0) + timedelta(minutes=45)
        if now > due and not day.get("ok"):
            return "stale", f"예약 실행({cfg.get('daily_time')} ET) 후 45분이 지나도록 사이클 없음"
        return None, ""
    gap = expected_gap(cfg)
    if since_open < OPEN_GRACE + gap:
        return None, ""
    last = float(state.get("last_cycle_ts") or 0)
    ref = max(last, opened.timestamp())
    idle = now_ts - ref
    if idle > gap:
        lt = _et(last).strftime("%H:%M ET") if last >= opened.timestamp() else "오늘 없음"
        return "stale", f"{idle/60:.0f}분째 사이클 없음 (마지막 {lt}) — 맥 잠자기·앱 멈춤 확인"
    return None, ""


def check_and_alert(cfg: dict, process_alive: Optional[bool] = None,
                    push: Callable[[str], bool] = _push, now_ts: Optional[float] = None,
                    source: str = "앱") -> Optional[str]:
    """건강 검사 → 새 사건이면 경보 1회, 사건이 끝나면 복구 알림. 반환: 보낸 메시지."""
    now_ts = now_ts or time.time()

    import market_hours as mh
    market_open = mh.is_market_open(_et(now_ts))
    watching = bool(cfg.get("enabled", True) and cfg.get("auto_running", False))

    def f(d):
        kind, detail = health(cfg, d, now_ts, process_alive)
        inc = d.get("incident")
        if kind and not inc:
            d["incident"] = {"kind": kind, "since": now_ts, "detail": detail}
            return (f"⚠️ 자동매매 이상 감지 ({source})\n{detail}\n"
                    f"의도한 정지가 아니면 앱을 확인하세요. 복구되면 다시 알려드립니다.")
        if kind and inc and kind != inc.get("kind"):
            inc.update(kind=kind, detail=detail)
            return f"⚠️ 자동매매 상태 변경 ({source})\n{detail}"
        if not kind and inc:
            mins = (now_ts - float(inc.get("since", now_ts))) / 60
            d["incidents"].append({"kind": inc.get("kind"), "since": inc.get("since"),
                                   "until": now_ts, "day": _day_key(inc.get("since"))})
            d["incident"] = None
            if not market_open:
                return f"🔕 장 마감 — 이상 상태({inc.get('detail', inc.get('kind'))})가 {mins:.0f}분간 이어진 채 마감했습니다"
            if not watching:
                return "⏸ 자동매매가 정지돼 이상 감시를 멈춥니다"
            return f"✅ 자동매매 정상화 — {mins:.0f}분 만에 복구 ({inc.get('kind')})"
        return None
    msg = _update(f)
    if msg:
        push(msg)
    return msg


def check_offhours_fills(push: Callable[[str], bool] = _push) -> list:
    """장 마감·주말에 기록된 체결을 찾아 경보. 처음 실행 땐 기존 기록을 기준점으로만 삼는다."""
    import market_hours as mh
    from safe_store import safe_read_json
    from core import execution as EX
    orders = (safe_read_json(EX.ORDERS_FILE, default={"orders": []}) or {}).get("orders", [])
    tz = mh.now_et().tzinfo

    def f(d):
        last = d.get("offhours_checked")
        newest = orders[-1]["ts"] if orders else last
        if last is None:
            d["offhours_checked"] = newest or ""
            return []
        found = []
        for o in orders:
            ts = o.get("ts", "")
            if ts <= last:
                continue
            try:
                dt = datetime.fromisoformat(ts).astimezone(tz)   # 기록은 로컬(KST) 시각
            except Exception:
                continue
            if not mh.is_market_open(dt):
                found.append({"ts": ts, "ticker": o.get("ticker"), "side": o.get("side"),
                              "shares": o.get("shares"), "price": o.get("price"),
                              "source": o.get("source"), "day": dt.date().isoformat()})
        if newest:
            d["offhours_checked"] = newest
        d["offhours"].extend(found)
        return found
    found = _update(f)
    if found:
        lines = [f"🚨 장외 체결 감지 {len(found)}건 — 정규장 밖 체결(1단계 실패 조건)"]
        for o in found[:8]:
            side = "매수" if o["side"] == "buy" else "매도"
            lines.append(f"· {o['ticker']} {side} {o['shares']:g}주 @ ${float(o['price'] or 0):.2f} "
                         f"({o['ts'][5:16].replace('T', ' ')} KST, {o['source']})")
        lines.append("직접 주문(수동)이었다면 무시해도 됩니다.")
        push("\n".join(lines))
    return found


# ── 자산 스냅샷 (일일 리포트 기준선) ─────────────────────────────────────
def load_history() -> list:
    from safe_store import safe_read_json
    return (safe_read_json(_history_file(), default={"snapshots": []}) or {}).get("snapshots", [])


def save_snapshot(snap: dict):
    """같은 날짜는 교체, 날짜순 유지."""
    from safe_store import atomic_write_json
    with _locked():
        rows = [s for s in load_history() if s.get("date") != snap["date"]]
        rows.append(snap)
        rows.sort(key=lambda s: s["date"])
        atomic_write_json(_history_file(), {"snapshots": rows[-400:]})


def account_snapshot(day: str, paper: bool = True) -> dict:
    """시가평가 자산 (현금 + Σ수량×현재가) + SPY 종가. 실거래는 브로커 계좌 현금·자산 우선."""
    import paper_account as pa
    import portfolio
    import stock_browser as sb
    pm = portfolio.PortfolioManager(paper=paper)
    tks = list(pm.positions)
    try:
        px = sb.quote_prices(tks + ["SPY"])
    except Exception:
        px = {}
    pos = []
    mv = 0.0
    for t, p in pm.positions.items():
        cur = float(px.get(t) or p.entry_price)
        mv += p.shares * cur
        pos.append({"ticker": t, "shares": p.shares, "entry": p.entry_price, "cur": cur,
                    "pnl_pct": (cur - p.entry_price) / p.entry_price if p.entry_price else 0.0})
    equity = None
    if paper:
        cash = pa.cash()
    else:
        try:
            from broker import Broker
            acct = Broker(paper=False).get_account()
            cash, equity = float(acct.get("cash") or 0), float(acct.get("equity") or 0) or None
        except Exception:
            cash = pm.available_capital()
    return {"date": day, "equity": round(equity if equity else cash + mv, 2), "cash": round(cash, 2),
            "spy": float(px.get("SPY") or 0) or None, "positions": pos, "ts": time.time()}


# ── 리포트 본문 (순수 함수 — 테스트 대상) ─────────────────────────────────
def _pct(a, b):
    return (a / b - 1.0) if a and b else None


def _fmt_pct(x, plus=True):
    return "—" if x is None else (f"{x:+.1%}" if plus else f"{x:.1%}")


def _md(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.month}/{d.day}({_WD[(d.weekday() + 1) % 7]})"


def max_gap_minutes(ts_list: list, open_ts: float, close_ts: float) -> float:
    pts = sorted(t for t in ts_list if open_ts <= t <= close_ts)
    if not pts:
        return (close_ts - open_ts) / 60
    edges = [open_ts] + pts + [close_ts]
    return max(b - a for a, b in zip(edges, edges[1:])) / 60


def build_daily_report(day: str, snap: dict, prev: Optional[dict], base: Optional[dict],
                       fills: list, closed: list, ev_counts: Optional[dict],
                       day_stats: dict, offhours_n: int, mode: str = "모의") -> str:
    eq = snap["equity"]
    lines = [f"📊 일일 리포트 · {_md(day)} · {mode}"]
    if prev:
        r, s = _pct(eq, prev["equity"]), _pct(snap.get("spy"), prev.get("spy"))
        lines.append(f"총자산 ${eq:,.0f} · 오늘 {_fmt_pct(r)} (${eq - prev['equity']:+,.0f})")
        if s is not None and r is not None:
            lines.append(f"S&P 500 {_fmt_pct(s)} → 초과 {(r - s) * 100:+.1f}%p")
    else:
        lines.append(f"총자산 ${eq:,.0f}")
    if base and base is not prev and base.get("date") != day:
        r, s = _pct(eq, base["equity"]), _pct(snap.get("spy"), base.get("spy"))
        lines.append(f"누적({_md(base['date'])} 기준) {_fmt_pct(r)} · S&P 500 {_fmt_pct(s)}")
    buys = [f for f in fills if f.get("side") == "buy"]
    sells = [f for f in fills if f.get("side") == "sell"]
    realized = sum((t["exit_price"] - t["entry_price"]) * t["shares"] for t in closed)
    lines.append("─────")
    lines.append(f"체결 {len(fills)}건 · 매수 {len(buys)} · 매도 {len(sells)}"
                 + (f" · 실현 ${realized:+,.0f}" if closed else ""))
    rmap = {t["ticker"]: t for t in closed}
    for f in fills[:8]:
        if f.get("side") == "buy":
            lines.append(f"· 🔴 {f['ticker']} {f['shares']:g}주 @ ${f['price']:.2f}")
        else:
            t = rmap.get(f["ticker"])
            extra = (f" ({(t['exit_price'] / t['entry_price'] - 1):+.1%} · {t.get('reason', '')})"
                     if t and t.get("entry_price") else "")
            lines.append(f"· 🔵 {f['ticker']} {f['shares']:g}주 @ ${f['price']:.2f}{extra}")
    pos = sorted(snap.get("positions", []), key=lambda p: -p["pnl_pct"])
    lines.append(f"보유 {len(pos)}종목 · 현금 ${snap['cash']:,.0f}")
    if pos:
        lines.append("· " + " · ".join(f"{p['ticker']} {p['pnl_pct']:+.1%}" for p in pos[:6]))
    if ev_counts:
        lines.append(f"EV(마지막 사이클): 매수 {ev_counts.get('BUY', 0)} · 대기 {ev_counts.get('WAIT', 0)} · "
                     f"보유 {ev_counts.get('HOLD', 0)} · 매도 {ev_counts.get('SELL', 0)}")
    lines.append(f"운영: 사이클 {day_stats.get('ok', 0)}회 · 오류 {day_stats.get('err', 0)} · "
                 f"최대 공백 {day_stats.get('max_gap', 0):.0f}분 · 장외 체결 {offhours_n}"
                 + (" ✅" if offhours_n == 0 and not day_stats.get("err") else " ⚠️"))
    return "\n".join(lines)


def build_weekly_report(days: list, snaps: list, start: Optional[dict], base: Optional[dict],
                        closed: list, incidents_n: int, offhours_n: int, errors_n: int,
                        reports_n: int, mode: str = "모의") -> str:
    """days: 이번 주 거래일(ISO) 목록, snaps: 이번 주 스냅샷(날짜순), start: 주 시작 전 마지막 스냅샷."""
    end = snaps[-1] if snaps else None
    lines = [f"📅 주간 리포트 · {_md(days[0])}~{_md(days[-1])} · {mode}"]
    if end and start:
        r, s = _pct(end["equity"], start["equity"]), _pct(end.get("spy"), start.get("spy"))
        lines.append(f"총자산 ${end['equity']:,.0f} · 주간 {_fmt_pct(r)} · S&P 500 {_fmt_pct(s)}"
                     + (f" → 초과 {(r - s) * 100:+.1f}%p" if r is not None and s is not None else ""))
    if end and base and base.get("date") != (start or {}).get("date"):
        lines.append(f"누적({_md(base['date'])} 기준) {_fmt_pct(_pct(end['equity'], base['equity']))} · "
                     f"S&P 500 {_fmt_pct(_pct(end.get('spy'), base.get('spy')))}")
    series = [x["equity"] for x in ([start] if start else []) + snaps]
    if len(series) >= 2:
        peak, mdd = series[0], 0.0
        for v in series:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        lines.append(f"주간 최대낙폭 {mdd:.1%}")
    if closed:
        rets = [(t["exit_price"] / t["entry_price"] - 1, t["ticker"]) for t in closed if t.get("entry_price")]
        wins = sum(1 for r, _ in rets if r > 0)
        best, worst = max(rets), min(rets)
        lines.append(f"청산 {len(rets)}건 · 승률 {wins / len(rets):.0%} · 평균 {sum(r for r, _ in rets) / len(rets):+.1%} · "
                     f"최고 {best[1]} {best[0]:+.1%} · 최악 {worst[1]} {worst[0]:+.1%}")
    else:
        lines.append("청산 0건")
    ok = lambda c: "✅" if c else "⚠️"
    lines.append("1단계 점검: "
                 f"장외 체결 {offhours_n}건 {ok(offhours_n == 0)} · 중단 경보 {incidents_n}건 {ok(incidents_n == 0)} · "
                 f"사이클 오류 {errors_n} {ok(errors_n == 0)} · 일일 리포트 {reports_n}/{len(days)} {ok(reports_n >= len(days))}")
    return "\n".join(lines)


# ── 일일·주간 작업 ──────────────────────────────────────────────────────
def _session_bounds(day: str) -> tuple:
    import market_hours as mh
    d = date.fromisoformat(day)
    tz = mh.now_et().tzinfo
    o = datetime.combine(d, mh.OPEN_T)
    c = datetime.combine(d, mh.close_time(d))
    if hasattr(tz, "localize"):
        o, c = tz.localize(o), tz.localize(c)
    else:
        o, c = o.replace(tzinfo=tz), c.replace(tzinfo=tz)
    return o.timestamp(), c.timestamp()


def _fills_for(day: str) -> list:
    from safe_store import safe_read_json
    from core import execution as EX
    orders = (safe_read_json(EX.ORDERS_FILE, default={"orders": []}) or {}).get("orders", [])
    return [o for o in orders if o.get("date") == day]


def _closed_between(d0: str, d1: str, paper: bool = True) -> list:
    import portfolio
    from safe_store import safe_read_json
    f = portfolio.STATE_FILE.parent / ("trades_paper.json" if paper else "trades.json")
    rows = (safe_read_json(f, default={"trades": []}) or {}).get("trades", [])
    return [t for t in rows if d0 <= str(t.get("exit_date", "")) <= d1]


def _week_days(day: str) -> list:
    import market_hours as mh
    d = date.fromisoformat(day)
    monday = d - timedelta(days=d.weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(5)
            if mh.is_trading_day(monday + timedelta(days=i)) and monday + timedelta(days=i) <= d]


def is_last_trading_day_of_week(day: str) -> bool:
    import market_hours as mh
    d = date.fromisoformat(day)
    n = d + timedelta(days=1)
    while not mh.is_trading_day(n):
        n += timedelta(days=1)
    return n.isocalendar()[1] != d.isocalendar()[1]


def ensure_baseline(snap_fn: Optional[Callable[[str], dict]] = None):
    """기록이 비어 있으면 '직전 거래일 종가' 기준선 1개를 만든다(초기화 직후 호출)."""
    import market_hours as mh
    if load_history():
        return
    d = mh.now_et().date()
    if mh.is_market_open() or (mh.is_trading_day(d) and mh.now_et().time() < mh.OPEN_T):
        d -= timedelta(days=1)
    while not mh.is_trading_day(d):
        d -= timedelta(days=1)
    s = (snap_fn or account_snapshot)(d.isoformat())
    s["baseline"] = True
    save_snapshot(s)


def daily_job(cfg: dict, now_ts: Optional[float] = None, push: Callable[[str], bool] = _push,
              snap_fn: Optional[Callable[[str], dict]] = None, force: bool = False) -> Optional[str]:
    """그날(ET) 마감 + REPORT_DELAY 이후 1회: 스냅샷 저장 → 일일 리포트(+주 마지막 거래일 주간 리포트)."""
    import market_hours as mh
    now = _et(now_ts)
    day = now.date().isoformat()
    if not mh.is_trading_day(now.date()):
        return None
    _, close_ts = _session_bounds(day)
    if now.timestamp() < close_ts + REPORT_DELAY:
        return None
    st = load_state()
    if st.get("reported_daily") == day and not force:
        return None
    paper = bool(cfg.get("paper", True))
    mode = "모의" if paper else "실거래"
    hist = load_history()
    snap = (snap_fn or (lambda d: account_snapshot(d, paper)))(day)
    prev = next((s for s in reversed(hist) if s["date"] < day), None)
    base = hist[0] if hist else None
    save_snapshot(snap)
    open_ts, _ = _session_bounds(day)
    ds = dict(st.get("days", {}).get(day, {}))
    ds["max_gap"] = max_gap_minutes(ds.get("ts", []), open_ts, close_ts) if ds.get("ok") else 0
    try:
        from core import ev as _ev
        ev_counts = (_ev.load() or {}).get("counts")
    except Exception:
        ev_counts = None
    off_n = sum(1 for o in st.get("offhours", []) if o.get("day") == day)
    msg = build_daily_report(day, snap, prev, base, _fills_for(day), _closed_between(day, day, paper),
                             ev_counts, ds, off_n, mode)
    sent = push(msg)

    def mark(d):
        d["reported_daily"] = day
        d.setdefault("reports", {})[day] = bool(sent)
        keep = sorted(d["reports"])[-60:]
        d["reports"] = {k: d["reports"][k] for k in keep}
    _update(mark)

    if is_last_trading_day_of_week(day):
        weekly_job(day, cfg, push=push)
    return msg


def weekly_job(day: str, cfg: dict, push: Callable[[str], bool] = _push) -> str:
    days = _week_days(day)
    hist = load_history()
    snaps = [s for s in hist if days[0] <= s["date"] <= day]
    start = next((s for s in reversed(hist) if s["date"] < days[0]), None)
    base = hist[0] if hist else None
    st = load_state()
    inc_n = sum(1 for i in st.get("incidents", []) if days[0] <= str(i.get("day", "")) <= day)
    if st.get("incident"):
        inc_n += 1
    off_n = sum(1 for o in st.get("offhours", []) if days[0] <= o.get("day", "") <= day)
    err_n = sum(int(st.get("days", {}).get(d, {}).get("err", 0)) for d in days)
    rep_n = sum(1 for d in days if st.get("reports", {}).get(d))
    paper = bool(cfg.get("paper", True))
    msg = build_weekly_report(days, snaps, start, base, _closed_between(days[0], day, paper),
                              inc_n, off_n, err_n, rep_n, "모의" if paper else "실거래")
    push(msg)
    return msg


def preview_report(cfg: dict) -> str:
    """텔레그램 /report — 오늘(진행 중이면 지금까지) 리포트 미리보기. 스냅샷·발송 기록 안 남김."""
    import market_hours as mh
    now = mh.now_et()
    d = now.date()
    if not mh.is_trading_day(d) or now.time() < mh.OPEN_T:
        d -= timedelta(days=1)
        while not mh.is_trading_day(d):
            d -= timedelta(days=1)
    day = d.isoformat()
    hist = load_history()
    st = load_state()
    paper = bool(cfg.get("paper", True))
    snap = account_snapshot(day, paper)
    prev = next((s for s in reversed(hist) if s["date"] < day), None)
    base = hist[0] if hist else None
    open_ts, close_ts = _session_bounds(day)
    ds = dict(st.get("days", {}).get(day, {}))
    ds["max_gap"] = (max_gap_minutes(ds.get("ts", []), open_ts, min(close_ts, time.time()))
                     if ds.get("ok") else 0)
    try:
        from core import ev as _ev
        ev_counts = (_ev.load() or {}).get("counts")
    except Exception:
        ev_counts = None
    off_n = sum(1 for o in st.get("offhours", []) if o.get("day") == day)
    body = build_daily_report(day, snap, prev, base, _fills_for(day), _closed_between(day, day, paper),
                              ev_counts, ds, off_n, "모의" if paper else "실거래")
    return body.replace("📊 일일 리포트", "📊 리포트 미리보기", 1)


# ── 앱 백그라운드 루프 ──────────────────────────────────────────────────
_STARTED = {"on": False}


def tick():
    from core import control
    cfg = control.load_config()
    for fn in (lambda: check_offhours_fills(),
               lambda: check_and_alert(cfg, process_alive=None, source="앱"),
               lambda: daily_job(cfg)):
        try:
            fn()
        except Exception:
            pass


def start(interval: float = 60.0) -> bool:
    if _STARTED["on"]:
        return False
    _STARTED["on"] = True
    try:
        ensure_baseline()
    except Exception:
        pass

    def _loop():
        while True:
            tick()
            time.sleep(interval)
    threading.Thread(target=_loop, daemon=True, name="monitor").start()
    return True
