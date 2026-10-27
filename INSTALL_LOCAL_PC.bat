@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo Deep_Amnex - Local PC Processing Setup
echo ============================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or 3.12 from python.org
    echo and enable "Add Python to PATH" during installation.
    pause
    exit /b 1
  )
  set PY=python
)
%PY% -m venv .venv
if errorlevel 1 goto failed
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto failed
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo.
echo Installation complete. Run START_LOCAL_PC.bat to start the app.
pause
exit /b 0
:failed
echo.
echo Setup failed. Read the error above. Geospatial packages may require a supported Python version.
pause
exit /b 1
