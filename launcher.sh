#!/bin/bash
# AI 트레이딩 — macOS 런처
# NiceGUI 앱(ui_ng)을 전용 venv에서 pywebview 네이티브 창으로 실행 (구 Streamlit app.py 대체)
# 전용 venv(--system-site-packages)로 pandas/yfinance 등은 재사용하고 nicegui만 추가해
# 시스템 Python 의존성을 오염시키지 않는다.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1

LOG="$HOME/Library/Logs/AITrading.log"
mkdir -p "$(dirname "$LOG")"
echo "=== $(date) 시작 ===" >> "$LOG"

# ── 베이스 Python 선택 (백엔드 의존성 pandas 등 보유한 것 우선) ──
CANDIDATES=(
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3
  /usr/local/bin/python3
  /opt/homebrew/bin/python3
  "$(command -v python3)"
)
BASE_PY=""
for cand in "${CANDIDATES[@]}"; do
  if [ -x "$cand" ] && "$cand" -c "import pandas" 2>/dev/null; then
    BASE_PY="$cand"; break
  fi
done
if [ -z "$BASE_PY" ]; then
  for cand in "${CANDIDATES[@]}"; do [ -x "$cand" ] && BASE_PY="$cand" && break; done
fi
if [ -z "$BASE_PY" ]; then
  osascript -e 'display alert "Python 필요" message "Python 3가 설치되어 있지 않습니다. python.org 에서 설치 후 다시 실행하세요."'
  exit 1
fi
echo "베이스 Python: $BASE_PY" >> "$LOG"

# ── 전용 venv (최초 1회 생성, 시스템 패키지 재사용) ──
VENV="$HOME/Library/Application Support/AITrading/venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "venv 생성: $VENV" >> "$LOG"
  osascript -e 'display notification "최초 실행 준비 중입니다 (1~2분 소요)" with title "AI 트레이딩"'
  "$BASE_PY" -m venv --system-site-packages "$VENV" >> "$LOG" 2>&1
fi
PY="$VENV/bin/python"

# ── nicegui / pywebview 보장 ──
"$PY" -c "import nicegui" 2>/dev/null \
  || "$PY" -m pip install -q nicegui >> "$LOG" 2>&1
"$PY" -c "import webview" 2>/dev/null \
  || "$PY" -m pip install -q pywebview >> "$LOG" 2>&1

echo "NiceGUI 네이티브 앱 실행 (ui_ng.window)" >> "$LOG"
# -m 실행이므로 반드시 프로젝트 루트($APP_DIR)에서 돌려야 ui_ng/백엔드 import 가 된다.
# arm64 강제: 유니버설 Python이 Rosetta 모드로 실행되면 arm64-only .so 로드 실패.
if arch -arm64 true 2>/dev/null; then
  exec arch -arm64 "$PY" -m ui_ng.window >> "$LOG" 2>&1
else
  exec "$PY" -m ui_ng.window >> "$LOG" 2>&1
fi
