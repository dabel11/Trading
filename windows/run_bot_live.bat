@echo off
chcp 65001 >nul
title AI 트레이딩 - 자동매매 봇 (24시간, 실거래)
pushd "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [안내] 먼저 setup.bat 을 실행해 설치하세요.
  echo.
  pause
  popd
  exit /b 1
)

echo ============================================
echo    !! 실거래 자동매매 봇 - 실제 자금이 거래됩니다 !!
echo.
echo    Alpaca API 키가 .env 에 설정돼 있어야 동작합니다.
echo    정말 실제 자금으로 돌리시겠습니까?
echo ============================================
echo.
set /p OK="실거래로 시작하려면 YES 입력 후 Enter: "
if /I not "%OK%"=="YES" (
  echo 취소되었습니다.
  pause
  popd
  exit /b 0
)

".venv\Scripts\python.exe" scheduler.py --live

popd
