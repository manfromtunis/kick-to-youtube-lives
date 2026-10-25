@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

set "PYCMD="

py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3.12"

if not defined PYCMD (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYCMD=py -3"
)

if not defined PYCMD (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PYCMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if not defined PYCMD (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Python 3.12 or newer was not found.
    echo Install Python from https://www.python.org/downloads/ then run this file again.
    pause
    exit /b 1
  )
  echo Installing Python 3.12 with winget...
  winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
  if errorlevel 1 goto :failed
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set PYCMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  ) else (
    set "PYCMD=py -3.12"
  )
)

%PYCMD% -m k2y setup-web
exit /b %errorlevel%

:failed
echo.
echo Failed. Read the message above.
pause
exit /b 1
