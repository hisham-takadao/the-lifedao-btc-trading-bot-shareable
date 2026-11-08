@echo off
rem Double-click to start The Lifedao BTC Trading Bot.
rem The first time it installs what the bot needs (a few minutes) and asks for your key.
setlocal
cd /d "%~dp0"
title The Lifedao BTC Trading Bot

rem --- Find Python 3.11 or newer -------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

rem --- One-time install into a private folder (.venv) ------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Setting up for the first time. This takes a few minutes...
    %PY% -m venv .venv || goto :failed
)
fc /b requirements.txt ".venv\installed.txt" >nul 2>nul
if errorlevel 1 (
    echo Installing the bot's components...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :failed
    copy /y requirements.txt ".venv\installed.txt" >nul
)

rem --- Run the setup checks and the bot -------------------------------------
".venv\Scripts\python.exe" start.py %*
echo.
echo The bot has stopped.
pause
exit /b 0

:nopython
echo.
echo Python 3.11 or newer is needed and wasn't found.
echo.
echo   1. Go to https://www.python.org/downloads/ and download Python for Windows.
echo   2. Run the installer and TICK "Add python.exe to PATH" on the first screen.
echo   3. Double-click this file again.
echo.
set /p "ANS=Or type Y and press Enter to install it now with Windows' own installer (winget): "
if /i "%ANS%"=="Y" (
    winget install -e --id Python.Python.3.12 --accept-package-agreements
    echo.
    echo When the install has finished, close this window and double-click this file again.
)
pause
exit /b 1

:failed
echo.
echo Setup didn't finish. Check your internet connection and double-click this file again.
echo If it keeps failing, delete the .venv folder here and try once more.
pause
exit /b 1
