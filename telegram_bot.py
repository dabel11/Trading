"""
텔레그램 봇 — 명령어로 트레이딩 현황 조회.

명령어:
  /ping       봇·트레이딩 정상 동작 확인 (빠른 헬스체크)
  /status     전체 현황 (모드·전략·자동매매·자산·마지막 사이클)
  /balance    잔액 (총자산·현금·투자중·평가손익)
  /portfolio  보유 종목 목록 + 손익
  /positions  /portfolio 와 동일
  /scores     현재 상위 점수 후보 (매수 검토 종목)
  /buy T 500  T 종목을 $500어치 매수 (/buy T 3주 = 3주) → /confirm 으로 확정
  /sell T     T 전량 매도 (/sell T 2 = 2주만)          → /confirm 으로 확정
  /confirm    대기 중인 주문 확정 실행  · /cancel 취소
  /help       명령어 목록

상태 조회는 bot_status.json(앱·데몬이 기록)만 읽고, 매매는 장부 모듈을 직접
호출한다(앱과 동일 경로). 보안: 설정된 Chat ID 에만 응답·매매 허용,
주문은 미리보기 → /confirm 2단계(90초 내).
"""
import json
import time
import threading
from pathlib import Path

import requests

DIR = Path(__file__).resolve().parent
STATUS_FILE = DIR / "bot_status.json"

_API = "https://api.telegram.org/bot{token}/{method}"
_stop = threading.Event()
_started = False


# ─────────────────────────────────────────────────────────────── 상태 읽기

def _status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def _age_str(ts: float) -> str:
    if not ts:
        return "기록 없음"
    d = max(0, int(time.time() - ts))
    if d < 60:   return f"{d}초 전"
    if d < 3600: return f"{d//60}분 전"
    return f"{d/3600:.1f}시간 전"


# ─────────────────────────────────────────────────────────── 명령어 포맷

def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except Exception:
        return "—"


def cmd_ping(s: dict) -> str:
    on = s.get("auto_on")
    run = _age_str(s.get("updated_ts", 0))
    mk = "개장" if s.get("market_open") else "마감"
    line = "🟢 정상 작동" if s else "⚠️ 상태 파일 없음 (앱 실행 중인지 확인)"
    auto = ("자동매매 ON" if on else "자동매매 OFF")
    return (f"{line}\n"
            f"· {auto} · 시장 {mk}\n"
            f"· 마지막 갱신 {run}\n"
            f"· 마지막 사이클 {s.get('last_run','—')}")


def cmd_status(s: dict) -> str:
    if not s:
        return "상태 정보가 없습니다. 앱이 실행 중이고 자동매매가 한 번 돌았는지 확인하세요."
    up = s.get("upnl", 0)
    arrow = "▲" if up >= 0 else "▼"
    eng = "🧠 AI 인플루언서" if s.get("engine") == "ai_influencer" else s.get("strategy", "—")
    return (
        f"📊 트레이딩 현황\n"
        f"모드: {s.get('mode','—')} · 엔진: {eng}\n"
        f"기간: {s.get('horizon','—')} · 배분: {s.get('alloc','—')}\n"
        f"자동매매: {'ON' if s.get('auto_on') else 'OFF'} · 시장 {'개장' if s.get('market_open') else '마감'}\n"
        f"\n"
        f"총자산 {_fmt_money(s.get('equity'))} · 현금 {_fmt_money(s.get('cash'))}\n"
        f"투자중 {_fmt_money(s.get('invested'))} · 평가손익 {arrow}{_fmt_money(abs(up))} "
        f"({s.get('upnl_pct',0):+.2%})\n"
        f"보유 {s.get('n_positions',0)}종목\n"
        f"\n마지막 사이클: {s.get('last_run','—')} ({_age_str(s.get('updated_ts',0))})"
    )


def cmd_balance(s: dict) -> str:
    up = s.get("upnl", 0); arrow = "▲" if up >= 0 else "▼"
    return (f"💰 잔액 ({s.get('mode','—')})\n"
            f"총자산 {_fmt_money(s.get('equity'))}\n"
            f"현금 {_fmt_money(s.get('cash'))}\n"
            f"투자중 {_fmt_money(s.get('invested'))}\n"
            f"평가손익 {arrow}{_fmt_money(abs(up))} ({s.get('upnl_pct',0):+.2%})")


