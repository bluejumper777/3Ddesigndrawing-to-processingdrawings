@echo off
title Hole Annotator v5.0
cd /d "%~dp0"
echo.
echo   3D Hole Annotator Tool v5.0
echo   Starting server...
echo.
if not exist "python\python.exe" goto :nopython
"python\python.exe" app.py
if errorlevel 1 goto :crashed
goto :eof
:nopython
echo [ERROR] python\python.exe not found
pause
goto :eof
:crashed
echo.
echo [ERROR] App exited with error.
echo Install VC++ Runtime: https://aka.ms/vs/17/release/vc_redist.x64.exe
pause