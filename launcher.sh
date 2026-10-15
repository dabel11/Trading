#!/bin/bash
# AI 트레이딩 — macOS 런처
# NiceGUI 앱(ui_ng)을 pywebview 네이티브 창으로 실행 (구 Streamlit app.py 대체)

# 앱이 위치한 디렉터리 (.app/Contents/Resources/trade)
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1

PORT=8501
LOG="$HOME/Library/Logs/AITrading.log"
mkdir -p "$(dirname "$LOG")"

CANDIDATES=(
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3
  /usr/local/bin/python3
  /opt/homebrew/bin/python3
  "$(command -v python3)"
)

echo "=== $(date) 시작 ===" >> "$LOG"

# 1순위: nicegui가 이미 설치된 Python 선택
PY=""
for cand in "${CANDIDATES[@]}"; do
  if [ -x "$cand" ] && "$cand" -c "import nicegui" 2>/dev/null; then
    PY="$cand"; echo "nicegui 보유 Python: $PY" >> "$LOG"; break
  fi
done

# 없으면: 사용 가능한 첫 Python에 설치
if [ -z "$PY" ]; then
  for cand in "${CANDIDATES[@]}"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
  if [ -z "$PY" ]; then
    osascript -e 'display alert "Python 필요" message "Python 3가 설치되어 있지 않습니다. python.org 에서 설치 후 다시 실행하세요."'
    exit 1
  fi
  echo "패키지 설치 대상 Python: $PY" >> "$LOG"
  osascript -e 'display notification "필수 패키지를 설치합니다 (1~2분 소요)" with title "AI 트레이딩"'
  PKGS="nicegui plotly pandas numpy yfinance finnhub-python alpaca-py ta requests pywebview lxml"
  # PEP 668(외부관리 환경) 우회 + --user 폴백
  "$PY" -m pip install --user -q $PKGS >> "$LOG" 2>&1 \
    || "$PY" -m pip install --user --break-system-packages -q $PKGS >> "$LOG" 2>&1
fi

# pywebview 보장 (네이티브 창)
"$PY" -c "import webview" 2>/dev/null \
  || "$PY" -m pip install --user -q pywebview >> "$LOG" 2>&1 \
  || "$PY" -m pip install --user --break-system-packages -q pywebview >> "$LOG" 2>&1

echo "NiceGUI 네이티브 앱 실행 (ui_ng.window)" >> "$LOG"

# ui_ng/window.py 가 NiceGUI 서버 기동 + pywebview 네이티브 창 표시를 모두 담당.
# -m 실행이므로 반드시 프로젝트 루트($APP_DIR)에서 돌려야 ui_ng/백엔드 import 가 된다.
# arm64 강제: 유니버설 Python이 Rosetta 모드로 실행되면 arm64-only .so 로드 실패.
if arch -arm64 true 2>/dev/null; then
  exec arch -arm64 "$PY" -m ui_ng.window >> "$LOG" 2>&1
else
  exec "$PY" -m ui_ng.window >> "$LOG" 2>&1
fi