def cmd_portfolio(s: dict) -> str:
    pos = s.get("positions") or []
    if not pos:
        return "보유 종목이 없습니다."
    lines = [f"📁 보유 {len(pos)}종목 ({s.get('mode','—')})"]
    for p in pos:
        pp = p.get("pnl_pct", 0); ar = "▲" if pp >= 0 else "▼"
        lines.append(f"· {p['ticker']} {p.get('shares',0):.0f}주 @ ${p.get('entry',0):,.2f} "
                     f"→ ${p.get('cur',0):,.2f} {ar}{abs(pp):.1%}")
    lines.append(f"\n평가액 합계 {_fmt_money(s.get('invested'))}")
    return "\n".join(lines)


def cmd_scores(s: dict) -> str:
    top = s.get("top_scores") or []
    if not top:
        return "아직 채점된 후보가 없습니다 (자동매매가 한 사이클 돌면 표시)."
    th = s.get("buy_th", 60)
    lines = [f"🎯 상위 점수 후보 (매수문턱 {th})"]
    for r in top[:10]:
        sc = r.get("score", 0)
        sig = "매수" if sc >= th else "관망" if sc >= th-10 else "약세"
        lines.append(f"· {r['ticker']:<5} {sc:>4.0f}  {sig}")
    return "\n".join(lines)


def cmd_ai(args: list, s: dict) -> str:
    """AI 인플루언서 엔진 — 인자 없으면 현황, 티커 주면 즉시 실시간 판단."""
    buy_th = s.get("buy_th", 60)
    if args:
        tk = args[0].upper().lstrip("$")
        try:
            from influence import engine as eng
            v = eng.verdict_for(tk, buy_th=buy_th)
        except Exception as e:
            return f"AI 판단 불가: {e}"
        emoji = {"BUY": "🟢 매수", "SELL": "🔴 매도", "HOLD": "⚪ 관망"}.get(v.action, "·")
        lines = [f"🧠 AI 판단 · {tk}",
                 f"{emoji}  확신점수 {v.score:.0f}/100 (확신도 {int(v.conviction*100)}%)",
                 "", "· 공신력 소스별 의견"]
        if v.evidence:
            for e in sorted(v.evidence, key=lambda x: -x.weight):
                ar = "▲" if e.stance > 0.1 else ("▼" if e.stance < -0.1 else "─")
                lines.append(f"  {ar} {e.source_label}: {e.detail} (공신력 {int(e.credibility*100)})")
        else:
            lines.append("  (수집된 근거 없음)")
        lines.append("")
        lines.append(f"엔진: {'LLM' if v.by == 'llm' else '규칙(공신력 가중)'} · {v.rationale}")
        return "\n".join(lines)

    ai = s.get("ai") or {}
    try:
        from influence import roster
        active = ", ".join(x["label"] for x in roster.summary() if x["active"])
    except Exception:
        active = "—"
    lines = ["🧠 AI 인플루언서 엔진",
             f"활성 소스: {active}",
             f"판단기: {'LLM' if ai.get('llm_active') else '규칙(공신력 가중)'}"]
    vs = ai.get("verdicts") or []
    if vs:
        lines.append(f"\n최신 판단 · 국면 {ai.get('regime', '-')}  (매수문턱 {buy_th})")
        for v in vs[:8]:
            tag = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(v.get("action"), "·")
            lines.append(f"{tag} {v['ticker']:<5} {v['score']:>4.0f}  {v.get('action')}")
    else:
        lines.append("\n아직 판단 없음 — 자동매매가 한 사이클 돌면 표시됩니다.")
    lines.append("\n개별 종목 즉시 판단: /ai AAPL")
    return "\n".join(lines)


