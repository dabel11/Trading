@echo off
chcp 65001 >nul
title AI 트레이딩 - 자동매매 봇 (24시간, 모의)
pushd "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [안내] 먼저 setup.bat 을 실행해 설치하세요.
  echo.
  pause
  popd
  exit /b 1
)

echo ============================================
echo    24시간 자동매매 봇  (모의 / 페이퍼)
echo.
echo    미국장 시간 내내 주기적으로 스캔-매매 사이클을 실행합니다.
echo    (화면 꺼져 있어도 동작 · 앱과 독립)
echo    종료하려면 이 창에서  Ctrl + C
echo.
echo    * 설정(전략·기간·주기)은 앱에서 바꾸면 다음 사이클에 반영됩니다.
echo    * 실거래로 돌리려면 run_bot_live.bat 을 쓰세요 (실제 자금!)
echo ============================================
echo.

".venv\Scripts\python.exe" autotrader.py

popd
