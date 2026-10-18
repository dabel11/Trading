@echo off
chcp 65001 >nul
title AI 트레이딩
pushd "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [안내] 먼저 setup.bat 을 실행해 설치하세요.
  echo.
  pause
  popd
  exit /b 1
)

echo ============================================
echo    AI 트레이딩  실행 중...
echo    잠시 후 앱 창(또는 브라우저)이 열립니다.
echo    이 검은 창을 닫으면 앱이 종료됩니다.
echo ============================================
echo.

".venv\Scripts\python.exe" native_app.py

popd