def cmd_help(_s: dict) -> str:
    return ("🤖 AI 트레이딩 봇 명령어\n"
            "/id — 내 Chat ID 확인(최초 설정용)\n"
            "/ping — 정상 동작 확인\n"
            "/status — 전체 현황\n"
            "/ai — AI 인플루언서 판단 현황 (/ai AAPL = 종목 즉시 판단)\n"
            "/balance — 잔액·손익\n"
            "/portfolio — 보유 종목\n"
            "/scores — 상위 점수 후보\n"
            "─ 모드 ─\n"
            "/mode — 현재 모드 조회\n"
            "/모의 (=/mode paper) — 모의투자 전환\n"
            "/실거래 (=/mode live) — 실거래 전환\n"
            "─ 매매 ─\n"
            "/buy AAPL 500 — $500어치 매수\n"
            "/buy AAPL 3주 — 3주 매수\n"
            "/sell AAPL — 전량 매도 (/sell AAPL 2 = 2주)\n"
            "/confirm — 주문 확정 · /cancel — 취소\n"
            "/help — 도움말")


# ─────────────────────────────────────────────────────────── 매매 실행

_pending: dict = {}        # chat_id → {side, ticker, shares, est_price, paper, ts}
_PENDING_TTL = 90          # 초 — 미확정 주문 자동 만료


def _quote(tk: str):
    """현재가 조회 — 실시간 피드 캐시 우선, 없으면 yfinance."""
    try:
        import realtime_feed as rtf
        d = rtf.get_price(tk)
        if d and d.get("price"):
            return float(d["price"])
    except Exception:
        pass
    try:
        import yfinance as yf
        p = yf.Ticker(tk).fast_info.last_price
        return float(p) if p else None
    except Exception:
        return None


def _cfg_paper() -> bool:
    """현재 매매 모드 — config(autotrader_config.json)의 paper 플래그가 단일 소스."""
    try:
        from core.control import load_config
        return bool(load_config().get("paper", True))
    except Exception:
        return bool(_status().get("paper", True))


def _set_paper(paper: bool):
    """모드 전환 — config 저장 + 상태 파일 즉시 반영."""
    try:
        from core.control import load_config, save_config
        cfg = load_config()
        cfg["paper"] = bool(paper)
        save_config(cfg)
    except Exception:
        pass
    try:
        s = _status()
        s["paper"] = bool(paper)
        s["mode"] = "모의(페이퍼)" if paper else "실거래"
        write_status(s)
    except Exception:
        pass


def cmd_mode(args: list) -> str:
    """/mode (조회) · /mode paper (모의) · /mode live (실거래)."""
    cur = _cfg_paper()
    if not args:
        return (f"현재 모드: {'🟢 모의투자(페이퍼)' if cur else '🔴 실거래'}\n"
                "전환: /mode paper (모의) · /mode live (실거래)\n"
                "단축: /모의 · /실거래")
    a = args[0].lower().strip()
    if a in ("paper", "모의", "sim", "demo"):
        _set_paper(True)
        return "🟢 모의투자(페이퍼) 모드로 전환했습니다.\n이후 모든 매매는 가상 현금으로 체결됩니다."
    if a in ("live", "실거래", "real"):
        _set_paper(False)
        warn = ""
        try:
            import config as _c
            if "your_" in _c.ALPACA_API_KEY:
                warn = "\n⚠️ Alpaca API 키가 없어 실제 주문은 실패합니다 (.env 설정 필요)."
        except Exception:
            pass
        return ("🔴 실거래 모드로 전환했습니다.\n이후 매매는 **실제 계좌·실제 자금**으로 체결됩니다 — 신중히!"
                + warn)
    return "사용법: /mode paper (모의) · /mode live (실거래)"


