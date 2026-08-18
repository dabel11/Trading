"""
알림 모듈: 매수/매도 신호 발생 시 Slack, 카카오톡, 이메일로 알림 전송.

지원 채널:
  - Slack  : Incoming Webhook URL
  - 카카오톡: 카카오 REST API '나에게 보내기'
  - 이메일  : SMTP (Gmail 앱 비밀번호)
"""

import json
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

NOTIFY_CONFIG_FILE = Path(__file__).parent / "notify_config.json"


# ──────────────────────────────────────────────────────────────────────────────
# 설정 로드/저장
# ──────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if NOTIFY_CONFIG_FILE.exists():
        try:
            cfg = json.loads(NOTIFY_CONFIG_FILE.read_text())
            # 새 채널 기본값 병합
            cfg.setdefault("macos",    {"enabled": True})
            cfg.setdefault("telegram", {"enabled": False, "bot_token": "", "chat_id": ""})
            return cfg
        except Exception:
            pass
    return {
        "macos":    {"enabled": True},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "slack":    {"enabled": False, "webhook_url": ""},
        "kakao":    {"enabled": False, "access_token": ""},
        "email":    {"enabled": False, "smtp_host": "smtp.gmail.com",
                     "smtp_port": 587, "user": "", "password": "", "to": ""},
    }


def save_config(cfg: dict):
    NOTIFY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────────────────
# 메시지 포맷터
# ──────────────────────────────────────────────────────────────────────────────

def _format_buy(ticker: str, shares: int, price: float,
                score=None, cost: float = 0.0, source: str = "",
                balance=None) -> dict:
    """매수 알림. score=None(수동 주문)이면 스코어 표기 생략.
    balance 지정 시 '잔액 $X' 표기(구매 후 현재 계좌 잔액)."""
    now = datetime.now().strftime("%H:%M")
    parts = [f"{shares}주 @ ${price:.2f}"]
    if cost:
        parts.append(f"${cost:,.0f}")
    if source:
        parts.append(source)
    if score is not None and score >= 0:
        parts.append(f"스코어 {score:.0f}")
    parts.append(now)
    body = " · ".join(parts)
    if balance is not None:
        body += f"\n잔액 ${balance:,.0f}"
    return {
        "title": f"매수 · {ticker}",
        "body":  body,
        "color": "#00D084",
    }


def _format_sell(ticker: str, shares: int, price: float,
                 pnl_pct=None, reason: str = "", source: str = "") -> dict:
    """매도 알림. pnl_pct=None(수동 주문)이면 손익률 표기 생략."""
    now = datetime.now().strftime("%H:%M")
    reason_map = {
        "take_profit":   "익절",
        "stop_loss":     "손절",
        "trailing_stop": "트레일링 청산",
        "score_drop":    "신호 약화",
        "max_hold":      "보유기간 만료",
        "manual":        "수동 청산",
    }
    _key = reason.split(" ")[0] if reason else ""
    rlabel = reason_map.get(_key, reason or "")
    title = f"매도 · {ticker}"
    if pnl_pct is not None:
        arrow = "▲" if pnl_pct >= 0 else "▼"
        title += f"  {arrow}{abs(pnl_pct):.1%}"
    parts = [f"{shares}주 @ ${price:.2f}"]
    if rlabel:
        parts.append(rlabel)
    if source:
        parts.append(source)
    parts.append(now)
    return {
        "title": title,
        "body":  " · ".join(parts),
        "color": "#FF6B6B" if (pnl_pct or 0) >= 0 else "#3182F6",
    }


