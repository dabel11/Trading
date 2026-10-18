# legacy — 구 Streamlit UI (보관용, 실행 경로에서 제외)

2026-10 NiceGUI(`ui_ng/`) 이전 완료로 실행 경로에서 빠진 파일들. 앱·데몬·테스트
어느 쪽에서도 import 되지 않고 CLI 진입점도 아님을 확인한 뒤 옮겼다(git 이력 보존).
(`engine.py` 는 scan/backtest CLI 라 루트에 그대로 둔다.)

| 파일 | 원래 역할 |
|---|---|
| `app.py` | Streamlit 단일 파일 UI (5,900줄) — `ui_ng/dashboard.py` 로 대체 |
| `native_app.py` | Streamlit 서버 + pywebview 래퍼 — `ui_ng/window.py` 로 대체 |
| `market_analyzer.py` | app.py 전용 시장 분석 모듈 |
| `strategies/` | app.py 전용 전략 클래스 — 현재는 `strategy_catalog.py` + `backtester._strategy_score_bt` |
| `ui_ng/native.py` | NiceGUI `native=True` 실험본 (window.py 방식 채택) |
| `start_app.bat` | 윈도우용 Streamlit 앱 실행기 |

실행하려면 프로젝트 루트로 되돌려야 한다(`git mv legacy/app.py .` 등).
필요 패키지는 `requirements-legacy.txt`.