def cmd_buy(args: list, chat_id: str) -> str:
    """/buy TICKER 500  ($500어치) · /buy TICKER 3주 (3주)."""
    if len(args) < 2:
        return "사용법: /buy AAPL 500  ($500어치)\n또는 /buy AAPL 3주"
    tk = args[0].upper().strip()
    amt = args[1].replace("$", "").replace(",", "").strip()
    price = _quote(tk)
    if not price:
        return f"{tk} 시세를 가져올 수 없습니다 — 티커를 확인하세요"
    if amt.endswith("주") or amt.lower().endswith("s"):
        try:
            qty = int(float(amt.rstrip("주sS")))
        except Exception:
            return "수량을 해석할 수 없습니다 (예: 3주)"
    else:
        try:
            qty = int(float(amt) / price)
        except Exception:
            return "금액을 해석할 수 없습니다 (예: 500)"
    if qty < 1:
        return f"금액이 1주 가격(${price:,.2f})보다 작습니다"
    paper = _cfg_paper()
    _pending[chat_id] = {"side": "buy", "ticker": tk, "shares": qty,
                         "est_price": price, "paper": paper, "ts": time.time()}
    return (f"📥 매수 주문 미리보기 ({'모의' if paper else '⚠️ 실거래'})\n"
            f"{tk} {qty}주 @ ~${price:,.2f} = ${qty*price:,.0f}\n\n"
            f"90초 내 /confirm 으로 확정 · /cancel 취소")


def cmd_sell(args: list, chat_id: str) -> str:
    """/sell TICKER (전량) · /sell TICKER 2 (2주)."""
    if not args:
        return "사용법: /sell AAPL  (전량)\n또는 /sell AAPL 2  (2주)"
    tk = args[0].upper().strip()
    paper = _cfg_paper()
    try:
        from portfolio import PortfolioManager
        pos = PortfolioManager(paper=paper).positions.get(tk)
    except Exception:
        pos = None
    if not pos:
        return f"{tk} — 보유하지 않은 종목입니다 (/portfolio 로 확인)"
    qty = pos.shares
    if len(args) >= 2:
        try:
            qty = min(int(float(args[1].rstrip("주sS"))), int(pos.shares))
        except Exception:
            return "수량을 해석할 수 없습니다 (예: /sell AAPL 2)"
    price = _quote(tk) or pos.entry_price
    pnl = (price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
    _pending[chat_id] = {"side": "sell", "ticker": tk, "shares": qty,
                         "est_price": price, "paper": paper, "ts": time.time()}
    return (f"📤 매도 주문 미리보기 ({'모의' if paper else '⚠️ 실거래'})\n"
            f"{tk} {qty:.0f}주 @ ~${price:,.2f} = ${qty*price:,.0f}\n"
            f"예상 손익 {pnl:+.2%}\n\n"
            f"90초 내 /confirm 으로 확정 · /cancel 취소")


def cmd_confirm(chat_id: str) -> str:
    p = _pending.pop(chat_id, None)
    if not p:
        return "대기 중인 주문이 없습니다 (/buy 또는 /sell 먼저)"
    if time.time() - p["ts"] > _PENDING_TTL:
        return "주문이 만료됐습니다(90초 초과) — 다시 시도하세요"
    tk, side, paper = p["ticker"], p["side"], p["paper"]
    qty = int(p["shares"])
    price = _quote(tk) or p["est_price"]
    try:
        import paper_account as pa
        from core.execution import execute_manual
        r = execute_manual(tk, qty, side, paper, est_price=price)
        bal = pa.cash() if paper else None
        if side == "buy":
            return (f"✅ 매수 체결 · {tk} {r['shares']}주 @ ${r['price']:,.2f} "
                    f"(${r['shares']*r['price']:,.0f})"
                    + (f"\n잔액 ${bal:,.0f}" if bal is not None else ""))
        return (f"✅ 매도 체결 · {tk} {r['shares']}주 @ ${r['price']:,.2f} "
                f"({r.get('pnl_pct', 0):+.2%})"
                + (f"\n잔액 ${bal:,.0f}" if bal is not None else ""))
    except RuntimeError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ 주문 실패: {e}"


def cmd_cancel(chat_id: str) -> str:
    return ("주문 취소됨" if _pending.pop(chat_id, None) else "대기 중인 주문이 없습니다")


_HANDLERS = {
    "ping": cmd_ping, "status": cmd_status, "balance": cmd_balance,
    "portfolio": cmd_portfolio, "positions": cmd_portfolio,
    "scores": cmd_scores, "score": cmd_scores, "help": cmd_help, "start": cmd_help,
}

_TRADE_CMDS = {"buy", "sell", "confirm", "cancel"}
_MODE_CMDS = {"mode", "paper", "live", "모의", "실거래"}
_allowed_chat = ""   # start()에서 설정 — 매매 명령은 이 채팅에서만


def handle(text: str, chat_id: str = "") -> str:
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]
    if cmd in ("id", "chatid", "myid", "아이디"):
        # 제한 없음 — Chat ID 확인용(설정에 입력)
        return (f"🆔 이 채팅의 Chat ID\n{chat_id}\n\n"
                "앱 ⚙️ 설정 → 텔레그램 봇 → Chat ID 칸에 위 숫자를 입력하고 저장하세요.\n"
                "그러면 /buy /sell /mode 등 매매·모드 명령이 열립니다.")
    if cmd in ("ai", "에이아이", "판단"):
        # 읽기 전용 — 제한 없음
        return cmd_ai(args, _status())
    if cmd in _MODE_CMDS:
        if not _allowed_chat or chat_id != _allowed_chat:
            return "⛔ 모드 전환은 설정된 Chat ID에서만 가능합니다 (설정 → 알림)."
        if cmd in ("paper", "모의"):
            return cmd_mode(["paper"])
        if cmd in ("live", "실거래"):
            return cmd_mode(["live"])
        return cmd_mode(args)
    if cmd in _TRADE_CMDS:
        # 매매는 Chat ID가 명시 설정된 경우에만 (오픈 채팅 매매 차단)
        if not _allowed_chat or chat_id != _allowed_chat:
            return "⛔ 매매 명령은 설정된 Chat ID에서만 가능합니다 (설정 → 알림에서 Chat ID 확인)"
        if cmd == "buy":
            return cmd_buy(args, chat_id)
        if cmd == "sell":
            return cmd_sell(args, chat_id)
        if cmd == "confirm":
            return cmd_confirm(chat_id)
        return cmd_cancel(chat_id)
    fn = _HANDLERS.get(cmd)
    if not fn:
        return f"알 수 없는 명령: /{cmd}\n/help 로 명령어 목록 확인"
    return fn(_status())


