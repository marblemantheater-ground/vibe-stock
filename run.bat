@echo off
rem Windows 실행 스크립트: 가상환경 준비 - 패키지 설치 - main.py 실행
rem 사용법: run.bat [--stream-test 60]
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] 가상환경 생성 ^(.venv^)
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" main.py %*
exit /b %errorlevel%
