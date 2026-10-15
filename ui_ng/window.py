"""
NiceGUI 대시보드 PoC — 네이티브 창 래퍼 (pywebview/WKWebView).

기존 native_app.py의 '검증된' 패턴을 그대로 이식:
  NiceGUI 서버(포트 8502)를 백그라운드로 띄우고 → pywebview 창에 표시.
NiceGUI 자체 native=True(멀티프로세싱) 대신 이 방식을 쓰는 이유:
  셸에서 분리 실행해도 창이 안정적으로 뜨고, native_app.py와 동일한 구조라
  나중에 런처 한 줄만 바꾸면 Streamlit→NiceGUI로 스위치됨.

실행:  ngvenv/bin/python -m ui_ng.window   (프로젝트 루트에서)
"""
from __future__ import annotations
import os, sys, time, socket, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8502
URL = f"http://127.0.0.1:{PORT}"


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


def _start_server():
    """NiceGUI 대시보드 서버(web, 포트 8502)를 백그라운드로 기동."""
    if _port_open(PORT):
        return None
    log = open(ROOT / "ui_ng" / "_server.log", "a")
    return subprocess.Popen(
        [sys.executable, "-m", "ui_ng.dashboard"],
        cwd=str(ROOT),
        env=dict(os.environ),   # 실제 장부 모드(기본). 개발 격리는 POC_ISOLATE=1 로 외부 지정.
        stdout=log, stderr=subprocess.STDOUT,
    )


def main():
    proc = _start_server()
    for _ in range(120):               # 최대 60초 대기
        if _port_open(PORT):
            break
        time.sleep(0.5)

    _set_app_name("AI 트레이딩")

    import webview
    win = webview.create_window(
        "AI 트레이딩",
        URL,
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
