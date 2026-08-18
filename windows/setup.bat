@echo off
chcp 65001 >nul
title AI 트레이딩 - 설치
pushd "%~dp0.."

echo ============================================
echo    AI 트레이딩   -   윈도우 설치
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] Python 이 설치되어 있지 않습니다.
  echo.
  echo   1) https://www.python.org/downloads/ 에서 Python 3.11 이상 설치
  echo   2) 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크
  echo   3) 설치 후 이 setup.bat 을 다시 실행
  echo.
  pause
  popd
  exit /b 1
)

echo [1/3] 가상환경 생성 (.venv) ...
python -m venv .venv

echo [2/3] 패키지 설치 - 처음엔 수 분 걸립니다 ...
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [참고] 일부 패키지 설치가 실패했을 수 있습니다.
  echo        앱은 브라우저 모드로도 동작하니 그대로 진행해도 됩니다.
)

echo.
echo [3/3] 설치 완료!
echo.
echo   * 앱(차트/백테스트/매매) 실행 :  start_app.bat
echo   * 24시간 자동매매 봇만 실행   :  run_bot.bat
echo.
pause
popd
