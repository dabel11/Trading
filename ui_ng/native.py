"""
NiceGUI 대시보드 PoC — 네이티브 창 런처 (pywebview/WKWebView).

기존 native_app.py가 'Streamlit 서버 subprocess + pywebview 창'이었다면,
이건 NiceGUI가 서버를 같은 프로세스 안에서 띄우고 창도 직접 연다.
→ 별도 서버 프로세스가 없으니 좀비/오펀 서버 문제 자체가 사라짐
  (창 닫으면 프로세스 종료 = 생명주기 일치).

실행:  ngvenv/bin/python -m ui_ng.native   (프로젝트 루트에서)
"""
from __future__ import annotations
import sys

# 페이지 정의/테마 등록 (import 시 @ui.page('/')와 head CSS가 붙는다)
from ui_ng import dashboard  # noqa: F401
from nicegui import ui


def _set_app_name(name: str = "AI 트레이딩"):
    """macOS 독/메뉴바 이름 강제 (기본 'Python' → 앱 이름). native_app.py에서 이식."""
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


if __name__ in {"__main__", "__mp_main__"}:
    _set_app_name()
    ui.run(
        native=True,
        window_size=(1440, 900),
        title="AI 트레이딩",
        dark=True,
        reload=False,
        favicon="📈",
    )
