"""
AI 트레이딩 — 네이티브 macOS 앱 래퍼.
Streamlit 서버를 백그라운드로 띄우고, pywebview(WKWebView)로
독에 자리잡는 자체 앱 창에 표시한다. 브라우저가 뜨지 않음.
"""
import os
import sys
import time
import socket
import threading
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PORT = 8501
URL = f"http://localhost:{PORT}"


def _log_path() -> Path:
    """OS별 로그 파일 경로 (맥: ~/Library/Logs, 그 외: 홈 디렉터리)."""
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "Logs" / "AITrading.log"
    else:
        p = Path.home() / "AITrading.log"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        p = APP_DIR / "AITrading.log"
    return p


def _set_app_name(name: str = "AI 트레이딩"):
    """macOS 메뉴바/독에 표시되는 앱 이름을 강제 설정 (기본 'Python' → 'AI 트레이딩')."""
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
    except Exception:
        pass


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_orphan_servers():
    """이 앱의 app.py를 돌리는 좀비 Streamlit 프로세스 정리.

    창이 비정상 종료되면 옛 서버가 포트 없이도 살아남아 세션 스레드(자동매매
    사이클 포함)를 계속 돌린다 — 새 창에서 토글을 꺼도 거래가 계속되는 버그의
    원인. 새로 뜨기 전에 같은 app.py를 물고 있는 기존 서버를 모두 종료한다."""
    if sys.platform.startswith("win"):
        return
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"streamlit run {APP_DIR / 'app.py'}"],
            capture_output=True, text=True).stdout.split()
        targets = [int(p) for p in out if p.isdigit() and int(p) != os.getpid()]
        for pid in targets:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
        # SIGTERM을 무시하고 살아남는 사례가 실제로 있었다 → 3초 뒤 강제 종료
        if targets:
            time.sleep(3)
            for pid in targets:
                try:
                    os.kill(pid, 9)
                except Exception:
                    pass
    except Exception:
        pass


def _start_streamlit():
    """Streamlit 서버를 백그라운드 프로세스로 기동 (이미 떠 있으면 스킵)."""
    if _port_open(PORT):
        return None
    # 포트는 닫혀 있는데 옛 서버 프로세스가 남아있는 좀비 상태 → 정리 후 기동
    _kill_orphan_servers()
    py = sys.executable
    proc = subprocess.Popen(
        [py, "-m", "streamlit", "run", str(APP_DIR / "app.py"),
         "--server.port", str(PORT),
         "--server.address", "127.0.0.1",  # 보안: 이 컴퓨터에서만 접속 가능 (외부 네트워크 노출 차단)
         "--server.headless", "true",
         "--server.runOnSave", "false",
         "--browser.gatherUsageStats", "false"],
        cwd=str(APP_DIR),
        stdout=open(_log_path(), "a"),
        stderr=subprocess.STDOUT,
    )
    return proc


def main():
    proc = _start_streamlit()

    # 서버가 응답할 때까지 대기 (최대 60초)
    for _ in range(120):
        if _port_open(PORT):
            break
        time.sleep(0.5)

    _set_app_name("AI 트레이딩")   # 메뉴바/독 이름 변경

    # pywebview 없거나 실패하면 기본 브라우저로 폴백 (Windows에서 견고)
    try:
        import webview
    except Exception:
        import webbrowser
        webbrowser.open(URL)
        print(f"[AI 트레이딩] 브라우저에서 열림: {URL}")
        print("창을 닫지 말고 이 콘솔을 켜두세요 (서버가 여기서 돕니다). 종료: Ctrl+C")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            if proc:
                proc.terminate()
        return

    # 네이티브 창 생성 — 독/작업표시줄에 자체 앱으로 표시됨
    window = webview.create_window(
        "AI 트레이딩",
        URL,
        width=1440,
        height=900,
        min_size=(1100, 700),
        background_color="#0B0B0F",
        text_select=True,
    )

    def _on_closed():
        # 창 닫으면 streamlit 서버도 종료 (내가 띄우지 않은 좀비 서버까지 정리)
        try:
            if proc:
                proc.terminate()
        except Exception:
            pass
        _kill_orphan_servers()

    window.events.closed += _on_closed
    webview.start()   # 메인 스레드 점유 → 앱 생명주기 = 창 생명주기


if __name__ == "__main__":
    main()
