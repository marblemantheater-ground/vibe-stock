@echo off
rem 긴급 정지 버튼: 미체결 주문 전부 취소 + 보유 종목 전부 매도 + 신규 매수 중지 (run.bat --resume 으로 해제)
chcp 65001 >nul
setlocal
title vibe-stock 긴급 정지
cd /d "%~dp0"
echo.
echo  [긴급 정지] 미체결 주문 전부 취소 + 보유 종목 전부 매도 + 신규 매수 중지
echo  해제하려면 run.bat --resume
echo.
choice /c YN /m "정말 실행할까요"
if errorlevel 2 exit /b 1
call run.bat --kill
set "RC=%errorlevel%"
echo.
pause
exit /b %RC%