# ─────────────────────────────────────────────────────────── 폴링 루프

def _get_updates(token: str, offset):
    try:
        r = requests.get(_API.format(token=token, method="getUpdates"),
                         params={"timeout": 25, "offset": offset}, timeout=30)
        if r.status_code == 200:
            return r.json().get("result", []) or []
    except Exception:
        pass
    return []


def _send(token: str, chat_id: str, text: str):
    try:
        requests.post(_API.format(token=token, method="sendMessage"),
                      json={"chat_id": chat_id, "text": text,
                            "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass


def poll_loop(token: str, allowed_chat_id: str = ""):
    """롱폴링으로 명령 수신·응답. allowed_chat_id 가 있으면 그 채팅에만 응답(보안)."""
    token = (token or "").strip()
    allowed = str(allowed_chat_id or "").strip()
    if not token:
        return
    offset = None
    # 시작 시점 이전의 밀린 메시지는 건너뛴다(getUpdates offset 초기화)
    try:
        _init = _get_updates(token, None)
        if _init:
            offset = _init[-1]["update_id"] + 1
    except Exception:
        pass
    while not _stop.is_set():
        for u in _get_updates(token, offset):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not text.startswith("/"):
                continue
            if allowed and chat_id != allowed:
                continue   # 설정된 채팅이 아니면 무시(보안)
            try:
                _send(token, chat_id, handle(text, chat_id))
            except Exception:
                pass
        _stop.wait(1.5)


def start(token: str, allowed_chat_id: str = "") -> bool:
    """봇 폴링 스레드를 1회만 기동. 이미 떠 있으면 무시."""
    global _started, _allowed_chat
    if _started:
        return False
    if not (token or "").strip():
        return False
    _allowed_chat = str(allowed_chat_id or "").strip()
    _stop.clear()
    th = threading.Thread(target=poll_loop, args=(token, allowed_chat_id), daemon=True)
    th.start()
    _started = True
    return True


def write_status(data: dict):
    """앱이 호출 — 현재 상태 스냅샷을 디스크에 기록(봇이 읽음)."""
    try:
        data = dict(data)
        data["updated_ts"] = time.time()
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass
