#!/bin/bash
# AI 트레이딩 — macOS 런처
# Streamlit 서버를 띄우고 기본 브라우저로 자동 접속

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

# 1순위: streamlit이 이미 설치된 Python 선택
PY=""
for cand in "${CANDIDATES[@]}"; do
  if [ -x "$cand" ] && "$cand" -c "import streamlit" 2>/dev/null; then
    PY="$cand"; echo "streamlit 보유 Python: $PY" >> "$LOG"; break
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
  PKGS="streamlit plotly pandas numpy yfinance finnhub-python alpaca-py ta requests streamlit-autorefresh pywebview lxml"
  # PEP 668(외부관리 환경) 우회 + --user 폴백
  "$PY" -m pip install --user -q $PKGS >> "$LOG" 2>&1 \
    || "$PY" -m pip install --user --break-system-packages -q $PKGS >> "$LOG" 2>&1
fi

# pywebview 보장 (네이티브 창)
"$PY" -c "import webview" 2>/dev/null \
  || "$PY" -m pip install --user -q pywebview >> "$LOG" 2>&1 \
  || "$PY" -m pip install --user --break-system-packages -q pywebview >> "$LOG" 2>&1

echo "네이티브 앱 실행 (pywebview)" >> "$LOG"

# native_app.py 가 streamlit 서버 기동 + 네이티브 창 표시를 모두 담당.
# arm64 강제: 유니버설 Python이 Rosetta 모드로 실행되면 arm64-only .so 로드 실패.
if arch -arm64 true 2>/dev/null; then
  exec arch -arm64 "$PY" "$APP_DIR/native_app.py" >> "$LOG" 2>&1
else
  exec "$PY" "$APP_DIR/native_app.py" >> "$LOG" 2>&1
fi
