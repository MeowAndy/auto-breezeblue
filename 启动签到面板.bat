@echo off
chcp 65001 >nul
title BreezeBlue 签到面板
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw bb_web.py
) else (
  start /min "" python bb_web.py
)
exit