def _format_summary(scores: list[dict]) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    top3 = scores[:3]
    lines = [f"  {s['ticker']:5s} | {s['total']:.0f}점" for s in top3]
    return {
        "title": "🔍 일일 스캔 완료",
        "body": (
            f"스캔 종목: {len(scores)}개\n"
            f"TOP 3:\n" + "\n".join(lines) + f"\n시각: {now}"
        ),
        "color": "#3182F6",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Slack
# ──────────────────────────────────────────────────────────────────────────────

def _send_slack(msg: dict, webhook_url: str) -> bool:
    payload = {
        "attachments": [{
            "color":  msg["color"],
            "title":  msg["title"],
            "text":   msg["body"],
            "footer": "AI 트레이딩 봇",
        }]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 카카오톡 나에게 보내기
# REST API: POST https://kapi.kakao.com/v2/api/talk/memo/default/send
# access_token: https://developers.kakao.com → 내 앱 → 발급
# ──────────────────────────────────────────────────────────────────────────────

def _send_kakao(msg: dict, access_token: str) -> bool:
    template = {
        "object_type": "text",
        "text": f"{msg['title']}\n\n{msg['body']}",
        "link": {"web_url": "", "mobile_web_url": ""},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }
    try:
        r = requests.post(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            headers=headers,
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def get_kakao_token(client_id: str, redirect_uri: str, code: str) -> str:
    """인증 코드로 액세스 토큰 발급 (최초 1회만 필요)."""
    r = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type":   "authorization_code",
            "client_id":    client_id,
            "redirect_uri": redirect_uri,
            "code":         code,
        },
        timeout=10,
    )
    return r.json().get("access_token", "")


# ──────────────────────────────────────────────────────────────────────────────
# 이메일 (Gmail)
# ──────────────────────────────────────────────────────────────────────────────

def _send_email(msg: dict, cfg: dict) -> bool:
    try:
        mail = MIMEMultipart("alternative")
        mail["Subject"] = msg["title"]
        mail["From"]    = cfg["user"]
        mail["To"]      = cfg["to"]

        html = f"""
        <div style="font-family:sans-serif;max-width:480px;margin:auto;">
          <div style="background:{msg['color']};padding:12px 20px;border-radius:8px 8px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:1.1rem;">{msg['title']}</h2>
          </div>
          <div style="background:#1A1A1F;padding:20px;border-radius:0 0 8px 8px;color:#F2F4F6;">
            <pre style="font-family:sans-serif;margin:0;white-space:pre-wrap;">{msg['body']}</pre>
          </div>
        </div>
        """
        mail.attach(MIMEText(msg["body"], "plain"))
        mail.attach(MIMEText(html, "html"))

        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], cfg["to"], mail.as_string())
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# 데스크톱 알림 — OS별 네이티브 (macOS / Windows / Linux)
# ──────────────────────────────────────────────────────────────────────────────

def _send_desktop(msg: dict) -> bool:
    """현재 OS에 맞는 네이티브 데스크톱 알림 전송 (논블로킹)."""
    import sys
    if sys.platform == "darwin":
        return _send_macos(msg)
    if sys.platform.startswith("win"):
        return _send_windows(msg)
    return _send_linux(msg)


def _send_macos(msg: dict) -> bool:
    """osascript 으로 macOS 네이티브 알림 전송 (논블로킹)."""
    import subprocess
    title = msg["title"].replace('"', '\\"')
    body  = msg["body"].split("\n")[0].replace('"', '\\"')  # 첫 줄만
    script = (
        f'display notification "{body}" '
        f'with title "AI 트레이딩" subtitle "{title}" sound name "Funk"'
    )
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _send_windows(msg: dict) -> bool:
    """Windows 풍선 알림(System.Windows.Forms NotifyIcon) — 추가 설치 불필요."""
    import subprocess, tempfile, os
    title = msg["title"].replace("'", "''")
    body  = msg["body"].split("\n")[0].replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$n = New-Object System.Windows.Forms.NotifyIcon\n"
        "$n.Icon = [System.Drawing.SystemIcons]::Information\n"
        "$n.BalloonTipTitle = 'AI 트레이딩'\n"
        f"$n.BalloonTipText = '{title} - {body}'\n"
        "$n.Visible = $true\n"
        "$n.ShowBalloonTip(8000)\n"
        "Start-Sleep -Seconds 9\n"
        "$n.Dispose()\n"
    )
    try:
        fd, path = tempfile.mkstemp(suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ps)
        # CREATE_NO_WINDOW(0x08000000) → 콘솔 창 깜빡임 없음
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        return True
    except Exception:
        return False


