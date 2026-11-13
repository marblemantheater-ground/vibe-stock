@echo off
rem Windows 실행 스크립트: 가상환경 준비 - 패키지 설치 - main.py 실행
rem 사용법: run.bat [--stream-test 60]
chcp 65001 >nul
setlocal
title vibe-stock 미국주식 자동매매 %*
cd /d "%~dp0"

rem 탐색기에서 더블클릭해 실행했는지 (그렇다면 오류 시 창이 바로 닫히지 않게 멈춘다)
set "DBLCLICK="
echo %cmdcmdline% | find /i "%~0" >nul && set "DBLCLICK=1"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] 가상환경 생성 ^(.venv^)
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 goto :fail
exit /b 0

:fail
set "RC=%errorlevel%"
echo.
echo [오류] 프로그램이 비정상 종료되었습니다 (코드 %RC%). 위 메시지와 logs\trader.log 를 확인하세요.
rem 무한 대기(pause) 대신 2분 뒤 자동으로 닫힘 → 작업 스케줄러 등 무인 실행에서도 멈춰 있지 않음
if defined DBLCLICK timeout /t 120
exit /b %RC%
