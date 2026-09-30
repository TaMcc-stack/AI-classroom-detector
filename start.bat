@echo off
setlocal
cd /d "%~dp0"
title Classroom YOLO Control

set "PY=.venv\Scripts\python.exe"

rem A .venv copied from another machine keeps a stale base interpreter path,
rem so the real test is whether it can run, not whether the file exists.
set "VENV_OK=0"
if exist "%PY%" "%PY%" -c "import sys" >nul 2>&1 && set "VENV_OK=1"

if "%VENV_OK%"=="0" (
  if exist ".venv" (
    echo [1/3] Existing Python environment is unusable. Moving it aside...
    if exist ".venv.broken" rmdir /s /q ".venv.broken"
    move ".venv" ".venv.broken" >nul
  ) else (
    echo [1/3] Creating Python environment...
  )
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
  if not exist "%PY%" (
    echo Could not create the Python environment. Install Python 3.12 and try again.
    goto :error
  )
)

echo [2/3] Checking dependencies...
"%PY%" -c "import fastapi,uvicorn,ultralytics,cv2,multipart" >nul 2>&1
if errorlevel 1 (
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 goto :error
)

echo [3/3] Starting at http://127.0.0.1:8765 ...
rem Open the browser only once the port answers, so the first run does not
rem land on a connection-refused page while the model is still loading.
start "" powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 180;$i++){try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8765);$c.Close();break}catch{Start-Sleep -Milliseconds 500}}; Start-Process 'http://127.0.0.1:8765'"
"%PY%" -m uvicorn app:app --host 127.0.0.1 --port 8765
goto :eof
:error
echo.
echo Startup failed. Check Python and network, then try again.
pause