def _send_linux(msg: dict) -> bool:
    """Linux notify-send (있으면). 없으면 조용히 실패."""
    import subprocess
    title = msg["title"]
    body  = msg["body"].split("\n")[0]
    try:
        subprocess.Popen(["notify-send", f"AI 트레이딩 · {title}", body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 텔레그램 Bot API
# ──────────────────────────────────────────────────────────────────────────────

def _telegram_send(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Telegram sendMessage. (성공여부, 사유) 반환.

    - 토큰/Chat ID 공백 제거 (붙여넣기 시 흔한 실수).
    - parse_mode 미사용(plain) → 마크다운 파싱 오류로 인한 실패 방지.
    - 실패 시 텔레그램 API의 description 을 그대로 사유로 돌려준다.
    """
    bot_token = (bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not bot_token or not chat_id:
        return False, "Bot Token 또는 Chat ID가 비어 있습니다"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=8,
        )
        if r.status_code == 200:
            return True, "성공"
        try:
            desc = r.json().get("description", r.text)
        except Exception:
            desc = r.text
        return False, f"{r.status_code} {desc}"
    except Exception as e:
        return False, f"네트워크 오류: {e}"


def _send_telegram(msg: dict, bot_token: str, chat_id: str) -> bool:
    """Telegram Bot API sendMessage (논블로킹 결과 bool)."""
    ok, _ = _telegram_send(bot_token, chat_id, f"{msg['title']}\n\n{msg['body']}")
    return ok


def telegram_test(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """설정 화면 진단용 — 테스트 메시지 전송 후 (성공여부, 사유) 반환."""
    return _telegram_send(
        bot_token, chat_id,
        "AI 트레이딩 알림 테스트 — 정상적으로 연결되었습니다.")


def telegram_get_chat_ids(bot_token: str) -> tuple[list[dict], str]:
    """getUpdates 로 최근 봇과 대화한 chat 들의 Chat ID 목록을 가져온다.

    사용자가 텔레그램에서 봇에게 메시지를 한 번 보낸 뒤 이 함수를 호출하면
    본인 Chat ID 가 자동으로 잡힌다. 반환: ([{chat_id, name}], 사유).
    """
    bot_token = (bot_token or "").strip()
    if not bot_token:
        return [], "Bot Token이 비어 있습니다"
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            timeout=8)
        if r.status_code != 200:
            try:
                desc = r.json().get("description", r.text)
            except Exception:
                desc = r.text
            return [], f"{r.status_code} {desc}"
        updates = r.json().get("result", []) or []
        chats: dict = {}
        for upd in updates:
            m = (upd.get("message") or upd.get("edited_message")
                 or upd.get("channel_post") or {})
            chat = m.get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            name = (chat.get("title")
                    or (str(chat.get("first_name", "")) + " "
                        + str(chat.get("last_name", ""))).strip()
                    or chat.get("username") or str(cid))
            chats[cid] = name
        if not chats:
            return [], ("최근 대화가 없습니다 — 텔레그램 앱에서 봇에게 "
                        "아무 메시지나 1개 보낸 뒤 다시 누르세요")
        return [{"chat_id": str(k), "name": v} for k, v in chats.items()], ""
    except Exception as e:
        return [], f"네트워크 오류: {e}"


def _dispatch(msg: dict, cfg: dict = None) -> dict[str, bool]:
    if cfg is None:
        cfg = load_config()
    results = {}
    # "macos" 키는 하위호환 — 실제로는 현재 OS의 데스크톱 알림(맥/윈/리눅스)
    if cfg.get("macos", {}).get("enabled", True):
        results["desktop"] = _send_desktop(msg)
    if cfg.get("telegram", {}).get("enabled") and cfg["telegram"].get("bot_token"):
        results["telegram"] = _send_telegram(
            msg, cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    if cfg.get("slack", {}).get("enabled") and cfg["slack"].get("webhook_url"):
        results["slack"] = _send_slack(msg, cfg["slack"]["webhook_url"])
    if cfg.get("kakao", {}).get("enabled") and cfg["kakao"].get("access_token"):
        results["kakao"] = _send_kakao(msg, cfg["kakao"]["access_token"])
    if cfg.get("email", {}).get("enabled") and cfg["email"].get("user"):
        results["email"] = _send_email(msg, cfg["email"])
    return results


def notify_buy(ticker: str, shares: int, price: float,
               score=None, cost: float = 0.0, source: str = "",
               balance=None) -> dict[str, bool]:
    return _dispatch(_format_buy(ticker, shares, price, score, cost, source, balance))


def notify_sell(ticker: str, shares: int, price: float,
                pnl_pct=None, reason: str = "", source: str = "") -> dict[str, bool]:
    return _dispatch(_format_sell(ticker, shares, price, pnl_pct, reason, source))


def notify_scan_summary(scores: list[dict]) -> dict[str, bool]:
    return _dispatch(_format_summary(scores))


def test_all(cfg: dict) -> dict[str, bool]:
    """설정 페이지에서 '테스트 전송' 버튼 클릭 시 호출."""
    msg = {
        "title": "✅ AI 트레이딩 알림 테스트",
        "body":  "알림 채널이 정상적으로 연결되었습니다.\n연결 시각: " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        "color": "#3182F6",
    }
    return _dispatch(msg, cfg)
