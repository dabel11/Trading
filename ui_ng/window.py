"""
NiceGUI 대시보드 PoC — 네이티브 창 래퍼 (pywebview/WKWebView).

기존 native_app.py의 '검증된' 패턴을 그대로 이식:
  NiceGUI 서버(포트 8502)를 백그라운드로 띄우고 → pywebview 창에 표시.
NiceGUI 자체 native=True(멀티프로세싱) 대신 이 방식을 쓰는 이유:
  셸에서 분리 실행해도 창이 안정적으로 뜨고, native_app.py와 동일한 구조라
  나중에 런처 한 줄만 바꾸면 Streamlit→NiceGUI로 스위치됨.

실행:  ngvenv/bin/python -m ui_ng.window   (프로젝트 루트에서)

원격 모드: 매매를 서버로 옮긴 뒤엔 프로젝트 루트의 `.remote_url`(또는 환경변수
TRADE_REMOTE_URL)에 서버 주소를 적어 둔다. 그러면 로컬 서버를 띄우지 않고 그 주소만
창에 연다 — 맥과 서버가 동시에 매매·텔레그램 봇을 돌리는 일을 막는다.
"""
from __future__ import annotations
import os, sys, time, socket, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8502
URL = f"http://127.0.0.1:{PORT}"
REMOTE_FILE = ROOT / ".remote_url"


def _remote_url():
    """원격 모드면 서버 주소(아직 안 적었으면 ""), 로컬 모드면 None.
    .remote_url 파일이 '있기만 해도' 원격 모드 — 로컬 서버(=매매 루프·텔레그램 봇)를 절대 띄우지 않는다."""
    url = os.environ.get("TRADE_REMOTE_URL", "").strip()
    if url:
        return url
    try:
        lines = REMOTE_FILE.read_text().splitlines()
    except OSError:
        return None
    return next((ln.strip() for ln in lines
                 if ln.strip() and not ln.strip().startswith("#")), "")


_NO_URL_HTML = """<body style="background:#0B0B0F;color:#ddd;font:15px -apple-system;padding:48px">
<h2>매매는 서버에서 돌고 있습니다</h2>
<p>이 맥은 원격 모드입니다(<code>~/trade/.remote_url</code>). 서버 화면 주소를 그 파일에 적으면
이 창이 서버 화면을 엽니다. 텔레그램 봇으로도 상태를 볼 수 있습니다.</p></body>"""


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _set_app_name(name: str = "AI 트레이딩"):
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
    except Exception:
        pass


def _rotated(path: Path, max_bytes: int = 5 * 1024 * 1024) -> Path:
    """기동 시 로그가 max_bytes 를 넘으면 .1 로 밀어내고(1세대 보관) 새로 시작."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    return path


def _start_server():
    """NiceGUI 대시보드 서버(web, 포트 8502)를 백그라운드로 기동."""
    if _port_open(PORT):
        return None
    try:                                  # 서버(자식)가 물려받도록 먼저 한도 상향 —
        sys.path.insert(0, str(ROOT))     # launchd 로 뜬 앱의 기본 파일 한도는 256
        import yf_compat
        yf_compat.raise_fd_limit()
    except Exception:
        pass
    log = open(_rotated(ROOT / "ui_ng" / "_server.log"), "a")
    return subprocess.Popen(
        [sys.executable, "-m", "ui_ng.dashboard"],
        cwd=str(ROOT),
        env=dict(os.environ),   # 실제 장부 모드(기본). 개발 격리는 POC_ISOLATE=1 로 외부 지정.
        stdout=log, stderr=subprocess.STDOUT,
    )


def main():
    remote = _remote_url()
    proc = None
    if remote is None:
        proc = _start_server()
        for _ in range(120):               # 최대 60초 대기
            if _port_open(PORT):
                break
            time.sleep(0.5)

    _set_app_name("AI 트레이딩")

    import webview
    target = URL if remote is None else (remote or None)
    win = webview.create_window(
        "AI 트레이딩",
        target,
        html=None if target else _NO_URL_HTML,
        width=1440, height=900,
        min_size=(1100, 700),
        background_color="#0B0B0F",
        text_select=True,
    )

    def _on_closed():
        try:
            if proc:
                proc.terminate()
        except Exception:
            pass

    def _activate():
        # .app 래퍼 스크립트로 띄우면 자식 python 창이 전면 활성화가 안 돼
        # WKWebView가 페인트를 멈춰 '빈 창'처럼 보인다 → 명시적으로 전면 활성화.
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass

    win.events.shown += _activate
    win.events.closed += _on_closed
    webview.start()                     # 메인 스레드 점유 = 창 생명주기


if __name__ == "__main__":
    main()
